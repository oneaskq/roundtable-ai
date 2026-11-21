from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from delivery.connections import ProviderConnection
from delivery.provider import ProviderError


class ConfigurationWizardTests(SimpleTestCase):
    def test_requires_local_interactive_input_for_credentials(self):
        with patch('delivery.management.commands.configure_model.sys.stdin.isatty', return_value=False):
            with self.assertRaises(CommandError):
                call_command('configure_model', stdout=StringIO())

    def test_saves_only_an_actual_official_catalog_selection(self):
        output = StringIO()
        prefix = 'delivery.management.commands.configure_model.'
        with patch(prefix + 'sys.stdin.isatty', return_value=True), \
             patch(prefix + 'getpass.getpass', return_value='synthetic-private-key'), \
             patch('builtins.input', side_effect=['1', 'test-region', '2']), \
             patch(prefix + 'list_models', return_value=['actual-first', 'actual-second']) as discover, \
             patch(prefix + 'save_named_connection') as named, \
             patch(prefix + 'save_connection') as save:
            call_command('configure_model', stdout=output)
        self.assertEqual(discover.call_args.args[0].model, '')
        self.assertEqual(save.call_args.args[0].model, 'actual-second')
        self.assertEqual(save.call_args.args[0].provider, 'deepseek')
        named.assert_called_once_with(save.call_args.args[0])
        self.assertNotIn('synthetic-private-key', output.getvalue())

    def test_discovery_failure_preserves_existing_connection_and_redacts_error(self):
        prefix = 'delivery.management.commands.configure_model.'
        with patch(prefix + 'sys.stdin.isatty', return_value=True), \
             patch(prefix + 'getpass.getpass', return_value='synthetic-private-key'), \
             patch('builtins.input', side_effect=['2', 'test-region']), \
             patch(prefix + 'list_models', side_effect=ProviderError('synthetic-private-key')), \
             patch(prefix + 'save_named_connection') as named, \
             patch(prefix + 'save_connection') as save:
            with self.assertRaises(CommandError) as error:
                call_command('configure_model', stdout=StringIO())
        save.assert_not_called()
        named.assert_not_called()
        self.assertNotIn('synthetic-private-key', str(error.exception))

    def test_status_contains_no_key_or_fingerprint(self):
        connection = ProviderConnection(provider='deepseek', model='test-model', api_key='synthetic-private-key', service_region='test')
        output = StringIO()
        with patch('delivery.management.commands.configure_model.get_connection', return_value=connection):
            call_command('configure_model', status=True, stdout=output)
        self.assertIn('test-model', output.getvalue())
        self.assertNotIn(connection.api_key, output.getvalue())
        self.assertNotIn(connection.fingerprint, output.getvalue())

    def test_qwen_setup_is_offline_named_only_and_has_no_default_model(self):
        prefix = 'delivery.management.commands.configure_model.'
        output = StringIO()
        with patch(prefix + 'sys.stdin.isatty', return_value=True), \
             patch(prefix + 'getpass.getpass', return_value='synthetic-qwen-key'), \
             patch('builtins.input', side_effect=['3', '2', 'cn-beijing', 'qwen3.7-plus']), \
             patch(prefix + 'list_models') as discover, \
             patch(prefix + 'save_named_connection') as named, \
             patch(prefix + 'save_connection') as active, \
             patch('delivery.provider.urllib.request.build_opener') as network:
            call_command('configure_model', stdout=output)
        connection = named.call_args.args[0]
        self.assertEqual((connection.provider, connection.model, connection.service_region),
                         ('qwen', 'qwen3.7-plus', 'cn-beijing'))
        discover.assert_not_called()
        network.assert_not_called()
        active.assert_not_called()
        self.assertIn('尚未验证密钥或模型可用性', output.getvalue())
        self.assertNotIn(connection.api_key, output.getvalue())

    def test_qwen_invalid_region_or_empty_model_preserves_saved_connections(self):
        prefix = 'delivery.management.commands.configure_model.'
        for answers in (['2', 'unknown-region'], ['2', 'cn-beijing', ''], ['2', 'cn-beijing', 'qwq-plus'], ['1', 'ap-southeast-1']):
            with self.subTest(answers=answers), \
                 patch(prefix + 'sys.stdin.isatty', return_value=True), \
                 patch(prefix + 'getpass.getpass', return_value='synthetic-qwen-key'), \
                 patch('builtins.input', side_effect=answers), \
                 patch(prefix + 'list_models') as discover, \
                 patch(prefix + 'save_named_connection') as named, \
                 patch(prefix + 'save_connection') as active:
                with self.assertRaises(CommandError):
                    call_command('configure_model', provider='qwen', stdout=StringIO())
                discover.assert_not_called()
                named.assert_not_called()
                active.assert_not_called()

    def test_qwen_named_status_does_not_read_active_or_contact_provider(self):
        prefix = 'delivery.management.commands.configure_model.'
        connection = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing')
        output = StringIO()
        with patch(prefix + 'get_connection') as active, \
             patch(prefix + 'load_named_connection', return_value=connection) as named, \
             patch(prefix + 'list_models') as discover:
            call_command('configure_model', status=True, provider='qwen', stdout=output)
        named.assert_called_once_with('qwen')
        active.assert_not_called()
        discover.assert_not_called()
        self.assertIn('cn-beijing', output.getvalue())
        self.assertNotIn(connection.api_key, output.getvalue())

    def test_bailian_setup_is_offline_and_uses_beijing_endpoint(self):
        prefix = 'delivery.management.commands.configure_model.'
        with patch(prefix + 'sys.stdin.isatty', return_value=True), \
             patch(prefix + 'getpass.getpass', return_value='synthetic-bailian-key'), \
             patch('builtins.input', side_effect=['1', 'cn-beijing', 'qwen3.7-plus']), \
             patch(prefix + 'save_named_connection') as named, \
             patch(prefix + 'save_connection') as active, \
             patch('delivery.provider.urllib.request.build_opener') as network:
            call_command('configure_model', provider='qwen', stdout=StringIO())
        connection = named.call_args.args[0]
        self.assertEqual(connection.service_platform, 'bailian')
        self.assertEqual(connection.base_url, 'https://dashscope.aliyuncs.com/compatible-mode/v1')
        active.assert_not_called()
        network.assert_not_called()
