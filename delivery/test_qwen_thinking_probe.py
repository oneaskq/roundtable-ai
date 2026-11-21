"""The isolated thinking probe has one synthetic call and cannot approve a model."""
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.core import signing
from django.test import SimpleTestCase

from .acceptance import WRITING_PROMPT, _canonical, _probe_instruction
from .connections import ProviderConnection
from .provider import ProviderError
from diagnostics.qwen_thinking_probe import THINKING_BUDGET, main, thinking_generation


class QwenThinkingProbeTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.connection = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-secret',
                                             'cn-beijing', service_platform='bailian')
        self.messages = [{'role': 'user', 'content': WRITING_PROMPT}]
        self.instruction = _probe_instruction('chat.writing', [WRITING_PROMPT])
        self.reply = '\n'.join(f'{index}. ' + '字' * 40 for index in range(1, 4))

    def test_preview_and_missing_ack_do_not_load_connection(self):
        output = StringIO()
        with patch('sys.argv', ['qwen_thinking_probe.py']), patch('sys.stdout', output), \
                patch('delivery.connections.load_named_connection') as load:
            self.assertEqual(main(), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['generation_calls_max'], 1)
        self.assertEqual(plan['thinking_budget_tokens'], THINKING_BUDGET)
        self.assertFalse(plan['approve_or_enable'])
        load.assert_not_called()
        with patch('sys.argv', ['qwen_thinking_probe.py', '--execute']), \
                patch('sys.stderr', new_callable=StringIO), \
                patch('delivery.connections.load_named_connection') as load:
            with self.assertRaises(SystemExit):
                main()
        load.assert_not_called()

    def test_only_fixed_synthetic_prompt_is_allowed_and_thought_is_not_returned(self):
        response = {'role': 'assistant', 'content': self.reply, 'reasoning_content': 'private-thought'}
        with patch('delivery.provider._stream_reply', return_value=response) as stream:
            result = thinking_generation(self.messages, connection=self.connection, max_output_tokens=4096,
                                         system_instruction=self.instruction, on_text=lambda _part: None)
        self.assertEqual(result, {'output': self.reply, 'thinking_observed': True})
        self.assertNotIn('private-thought', json.dumps(result))
        payload = stream.call_args.args[1]
        self.assertTrue(payload['enable_thinking'])
        self.assertEqual(payload['thinking_budget'], 2048)
        self.assertFalse(payload['preserve_thinking'])
        self.assertEqual(payload['max_completion_tokens'], 4086)
        self.assertTrue(stream.call_args.kwargs['preserve_reasoning'])
        self.assertEqual(payload['messages'][-1], self.messages[-1])

        with patch('delivery.provider._stream_reply') as stream:
            with self.assertRaises(ProviderError):
                thinking_generation([{'role': 'user', 'content': 'non-test private content'}],
                                    connection=self.connection, max_output_tokens=4096,
                                    system_instruction=self.instruction, on_text=lambda _part: None)
        stream.assert_not_called()

    def run_fake(self, response):
        output = StringIO()
        stream = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
        args = ['qwen_thinking_probe.py', '--execute', '--acknowledge-paid-call']
        with self.settings(BASE_DIR=self.root), patch('sys.argv', args), patch('sys.stdout', output), \
                patch('delivery.connections.load_named_connection', return_value=self.connection), \
                patch('delivery.provider._stream_reply', stream):
            status = main()
        return status, json.loads(output.getvalue()), stream

    def test_success_is_signed_separately_and_does_not_save_thought_or_secret(self):
        status, result, stream = self.run_fake({'role': 'assistant', 'content': self.reply,
                                                'reasoning_content': 'private-thought'})
        self.assertEqual(status, 0)
        self.assertEqual(stream.call_count, 1)
        self.assertEqual(result['generation_attempts'], 1)
        self.assertTrue(result['thinking_observed'])
        self.assertFalse(result['enabled'])
        raw = Path(result['probe']).read_text(encoding='utf-8')
        document = json.loads(raw)
        probe = document['probe']
        self.assertFalse(probe['approvable'])
        self.assertTrue(probe['manual_review_required'])
        self.assertEqual(probe['prompt'], WRITING_PROMPT)
        self.assertEqual(probe['output'], self.reply)
        self.assertNotIn('private-thought', raw)
        self.assertNotIn('synthetic-secret', raw)
        self.assertEqual(document['signature'],
                         signing.Signer(salt='roundtable.qwen-thinking-probe.v1').signature(_canonical(probe)))

    def test_missing_thinking_signal_or_provider_failure_never_retry(self):
        status, result, stream = self.run_fake({'role': 'assistant', 'content': self.reply})
        self.assertEqual(status, 2)
        self.assertFalse(result['passed'])
        self.assertFalse(result['thinking_observed'])
        self.assertEqual(stream.call_count, 1)
        status, result, stream = self.run_fake(ProviderError('synthetic-secret', diagnostic_code='http_400'))
        self.assertEqual(status, 2)
        self.assertEqual(result['diagnostic_code'], 'http_400')
        self.assertIsNone(result['probe'])
        self.assertEqual(stream.call_count, 1)
        self.assertNotIn('synthetic-secret', Path(result['journal']).read_text(encoding='utf-8'))
