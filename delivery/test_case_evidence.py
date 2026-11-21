"""User-case evidence boundaries; all runtime results below are synthetic."""
from copy import deepcopy
from io import BytesIO
import json
import uuid
from unittest.mock import patch
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from workspace.models import Artifact, Conversation, Task
from .acceptance_cases import case_revision
from .artifact_bundles import artifact_bundle, save_bundle_artifact
from .bundles import zip_bundle
from .execution import policy_fingerprint, validate_execution
from .execution_evidence import seal_execution
from .models import TeamRun
from .provider import ProviderError


CASES = [{'stdin': '6\n', 'expected_stdout': '7\n'},
         {'stdin': '-1\n', 'expected_stdout': '0\n'}]
BUNDLE = {'summary': '合成验收样例', 'files': [
    {'path': 'main.py', 'content': 'print(int(input()) + 1)\n'}]}


def completed_case():
    return {'status': 'passed', 'matched_stdout': True,
            'command_completed': True, 'cleanup_complete': True}


class CaseEvidenceSaveTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='case-evidence@example.test')
        self.conversation = Conversation.objects.create(owner=self.user, acceptance_cases=deepcopy(CASES))
        self.task = Task.objects.create(owner=self.user, conversation=self.conversation,
                                       goal='合成测试', status='running', execution_id=uuid.uuid4())
        self.run = TeamRun.objects.create(task=self.task, execution_id=self.task.execution_id,
            team_revision='synthetic', workflow='adaptive', delivery_mode='files',
            acceptance_cases=deepcopy(CASES), plan={'required_tools': []})

    def report(self, run=None):
        run = run or self.run
        return {'passed': True, 'run_id': str(run.id), 'policy_fingerprint': policy_fingerprint(),
                'scope': '合成执行记录，未启动进程。', 'checks': [{
                    'tool': 'python_stdio', 'status': 'passed', 'independent_validation': True,
                    'requested_cases': len(run.acceptance_cases),
                    'case_fingerprint': case_revision(run.acceptance_cases),
                    'cases': [completed_case() for _ in run.acceptance_cases]}]}

    def save(self, report=None, *, run=None, draft=False, bundle=None):
        source = bundle or BUNDLE
        report = self.report() if report is None else report
        return save_bundle_artifact(self.task, self.user, source,
            {'execution': seal_execution(source, report)},
            review_status='draft' if draft else 'ready', run=run or self.run)

    def test_ready_uses_exact_run_snapshot_even_when_plan_omits_stdio(self):
        artifact = self.save()
        self.assertTrue(artifact.validation['passed'])
        self.assertEqual(artifact.validation['acceptance'], {
            'run_id': str(self.run.id), 'revision': case_revision(CASES),
            'source': 'user', 'case_count': 2, 'passed': True})
        self.assertNotIn('expected_stdout', json.dumps(artifact.validation))
        with ZipFile(BytesIO(zip_bundle(artifact_bundle(artifact)))) as archive:
            self.assertEqual(archive.namelist(), ['main.py'])
            self.assertEqual(archive.read('main.py').decode(), BUNDLE['files'][0]['content'])

    def test_missing_execution_or_explicit_run_cannot_be_ready(self):
        for validation, run in (({}, self.run), ({'execution': seal_execution(BUNDLE, self.report())}, None)):
            with self.subTest(run=run), self.assertRaises(ProviderError):
                save_bundle_artifact(self.task, self.user, BUNDLE, validation, run=run)
        self.assertFalse(Artifact.objects.exists())

    def test_runless_legacy_path_cannot_bypass_existing_snapshot_after_cases_removed(self):
        self.conversation.acceptance_cases = []
        self.conversation.save(update_fields=['acceptance_cases'])
        with self.assertRaises(ProviderError):
            save_bundle_artifact(self.task, self.user, BUNDLE, {})

    def test_run_must_belong_to_same_task_and_owner(self):
        other = get_user_model().objects.create_user(email='foreign-cases@example.test')
        conversation = Conversation.objects.create(owner=other)
        task = Task.objects.create(owner=other, conversation=conversation, goal='other')
        foreign_run = TeamRun.objects.create(task=task, execution_id=uuid.uuid4(), team_revision='synthetic')
        with self.assertRaises(ProviderError):
            self.save(run=foreign_run)

    def test_old_run_and_replayed_receipt_cannot_certify_current_execution(self):
        old_report = self.report()
        current = TeamRun.objects.create(task=self.task, execution_id=uuid.uuid4(), team_revision='synthetic',
            workflow='adaptive', delivery_mode='files', acceptance_cases=deepcopy(CASES), plan={'required_tools': []})
        # Windows clocks can assign the same creation timestamp to both rows.
        TeamRun.objects.filter(pk=current.pk).update(created_at=self.run.created_at)
        Task.objects.filter(pk=self.task.pk).update(execution_id=current.execution_id)
        with self.assertRaises(ProviderError):
            self.save(old_report)
        with self.assertRaises(ProviderError):
            self.save(old_report, run=current)
        artifact = self.save(self.report(current), run=current)
        self.assertTrue(artifact.validation['passed'])

    def test_cleared_claim_with_equal_run_timestamps_fails_closed(self):
        later = TeamRun.objects.create(task=self.task, execution_id=uuid.uuid4(), team_revision='synthetic',
            workflow='adaptive', delivery_mode='files', acceptance_cases=deepcopy(CASES))
        TeamRun.objects.filter(pk=later.pk).update(created_at=self.run.created_at)
        Task.objects.filter(pk=self.task.pk).update(status='completed', execution_id=None)
        for run in (self.run, later):
            with self.subTest(run=run.pk), self.assertRaises(ProviderError):
                self.save(self.report(run), run=run)

    def test_execution_claim_or_paused_task_cannot_receive_late_ready_result(self):
        for status, execution_id in (('running', uuid.uuid4()), ('paused', None)):
            with self.subTest(status=status):
                Task.objects.filter(pk=self.task.pk).update(status=status, execution_id=execution_id)
                with self.assertRaises(ProviderError):
                    self.save()

    def test_finished_current_task_can_save_after_atomic_execution_claim_is_cleared(self):
        Task.objects.filter(pk=self.task.pk).update(status='completed', execution_id=None)
        self.assertEqual(self.save().review_status, 'ready')

    def test_changed_current_user_cases_invalidate_old_ready_evidence(self):
        self.conversation.acceptance_cases = [{'stdin': '8\n', 'expected_stdout': '9\n'}]
        self.conversation.save(update_fields=['acceptance_cases'])
        with self.assertRaises(ProviderError):
            self.save()
        draft = self.save(draft=True)
        self.assertFalse(draft.validation['passed'])
        self.assertFalse(draft.validation['acceptance']['passed'])
        self.assertEqual(draft.validation['acceptance']['revision'], case_revision(CASES))

    def test_caller_cannot_replace_snapshot_in_memory(self):
        self.run.acceptance_cases = [{'stdin': '6\n', 'expected_stdout': 'changed\n'}]
        with self.assertRaises(ProviderError):
            self.save(self.report())

    def test_all_independent_evidence_facts_are_required(self):
        mutations = [
            lambda report: report.update(run_id=str(uuid.uuid4())),
            lambda report: report.update(policy_fingerprint='old-policy'),
            lambda report: report['checks'][0].update(case_fingerprint='old-cases'),
            lambda report: report['checks'][0].update(independent_validation=False),
            lambda report: report['checks'][0].update(requested_cases=True),
            lambda report: report['checks'][0].update(requested_cases=1),
            lambda report: report['checks'][0].update(cases=[completed_case()]),
            lambda report: report['checks'].append(deepcopy(report['checks'][0])),
            lambda report: report['checks'][0]['cases'][0].update(status='not_run'),
            lambda report: report['checks'][0]['cases'][0].update(matched_stdout=False),
            lambda report: report['checks'][0]['cases'][0].update(command_completed=False),
            lambda report: report['checks'][0]['cases'][0].update(cleanup_complete=False),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                report = self.report()
                mutate(report)
                with self.assertRaises(ProviderError):
                    self.save(report)
        self.assertFalse(Artifact.objects.exists())

    def test_changed_file_cannot_reuse_receipt(self):
        changed = deepcopy(BUNDLE)
        changed['files'][0]['content'] = 'print(99)\n'
        with self.assertRaises(ProviderError):
            save_bundle_artifact(self.task, self.user, changed,
                {'execution': seal_execution(BUNDLE, self.report())}, run=self.run)

    def test_unexecuted_draft_keeps_source_without_claiming_pass_or_copying_expected_text(self):
        Task.objects.filter(pk=self.task.pk).update(status='failed', execution_id=None)
        draft = save_bundle_artifact(self.task, self.user, BUNDLE, {}, review_status='draft', run=self.run)
        self.assertFalse(draft.validation['passed'])
        self.assertFalse(draft.validation['acceptance']['passed'])
        self.assertEqual(draft.validation['acceptance']['run_id'], str(self.run.id))
        self.assertEqual(draft.validation['checks'][-1]['status'], 'not_run')
        self.assertNotIn('expected_stdout', json.dumps(draft.validation))


class CaseExecutionMetadataTests(SimpleTestCase):
    def ready(self):
        return {'available_tools': ['python_stdio'], 'checks': [], 'policy_fingerprint': policy_fingerprint()}

    def execute(self, *, state=None, bundle=None, outcomes=None):
        with patch('delivery.execution.readiness', return_value=state or self.ready()), \
                patch('delivery.execution._execute', side_effect=outcomes) as invoked:
            result = validate_execution(bundle or BUNDLE, ('python_stdio',), cases=deepcopy(CASES))
        return result['checks'][0], invoked

    def test_unavailable_environment_preserves_case_identity_and_marks_all_unrun(self):
        state = {'available_tools': [], 'checks': [], 'policy_fingerprint': policy_fingerprint()}
        check, invoked = self.execute(state=state)
        self.assertEqual(check['status'], 'not_run')
        self.assertEqual(check['requested_cases'], 2)
        self.assertEqual(check['case_fingerprint'], case_revision(CASES))
        self.assertEqual([item['status'] for item in check['cases']], ['not_run', 'not_run'])
        self.assertNotIn('expected_stdout', json.dumps(check))
        invoked.assert_not_called()

    def test_missing_entry_point_has_identity_and_is_repairable_failure(self):
        check, invoked = self.execute(bundle={'summary': '缺入口', 'files': [{'path': 'other.py', 'content': ''}]})
        self.assertEqual(check['status'], 'failed')
        self.assertEqual(check['reason_code'], 'missing_source')
        self.assertEqual(check['case_fingerprint'], case_revision(CASES))
        self.assertEqual(len(check['cases']), 2)
        invoked.assert_not_called()

    def test_lost_runtime_propagates_not_run_and_prevents_paid_repair(self):
        check, invoked = self.execute(outcomes=[{'status': 'not_run', 'reason_code': 'environment_unavailable',
                                                'detail': '合成环境中断。'}])
        self.assertEqual(check['status'], 'not_run')
        self.assertEqual(check['reason_code'], 'environment_unavailable')
        self.assertEqual([item['status'] for item in check['cases']], ['not_run', 'not_run'])
        self.assertEqual(invoked.call_count, 1)

    def test_actual_failure_remains_repairable_despite_unattempted_following_cases(self):
        check, invoked = self.execute(outcomes=[{'status': 'failed', 'reason_code': 'runtime_failure',
                                                'matched_stdout': False, 'command_completed': True}])
        self.assertEqual(check['status'], 'failed')
        self.assertEqual([item['status'] for item in check['cases']], ['failed', 'not_run'])
        self.assertEqual(check['cases'][1]['reason_code'], 'previous_case_failed')
        self.assertEqual(invoked.call_count, 1)

    def test_policy_change_midrun_records_unrun_cases_without_extra_execution(self):
        fingerprint = policy_fingerprint()
        with patch('delivery.execution.policy_fingerprint', side_effect=[fingerprint, fingerprint,
                                                                        fingerprint, 'changed-policy']):
            check, invoked = self.execute(state={'available_tools': ['python_stdio'], 'checks': [],
                                                'policy_fingerprint': fingerprint}, outcomes=[completed_case()])
        self.assertEqual(check['status'], 'not_run')
        self.assertEqual(check['reason_code'], 'policy_changed')
        self.assertEqual([item['status'] for item in check['cases']], ['passed', 'not_run'])
        self.assertEqual(invoked.call_count, 1)

    def test_batch_time_budget_marks_unattempted_cases_without_execution(self):
        with patch('delivery.execution.time.monotonic', side_effect=[0, 31]):
            check, invoked = self.execute(outcomes=[])
        self.assertEqual(check['status'], 'not_run')
        self.assertEqual(check['reason_code'], 'time_limit')
        self.assertEqual([item['status'] for item in check['cases']], ['not_run', 'not_run'])
        invoked.assert_not_called()
