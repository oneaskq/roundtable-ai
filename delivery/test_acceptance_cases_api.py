"""Owner acceptance persistence and run consent, entirely offline."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase

from workspace.models import Artifact, Conversation, Message, Task
from workspace.views import export_data
from .acceptance_cases import case_revision, normalize_cases
from .models import AgentStep, RequestBudget, TeamRun
from .provider import ProviderError
from .team_config import ROLES


CASES = [{'stdin': ' 你好\r\n\t', 'expected_stdout': ' 好\n\n'}]


class AcceptanceNormalizationTests(SimpleTestCase):
    def test_exact_text_copied_and_revision_matches_execution_contract(self):
        normalized = normalize_cases(CASES)
        self.assertEqual(normalized, CASES)
        self.assertIsNot(normalized, CASES)
        self.assertIsNot(normalized[0], CASES[0])
        self.assertEqual(case_revision(CASES), hashlib.sha256(
            json.dumps(CASES, sort_keys=True).encode()).hexdigest())
        self.assertEqual(case_revision([]), hashlib.sha256(b'[]').hexdigest())
        self.assertEqual(normalize_cases([{'stdin': '', 'expected_stdout': ''}]),
                         [{'stdin': '', 'expected_stdout': ''}])
        self.assertEqual(len(normalize_cases([{'stdin': '字' * 2000, 'expected_stdout': ' ' * 2000}] * 5)), 5)

    def test_rejects_non_utf8_unknown_fields_types_and_excess_limits(self):
        bad_cases = [None, {}, '', True, [None], [CASES[0]] * 6,
                     [{'stdin': ''}], [{'stdin': '', 'expected_stdout': '', 'source': 'user'}],
                     [{'stdin': 1, 'expected_stdout': ''}], [{'stdin': '', 'expected_stdout': False}],
                     [{'stdin': 'a' * 2001, 'expected_stdout': ''}],
                     [{'stdin': '', 'expected_stdout': '字' * 2001}],
                     [{'stdin': '\ud800', 'expected_stdout': ''}],
                     [{'stdin': '', 'expected_stdout': '\udfff'}]]
        for cases in bad_cases:
            with self.subTest(cases=repr(cases)[:80]), self.assertRaises(ProviderError):
                normalize_cases(cases)


class AcceptanceCasesAPITests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(email='cases@example.test', password='ExamplePass123!')
        self.other = get_user_model().objects.create_user(email='cases-other@example.test', password='ExamplePass123!')
        self.client.force_login(self.owner)
        self.conversation = Conversation.objects.create(owner=self.owner, title='验收任务')
        self.task = Task.objects.create(owner=self.owner, conversation=self.conversation, goal='保留需求', status='waiting')
        Message.objects.create(conversation=self.conversation, role='user', content='保留需求')
        self.url = f'/api/conversations/{self.conversation.pk}/acceptance-cases'
        self.run_url = f'/api/conversations/{self.conversation.pk}/collaborate'
        self.connections = {role: SimpleNamespace(provider='synthetic', model='offline') for role, _ in ROLES}
        self.resolve = self.patch('delivery.team_views.resolve_team', return_value=self.connections)
        self.patch('delivery.team_views.team_summary', return_value={'revision': 'team-consent', 'readiness': 'available'})
        self.readiness = self.patch('delivery.team_views.readiness', return_value={'available_tools': ['python_stdio']})
        self.api_readiness = self.patch('workspace.views.readiness', return_value={'available_tools': []})
        self.engine = self.patch('delivery.team_views.run_team', side_effect=ProviderError('合成停止；没有发出模型请求。'))

    def patch(self, target, **kwargs):
        item = patch(target, **kwargs)
        self.addCleanup(item.stop)
        return item.start()

    def put(self, cases, expected=None, **extra):
        body = {'cases': cases, 'expected_revision': case_revision([]) if expected is None else expected, **extra}
        return self.client.put(self.url, json.dumps(body), content_type='application/json')

    def collaborate(self, **extra):
        self.conversation.refresh_from_db()
        task = self.conversation.tasks.order_by('-created_at').first()
        body = {'confirm_cost': True, 'expected_team': 'team-consent', 'delivery_mode': 'files',
                'workflow': 'adaptive', 'expected_max_calls': 10,
                'expected_task': str(task.id), 'expected_task_updated_at': task.updated_at.isoformat(),
                'expected_acceptance': case_revision(self.conversation.acceptance_cases), **extra}
        return self.client.post(self.run_url, json.dumps(body), content_type='application/json')

    def test_get_put_clear_and_conversation_payload_preserve_exact_text(self):
        state = self.client.get(self.url).json()
        self.assertEqual((state['cases'], state['revision']), ([], case_revision([])))
        self.assertFalse(state['execution_available'])
        self.assertIn('不会运行代码或调用模型', state['notice'])
        response = self.put(CASES)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['cases'], CASES)
        self.assertEqual(response.json()['conversation']['acceptance_cases'], CASES)
        self.assertEqual(response.json()['conversation']['acceptance_revision'], case_revision(CASES))
        self.assertFalse(response.json()['execution_available'])
        self.api_readiness.return_value = {'available_tools': ['python_stdio']}
        self.assertTrue(self.client.get(self.url).json()['execution_available'])
        detail = self.client.get(f'/api/conversations/{self.conversation.pk}').json()['conversation']
        self.assertEqual(detail['acceptance_cases'], CASES)
        self.assertEqual(self.client.get(self.url).json()['cases'], CASES)
        self.assertEqual(self.put([], expected=case_revision(CASES)).status_code, 200)
        self.assertEqual(self.client.get(self.url).json()['revision'], case_revision([]))
        self.engine.assert_not_called()

    def test_api_boundary_lengths_and_invalid_values_leave_saved_cases_unchanged(self):
        largest = [{'stdin': '字' * 2000, 'expected_stdout': '\n' * 2000}] * 5
        self.assertEqual(self.put(largest).status_code, 200)
        for cases in (None, {}, [CASES[0]] * 6, [{'stdin': '', 'expected_stdout': 1}],
                      [{'stdin': '', 'expected_stdout': '', 'source': 'model'}],
                      [{'stdin': '\ud800', 'expected_stdout': ''}],
                      [{'stdin': '', 'expected_stdout': '字' * 2001}]):
            with self.subTest(value=repr(cases)[:80]):
                self.assertEqual(self.put(cases, expected=case_revision(largest)).status_code, 400)
        self.assertEqual(self.client.get(self.url).json()['cases'], largest)

    def test_revision_required_and_stale_updates_conflict(self):
        for expected in (False, 0, [], 'stale', ''):
            self.assertEqual(self.put(CASES, expected=expected).status_code, 409)
        missing = self.client.put(self.url, json.dumps({'cases': CASES}), content_type='application/json')
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(self.put(CASES).status_code, 200)
        self.assertEqual(self.put([]).status_code, 409)
        self.assertEqual(self.client.get(self.url).json()['cases'], CASES)

    def test_ownership_login_csrf_and_mfa_follow_existing_controls(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.put(CASES).status_code, 404)
        self.assertEqual(self.collaborate().status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(self.put(CASES).status_code, 401)
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.owner)
        response = browser.put(self.url, json.dumps({'cases': CASES, 'expected_revision': case_revision([])}),
                               content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.owner.is_superuser = True
        self.owner.save(update_fields=['is_superuser'])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.put(CASES).status_code, 403)
        self.engine.assert_not_called()

    def test_running_rejects_edit_while_other_statuses_allow_it(self):
        Task.objects.filter(pk=self.task.pk).update(status='running')
        self.assertEqual(self.put(CASES).status_code, 409)
        for status in ('paused', 'completed', 'failed', 'waiting'):
            with self.subTest(status=status):
                Task.objects.filter(pk=self.task.pk).update(status=status)
                current = self.client.get(self.url).json()['revision']
                self.assertEqual(self.put(CASES, expected=current).status_code, 200)

    def test_missing_or_stale_start_revision_never_claims_or_calls(self):
        self.put(CASES)
        for expected in ('old', None, True, []):
            self.assertEqual(self.collaborate(expected_acceptance=expected).status_code, 409)
        payload = {'delivery_mode': 'files', 'workflow': 'adaptive', 'expected_max_calls': 10}
        self.assertEqual(self.client.post(self.run_url, json.dumps(payload), content_type='application/json').status_code, 409)
        self.engine.assert_not_called()
        self.resolve.assert_not_called()
        self.assertFalse(TeamRun.objects.exists())
        self.assertFalse(RequestBudget.objects.exists())

    def test_case_cleared_after_consent_rejects_old_revision(self):
        self.put(CASES)
        self.put([], expected=case_revision(CASES))
        self.assertEqual(self.collaborate(expected_acceptance=case_revision(CASES)).status_code, 409)
        self.engine.assert_not_called()

    def assert_task_consent_stopped(self, response):
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'acceptance_task_changed')
        self.readiness.assert_not_called()
        self.resolve.assert_not_called()
        self.engine.assert_not_called()
        self.assertFalse(TeamRun.objects.exists())
        self.assertFalse(RequestBudget.objects.exists())

    def test_cases_require_both_task_consent_fields_without_any_dispatch(self):
        self.put(CASES)
        valid = {'confirm_cost': True, 'expected_team': 'team-consent', 'delivery_mode': 'files',
                 'workflow': 'adaptive', 'expected_max_calls': 10, 'expected_acceptance': case_revision(CASES),
                 'expected_task': str(self.task.id), 'expected_task_updated_at': self.task.updated_at.isoformat()}
        for missing in [('expected_task',), ('expected_task_updated_at',),
                        ('expected_task', 'expected_task_updated_at')]:
            with self.subTest(missing=missing):
                body = {key: value for key, value in valid.items() if key not in missing}
                response = self.client.post(self.run_url, json.dumps(body), content_type='application/json')
                self.assert_task_consent_stopped(response)
        self.assert_task_consent_stopped(self.collaborate(expected_task=True))
        self.assert_task_consent_stopped(self.collaborate(expected_task_updated_at=[]))

    def test_previous_task_consent_cannot_start_newer_task(self):
        self.put(CASES)
        old_timestamp = self.task.updated_at.isoformat()
        Task.objects.filter(pk=self.task.pk).update(status='completed')
        newer = Task.objects.create(owner=self.owner, conversation=self.conversation, goal='不同的新任务', status='waiting')
        # The clock can give consecutive tasks the same timestamp. Active work
        # must still win over an old terminal task, independently of UUID order.
        Task.objects.filter(pk=newer.pk).update(created_at=self.task.created_at)
        self.assert_task_consent_stopped(self.collaborate(
            expected_task=str(self.task.pk), expected_task_updated_at=old_timestamp))
        newer.refresh_from_db()
        self.assertEqual((newer.status, newer.execution_id), ('waiting', None))
        valid = self.collaborate(expected_task=str(newer.pk), expected_task_updated_at=newer.updated_at.isoformat())
        self.assertEqual(valid.status_code, 502)  # Synthetic engine stop, after accepting only the current task.
        self.assertEqual(TeamRun.objects.get().task_id, newer.pk)
        self.assertEqual(self.engine.call_count, 1)

    def test_supplemented_goal_invalidates_consent_for_same_task(self):
        self.put(CASES)
        old_timestamp = self.task.updated_at.isoformat()
        with patch('workspace.views.timezone.now', return_value=self.task.updated_at):
            response = self.client.post(f'/api/conversations/{self.conversation.pk}',
                json.dumps({'content': '新补充的用户目标'}), content_type='application/json')
        self.assertEqual(response.status_code, 201)
        self.task.refresh_from_db()
        self.assertEqual(self.task.goal, '新补充的用户目标')
        self.assertNotEqual(self.task.updated_at.isoformat(), old_timestamp)
        self.assert_task_consent_stopped(self.collaborate(expected_task_updated_at=old_timestamp))
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.execution_id), ('waiting', None))

    def test_editor_and_start_reread_revision_after_shared_lock(self):
        from workspace.views import lock_conversation
        replacement = [{'stdin': 'new', 'expected_stdout': 'new\n'}]
        def changed_before_lock(owner, conversation_id):
            Conversation.objects.filter(pk=conversation_id).update(acceptance_cases=replacement)
            return lock_conversation(owner, conversation_id)
        with patch('workspace.views.lock_conversation', side_effect=changed_before_lock):
            self.assertEqual(self.put(CASES).status_code, 409)
        # The synthetic race occurs inside this request's transaction, which
        # rolls it back along with the rejected edit.
        self.assertEqual(self.client.get(self.url).json()['cases'], [])
        Conversation.objects.filter(pk=self.conversation.pk).update(acceptance_cases=CASES)
        with patch('delivery.team_views.lock_conversation', side_effect=changed_before_lock):
            self.assertEqual(self.collaborate().status_code, 409)
        self.engine.assert_not_called()
        self.assertFalse(RequestBudget.objects.exists())
        self.assertFalse(TeamRun.objects.exists())

    def test_cases_require_adaptive_files_before_configuration_or_model_calls(self):
        self.put(CASES)
        for mode, flow, limit in [('text', 'fixed', 5), ('text', 'adaptive', 8), ('files', 'fixed', 7)]:
            with self.subTest(mode=mode, workflow=flow):
                response = self.collaborate(delivery_mode=mode, workflow=flow, expected_max_calls=limit)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['code'], 'acceptance_mode_required')
        self.engine.assert_not_called()
        self.resolve.assert_not_called()
        self.assertFalse(TeamRun.objects.exists())

    def test_unavailable_execution_preserves_requirement_and_uses_no_budget(self):
        self.put(CASES)
        self.readiness.return_value = {'available_tools': []}
        response = self.collaborate()
        self.assertEqual(response.status_code, 422)
        self.assertEqual((response.json()['code'], response.json()['status']), ('tools_unavailable', 'not_run'))
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.goal), ('waiting', '保留需求'))
        self.assertEqual(Message.objects.filter(role='user').get().content, '保留需求')
        self.assertEqual(self.client.get(self.url).json()['cases'], CASES)
        self.engine.assert_not_called()
        self.resolve.assert_not_called()
        self.assertFalse(TeamRun.objects.exists())
        self.assertFalse(RequestBudget.objects.exists())
        self.assertFalse(Artifact.objects.exists())

    def test_start_uses_database_snapshot_and_export_keeps_old_run_after_edit(self):
        self.put(CASES)
        fake_cases = [{'stdin': 'client injection', 'expected_stdout': 'client injection'}]
        response = self.collaborate(cases=fake_cases, acceptance_cases=fake_cases, acceptance_source='model')
        self.assertEqual(response.status_code, 502)
        run = TeamRun.objects.get()
        self.assertEqual(run.acceptance_cases, CASES)
        self.assertEqual(self.engine.call_args.kwargs['acceptance_cases'], CASES)
        self.assertEqual(self.engine.call_args.kwargs['execution_run_id'], str(run.pk))
        self.assertEqual(self.put(fake_cases, expected=case_revision(CASES)).status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.acceptance_cases, CASES)
        exported = export_data(self.owner)
        self.assertEqual(exported['conversations'][0]['acceptance_cases'], fake_cases)
        self.assertEqual(exported['team_runs'][0]['acceptance_cases'], CASES)
        self.assertEqual(exported['team_runs'][0]['acceptance_revision'], case_revision(CASES))
        self.assertEqual(exported['team_runs'][0]['acceptance_source'], 'user')
        self.assertEqual(export_data(self.other)['team_runs'], [])

    def test_body_injection_cannot_enable_acceptance_and_old_clients_work(self):
        self.engine.side_effect = None
        self.engine.return_value = '离线结果'
        body = {'confirm_cost': True, 'expected_team': 'team-consent', 'acceptance_cases': CASES, 'cases': CASES}
        response = self.client.post(self.run_url, json.dumps(body), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(TeamRun.objects.get().acceptance_cases, [])
        self.assertEqual(self.engine.call_args.kwargs['acceptance_cases'], [])
        self.readiness.assert_not_called()

    def test_running_api_edit_cannot_change_snapshot(self):
        self.put(CASES)
        def during_run(*args, **kwargs):
            self.assertEqual(self.put([], expected=case_revision(CASES)).status_code, 409)
            self.assertEqual(TeamRun.objects.get().acceptance_cases, CASES)
            raise ProviderError('合成停止')
        self.engine.side_effect = during_run
        self.assertEqual(self.collaborate().status_code, 502)

    def test_out_of_band_change_stops_before_call_tool_and_writeback(self):
        self.put(CASES)
        callbacks = ('before_step', 'before_tool', 'after_step', 'after_plan', 'after_check')
        for callback in callbacks:
            with self.subTest(callback=callback):
                Task.objects.filter(pk=self.task.pk).update(status='waiting', execution_id=None)
                Conversation.objects.filter(pk=self.conversation.pk).update(acceptance_cases=CASES)
                def changed(*args, **kwargs):
                    Conversation.objects.filter(pk=self.conversation.pk).update(acceptance_cases=[])
                    if callback == 'before_step':
                        kwargs[callback]('planner', '任务规划', self.connections['synthesizer'])
                    elif callback == 'before_tool':
                        kwargs[callback]()
                    elif callback == 'after_step':
                        kwargs[callback]('planner', '不应保留的输出')
                    elif callback == 'after_plan':
                        kwargs[callback]({'work_items': [], 'criteria': []}, [])
                    else:
                        kwargs[callback]('synthesizer', {'passed': True, 'checks': []})
                self.engine.side_effect = changed
                self.assertEqual(self.collaborate().status_code, 409)
                run = TeamRun.objects.latest('created_at')
                self.assertEqual(run.acceptance_cases, CASES)
                self.assertEqual((run.plan, run.checks, run.calls_used), ({}, [], 0))
                self.assertFalse(run.steps.exclude(output='').exists())
        self.assertFalse(RequestBudget.objects.exists())
        self.assertFalse(Artifact.objects.exists())

    def test_out_of_band_change_prevents_final_result_and_candidate_retention(self):
        self.put(CASES)
        def changed(*args, **kwargs):
            run = TeamRun.objects.get()
            AgentStep.objects.create(run=run, position=1, role='synthesizer', label='合成',
                provider='synthetic', model='offline', status='completed',
                output=json.dumps({'summary': '旧候选', 'files': [{'path': 'main.py', 'content': 'print(1)'}]}))
            Conversation.objects.filter(pk=self.conversation.pk).update(acceptance_cases=[])
            return '不应写回的结果'
        self.engine.side_effect = changed
        self.assertEqual(self.collaborate().status_code, 409)
        self.assertFalse(Message.objects.filter(role='assistant').exists())
        self.assertFalse(Artifact.objects.exists())

    def test_previous_validation_hides_expected_output_and_execution_output(self):
        Artifact.objects.create(owner=self.owner, task=self.task, name='历史文件', kind='bundle',
            content='历史摘要', files=[{'path': 'main.py', 'content': 'print(1)'}], validation={
                'passed': False, 'checks': [], 'execution': {'report': {'checks': [
                    {'tool': 'python_stdio', 'status': 'failed', 'output': 'PRIVATE_EXECUTION_OUTPUT',
                     'cases': [{'stdin': 'PRIVATE_STDIN', 'expected_stdout': 'PRIVATE_EXPECTED', 'output': 'PRIVATE_OUTPUT'}]}
                ]}}})
        self.assertEqual(self.collaborate().status_code, 502)
        context = json.dumps(self.engine.call_args.args[0])
        for secret in ('PRIVATE_EXECUTION_OUTPUT', 'PRIVATE_STDIN', 'PRIVATE_EXPECTED', 'PRIVATE_OUTPUT'):
            self.assertNotIn(secret, context)
