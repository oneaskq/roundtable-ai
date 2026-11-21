"""The opt-in thinking acceptance keeps the saved connection and spend bound."""
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core import signing
from django.test import TestCase

from .acceptance import WRITING_PROMPT, _canonical, _probe_instruction, read_report
from .connections import ProviderConnection
from .models import CapabilityEvidence
from .provider import ProviderError, ProviderReply
from diagnostics.qwen_thinking_acceptance import main, verify_source


class QwenThinkingAcceptanceTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        override = self.settings(BASE_DIR=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.connection = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-secret',
                                             'cn-beijing', '', 'bailian')
        self.writing = '\n'.join(f'{index}. ' + '字' * 40 for index in range(1, 4))
        self.source = {'schema_version': 1, 'kind': 'preliminary_qwen_thinking_writing',
                       'approvable': False, 'passed': True, 'thinking_observed': True,
                       'mode': 'isolated_thinking_diagnostic', 'provider': 'qwen',
                       'model': 'qwen3.7-plus', 'service_platform': 'bailian',
                       'service_region': 'cn-beijing', 'saved_connection_fingerprint': self.connection.fingerprint,
                       'prompt': WRITING_PROMPT,
                       'system_instruction': _probe_instruction('chat.writing', [WRITING_PROMPT]),
                       'parameters': {'enable_thinking': True, 'thinking_budget': 2048,
                                      'preserve_thinking': False, 'max_output_tokens': 4096},
                       'output': self.writing}

    def write_source(self, payload):
        path = self.root / 'probe.json'
        document = {'probe': payload, 'signature': signing.Signer(
            salt='roundtable.qwen-thinking-probe.v1').signature(_canonical(payload))}
        path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
        return path

    def test_source_signature_connection_prompt_and_output_are_all_checked(self):
        path = self.write_source(self.source)
        self.assertEqual(verify_source(self.connection, path)['output'], self.writing)
        with self.assertRaises(ValueError):
            verify_source(replace(self.connection, api_key='different'), path)
        for changes in ({'prompt': 'private user input'}, {'output': '太短'},
                        {'thinking_observed': False}, {'passed': False}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                verify_source(self.connection, self.write_source({**self.source, **changes}))
        path = self.write_source(self.source)
        document = json.loads(path.read_text(encoding='utf-8'))
        document['probe']['output'] = 'tampered'
        path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
        with self.assertRaises(ValueError):
            verify_source(self.connection, path)

    def test_preview_and_missing_ack_do_not_load_connection(self):
        output = StringIO()
        with patch('sys.argv', ['qwen_thinking_acceptance.py']), patch('sys.stdout', output), \
                patch('delivery.connections.load_named_connection') as load:
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue())['generation_calls_max'], 5)
        load.assert_not_called()
        with patch('sys.argv', ['qwen_thinking_acceptance.py', '--execute']), \
                patch('sys.stderr', new_callable=StringIO), \
                patch('delivery.connections.load_named_connection') as load:
            with self.assertRaises(SystemExit):
                main()
        load.assert_not_called()

    def synthetic_reply(self, messages, **kwargs):
        from django.conf import settings
        self.assertFalse(settings.QWEN_STRUCTURED_HANZI_ENABLED)
        self.assertEqual(kwargs['connection'], replace(self.connection, reasoning_effort='low'))
        self.assertEqual(kwargs['max_output_tokens'], 4096)
        prompt = messages[-1]['content']
        if '17乘19' in prompt:
            return ProviderReply('323', thinking_observed=True)
        if '第一次去自习室' in prompt:
            return ProviderReply(self.writing, thinking_observed=True)
        if '以下是虚构项目资料' in prompt:
            return ProviderReply('- 项目名：松林\n- 启动日期：2026年11月3日\n- 参与人数：8人')
        if '这是合成记忆测试' in prompt:
            return ProviderReply('已记住')
        phrase = messages[0]['content'].split('短语：')[1].split('。')[0]
        return ProviderReply(phrase)

    def run_fake(self, generate):
        output = StringIO()
        args = ['qwen_thinking_acceptance.py', '--execute', '--acknowledge-paid-calls', '--source', 'synthetic-source.json']
        with patch('sys.argv', args), patch('sys.stdout', output), \
                patch('diagnostics.qwen_thinking_acceptance.current_server_version', return_value='0.14.1-internal'), \
                patch('diagnostics.qwen_thinking_acceptance.verify_source', return_value=self.source), \
                patch('delivery.connections.load_named_connection', return_value=self.connection), \
                patch('delivery.connections.save_named_connection') as save, \
                patch('delivery.acceptance.generate_reply', side_effect=generate) as calls:
            status = main()
        save.assert_not_called()
        return status, json.loads(output.getvalue()), calls

    def test_full_batch_never_changes_connection_or_approves_capabilities(self):
        with self.settings(QWEN_STRUCTURED_HANZI_ENABLED=True):
            status, result, calls = self.run_fake(self.synthetic_reply)
        self.assertEqual(status, 0)
        self.assertEqual(calls.call_count, 5)
        self.assertEqual(result['generation_attempts'], 5)
        self.assertTrue(result['passed'])
        self.assertFalse(result['saved_connection_changed'])
        self.assertFalse(result['enabled'])
        report = read_report(result['report'])
        self.assertTrue(report['passed'])
        self.assertEqual(report['reasoning_effort'], 'low')
        self.assertEqual(report['connection_fingerprint'], replace(self.connection, reasoning_effort='low').fingerprint)
        self.assertTrue(report['results'][1]['thinking_observed'])
        self.assertFalse(CapabilityEvidence.objects.exists())
        self.assertNotIn('synthetic-secret', Path(result['journal']).read_text(encoding='utf-8'))

    def test_failed_writing_or_request_stops_without_later_calls(self):
        def bad_writing(messages, **kwargs):
            if '第一次去自习室' in messages[-1]['content']:
                return ProviderReply('太短', thinking_observed=True)
            return self.synthetic_reply(messages, **kwargs)

        status, result, calls = self.run_fake(bad_writing)
        self.assertEqual(status, 2)
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(result['generation_attempts'], 2)
        report = read_report(result['report'])
        self.assertTrue(all(item['diagnostic_code'] == 'not_run' for item in report['results'][2:]))
        self.assertFalse(CapabilityEvidence.objects.exists())
        status, result, calls = self.run_fake(ProviderError('synthetic-secret', diagnostic_code='http_400'))
        self.assertEqual(status, 2)
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(result['generation_attempts'], 1)
        self.assertFalse(result['passed'])
        self.assertNotIn('synthetic-secret', Path(result['report']).read_text(encoding='utf-8'))
