"""The opt-in retest must not dispatch extra or unrecorded paid calls."""
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from operations.version import APP_VERSION

from .connections import ProviderConnection
from .provider import ProviderError
from diagnostics.qwen_budget_retest import BoundedGeneration, Journal, main


class QwenRetestGuardTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'ledger.jsonl'
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.close)
        self.connection = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-secret',
                                             'cn-beijing', service_platform='bailian')
        self.generate = Mock(return_value='synthetic reply')
        self.current = Mock(return_value=self.connection)
        self.guard = BoundedGeneration(self.generate, self.connection, self.current, self.journal)
        self.kwargs = {'connection': self.connection, 'max_output_tokens': 4096,
                       'on_text': lambda _fragment: None}

    def events(self):
        return [json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines()]

    def test_preview_does_not_load_connections_or_dispatch(self):
        output = StringIO()
        with patch('sys.argv', ['qwen_budget_retest.py']), patch('sys.stdout', output), \
                patch('delivery.connections.load_named_connection') as load:
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue())['generation_calls_max'], 5)
        load.assert_not_called()
        self.generate.assert_not_called()

    def test_execution_without_paid_call_acknowledgement_never_loads_connection(self):
        with patch('sys.argv', ['qwen_budget_retest.py', '--execute']), \
                patch('sys.stderr', StringIO()), patch('delivery.connections.load_named_connection') as load:
            with self.assertRaises(SystemExit) as stopped:
                main()
        self.assertEqual(stopped.exception.code, 2)
        load.assert_not_called()
        self.generate.assert_not_called()

    def test_staged_probe_keeps_old_live_service_and_never_enables_capabilities(self):
        from . import acceptance

        def simulate_batch(connection, stop_on_check_failure):
            self.assertTrue(stop_on_check_failure)
            acceptance.generate_reply([{'role': 'user', 'content': 'synthetic'}],
                                      connection=connection, max_output_tokens=4096,
                                      on_text=lambda _fragment: None)
            return Path(self.directory.name) / 'report.json', {'report_id': 'synthetic', 'passed': False}

        output = StringIO()
        arguments = ['qwen_budget_retest.py', '--execute', '--acknowledge-paid-calls']
        with self.settings(BASE_DIR=Path(self.directory.name)), patch('sys.argv', arguments), \
                patch('sys.stdout', output), \
                patch('diagnostics.qwen_budget_retest.current_server_version', return_value='0.14.1-internal'), \
                patch('delivery.connections.load_named_connection', return_value=self.connection), \
                patch('delivery.acceptance.generate_reply', self.generate), \
                patch('delivery.acceptance.run_acceptance', side_effect=simulate_batch):
            self.assertEqual(main(), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result['live_web_version'], '0.14.1-internal')
        self.assertEqual(result['source_version'], APP_VERSION)
        self.assertFalse(result['enabled'])
        self.assertEqual(self.generate.call_count, 1)
        events = [json.loads(line) for line in Path(result['journal']).read_text(encoding='utf-8').splitlines()]
        self.assertEqual([event['event'] for event in events],
                         ['batch_started', 'request_started', 'response_received', 'batch_finished'])

    def test_experimental_format_is_scoped_to_the_retest_process(self):
        from . import acceptance
        from django.conf import settings

        def simulate_batch(connection, stop_on_check_failure):
            self.assertTrue(stop_on_check_failure)
            self.assertTrue(settings.QWEN_STRUCTURED_HANZI_ENABLED)
            acceptance.generate_reply([{'role': 'user', 'content': 'synthetic'}],
                                      connection=connection, max_output_tokens=4096,
                                      on_text=lambda _fragment: None)
            return Path(self.directory.name) / 'report.json', {'report_id': 'synthetic', 'passed': False}

        output = StringIO()
        arguments = ['qwen_budget_retest.py', '--execute', '--acknowledge-paid-calls',
                     '--experimental-structured']
        with self.settings(BASE_DIR=Path(self.directory.name), QWEN_STRUCTURED_HANZI_ENABLED=False), \
                patch('sys.argv', arguments), patch('sys.stdout', output), \
                patch('diagnostics.qwen_budget_retest.current_server_version', return_value='0.14.1-internal'), \
                patch('delivery.connections.load_named_connection', return_value=self.connection), \
                patch('delivery.acceptance.generate_reply', self.generate), \
                patch('delivery.acceptance.run_acceptance', side_effect=simulate_batch):
            self.assertEqual(main(), 2)
            self.assertFalse(settings.QWEN_STRUCTURED_HANZI_ENABLED)
        result = json.loads(output.getvalue())
        self.assertTrue(result['experimental_structured'])
        self.assertFalse(result['enabled'])
        self.assertEqual(self.generate.call_count, 1)

    def test_each_request_is_journaled_before_dispatch_and_stops_at_five(self):
        def response(*_args, **_kwargs):
            self.assertEqual(self.events()[-1]['event'], 'request_started')
            return 'reply'

        self.generate.side_effect = response
        for _ in range(5):
            self.assertEqual(self.guard([{'role': 'user', 'content': 'synthetic'}], **self.kwargs), 'reply')
        with self.assertRaises(ProviderError):
            self.guard([], **self.kwargs)
        self.assertEqual(self.generate.call_count, 5)
        events = self.events()
        self.assertEqual([event['event'] for event in events],
                         ['request_started', 'response_received'] * 5)
        self.assertNotIn('synthetic-secret', self.path.read_text(encoding='utf-8'))

    def test_provider_error_stops_and_is_not_retried_or_logged_with_secrets(self):
        self.generate.side_effect = ProviderError('synthetic-secret')
        with self.assertRaises(ProviderError):
            self.guard([], **self.kwargs)
        with self.assertRaises(ProviderError):
            self.guard([], **self.kwargs)
        self.assertEqual(self.generate.call_count, 1)
        self.assertEqual([event['event'] for event in self.events()], ['request_started', 'request_failed'])
        self.assertNotIn('synthetic-secret', self.path.read_text(encoding='utf-8'))

    def test_rejected_provider_response_is_distinguished_from_transport_failure(self):
        self.generate.side_effect = ProviderError('synthetic', diagnostic_code='structured_validation_failed')
        with self.assertRaises(ProviderError):
            self.guard([], **self.kwargs)
        events = self.events()
        self.assertEqual([event['event'] for event in events], ['request_started', 'response_rejected'])
        self.assertEqual(events[-1]['diagnostic_code'], 'structured_validation_failed')
        self.assertEqual(self.generate.call_count, 1)

    def test_journal_failure_prevents_the_request(self):
        with patch.object(self.journal, 'write', side_effect=OSError('no write')):
            with self.assertRaises(OSError):
                self.guard([], **self.kwargs)
        with self.assertRaises(ProviderError):
            self.guard([], **self.kwargs)
        self.generate.assert_not_called()

    def test_changed_connection_or_output_limit_never_dispatches(self):
        self.current.return_value = ProviderConnection('qwen', 'qwen3.7-plus', 'changed-secret',
                                                       'cn-beijing', service_platform='bailian')
        with self.assertRaises(ProviderError):
            self.guard([], **self.kwargs)
        self.generate.assert_not_called()
        self.assertEqual(self.events(), [])

        guard = BoundedGeneration(self.generate, self.connection, lambda: self.connection, self.journal)
        with self.assertRaises(ProviderError):
            guard([], **{**self.kwargs, 'max_output_tokens': 4097})
        self.generate.assert_not_called()
        self.assertEqual(self.events(), [])

    def test_existing_ledger_is_never_overwritten(self):
        with self.assertRaises(FileExistsError):
            Journal(self.path)
