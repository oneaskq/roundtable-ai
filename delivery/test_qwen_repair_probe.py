"""The optional revision probe is synthetic, bounded and never approves anything."""
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .acceptance import WRITING_PROMPT
from .connections import ProviderConnection
from .provider import ProviderError
from diagnostics.qwen_repair_probe import main, source_draft


class QwenRepairProbeTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.connection = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-secret',
                                             'cn-beijing', service_platform='bailian')
        self.draft = '字' * 201
        self.source = {'report_id': 'synthetic', 'synthetic_only': True, 'provider': 'qwen',
                       'model': 'qwen3.7-plus', 'service_region': 'cn-beijing', 'service_platform': 'bailian',
                       'connection_fingerprint': self.connection.fingerprint,
                       'results': [{'capability': 'chat.writing', 'prompts': [WRITING_PROMPT], 'passed': False,
                                    'diagnostic_code': 'structured_validation_failed',
                                    'provider_outputs': [json.dumps({'answer': self.draft}, ensure_ascii=False)]}]}

    def test_preview_does_not_load_connection_or_send_request(self):
        output = StringIO()
        with patch('sys.argv', ['qwen_repair_probe.py']), patch('sys.stdout', output), \
                patch('delivery.connections.load_named_connection') as load:
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue())['generation_calls_max'], 1)
        load.assert_not_called()

    def test_execute_requires_explicit_acknowledgement(self):
        with patch('sys.argv', ['qwen_repair_probe.py', '--execute']), \
                patch('sys.stderr', new_callable=StringIO), \
                patch('delivery.connections.load_named_connection') as load:
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 2)
        load.assert_not_called()

    def test_source_must_be_synthetic_and_match_current_connection(self):
        with patch('delivery.acceptance.read_report', return_value=self.source):
            _source, draft, count = source_draft(self.connection, self.root / 'synthetic-source.json')
        self.assertEqual((draft, count), (self.draft, 201))
        for changes in ({'synthetic_only': False}, {'connection_fingerprint': 'different'},
                        {'model': 'another-model'}):
            with self.subTest(changes=changes), patch('delivery.acceptance.read_report',
                                                      return_value={**self.source, **changes}):
                with self.assertRaises(ValueError):
                    source_draft(self.connection, self.root / 'synthetic-source.json')

    def test_one_planned_revision_is_recorded_but_cannot_approve_capability(self):
        output = StringIO()
        revised = '\n'.join(f'{index}. ' + '字' * 40 for index in range(1, 4))
        generate = Mock(return_value=revised)
        arguments = ['qwen_repair_probe.py', '--execute', '--acknowledge-paid-call', '--source', str(self.root / 'synthetic-source.json')]
        with self.settings(BASE_DIR=self.root), patch('sys.argv', arguments), patch('sys.stdout', output), \
                patch('delivery.connections.load_named_connection', return_value=self.connection), \
                patch('delivery.acceptance.read_report', return_value=self.source), \
                patch('delivery.acceptance.generate_reply', generate):
            self.assertEqual(main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['generation_attempts'], 1)
        self.assertTrue(result['passed'])
        self.assertFalse(result['enabled'])
        self.assertEqual(generate.call_count, 1)
        packet = json.loads(generate.call_args.args[0][0]['content'])
        self.assertEqual(packet['original_request'], WRITING_PROMPT)
        self.assertEqual(packet['previous_draft'], self.draft)
        self.assertEqual(packet['measured_hanzi'], 201)
        saved = json.loads(Path(result['probe']).read_text(encoding='utf-8'))['probe']
        self.assertFalse(saved['approvable'])
        self.assertTrue(saved['manual_review_required'])
        self.assertNotIn('synthetic-secret', Path(result['probe']).read_text(encoding='utf-8'))

    def test_provider_failure_does_not_retry(self):
        output = StringIO()
        generate = Mock(side_effect=ProviderError('synthetic-secret', diagnostic_code='http_400'))
        arguments = ['qwen_repair_probe.py', '--execute', '--acknowledge-paid-call', '--source', str(self.root / 'synthetic-source.json')]
        with self.settings(BASE_DIR=self.root), patch('sys.argv', arguments), patch('sys.stdout', output), \
                patch('delivery.connections.load_named_connection', return_value=self.connection), \
                patch('delivery.acceptance.read_report', return_value=self.source), \
                patch('delivery.acceptance.generate_reply', generate):
            self.assertEqual(main(), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result['generation_attempts'], 1)
        self.assertEqual(result['diagnostic_code'], 'http_400')
        self.assertIsNone(result['probe'])
        self.assertEqual(generate.call_count, 1)
        self.assertNotIn('synthetic-secret', Path(result['journal']).read_text(encoding='utf-8'))
