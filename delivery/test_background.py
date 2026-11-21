"""Persistent queue tests with synthetic replies; no model network or generated code."""
import json
import threading
import uuid
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, OperationalError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from workspace.models import Artifact, Conversation, Message, Task
from workspace.views import export_data
from .acceptance_cases import case_revision
from .background import claim_next_run, execution_heartbeat, process_next_run
from .catalog import IMPLEMENTED
from .connections import get_connection
from .models import AgentStep, CapabilityEvidence, RequestBudget, TeamRun
from .team_config import team_summary
from .team_views import execute_team_run


@override_settings(AI_MODEL='test/queued-model', AI_GATEWAY_API_KEY='synthetic', SERVICE_REGION='test',
                   TEAM_ROLE_CONNECTIONS={})
class BackgroundWorkflowTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(email='queue@example.test', password='ExamplePass123!')
        self.other = get_user_model().objects.create_user(email='queue-other@example.test', password='ExamplePass123!')
        self.client.force_login(self.owner)
        self.conversation = Conversation.objects.create(owner=self.owner, title='后台离线验收')
        self.task = Task.objects.create(owner=self.owner, conversation=self.conversation, goal='学习计划', status='waiting')
        Message.objects.create(conversation=self.conversation, role='user', content='写一份学习计划')
        self.url = f'/api/conversations/{self.conversation.id}/collaborate'
        self.detail_url = f'/api/conversations/{self.conversation.id}'
        connection = get_connection()
        for capability in IMPLEMENTED:
            CapabilityEvidence.objects.create(capability=capability, provider=connection.provider,
                model=connection.model, connection_fingerprint=connection.fingerprint,
                service_region=connection.service_region, evidence='Synthetic queue fixture only',
                verified_at=timezone.now(), expires_at=timezone.now() + timezone.timedelta(days=1),
                enabled=True, region_approved=True, compliance_approved=True)
        self.body = {'background': True, 'client_request_id': str(uuid.uuid4()), 'confirm_cost': True,
                     'expected_team': team_summary()['revision'], 'expected_task': str(self.task.id),
                     'expected_task_updated_at': self.task.updated_at.isoformat()}
        self.replies = ['方案甲', '独立方案乙', '比较与审查', '最终学习计划', '{"approved":true,"issues":[]}']
        self.mock_generate = patch('delivery.team_engine.generate_reply', side_effect=self.replies)
        self.generate = self.mock_generate.start()
        self.addCleanup(self.mock_generate.stop)

    def post(self, **overrides):
        return self.client.post(self.url, json.dumps({**self.body, **overrides}), content_type='application/json')

    def enqueue(self):
        response = self.post()
        self.assertEqual(response.status_code, 202, response.content)
        return TeamRun.objects.get(pk=response.json()['run']['id'])

    def test_hourly_allowance_is_owner_scoped_and_exhausted_submission_preserves_task(self):
        budget = RequestBudget.objects.create(user=self.other, attempts=20)
        self.assertEqual(self.client.get('/api/capabilities').json()['hourly_budget']['remaining'], 20)
        budget.user = self.owner
        budget.save()
        self.assertEqual(self.client.get('/api/capabilities').json()['hourly_budget']['remaining'], 0)
        self.assertEqual(self.post().status_code, 429)
        self.assertFalse(TeamRun.objects.exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'waiting')
        RequestBudget.objects.filter(pk=budget.pk).update(period_started_at=timezone.now() - timezone.timedelta(hours=2))
        self.assertEqual(self.post().status_code, 202)
        self.generate.assert_not_called()

    def test_enqueue_is_persistent_zero_calls_and_get_restores_progress(self):
        run = self.enqueue()
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.execution_id), ('running', run.execution_id))
        self.assertEqual((run.status, run.calls_used, run.started_at), ('queued', 0, None))
        self.assertEqual(run.input_context, [{'role': 'user', 'content': '写一份学习计划'}])
        self.generate.assert_not_called()
        self.assertFalse(RequestBudget.objects.exists())
        self.client.logout()
        self.client.force_login(self.owner)
        detail = self.client.get(self.detail_url).json()
        self.assertEqual(detail['tasks'][0]['runs'][0]['status'], 'queued')
        self.assertNotIn('input_context', detail['tasks'][0]['runs'][0])
        self.assertNotIn('input_context', export_data(self.owner)['team_runs'][0])
        self.assertNotIn('client_request_id', export_data(self.owner)['team_runs'][0])
        self.assertEqual(self.client.post(self.detail_url, json.dumps({'content': '不允许改掉正在执行目标'}),
                                         content_type='application/json').status_code, 409)

    def test_duplicate_submission_and_lost_receipt_cannot_create_second_run_or_call(self):
        run = self.enqueue()
        replay = self.post()
        self.assertEqual(replay.status_code, 202)
        self.assertTrue(replay.json()['replayed'])
        self.assertEqual(replay.json()['run']['id'], str(run.id))
        self.assertEqual(TeamRun.objects.count(), 1)
        self.assertEqual(self.post(delivery_mode='files', expected_max_calls=7).status_code, 409)
        self.assertEqual(self.post(client_request_id=str(uuid.uuid4())).status_code, 409)
        self.assertFalse(RequestBudget.objects.exists())
        self.generate.assert_not_called()

    def test_receipt_lookup_is_owner_scoped_and_never_dispatches_a_run(self):
        receipt_url = f'/api/conversations/{self.conversation.id}/submissions/{self.body["client_request_id"]}'
        missing = self.client.get(receipt_url)
        self.assertEqual(missing.status_code, 200)
        self.assertFalse(missing.json()['found'])
        run = self.enqueue()
        found = self.client.get(receipt_url)
        self.assertEqual(found.status_code, 200)
        self.assertTrue(found.json()['found'])
        self.assertEqual(found.json()['run']['id'], str(run.id))
        self.assertEqual(found.json()['run']['status'], 'queued')
        self.assertFalse(RequestBudget.objects.exists())
        self.generate.assert_not_called()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(receipt_url).status_code, 404)

    def test_request_id_collision_in_other_conversation_or_owner_is_private_and_atomic(self):
        original = self.enqueue()
        conflict = {'error': '提交未被接受，请刷新任务状态后重试。', 'code': 'request_conflict'}
        self.assertEqual(self.post(delivery_mode='files', expected_max_calls=7).json(), conflict)
        for owner in (self.owner, self.other):
            with self.subTest(owner=owner.pk):
                conversation = Conversation.objects.create(owner=owner, title='另一份任务')
                task = Task.objects.create(owner=owner, conversation=conversation, goal='独立任务', status='waiting')
                original_updated_at = task.updated_at
                self.client.force_login(owner)
                body = {**self.body, 'expected_task': str(task.id),
                        'expected_task_updated_at': task.updated_at.isoformat()}
                response = self.client.post(f'/api/conversations/{conversation.id}/collaborate',
                                           json.dumps(body), content_type='application/json')
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json(), conflict)
                task.refresh_from_db()
                self.assertEqual((task.status, task.execution_id, task.updated_at),
                                 ('waiting', None, original_updated_at))
                self.assertFalse(task.runs.exists())
                self.assertFalse(task.events.exists())
        self.assertEqual(list(TeamRun.objects.values_list('id', flat=True)), [original.id])
        self.assertFalse(RequestBudget.objects.exists())
        self.generate.assert_not_called()

    def test_request_uniqueness_race_rolls_back_task_claim_before_response(self):
        original_updated_at = self.task.updated_at
        with patch('delivery.team_views.TeamRun.objects.create', side_effect=IntegrityError('synthetic race')):
            response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'request_conflict')
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.execution_id, self.task.updated_at),
                         ('waiting', None, original_updated_at))
        self.assertFalse(TeamRun.objects.exists())
        self.assertFalse(self.task.events.exists())
        self.assertFalse(RequestBudget.objects.exists())
        self.generate.assert_not_called()

    def test_worker_completes_once_and_terminal_replay_only_returns_record(self):
        run = self.enqueue()
        self.assertEqual(process_next_run(), run.id)
        self.assertIsNone(process_next_run())
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used), ('completed', 5))
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.input_context, [])
        self.assertEqual(RequestBudget.objects.get(user=self.owner).attempts, 5)
        self.assertEqual(Artifact.objects.filter(task=self.task).count(), 1)
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['replayed'])
        self.assertEqual(response.json()['run']['status'], 'completed')
        self.assertEqual(self.generate.call_count, 5)

    def test_two_claims_cannot_pick_same_job_and_crashed_claim_is_not_replayed(self):
        run = self.enqueue()
        claimed = claim_next_run()
        self.assertEqual(claimed.id, run.id)
        self.assertIsNone(claim_next_run())
        self.assertIsNone(process_next_run())
        self.generate.assert_not_called()
        Task.objects.filter(pk=self.task.id).update(updated_at=timezone.now() - timezone.timedelta(minutes=6))
        TeamRun.objects.filter(pk=run.id).update(updated_at=timezone.now() - timezone.timedelta(minutes=6))
        call_command('recover_tasks', stdout=StringIO())
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIn('未自动重试', run.error)
        self.assertEqual(run.steps.filter(status='skipped').count(), 5)
        self.assertIsNone(process_next_run())
        self.generate.assert_not_called()

    def test_worker_command_once_reads_queue_and_does_not_repeat_paid_steps(self):
        self.enqueue()
        call_command('run_team_worker', once=True, stdout=StringIO())
        self.assertEqual(self.generate.call_count, 5)
        call_command('run_team_worker', once=True, stdout=StringIO())
        self.assertEqual(self.generate.call_count, 5)

    def test_replacement_worker_never_replays_already_started_work(self):
        run = self.enqueue()
        self.assertEqual(claim_next_run().id, run.id)
        call_command('run_team_worker', once=True, stdout=StringIO())
        run.refresh_from_db()
        self.assertEqual(run.status, 'running')
        self.assertEqual(run.calls_used, 0)
        self.generate.assert_not_called()
        self.assertFalse(RequestBudget.objects.exists())

    def test_queued_pause_cancels_without_call_and_resume_requires_new_confirmation(self):
        run = self.enqueue()
        url = f'/api/tasks/{self.task.id}/transition'
        self.assertEqual(self.client.post(url, json.dumps({'action': 'pause'}), content_type='application/json').status_code, 200)
        run.refresh_from_db()
        self.assertEqual((run.status, run.input_context), ('paused', []))
        self.assertIsNone(process_next_run())
        self.assertEqual(self.client.post(url, json.dumps({'action': 'resume'}), content_type='application/json').status_code, 200)
        self.assertIsNone(process_next_run())
        self.assertEqual(self.post().json()['run']['status'], 'paused')
        self.assertEqual(self.post(client_request_id=str(uuid.uuid4())).status_code, 409)
        self.generate.assert_not_called()

    def test_queued_state_uses_15_minute_expiry_not_running_recovery_cutoff(self):
        run = self.enqueue()
        past = timezone.now() - timezone.timedelta(minutes=6)
        Task.objects.filter(pk=self.task.id).update(updated_at=past)
        TeamRun.objects.filter(pk=run.id).update(created_at=past)
        call_command('recover_tasks', stdout=StringIO())
        run.refresh_from_db()
        self.assertEqual(run.status, 'queued')
        TeamRun.objects.filter(pk=run.id).update(created_at=timezone.now() - timezone.timedelta(minutes=16))
        call_command('recover_tasks', stdout=StringIO())
        run.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual((run.status, self.task.status), ('failed', 'failed'))
        self.assertIn('15分钟', run.error)
        self.assertIsNone(process_next_run())
        self.generate.assert_not_called()

    def test_stale_queue_is_resolved_when_owner_reads_without_worker(self):
        run = self.enqueue()
        TeamRun.objects.filter(pk=run.id).update(created_at=timezone.now() - timezone.timedelta(minutes=16))
        detail = self.client.get(self.detail_url)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['tasks'][0]['runs'][0]['status'], 'failed')
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'failed')
        self.generate.assert_not_called()

    def test_recent_run_heartbeat_prevents_recovery_of_active_task(self):
        run = self.enqueue()
        claim_next_run()
        Task.objects.filter(pk=self.task.id).update(updated_at=timezone.now() - timezone.timedelta(minutes=6))
        call_command('recover_tasks', stdout=StringIO())
        run.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual((run.status, self.task.status), ('running', 'running'))
        self.generate.assert_not_called()

    def test_readiness_revoked_while_waiting_stops_before_first_call(self):
        run = self.enqueue()
        CapabilityEvidence.objects.update(enabled=False)
        process_next_run()
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used), ('failed', 0))
        self.generate.assert_not_called()
        self.assertFalse(RequestBudget.objects.exists())

    def test_queue_rechecks_team_revision_and_runtime_before_dispatch(self):
        run = self.enqueue()
        original_summary = team_summary()
        with patch('delivery.team_views.team_summary', return_value={**original_summary, 'revision': 'changed'}):
            process_next_run()
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used), ('failed', 0))
        self.generate.assert_not_called()
        self.task.refresh_from_db()
        cases = [{'stdin': '2 3\n', 'expected_stdout': '5\n'}]
        Conversation.objects.filter(pk=self.conversation.pk).update(acceptance_cases=cases)
        self.body.update(client_request_id=str(uuid.uuid4()), expected_task_updated_at=self.task.updated_at.isoformat(),
                         expected_acceptance=case_revision(cases), delivery_mode='files', workflow='adaptive',
                         expected_max_calls=10)
        with patch('delivery.team_views.readiness', return_value={'available_tools': ['python_stdio']}):
            run = self.enqueue()
        with patch('delivery.team_views.readiness', return_value={'available_tools': []}):
            process_next_run()
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used), ('failed', 0))
        self.assertEqual(run.acceptance_cases, cases)
        self.generate.assert_not_called()
        self.assertFalse(RequestBudget.objects.exists())

    def test_account_deactivation_and_case_change_rechecked_before_dispatch(self):
        run = self.enqueue()
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertIsNone(process_next_run())
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.generate.assert_not_called()
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=True)
        self.task.refresh_from_db()
        self.body.update(client_request_id=str(uuid.uuid4()), expected_task_updated_at=self.task.updated_at.isoformat())
        run = self.enqueue()
        Conversation.objects.filter(pk=self.conversation.pk).update(
            acceptance_cases=[{'stdin': 'new', 'expected_stdout': 'changed'}])
        process_next_run()
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used), ('failed', 0))
        self.generate.assert_not_called()

    def test_pause_during_background_call_discards_late_output(self):
        run = self.enqueue()
        def pause(*args, **kwargs):
            self.client.post(f'/api/tasks/{self.task.id}/transition', json.dumps({'action': 'pause'}),
                             content_type='application/json')
            return '迟到内容不能保存'
        self.generate.side_effect = pause
        process_next_run()
        run.refresh_from_db()
        self.assertEqual(run.status, 'paused')
        self.assertEqual(self.generate.call_count, 1)
        self.assertFalse(AgentStep.objects.exclude(output='').exists())
        self.assertFalse(Artifact.objects.exists())

    def test_request_identity_and_owner_boundary_reject_without_dispatch(self):
        for bad in ('bad', None, 42, {}, ''):
            self.assertEqual(self.post(client_request_id=bad).status_code, 400)
        self.assertEqual(self.post(background='true').status_code, 400)
        self.assertEqual(self.post(confirm_cost=False).status_code, 400)
        self.assertEqual(self.post(expected_task_updated_at='stale').status_code, 409)
        self.enqueue()
        self.client.force_login(self.other)
        self.assertEqual(self.post().status_code, 404)
        self.assertEqual(self.client.get(self.detail_url).status_code, 404)
        self.generate.assert_not_called()

    def test_claim_revalidates_current_task_execution_and_never_overwrites_replacement(self):
        run = self.enqueue()
        replacement = uuid.uuid4()
        Task.objects.filter(pk=self.task.id).update(execution_id=replacement)
        self.assertIsNone(process_next_run())
        run.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertEqual(self.task.execution_id, replacement)
        self.generate.assert_not_called()

    def test_claimed_run_cannot_be_executed_again_after_completion(self):
        run = self.enqueue()
        process_next_run()
        run.refresh_from_db()
        response = execute_team_run(run)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.generate.call_count, 5)
        self.assertEqual(Artifact.objects.count(), 1)


