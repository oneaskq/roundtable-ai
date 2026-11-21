"""Independent owner cases stay outside prompts and bind every file revision."""
import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .acceptance_cases import case_revision
from .adaptive_team_engine import run_adaptive_team
from .check_summaries import model_validation
from .connections import ProviderConnection
from .execution_evidence import verified_execution
from .file_team_engine import run_file_team
from .planning import PlanToolsUnavailable, parse_plan
from .provider import ProviderError
from .team_config import ROLES
from .team_engine import TeamReviewError, run_team
from .test_adaptive_team import plan_fixture


CASES = [{'stdin': 'PRIVATE_INPUT_A\n', 'expected_stdout': 'PRIVATE_EXPECT_A\n'}]
RUN_ID = 'e45ee076-832a-4cbd-b3ee-5a9daec7203c'
APPROVED = '{"approved":true,"issues":[]}'


def candidate():
    return json.dumps({'summary': '源码与实际检查范围见记录。', 'files': [
        {'path': 'main.py', 'content': 'import sys\nprint(sys.stdin.read().strip())\n'}]})


def execution_report(status='passed'):
    return {'passed': status == 'passed', 'scope': '仅覆盖用户独立提供的用例。',
            'policy_fingerprint': 'mock-policy', 'checks': [{
                'tool': 'python_stdio', 'path': 'main.py', 'check': 'Python 输入输出验收',
                'status': status, 'detail': '独立输入输出比对', 'independent_validation': True,
                'requested_cases': 1, 'case_fingerprint': case_revision(CASES),
                'cases': [{'status': status, 'output': 'PRIVATE_INPUT_A\n',
                           'stderr': 'PRIVATE_EXPECT_A\n', 'matched_stdout': status == 'passed',
                           'command_completed': True, 'cleanup_complete': True}]}]}


