import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from workspace.models import Artifact, Conversation, Message, Task
from .catalog import IMPLEMENTED
from .connections import get_connection
from .models import CapabilityEvidence, RequestBudget, TeamRun
from .provider import ProviderError
from .team_config import team_summary


@override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='synthetic', SERVICE_REGION='test', TEAM_ROLE_CONNECTIONS={})
class FileWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='files-flow@example.test', password='ExamplePass123!')
        self.client.force_login(self.user)
        self.conversation = Conversation.objects.create(owner=self.user, title='多文件合成流程')
        self.message = Message.objects.create(conversation=self.conversation, role='user', content='生成一个Python源码和说明文件')
        self.task = Task.objects.create(owner=self.user, conversation=self.conversation, goal=self.message.content, status='waiting')
        self.url = f'/api/conversations/{self.conversation.id}/collaborate'
        connection = get_connection()
        for capability in IMPLEMENTED:
            CapabilityEvidence.objects.create(capability=capability, provider=connection.provider,
                model=connection.model, connection_fingerprint=connection.fingerprint, service_region=connection.service_region,
                evidence='Synthetic test only', verified_at=timezone.now(), expires_at=timezone.now() + timezone.timedelta(days=1),
                enabled=True, region_approved=True, compliance_approved=True)
        self.files = {'summary': '源码已准备，未进行功能运行。', 'files': [
            {'path': 'src/main.py', 'content': 'print("version one")'},
            {'path': 'README.md', 'content': '这是合成样例；尚未运行代码。'}]}
        self.responses = ['独立方案甲', '替代方案乙', '交叉审查意见', json.dumps(self.files), '{"approved":true,"issues":[]}']

    def post(self, **changes):
        body = {'confirm_cost': True, 'expected_team': team_summary()['revision'],
                'delivery_mode': 'files', 'expected_max_calls': 7, **changes}
        return self.client.post(self.url, json.dumps(body), content_type='application/json')

    @patch('delivery.file_team_engine.generate_reply')
    def test_file_mode_requires_explicit_seven_call_limit(self, generate):
        self.assertEqual(self.post(expected_max_calls=5).status_code, 400)
        self.assertEqual(self.post(expected_max_calls=True).status_code, 400)
        self.assertEqual(self.post(delivery_mode='execute').status_code, 400)
        generate.assert_not_called()
        self.assertFalse(TeamRun.objects.exists())

    @patch('delivery.file_team_engine.generate_reply')
    def test_complete_bundle_and_continue_modifying_create_separate_versions(self, generate):
        generate.side_effect = self.responses
        self.assertEqual(self.post().status_code, 200)
        first = Artifact.objects.get(kind='bundle')
        self.assertEqual((first.revision, first.review_status), (1, 'ready'))
        run = TeamRun.objects.get()
        self.assertEqual((run.max_calls, run.calls_used, run.delivery_mode), (7, 5, 'files'))
        self.assertTrue(run.checks[0]['passed'])
        self.assertEqual(run.steps.filter(status='skipped').count(), 2)
        response = self.client.post(f'/api/conversations/{self.conversation.id}',
                                   json.dumps({'content': '把输出改成version two，保留说明文件'}), content_type='application/json')
        self.assertEqual(response.status_code, 201)
        changed = {**self.files, 'files': [{**self.files['files'][0], 'content': 'print("version two")'}, self.files['files'][1]]}
        generate.side_effect = self.responses[:3] + [json.dumps(changed), self.responses[-1]]
        self.assertEqual(self.post().status_code, 200)
        context = json.loads(generate.call_args_list[5].args[0][0]['content'])['conversation']
        self.assertTrue(any('src/main.py' in entry['content'] and 'version one' in entry['content'] for entry in context))
        self.assertEqual(list(Artifact.objects.filter(kind='bundle').order_by('revision').values_list('revision', flat=True)), [1, 2])
        first.refresh_from_db()
        self.assertIn('version one', first.files[0]['content'])
        self.assertEqual(len(self.client.get(f'/api/conversations/{self.conversation.id}/artifacts').json()['artifacts']), 2)

    @patch('delivery.file_team_engine.generate_reply')
    def test_failed_recheck_preserves_draft_and_does_not_publish_final_message(self, generate):
        rejected = '{"approved":false,"issues":["需要改进说明"]}'
        generate.side_effect = self.responses[:4] + [rejected, self.responses[3], rejected]
        self.assertEqual(self.post().status_code, 422)
        artifact = Artifact.objects.get(kind='bundle')
        self.assertEqual(artifact.review_status, 'draft')
        self.assertEqual(TeamRun.objects.get().status, 'needs_review')
        self.assertFalse(Message.objects.filter(role='assistant').exists())
        self.assertEqual(RequestBudget.objects.get().attempts, 7)
        generate.side_effect = self.responses
        # A coarse Windows clock can give the next run the same timestamp as
        # this draft. Its empty review must not hide the preceding rejection.
        TeamRun.objects.update(created_at=artifact.created_at - timezone.timedelta(microseconds=1))
        with patch('django.utils.timezone.now', return_value=artifact.created_at):
            self.assertEqual(self.post().status_code, 200)
        packet = generate.call_args_list[7].args[0][0]['content']
        self.assertIn('需要改进说明', packet)

    @patch('delivery.file_team_engine.generate_reply')
    def test_later_provider_failure_keeps_downloadable_candidate_as_draft(self, generate):
        generate.side_effect = self.responses[:4] + [ProviderError('合成服务中断')]
        self.assertEqual(self.post().status_code, 502)
        artifact = Artifact.objects.get(kind='bundle')
        self.assertEqual(artifact.review_status, 'draft')
        self.assertEqual(self.client.get(f'/api/artifacts/{artifact.id}/download').status_code, 200)
        self.assertFalse(Message.objects.filter(role='assistant').exists())
        self.assertEqual(generate.call_count, 5)

    @patch('delivery.file_team_engine.generate_reply')
    def test_large_delivered_file_still_fits_followup_edit_context(self, generate):
        large = {'summary': '待实际运行的较大源码样例', 'files': [
            {'path': 'main.py', 'content': '# ' + 'x' * 17000 + '\nprint(1)\n'}]}
        generate.side_effect = self.responses[:3] + [json.dumps(large, ensure_ascii=False), self.responses[-1]]
        self.assertEqual(self.post().status_code, 200)
        self.client.post(f'/api/conversations/{self.conversation.id}', json.dumps({'content': '最后一行改为print(2)'}),
                         content_type='application/json')
        revised = {'summary': '已修改末行，仍未运行', 'files': [
            {'path': 'main.py', 'content': large['files'][0]['content'].replace('print(1)', 'print(2)')}]}
        generate.side_effect = self.responses[:3] + [json.dumps(revised, ensure_ascii=False), self.responses[-1]]
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Artifact.objects.filter(kind='bundle').count(), 2)

    @patch('delivery.file_team_engine.generate_reply')
    def test_pause_before_repair_does_not_spend_remaining_budget(self, generate):
        invalid = json.dumps({**self.files, 'files': [{'path': 'main.py', 'content': 'def broken('}]})
        calls = iter(self.responses[:3] + [invalid])
        def pause(*args, **kwargs):
            result = next(calls)
            if 'broken' in result:
                self.client.post(f'/api/tasks/{self.task.id}/transition', json.dumps({'action': 'pause'}), content_type='application/json')
            return result
        generate.side_effect = pause
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(generate.call_count, 4)
        self.assertFalse(Artifact.objects.exists())
