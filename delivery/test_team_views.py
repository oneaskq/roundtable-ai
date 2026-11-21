"""Synthetic end-to-end collaboration tests. No network requests are allowed."""
import json
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from workspace.models import Artifact, Conversation, Message, Task
from .catalog import IMPLEMENTED
from .connections import get_connection
from .models import AgentStep, CapabilityEvidence, RequestBudget, TeamRun
from .provider import ProviderError
from .team_config import team_summary


@override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='synthetic', SERVICE_REGION='test',
                   TEAM_ROLE_CONNECTIONS={})
class TeamWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='team@example.test', password='ExamplePass123!')
        self.other = get_user_model().objects.create_user(email='other-team@example.test', password='ExamplePass123!')
        self.client.force_login(self.user)
        self.conversation = Conversation.objects.create(owner=self.user, title='离线协作测试')
        Message.objects.create(conversation=self.conversation, role='user', content='写一份学习计划')
        self.task = Task.objects.create(owner=self.user, conversation=self.conversation, goal='学习计划', status='waiting')
        self.url = f'/api/conversations/{self.conversation.id}/collaborate'
        self.approve()
        self.replies = ['方案甲', '独立方案乙', '比较两份方案并指出待核实内容', '最终学习计划',
                        '{"approved":true,"issues":[]}']

    def approve(self):
        connection = get_connection()
        for capability in IMPLEMENTED:
            CapabilityEvidence.objects.create(capability=capability, provider=connection.provider,
                model=connection.model, connection_fingerprint=connection.fingerprint,
                service_region=connection.service_region, evidence='Synthetic fixture, not live validation',
                verified_at=timezone.now(), expires_at=timezone.now() + timezone.timedelta(days=1),
                enabled=True, region_approved=True, compliance_approved=True)

    def post(self, **overrides):
        body = {'confirm_cost': True, 'expected_team': team_summary()['revision'], **overrides}
        return self.client.post(self.url, json.dumps(body), content_type='application/json')

    @patch('delivery.team_engine.generate_reply')
    def test_verified_text_persists_five_steps_and_downloadable_result(self, generate):
        generate.side_effect = self.replies
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['message']['content'], '最终学习计划')
        self.assertEqual(generate.call_count, 5)
        self.assertEqual(RequestBudget.objects.get(user=self.user).attempts, 5)
        run = TeamRun.objects.get(task=self.task)
        self.assertEqual((run.status, run.calls_used), ('completed', 5))
        self.assertEqual(run.steps.filter(status='completed').count(), 5)
        artifact = Artifact.objects.get(task=self.task)
        self.assertEqual(self.client.get(f'/api/artifacts/{artifact.id}/download').content.decode(), '最终学习计划')
        detail = self.client.get(f'/api/conversations/{self.conversation.id}').json()
        self.assertEqual(len(detail['tasks'][0]['runs'][0]['steps']), 5)
        self.assertNotIn('team_revision', detail['tasks'][0]['runs'][0])
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(generate.call_count, 5)

    @patch('delivery.team_engine.generate_reply')
    def test_pending_or_stale_team_never_sends_user_content(self, generate):
        self.assertEqual(self.post(expected_team='old-configuration').status_code, 409)
        self.assertEqual(self.post(confirm_cost=False).status_code, 400)
        CapabilityEvidence.objects.filter(capability='chat.context').update(enabled=False)
        self.assertEqual(self.post().status_code, 503)
        generate.assert_not_called()
        self.assertFalse(TeamRun.objects.exists())

    @patch('delivery.team_engine.generate_reply')
    def test_pause_during_provider_call_stops_remaining_steps_and_late_output(self, generate):
        def pause(*args, **kwargs):
            response = self.client.post(f'/api/tasks/{self.task.id}/transition',
                                        json.dumps({'action': 'pause'}), content_type='application/json')
            self.assertEqual(response.status_code, 200)
            return '迟到输出'
        generate.side_effect = pause
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(generate.call_count, 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'paused')
        self.assertEqual(TeamRun.objects.get().status, 'paused')
        self.assertFalse(AgentStep.objects.exclude(output='').exists())
        self.assertFalse(Artifact.objects.exists())

    @patch('delivery.team_engine.generate_reply')
    def test_revocation_during_call_stops_late_output(self, generate):
        def revoke(*args, **kwargs):
            CapabilityEvidence.objects.update(enabled=False)
            return '不应写入'
        generate.side_effect = revoke
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(TeamRun.objects.get().status, 'failed')
        self.assertFalse(AgentStep.objects.exclude(output='').exists())

    @patch('delivery.team_engine.generate_reply')
    def test_budget_is_counted_per_call_and_halts_partway(self, generate):
        RequestBudget.objects.create(user=self.user, attempts=18)
        generate.side_effect = self.replies
        self.assertEqual(self.post().status_code, 429)
        self.assertEqual(generate.call_count, 2)
        run = TeamRun.objects.get()
        self.assertEqual((run.status, run.calls_used), ('failed', 2))
        self.assertEqual(run.steps.filter(status='skipped').count(), 3)
        self.assertEqual(RequestBudget.objects.get(user=self.user).attempts, 20)
        self.assertFalse(Artifact.objects.exists())

    @patch('delivery.team_engine.generate_reply')
    def test_failed_review_retains_marked_draft_without_final_answer(self, generate):
        generate.side_effect = self.replies[:-1] + ['{"approved":false,"issues":["时间安排需要复核"]}']
        response = self.post()
        self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(TeamRun.objects.get().status, 'needs_review')
        self.assertFalse(Message.objects.filter(role='assistant').exists())
        artifact = Artifact.objects.get()
        self.assertEqual(artifact.name, '待复核草稿.md')
        self.assertIn('不是已验收成果', artifact.content)
        self.assertIn('时间安排需要复核', artifact.content)
        generate.side_effect = self.replies
        self.assertEqual(self.post().status_code, 200)
        self.assertIn('此前团队待复核材料', generate.call_args_list[5].args[0][0]['content'])
        self.assertIn('时间安排需要复核', generate.call_args_list[5].args[0][0]['content'])
        self.assertEqual(Message.objects.filter(role='user').count(), 1)
        self.assertEqual(TeamRun.objects.count(), 2)

    @patch('delivery.team_engine.generate_reply', side_effect=ProviderError('合成故障'))
    def test_provider_failure_has_no_automatic_retry(self, generate):
        self.assertEqual(self.post().status_code, 502)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(AgentStep.objects.get(role='proposer').status, 'failed')
        self.assertFalse(Artifact.objects.exists())

    @patch('delivery.team_engine.generate_reply')
    def test_foreign_user_cannot_invoke_read_or_export_team_data(self, generate):
        generate.side_effect = self.replies
        self.assertEqual(self.post().status_code, 200)
        self.client.force_login(self.other)
        self.assertEqual(self.post().status_code, 404)
        self.assertEqual(self.client.get(f'/api/conversations/{self.conversation.id}').status_code, 404)
        from workspace.views import export_data
        self.assertEqual(export_data(self.other)['team_runs'], [])
        self.assertEqual(len(export_data(self.user)['team_runs']), 1)
        self.assertEqual(generate.call_count, 5)

    @patch('delivery.team_engine.generate_reply')
    def test_restart_recovery_marks_run_and_step_without_retry(self, generate):
        import uuid
        execution_id = uuid.uuid4()
        Task.objects.filter(pk=self.task.pk).update(status='running', execution_id=execution_id,
                                                   updated_at=timezone.now() - timezone.timedelta(minutes=6))
        run = TeamRun.objects.create(task=self.task, execution_id=execution_id, team_revision='test')
        TeamRun.objects.filter(pk=run.pk).update(updated_at=timezone.now() - timezone.timedelta(minutes=6))
        AgentStep.objects.create(run=run, position=0, role='proposer', label='独立方案', provider='gateway',
                                 model='test/model', status='running')
        call_command('recover_tasks', stdout=StringIO())
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.steps.get().status, 'failed')
        generate.assert_not_called()