class OwnerCaseEngineTests(SimpleTestCase):
    def setUp(self):
        self.connections = {role: ProviderConnection('deepseek', 'fake-model', 'fake-key', 'test')
                            for role, _ in ROLES}
        self.context = [{'role': 'user', 'content': '交付一个读取标准输入的Python程序。'}]
        self.preparation = {'plan': plan_fixture(tools=['python_stdio']), 'work_outputs': {},
                            'review': '公开审查材料'}

    def test_server_mandatory_check_survives_planner_omission(self):
        plan = parse_plan(json.dumps(plan_fixture()), delivery_mode='files',
                          available_tools=['python_stdio'], mandatory_tools=['python_stdio'])
        self.assertEqual(plan['required_tools'], ['python_stdio'])
        with self.assertRaises(PlanToolsUnavailable):
            parse_plan(json.dumps(plan_fixture()), delivery_mode='files', mandatory_tools=['python_stdio'])

    @patch('delivery.adaptive_team_engine.generate_reply')
    @patch('delivery.adaptive_team_engine.readiness', return_value={'available_tools': []})
    def test_unavailable_runtime_stops_before_planner(self, readiness, generate):
        with self.assertRaises(PlanToolsUnavailable):
            run_adaptive_team(self.context, self.connections, Mock(), Mock(), delivery_mode='files',
                              acceptance_cases=CASES, execution_run_id=RUN_ID)
        generate.assert_not_called()

    @patch('delivery.adaptive_team_engine.generate_reply', return_value=json.dumps(plan_fixture(tools=['python_stdio'])))
    @patch('delivery.adaptive_team_engine.readiness', return_value={'available_tools': ['python_stdio']})
    def test_planner_cannot_invent_authoritative_cases(self, readiness, generate):
        with self.assertRaises(PlanToolsUnavailable):
            run_adaptive_team(self.context, self.connections, Mock(), Mock(), delivery_mode='files')
        self.assertEqual(generate.call_count, 1)

    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_text_and_fixed_cannot_drop_user_cases(self, generate):
        for mode, workflow in [('text', 'adaptive'), ('files', 'fixed')]:
            with self.assertRaises(ProviderError):
                run_team(self.context, self.connections, Mock(), Mock(), delivery_mode=mode,
                         workflow=workflow, acceptance_cases=CASES, execution_run_id=RUN_ID)
        generate.assert_not_called()

    @patch('delivery.execution.readiness', return_value={'available_tools': ['python_stdio']})
    @patch('delivery.execution.validate_execution', side_effect=lambda *a, **k: execution_report())
    @patch('delivery.file_team_engine.generate_reply', side_effect=[candidate(), APPROVED])
    @patch('delivery.adaptive_team_engine.generate_reply', side_effect=[json.dumps(plan_fixture()), '独立材料甲', '独立材料乙', '审查完成'])
    @patch('delivery.adaptive_team_engine.readiness', return_value={'available_tools': ['python_stdio']})
    def test_full_adaptive_flow_forces_case_check_and_hides_io(self, ready, planning, files, execute, runtime):
        after_plan = Mock()
        result = run_team(self.context, self.connections, Mock(), Mock(), delivery_mode='files',
                          workflow='adaptive', acceptance_cases=CASES, execution_run_id=RUN_ID,
                          after_plan=after_plan)
        self.assertEqual(after_plan.call_args.args[0]['required_tools'], ['python_stdio'])
        self.assertEqual(execute.call_args.kwargs['cases'], CASES)
        receipt = verified_execution(result.bundle, result.validation['execution'])
        self.assertEqual(receipt['run_id'], RUN_ID)
        self.assertEqual(receipt['checks'][0]['case_fingerprint'], case_revision(CASES))
        for call in planning.call_args_list + files.call_args_list:
            payload = call.args[0][0]['content'] + call.kwargs['system_instruction']
            self.assertNotIn('PRIVATE_INPUT_A', payload)
            self.assertNotIn('PRIVATE_EXPECT_A', payload)

    @patch('delivery.execution.readiness', return_value={'available_tools': ['python_stdio']})
    @patch('delivery.execution.validate_execution', side_effect=[execution_report('failed'), execution_report()])
    @patch('delivery.file_team_engine.generate_reply', side_effect=[candidate(), candidate(), APPROVED])
    def test_repair_reuses_same_snapshot_and_sanitizes_failed_stdout(self, generate, execute, runtime):
        result = run_file_team(self.context, self.connections, Mock(), Mock(), preparation=self.preparation,
                               acceptance_cases=CASES, execution_run_id=RUN_ID)
        self.assertTrue(result.validation['passed'])
        self.assertEqual(execute.call_count, 2)
        self.assertEqual([call.kwargs['cases'] for call in execute.call_args_list], [CASES, CASES])
        self.assertEqual(generate.call_count, 3)
        repair = generate.call_args_list[1].args[0][0]['content']
        self.assertNotIn('PRIVATE_INPUT_A', repair)
        self.assertNotIn('PRIVATE_EXPECT_A', repair)

    @patch('delivery.execution.readiness', return_value={'available_tools': ['python_stdio']})
    @patch('delivery.execution.validate_execution')
    @patch('delivery.file_team_engine.generate_reply', return_value=candidate())
    def test_environment_lost_after_generation_preserves_draft_without_repair(self, generate, execute, runtime):
        report = execution_report('not_run')
        report['checks'][0]['reason_code'] = 'environment_unavailable'
        execute.return_value = report
        with self.assertRaises(TeamReviewError) as caught:
            run_file_team(self.context, self.connections, Mock(), Mock(), preparation=self.preparation,
                          acceptance_cases=CASES, execution_run_id=RUN_ID)
        self.assertIsNotNone(caught.exception.bundle)
        self.assertFalse(caught.exception.validation['passed'])
        self.assertEqual(generate.call_count, 1)

    def test_history_projection_never_includes_nested_case_data_or_receipts(self):
        report = execution_report()
        report['execution'] = {'report': {'secret': CASES}, 'signature': 'private-receipt'}
        report['acceptance_cases'] = CASES
        report['checks'][0]['detail'] = 'PRIVATE_EXPECT_A'
        projected = model_validation(report)
        self.assertTrue(projected['passed'])
        self.assertEqual(projected['checks'][0]['case_statuses'], ['passed'])
        encoded = json.dumps(projected)
        for secret in ('PRIVATE_EXPECT_A', 'PRIVATE_INPUT_A', 'private-receipt', 'case_fingerprint'):
            self.assertNotIn(secret, encoded)
