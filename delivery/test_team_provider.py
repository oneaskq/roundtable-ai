from unittest.mock import patch

from django.test import SimpleTestCase

from .connections import ProviderConnection
from .provider import ProviderError, generate_reply


class TeamProviderProtocolTests(SimpleTestCase):
    @patch('delivery.provider._request')
    def test_role_prompt_is_system_and_kimi_private_thinking_is_not_carried(self, request):
        request.return_value = {'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': '公开成果', 'reasoning_content': '私有推理不会保留'}}]}
        connection = ProviderConnection('kimi', 'kimi-k3-test', 'synthetic', 'test')
        result = generate_reply([{'role': 'user', 'content': '{"task":"写计划"}'}],
                                connection=connection, system_instruction='受信角色职责', max_output_tokens=4096)
        payload = request.call_args.args[2]
        self.assertEqual(payload['messages'][0], {'role': 'system', 'content': '受信角色职责'})
        self.assertEqual(payload['messages'][1]['role'], 'user')
        self.assertEqual(payload['max_completion_tokens'], 4096)
        self.assertEqual(str(result), '公开成果')
        self.assertEqual(result.continuation, '')

    @patch('delivery.provider._request')
    def test_role_instruction_cannot_rewrite_an_existing_private_lineage(self, request):
        connection = ProviderConnection('kimi', 'kimi-k3-test', 'synthetic', 'test')
        for messages in ([{'role': 'assistant', 'content': '旧输出'}],
                         [{'role': 'user', 'content': '新问题', '_provider_context': 'old-token'}],
                         [{'role': 'user', 'content': '问题'}, {'role': 'assistant', 'content': '答复'}]):
            with self.assertRaises(ProviderError):
                generate_reply(messages, connection=connection, system_instruction='新角色')
        request.assert_not_called()