class HeartbeatTests(TransactionTestCase):
    def test_heartbeat_refreshes_run_without_changing_task_revision(self):
        def retry_sqlite_lock(operation):
            # Shared in-memory SQLite can reject a read while the heartbeat
            # commits. The production worker also retries transient DB locks.
            for attempt in range(20):
                try:
                    return operation()
                except OperationalError as error:
                    if 'locked' not in str(error).lower() or attempt == 19:
                        raise
                    threading.Event().wait(0.02)

        owner = get_user_model().objects.create_user(email='heartbeat@example.test', password='ExamplePass123!')
        conversation = Conversation.objects.create(owner=owner, title='模拟长任务')
        execution_id = uuid.uuid4()
        task = Task.objects.create(owner=owner, conversation=conversation, goal='模拟', status='running', execution_id=execution_id)
        run = TeamRun.objects.create(task=task, execution_id=execution_id, team_revision='synthetic', status='running')
        past = timezone.now() - timezone.timedelta(minutes=6)
        Task.objects.filter(pk=task.pk).update(updated_at=past)
        TeamRun.objects.filter(pk=run.pk).update(updated_at=past)
        with patch('delivery.background.HEARTBEAT_SECONDS', 0.05):
            with execution_heartbeat(run):
                for _ in range(30):
                    threading.Event().wait(0.05)
                    retry_sqlite_lock(run.refresh_from_db)
                    if run.updated_at > past:
                        break
                self.assertGreater(run.updated_at, past)
                retry_sqlite_lock(lambda: call_command('recover_tasks', stdout=StringIO()))
                retry_sqlite_lock(run.refresh_from_db)
                retry_sqlite_lock(task.refresh_from_db)
                self.assertEqual((run.status, task.status, task.updated_at), ('running', 'running', past))
