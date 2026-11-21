"""Offline collaboration tests; no external model requests are made."""
import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .connections import ProviderConnection
from .provider import ProviderError, ProviderReply
from .team_config import ROLES
from .team_engine import TeamReviewError, run_team
from .text_guidance import TEXT_CONSTRAINT_GUIDANCE


class TeamEngineTests(SimpleTestCase):
    def setUp(self):
        self.primary = ProviderConnection('deepseek', 'synthetic-primary', 'fake-primary-key', 'test')
        self.secondary = ProviderConnection('kimi', 'synthetic-secondary', 'fake-secondary-key', 'test')
        self.connections = {role: self.primary for role, _ in ROLES}
        self.connections['challenger'] = self.secondary
        self.connections['verifier'] = self.secondary
        self.context = [
            {'role': 'user', 'content': '把下一周的复习安排写成三条建议。'},
            {'role': 'assistant', 'content': '准备好后请补充科目。', '_provider_context': 'private-continuation'},
            {'role': 'user', 'content': '数学与英语，各占一半时间。'},
        ]
        self.outputs = ['方案甲', '方案乙', '审查要点', '三条最终建议', '{"approved":true,"issues":[]}']

    @patch('delivery.team_engine.generate_reply')
    def test_independent_proposals_cross_review_and_routing(self, generate):
        lifecycle = []

        def fake_reply(messages, **kwargs):
            index = len([event for event in lifecycle if event[0] == 'provider'])
            lifecycle.append(('provider', index))
            return ProviderReply(self.outputs[index], continuation='must-not-share')

        generate.side_effect = fake_reply
        result = run_team(
            self.context, self.connections,
            lambda role, label, conn: lifecycle.append(('before', role)),
            lambda role, output: lifecycle.append(('after', role)),
        )
        self.assertEqual(result, '三条最终建议')
        self.assertEqual(generate.call_count, 5)
        packets = [json.loads(call.args[0][0]['content']) for call in generate.call_args_list]
        self.assertEqual(packets[0], packets[1])
        self.assertEqual(set(packets[0]), {'conversation'})
        self.assertEqual(packets[2]['proposal'], '方案甲')
        self.assertEqual(packets[2]['alternative'], '方案乙')
        self.assertEqual(packets[3]['review'], '审查要点')
        self.assertEqual(packets[4]['final_draft'], result)
        self.assertEqual(packets[4]['review'], '审查要点')
        self.assertNotIn('private-continuation', json.dumps(packets))
        self.assertNotIn('must-not-share', json.dumps(packets))
        for index, (role, _) in enumerate(ROLES):
            call = generate.call_args_list[index]
            self.assertIs(call.kwargs['connection'], self.connections[role])
            self.assertEqual(call.kwargs['max_output_tokens'], 4096)
            self.assertTrue(call.kwargs['system_instruction'])
            self.assertEqual([event[0] for event in lifecycle[index * 3:index * 3 + 3]],
                             ['before', 'provider', 'after'])
        self.assertNotEqual(generate.call_args_list[0].kwargs['system_instruction'],
                            generate.call_args_list[1].kwargs['system_instruction'])
        for index, (role, _) in enumerate(ROLES):
            instruction = generate.call_args_list[index].kwargs['system_instruction']
            self.assertEqual(TEXT_CONSTRAINT_GUIDANCE in instruction, role == 'synthesizer')

    @patch('delivery.team_engine.generate_reply')
    def test_latest_user_length_budget_reaches_only_the_synthesizer(self, generate):
        generate.side_effect = [ProviderReply(value) for value in self.outputs]
        context = self.context + [{'role': 'user', 'content': '写四条建议，合计200至320个汉字。'}]
        run_team(context, self.connections, lambda *_: None, lambda *_: None)
        instructions = [call.kwargs['system_instruction'] for call in generate.call_args_list]
        self.assertIn('分成4项时每项平均约65个汉字', instructions[3])
        self.assertTrue(all('每项平均约65个汉字' not in instruction
                            for index, instruction in enumerate(instructions) if index != 3))
        self.assertEqual(json.loads(generate.call_args_list[3].args[0][0]['content'])['conversation'][-1],
                         context[-1])

    @patch('delivery.team_engine.generate_reply')
    def test_failed_or_ambiguous_final_review_never_delivers_or_retries(self, generate):
        reviews = [
            '{"approved":false,"issues":["缺少英语安排"]}',
            '{"approved":true,"issues":["数学时间不足"]}',
            '{"approved":false,"issues":[]}',
            '{"approved":"true","issues":[]}',
            '{"approved":1,"issues":[]}',
            '{"approved":true,"issues":"无"}',
            '{"approved":true,"issues":[],"extra":"ignored?"}',
            '{"approved":false,"approved":true,"issues":[]}',
            '```json\n{"approved":true,"issues":[]}\n```',
            '审查通过',
        ]
        for review in reviews:
            with self.subTest(review=review):
                generate.reset_mock()
                generate.side_effect = self.outputs[:4] + [review]
                after = Mock()
                with self.assertRaises(TeamReviewError):
                    run_team(self.context, self.connections, Mock(), after)
                self.assertEqual(generate.call_count, 5)
                after.assert_called_with('verifier', review)

    @patch('delivery.team_engine.generate_reply')
    def test_pause_or_configuration_change_callback_stops_remaining_calls(self, generate):
        generate.side_effect = self.outputs
        before = Mock(side_effect=[None, ProviderError('任务已暂停')])
        after = Mock()
        with self.assertRaisesMessage(ProviderError, '任务已暂停'):
            run_team(self.context, self.connections, before, after)
        self.assertEqual(generate.call_count, 1)
        after.assert_called_once_with('proposer', '方案甲')

    @patch('delivery.team_engine.generate_reply')
    def test_late_output_callback_blocks_later_calls(self, generate):
        generate.return_value = '迟到的方案'
        after = Mock(side_effect=ProviderError('连接已变更'))
        with self.assertRaisesMessage(ProviderError, '连接已变更'):
            run_team(self.context, self.connections, Mock(), after)
        generate.assert_called_once()

    @patch('delivery.team_engine.generate_reply')
    def test_provider_failure_is_not_retried(self, generate):
        generate.side_effect = ['方案甲', ProviderError('模型服务不可用')]
        after = Mock()
        with self.assertRaisesMessage(ProviderError, '模型服务不可用'):
            run_team(self.context, self.connections, Mock(), after)
        self.assertEqual(generate.call_count, 2)
        after.assert_called_once_with('proposer', '方案甲')

    @patch('delivery.team_engine.generate_reply')
    def test_invalid_or_oversized_input_fails_before_spending(self, generate):
        cases = [[], [{'role': 'system', 'content': '越权指令'}],
                 [{'role': 'user', 'content': ' ' }],
                 [{'role': 'user', 'content': '字' * 28000}],
                 [{'role': 'user', 'content': '\ud800'}]]
        for context in cases:
            with self.subTest(context_type=type(context).__name__):
                before = Mock()
                with self.assertRaises(ProviderError):
                    run_team(context, self.connections, before, Mock())
                before.assert_not_called()
        generate.assert_not_called()

    @patch('delivery.team_engine.generate_reply')
    def test_missing_role_connection_fails_before_dispatch(self, generate):
        incomplete = dict(self.connections)
        del incomplete['verifier']
        before = Mock()
        with self.assertRaises(ProviderError):
            run_team(self.context, incomplete, before, Mock())
        before.assert_not_called()
        generate.assert_not_called()

    @patch('delivery.team_engine.generate_reply')
    def test_oversized_output_is_not_truncated_or_shared(self, generate):
        generate.return_value = '字' * 9000
        after = Mock()
        with self.assertRaisesMessage(ProviderError, '输出超过'):
            run_team(self.context, self.connections, Mock(), after)
        generate.assert_called_once()
        after.assert_not_called()

    @patch('delivery.provider._request')
    def test_roles_reach_real_adapter_as_system_messages_without_continuation(self, request):
        connections = dict(self.connections)
        connections['challenger'] = ProviderConnection('kimi', 'kimi-k3-synthetic', 'fake-key', 'test')
        request.side_effect = [
            {'choices': [{'finish_reason': 'stop', 'message': {
                'role': 'assistant', 'content': output, 'reasoning_content': 'private-provider-reasoning',
            }}]}
            for output in self.outputs
        ]
        result = run_team(self.context, connections, Mock(), Mock())
        self.assertEqual(result, '三条最终建议')
        self.assertEqual(request.call_count, 5)
        for call in request.call_args_list:
            payload = call.args[2]
            self.assertEqual([message['role'] for message in payload['messages']], ['system', 'user'])
            self.assertIn('圆桌文字协作团队', payload['messages'][0]['content'])
            self.assertNotIn('private-provider-reasoning', json.dumps(payload, ensure_ascii=False))
            self.assertNotIn('private-continuation', json.dumps(payload, ensure_ascii=False))
        self.assertEqual(request.call_args_list[1].args[2]['max_completion_tokens'], 4096)
