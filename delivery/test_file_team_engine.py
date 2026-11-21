import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .connections import ProviderConnection
from .file_team_engine import run_file_team
from .provider import ProviderError
from .team_config import ROLES
from .team_engine import TeamReviewError


def candidate(content='print("hello")', path='main.py'):
    return json.dumps({'summary': '文件已生成，尚未运行功能测试。',
                       'files': [{'path': path, 'content': content}]}, ensure_ascii=False)


class FileTeamEngineTests(SimpleTestCase):
    def setUp(self):
        self.context = [{'role': 'user', 'content': '写一个简单的Python程序，提供源码'}]
        self.connections = {role: ProviderConnection('deepseek', 'synthetic-model', 'fake-key', 'test') for role, _ in ROLES}
        self.prefix = ['独立提案甲', '独立提案乙', '交叉审查公开意见']
        self.approved = '{"approved":true,"issues":[]}'

    @patch('delivery.file_team_engine.generate_reply')
    def test_source_gets_real_static_checks_without_executing_code(self, generate):
        generate.side_effect = self.prefix + [candidate('raise RuntimeError("MUST NOT EXECUTE")'), self.approved]
        checks = Mock()
        result = run_file_team(self.context, self.connections, Mock(), Mock(), after_check=checks)
        self.assertTrue(result.validation['passed'])
        self.assertEqual(generate.call_count, 5)
        checks.assert_called_once()
        packet = json.loads(generate.call_args_list[4].args[0][0]['content'])
        self.assertTrue(packet['system_checks']['passed'])
        self.assertEqual(packet['final_draft']['files'][0]['path'], 'main.py')

    @patch('delivery.file_team_engine.generate_reply')
    def test_invalid_python_is_repaired_once_then_checked_before_model_review(self, generate):
        generate.side_effect = self.prefix + [candidate('def broken('), candidate(), self.approved]
        before, checks = Mock(), Mock()
        result = run_file_team(self.context, self.connections, before, Mock(), after_check=checks)
        self.assertEqual([call.args[0] for call in before.call_args_list],
                         ['proposer', 'challenger', 'reviewer', 'synthesizer', 'repair', 'recheck'])
        self.assertFalse(checks.call_args_list[0].args[1]['passed'])
        self.assertTrue(checks.call_args_list[1].args[1]['passed'])
        repair_packet = json.loads(generate.call_args_list[4].args[0][0]['content'])
        self.assertFalse(repair_packet['system_checks']['passed'])
        self.assertTrue(result.validation['passed'])

    @patch('delivery.file_team_engine.generate_reply')
    def test_model_rejection_gets_only_one_revision_and_seven_calls(self, generate):
        reject = '{"approved":false,"issues":["用户要求未覆盖"]}'
        generate.side_effect = self.prefix + [candidate(), reject, candidate('print("revised")'), reject]
        with self.assertRaises(TeamReviewError) as caught:
            run_file_team(self.context, self.connections, Mock(), Mock())
        self.assertEqual(generate.call_count, 7)
        self.assertIn('revised', caught.exception.bundle['files'][0]['content'])
        self.assertTrue(caught.exception.validation['passed'])

    @patch('delivery.file_team_engine.generate_reply')
    def test_unsafe_paths_never_become_deliverable_even_after_repair(self, generate):
        generate.side_effect = self.prefix + [candidate(path='../outside.py'), candidate(path='C:/outside.py')]
        with self.assertRaises(TeamReviewError) as caught:
            run_file_team(self.context, self.connections, Mock(), Mock())
        self.assertIsNone(caught.exception.bundle)
        self.assertEqual(generate.call_count, 5)

    @patch('delivery.file_team_engine.generate_reply')
    def test_network_failure_never_triggers_automatic_paid_retry(self, generate):
        generate.side_effect = self.prefix + [ProviderError('合成服务故障')]
        with self.assertRaises(ProviderError):
            run_file_team(self.context, self.connections, Mock(), Mock())
        self.assertEqual(generate.call_count, 4)

    @patch('delivery.file_team_engine.generate_reply')
    def test_cancellation_after_static_check_stops_revision_and_review(self, generate):
        generate.side_effect = self.prefix + [candidate('def broken(')]
        with self.assertRaisesRegex(RuntimeError, 'paused'):
            run_file_team(self.context, self.connections, Mock(), Mock(), after_check=Mock(side_effect=RuntimeError('paused')))
        self.assertEqual(generate.call_count, 4)
