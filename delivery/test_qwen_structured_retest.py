"""Synthetic tests for the conditional, bounded structured-output retest."""
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .connections import ProviderConnection
from diagnostics.qwen_structured_retest import main


class QwenStructuredRetestTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.connection = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-secret',
                                             'cn-beijing', service_platform='bailian')
        self.generate = Mock(return_value='synthetic')

    def test_preview_is_read_only(self):
        output = StringIO()
        with patch('sys.argv', ['qwen_structured_retest.py']), patch('sys.stdout', output), \
                patch('delivery.connections.load_named_connection') as load:
            self.assertEqual(main(), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['generation_calls_max'], 6)
        self.assertFalse(plan['approve_or_enable'])
        load.assert_not_called()

    def run_fake_batch(self, preliminary_passed):
        from . import acceptance
        calls = {'full': 0}

        def invoke(connection):
            return acceptance.generate_reply([{'role': 'user', 'content': 'synthetic'}],
                connection=connection, max_output_tokens=4096, on_text=lambda _part: None)

        def probe(connection, capability, prompts):
            from django.conf import settings
            self.assertTrue(settings.QWEN_STRUCTURED_HANZI_ENABLED)
            invoke(connection)
            return {'capability': capability, 'prompts': prompts, 'outputs': ['synthetic'],
                    'passed': preliminary_passed, 'checks': [], 'error': ''}

        def full(connection, stop_on_check_failure):
            from django.conf import settings
            self.assertTrue(settings.QWEN_STRUCTURED_HANZI_ENABLED)
            self.assertTrue(stop_on_check_failure)
            calls['full'] += 1
            for _ in range(5):
                invoke(connection)
            return self.root / 'synthetic-report.json', {'report_id': 'synthetic', 'passed': True}

        output = StringIO()
        args = ['qwen_structured_retest.py', '--execute', '--acknowledge-paid-calls']
        with self.settings(BASE_DIR=self.root), patch('sys.argv', args), patch('sys.stdout', output), \
                patch('diagnostics.qwen_structured_retest.current_server_version', return_value='0.14.1-internal'), \
                patch('delivery.connections.load_named_connection', return_value=self.connection), \
                patch('delivery.acceptance.generate_reply', self.generate), \
                patch('delivery.acceptance._probe', side_effect=probe), \
                patch('delivery.acceptance.run_acceptance', side_effect=full):
            status = main()
        result = json.loads(output.getvalue())
        self.assertFalse(result['enabled'])
        self.assertEqual(result['live_web_version'], '0.14.1-internal')
        self.assertTrue(Path(result['probe']).exists())
        return status, result, calls

    def test_failed_preliminary_probe_stops_before_full_acceptance(self):
        status, result, calls = self.run_fake_batch(False)
        self.assertEqual(status, 2)
        self.assertFalse(result['probe_passed'])
        self.assertIsNone(result['report'])
        self.assertEqual(result['generation_attempts'], 1)
        self.assertEqual(calls['full'], 0)
        self.assertEqual(self.generate.call_count, 1)

    def test_passed_preliminary_probe_allows_only_five_more_calls(self):
        status, result, calls = self.run_fake_batch(True)
        self.assertEqual(status, 0)
        self.assertTrue(result['passed'])
        self.assertEqual(result['generation_attempts'], 6)
        self.assertEqual(calls['full'], 1)
        self.assertEqual(self.generate.call_count, 6)
