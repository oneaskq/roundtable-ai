"""Isolated provider tests: synthetic credentials and mocked HTTP only."""
from dataclasses import replace
from io import BytesIO
import http.client
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.request

from cryptography.fernet import Fernet
from django.test import SimpleTestCase, override_settings

from .connections import (ConnectionError, ProviderConnection, QWEN_REGIONS, get_connection, load_connection,
                          load_named_connection, save_connection, save_named_connection)
from .provider import MAX_RESPONSE_BYTES, NoRedirect, ProviderError, generate_reply, list_models


class IsolatedConnectionTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'provider-connection.json'
        self.named_directory = Path(self.directory.name) / 'named-connections'
        self.key = Fernet.generate_key().decode()
        isolated = override_settings(DATA_DIR=Path(self.directory.name), PROVIDER_CONNECTION_PATH=self.path,
                                     NAMED_CONNECTIONS_DIR=self.named_directory,
                                     MFA_ENCRYPTION_KEY=self.key, AI_PROVIDER='', AI_MODEL='',
                                     AI_GATEWAY_API_KEY='', SERVICE_REGION='test-region', AI_REASONING_EFFORT='')
        isolated.enable()
        self.addCleanup(isolated.disable)
        self.connection = ProviderConnection('deepseek', 'example-model', 'synthetic-secret', 'test-region')

    def test_empty_config_does_not_choose_a_model_or_provider(self):
        self.assertIsNone(load_connection())
        self.assertFalse(get_connection().configured)
        self.assertEqual(get_connection().model, '')

    def test_encrypted_roundtrip_and_precedence(self):
        save_connection(self.connection)
        raw = self.path.read_bytes()
        self.assertNotIn(b'synthetic-secret', raw)
        self.assertNotIn(b'example-model', raw)
        self.assertNotIn('synthetic-secret', repr(self.connection))
        self.assertEqual(load_connection(), self.connection)
        with override_settings(AI_MODEL='legacy/model', AI_GATEWAY_API_KEY='legacy-secret'):
            self.assertEqual(get_connection(), self.connection)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_atomic_failure_preserves_previous_configuration(self):
        save_connection(self.connection)
        with patch('delivery.connections.os.replace', side_effect=OSError('synthetic-secret')):
            with self.assertRaises(ConnectionError) as caught:
                save_connection(replace(self.connection, api_key='new-secret'))
        self.assertNotIn('synthetic-secret', str(caught.exception))
        self.assertEqual(load_connection(), self.connection)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_corrupt_and_wrong_key_fail_closed_without_environment_fallback(self):
        save_connection(self.connection)
        with override_settings(MFA_ENCRYPTION_KEY=Fernet.generate_key().decode(),
                               AI_MODEL='legacy/model', AI_GATEWAY_API_KEY='legacy-secret'):
            with self.assertRaises(ConnectionError):
                get_connection()
        self.path.write_bytes(b'{"api_key":"synthetic-secret"}')
        with self.assertRaises(ConnectionError) as caught:
            load_connection()
        self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_unknown_version_and_oversized_file_are_rejected(self):
        self.path.write_bytes(Fernet(self.key).encrypt(b'{"version":999}'))
        with self.assertRaises(ConnectionError):
            load_connection()
        self.path.write_bytes(b'x' * 32769)
        with self.assertRaises(ConnectionError):
            load_connection()

    def test_fingerprint_changes_on_every_review_relevant_field(self):
        for field, value in [('provider', 'kimi'), ('model', 'other-model'),
                             ('api_key', 'other-secret'), ('service_region', 'other-region'),
                             ('reasoning_effort', 'high')]:
            with self.subTest(field=field):
                self.assertNotEqual(self.connection.fingerprint, replace(self.connection, **{field: value}).fingerprint)
        with patch('delivery.connections.CONFIG_VERSION', 2):
            changed = self.connection.fingerprint
        self.assertNotEqual(self.connection.fingerprint, changed)
        self.assertEqual(len(self.connection.fingerprint), 64)

    def test_explicit_provider_environment_and_legacy_gateway(self):
        with override_settings(AI_PROVIDER='deepseek', AI_MODEL='chosen-model'), \
                patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'direct-secret', 'KIMI_API_KEY': 'other-secret'}):
            connection = get_connection()
            self.assertEqual(connection.provider, 'deepseek')
            self.assertEqual(connection.api_key, 'direct-secret')
        with override_settings(AI_MODEL='legacy/model', AI_GATEWAY_API_KEY='legacy-secret'):
            self.assertEqual(get_connection().provider, 'gateway')
            self.assertTrue(get_connection().configured)

    def test_connection_cannot_choose_an_arbitrary_endpoint_or_inject_headers(self):
        for replacement in ({'provider': 'https://attacker.invalid'}, {'api_key': 'key\r\nHost: attacker'},
                            {'model': 'bad model'}, {'service_region': ''}):
            with self.subTest(replacement=replacement), self.assertRaises(ConnectionError):
                replace(self.connection, **replacement)
        self.assertEqual(replace(self.connection, service_region='https://attacker.invalid').base_url,
                         'https://api.deepseek.com/v1')
        with self.assertRaises(ConnectionError):
            save_connection(replace(self.connection, model=''))

    def test_named_connections_encrypted_roundtrip_keeps_active_and_other_providers(self):
        kimi = replace(self.connection, provider='kimi', model='kimi-test', api_key='synthetic-kimi-secret')
        save_connection(kimi)
        active_before = self.path.read_bytes()
        self.assertIsNone(load_named_connection('deepseek'))
        self.assertIsNone(load_named_connection('kimi'))
        save_named_connection(self.connection)
        save_named_connection(kimi)
        self.assertEqual(load_named_connection('deepseek'), self.connection)
        self.assertEqual(load_named_connection('kimi'), kimi)
        self.assertEqual(get_connection(), kimi)
        self.assertEqual(self.path.read_bytes(), active_before)
        for provider, secret in [('deepseek', b'synthetic-secret'), ('kimi', b'synthetic-kimi-secret')]:
            encrypted = (self.named_directory / (provider + '.json')).read_bytes()
            self.assertNotIn(secret, encrypted)
            self.assertNotIn(provider.encode(), encrypted)
            record = json.loads(Fernet(self.key).decrypt(encrypted))
            self.assertEqual(record['provider'], provider)
        save_named_connection(replace(self.connection, api_key='new-synthetic-secret'))
        self.assertEqual(load_named_connection('kimi'), kimi)
        self.assertEqual(get_connection(), kimi)

    def test_named_connections_reject_paths_and_do_not_activate_automatically(self):
        for provider in ('', '../deepseek', 'deepseek/other', 'deepseek\\other', 'C:\\secret', 'DEEPSEEK',
                         'https://attacker.invalid', None, ['deepseek']):
            with self.subTest(provider=provider), self.assertRaises(ConnectionError):
                load_named_connection(provider)
        self.assertFalse(self.named_directory.exists())
        save_named_connection(self.connection)
        self.assertFalse(get_connection().configured)
        self.assertIsNone(load_connection())
        with self.assertRaises(ConnectionError):
            save_named_connection(replace(self.connection, model=''))
        with self.assertRaises(ConnectionError):
            save_named_connection({'provider': '../deepseek'})

    def test_named_record_cannot_be_swapped_between_providers(self):
        save_named_connection(self.connection)
        (self.named_directory / 'kimi.json').write_bytes((self.named_directory / 'deepseek.json').read_bytes())
        with self.assertRaises(ConnectionError):
            load_named_connection('kimi')

    def test_named_atomic_failure_preserves_saved_record(self):
        save_named_connection(self.connection)
        with patch('delivery.connections.os.replace', side_effect=OSError('synthetic-secret')):
            with self.assertRaises(ConnectionError) as caught:
                save_named_connection(replace(self.connection, api_key='new-secret'))
        self.assertNotIn('synthetic-secret', str(caught.exception))
        self.assertEqual(load_named_connection('deepseek'), self.connection)
        self.assertEqual(list(self.named_directory.iterdir()), [self.named_directory / 'deepseek.json'])

    def test_qwen_requires_explicit_allowlisted_creation_region(self):
        for region in QWEN_REGIONS:
            connection = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', region)
            self.assertEqual(connection.base_url,
                             f'https://{region}.maas.qwencloudapi.com/compatible-mode/v1')
        for region in ('local-development', '', 'beijing', 'https://attacker.invalid',
                       'cn-beijing.attacker.invalid', 'cn-beijing/../../', 'CN-BEIJING'):
            with self.subTest(region=region), self.assertRaises(ConnectionError):
                ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', region)
        with self.assertRaises(ConnectionError):
            ProviderConnection('qwen', 'qwen3.7-plus', 'sk-sp-' + 'test-fixture', 'cn-beijing')

    def test_bailian_platform_is_explicit_bound_to_beijing_and_changes_fingerprint(self):
        qwencloud = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-key', 'cn-beijing')
        bailian = replace(qwencloud, service_platform='bailian')
        self.assertEqual(bailian.base_url, 'https://dashscope.aliyuncs.com/compatible-mode/v1')
        self.assertNotEqual(qwencloud.fingerprint, bailian.fingerprint)
        self.assertIn('百炼', bailian.provider_label)
        with self.assertRaises(ConnectionError):
            replace(bailian, service_region='ap-southeast-1')
        with self.assertRaises(ConnectionError):
            replace(qwencloud, service_platform='unknown')
        with self.assertRaises(ConnectionError):
            replace(self.connection, service_platform='bailian')
        save_named_connection(bailian)
        self.assertEqual(load_named_connection('qwen'), bailian)
        self.assertNotIn(bailian.api_key.encode(), (self.named_directory / 'qwen.json').read_bytes())

    def test_legacy_qwen_connection_without_platform_remains_qwencloud(self):
        self.named_directory.mkdir(parents=True)
        old = {'version': 1, 'provider': 'qwen', 'model': 'qwen3.7-plus', 'api_key': 'synthetic-old-key',
               'service_region': 'cn-beijing', 'reasoning_effort': ''}
        (self.named_directory / 'qwen.json').write_bytes(Fernet(self.key).encrypt(json.dumps(old).encode()))
        loaded = load_named_connection('qwen')
        self.assertEqual(loaded.service_platform, '')
        self.assertEqual(loaded.base_url, 'https://cn-beijing.maas.qwencloudapi.com/compatible-mode/v1')
        self.assertEqual(loaded.fingerprint, replace(loaded, service_platform='qwencloud').fingerprint)

    def test_qwen_named_connection_does_not_replace_current_and_region_binds_fingerprint(self):
        save_connection(self.connection)
        original = self.path.read_bytes()
        qwen = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing')
        save_named_connection(qwen)
        self.assertEqual(load_named_connection('qwen'), qwen)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertNotIn(qwen.api_key.encode(), (self.named_directory / 'qwen.json').read_bytes())
        self.assertNotEqual(qwen.fingerprint, replace(qwen, service_region='ap-southeast-1').fingerprint)

    def test_qwen_environment_requires_explicit_region_and_own_key(self):
        with override_settings(AI_PROVIDER='qwen', AI_MODEL='qwen3.7-plus', SERVICE_REGION='cn-beijing'), \
                patch.dict(os.environ, {'QWEN_API_KEY': 'synthetic-qwen-key'}):
            self.assertEqual(get_connection().api_key, 'synthetic-qwen-key')
            self.assertEqual(get_connection().base_url,
                             'https://cn-beijing.maas.qwencloudapi.com/compatible-mode/v1')
        with override_settings(AI_PROVIDER='qwen', AI_MODEL='qwen3.7-plus', SERVICE_REGION='test-region'):
            with self.assertRaises(ConnectionError):
                get_connection()


class ProviderTransportTests(SimpleTestCase):
    def setUp(self):
        self.connection = ProviderConnection('deepseek', 'test-model', 'synthetic-secret', 'test-region')
        self.messages = [{'role': 'user', 'content': 'hello'}]
        self.opener_patch = patch('delivery.provider.urllib.request.build_opener')
        self.opener = self.opener_patch.start()
        self.addCleanup(self.opener_patch.stop)
        self.response = MagicMock()
        self.opener.return_value.open.return_value = self.response
        self.set_response({'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'complete'}}]})
        encryption = override_settings(MFA_ENCRYPTION_KEY=Fernet.generate_key().decode())
        encryption.enable()
        self.addCleanup(encryption.disable)

    def set_response(self, body):
        self.response.__enter__.return_value.read.return_value = json.dumps(body).encode()

    def payload(self):
        return json.loads(self.opener.return_value.open.call_args.args[0].data)

    def test_deepseek_contract_and_explicit_reasoning(self):
        reply = generate_reply(self.messages, self.connection)
        self.assertEqual(reply, 'complete')
        self.assertEqual(reply.continuation, '')
        request = self.opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.deepseek.com/v1/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-secret')
        self.assertEqual(self.payload()['thinking'], {'type': 'disabled'})
        self.assertEqual(self.payload()['max_tokens'], 4096)
        self.assertEqual(self.payload()['messages'][-1], self.messages[0])
        self.assertFalse(self.payload()['stream'])
        generate_reply(self.messages, replace(self.connection, reasoning_effort='high'), max_output_tokens=512)
        self.assertEqual(self.payload()['reasoning_effort'], 'high')
        self.assertEqual(self.payload()['max_tokens'], 512)

    def test_qwen_chat_uses_bounded_complete_output_and_disables_thinking(self):
        connection = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing')
        reply = generate_reply(self.messages, connection)
        request = self.opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url,
                         'https://cn-beijing.maas.qwencloudapi.com/compatible-mode/v1/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-qwen-key')
        self.assertEqual(self.payload()['max_completion_tokens'], 4086)
        self.assertFalse(self.payload()['enable_thinking'])
        self.assertFalse(self.payload()['preserve_thinking'])
        self.assertFalse(self.payload()['stream'])
        self.assertNotIn('max_tokens', self.payload())
        self.assertNotIn('reasoning_effort', self.payload())
        self.assertNotIn('extra_body', self.payload())
        self.assertEqual(reply.continuation, '')
        generate_reply(self.messages, connection, max_output_tokens=512)
        self.assertEqual(self.payload()['max_completion_tokens'], 502)

    def test_bailian_chat_targets_only_the_confirmed_official_endpoint(self):
        connection = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-bailian-key', 'cn-beijing', '', 'bailian')
        self.assertEqual(generate_reply(self.messages, connection), 'complete')
        request = self.opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions')
        self.assertEqual(self.payload()['max_completion_tokens'], 4086)
        self.assertFalse(self.payload()['enable_thinking'])

    def test_qwen_unsupported_catalog_or_model_cannot_trigger_network(self):
        connection = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing')
        with self.assertRaisesRegex(ProviderError, '不提供模型名单接口'):
            list_models(connection)
        for selected in (replace(connection, model='qwq-plus'),
                         replace(connection, model='qwen3.7-plus-injected'),
                         replace(connection, model='qwen3.7-max-2026-05-17'),
                         replace(connection, model='qwen3.7-max-preview'),
                         replace(connection, model='kimi-k3'),
                         replace(connection, reasoning_effort='high')):
            with self.assertRaises(ProviderError):
                generate_reply(self.messages, selected)
        with self.assertRaises(ProviderError):
            generate_reply(self.messages, connection, max_output_tokens=4097)
        self.opener.assert_not_called()

    def test_thinking_is_only_available_for_the_confirmed_bailian_model(self):
        connection = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key',
                                        'cn-beijing', 'low', 'bailian')
        self.assertEqual(generate_reply(self.messages, connection), 'complete')
        self.assertTrue(self.payload()['enable_thinking'])
        self.assertEqual(self.payload()['thinking_budget'], 2048)
        self.assertEqual(self.payload()['max_completion_tokens'], 4086)
        self.opener.reset_mock()
        for selected in (replace(connection, model='qwen3.7-max'),
                         replace(connection, service_platform='qwencloud'),
                         replace(connection, reasoning_effort='high')):
            with self.subTest(selected=selected), self.assertRaises(ProviderError):
                generate_reply(self.messages, selected)
        self.opener.assert_not_called()

    def test_discussion_defaults_respect_explicit_effort_and_other_model_modes(self):
        kimi = replace(self.connection, provider='kimi', model='kimi-k3.1-pro')
        for effort, fast, expected in [('', False, 'high'), ('', True, 'low'), ('high', True, 'high'), ('max', True, 'max')]:
            with self.subTest(effort=effort, fast=fast):
                generate_reply(self.messages, replace(kimi, reasoning_effort=effort), fast_reasoning=fast)
                self.assertEqual(self.payload()['reasoning_effort'], expected)

        qwen = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing', '', 'bailian')
        for connection in [qwen, replace(kimi, model='kimi-k2.6'), replace(self.connection, reasoning_effort='high')]:
            with self.subTest(model=connection.model):
                generate_reply(self.messages, connection)
                usual = self.payload()
                generate_reply(self.messages, connection, fast_reasoning=True)
                self.assertEqual(self.payload(), usual)

        generate_reply(self.messages, replace(qwen, reasoning_effort='low'), max_output_tokens=256, fast_reasoning=True)
        self.assertEqual(self.payload()['thinking_budget'], 182)

    def test_models_discovery_is_get_without_generation_or_model_selection(self):
        self.set_response({'data': [{'id': 'available-a'}, {'id': 'available-b'}, {'id': 'available-a'}]})
        for provider, endpoint in [('deepseek', 'https://api.deepseek.com/v1/models'),
                                   ('kimi', 'https://api.moonshot.cn/v1/models'),
                                   ('gateway', 'https://ai-gateway.vercel.sh/v1/models')]:
            with self.subTest(provider=provider):
                connection = replace(self.connection, provider=provider, model='')
                self.assertEqual(list_models(connection), ['available-a', 'available-b'])
                request = self.opener.return_value.open.call_args.args[0]
                self.assertEqual(request.full_url, endpoint)
                self.assertEqual(request.get_method(), 'GET')
                self.assertIsNone(request.data)
                self.assertEqual(connection.model, '')

    def test_invalid_models_response_is_not_echoed(self):
        for body in ({'data': None}, {'data': [{'id': 'synthetic-secret\ninvalid'}]}, {'data': [None]}, {}):
            self.set_response(body)
            with self.subTest(body=body), self.assertRaises(ProviderError) as caught:
                list_models(self.connection)
            self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_no_redirect_handler_blocks_cross_host_redirects(self):
        request = urllib.request.Request('https://api.deepseek.com/v1/models',
                                         headers={'Authorization': 'Bearer synthetic-secret'})
        self.assertIsNone(NoRedirect().redirect_request(request, None, 302, 'Moved', {}, 'https://attacker.invalid'))
        generate_reply(self.messages, self.connection)
        self.opener.assert_called_with(NoRedirect)

    def test_http_and_network_errors_are_redacted_and_never_retried(self):
        for error in [urllib.error.HTTPError('https://fake.invalid', status, 'synthetic-secret', {},
                                            BytesIO(b'synthetic-secret response'))
                      for status in (302, 400, 401, 402, 403, 404, 413, 422, 429, 500)] + [
                          urllib.error.URLError('synthetic-secret'), TimeoutError('synthetic-secret')]:
            self.opener.return_value.open.reset_mock()
            self.opener.return_value.open.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(ProviderError) as caught:
                generate_reply(self.messages, self.connection)
            self.assertNotIn('synthetic-secret', str(caught.exception))
            self.assertEqual(self.opener.return_value.open.call_count, 1)

    def test_response_limit_and_invalid_json(self):
        for raw in (b'x' * (MAX_RESPONSE_BYTES + 1), b'invalid synthetic-secret', b'[]'):
            self.response.__enter__.return_value.read.return_value = raw
            with self.assertRaises(ProviderError):
                generate_reply(self.messages, self.connection)
        self.response.__enter__.return_value.read.assert_called_with(MAX_RESPONSE_BYTES + 1)

    def test_http_protocol_failure_is_redacted_without_retry_and_next_call_recovers(self):
        for error in (http.client.IncompleteRead(b'synthetic-secret partial response', 100),
                      http.client.BadStatusLine('synthetic-secret invalid status'),
                      http.client.RemoteDisconnected('synthetic-secret disconnected')):
            self.opener.return_value.open.reset_mock()
            self.response.__enter__.return_value.read.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(ProviderError) as caught:
                generate_reply(self.messages, self.connection)
            self.assertNotIn('synthetic-secret', str(caught.exception))
            self.assertIn('没有自动重试', str(caught.exception))
            self.assertEqual(self.opener.return_value.open.call_count, 1)
            self.response.__enter__.return_value.read.side_effect = None
            self.assertEqual(generate_reply(self.messages, self.connection), 'complete')
            self.assertEqual(self.opener.return_value.open.call_count, 2)

    def test_incomplete_or_tool_responses_never_complete(self):
        for message, reason in [({'content': 'partial'}, 'length'), ({'content': ''}, 'stop'),
                                ({'content': 'answer', 'tool_calls': [{'id': 'call'}]}, 'stop'),
                                ({'content': None}, 'stop')]:
            self.set_response({'choices': [{'finish_reason': reason, 'message': message}]})
            with self.assertRaises(ProviderError):
                generate_reply(self.messages, self.connection)

    def test_invalid_context_and_output_limit_fail_before_network(self):
        for messages in ([], [{'role': 'user', 'content': 'x' * 60001}], self.messages * 101,
                         [{'role': 'user', 'content': '\ud800'}],
                         [{'role': 'tool', 'content': 'x'}],
                         [{'role': 'assistant', 'content': 'x', 'reasoning_content': 'injected'}]):
            with self.subTest(messages_count=len(messages)), self.assertRaises(ProviderError):
                generate_reply(messages, self.connection)
        for limit in (True, 1, 32769):
            with self.assertRaises(ProviderError):
                generate_reply(self.messages, self.connection, max_output_tokens=limit)
        with self.assertRaises(ProviderError):
            generate_reply(self.messages, replace(self.connection, model=''))
        self.opener.assert_not_called()

    def test_kimi_k3_preserves_private_fields_encrypted_and_in_original_position(self):
        connection = replace(self.connection, provider='kimi', model='kimi-k3.1-pro')
        raw_message = {'role': 'assistant', 'content': 'public answer', 'reasoning_content': 'private reasoning',
                       'tool_calls': None, 'provider_extra': {'opaque': 'keep exactly'}}
        self.set_response({'choices': [{'finish_reason': 'stop', 'message': raw_message}]})
        reply = generate_reply(self.messages, connection)
        self.assertEqual(str(reply), 'public answer')
        self.assertNotIn('private reasoning', repr(reply))
        self.assertNotIn('private reasoning', reply.continuation)
        self.assertEqual(self.payload()['reasoning_effort'], 'high')
        self.assertEqual(self.payload()['max_completion_tokens'], 4096)
        self.assertNotIn('thinking', self.payload())
        self.assertNotIn('max_tokens', self.payload())
        second = self.messages + [{'role': 'assistant', 'content': str(reply), '_provider_context': reply.continuation},
                                  {'role': 'user', 'content': 'follow up'}]
        generate_reply(second, connection)
        self.assertEqual(self.payload()['messages'][2], raw_message)
        self.assertNotIn('_provider_context', json.dumps(self.payload()))
        self.assertNotIn(reply.continuation, json.dumps(self.payload()))
        self.assertEqual(self.payload()['messages'][-1]['content'], 'follow up')

    def test_kimi_continuation_cannot_be_transplanted_or_mutated(self):
        connection = replace(self.connection, provider='kimi', model='kimi-k3.1-pro')
        reply = generate_reply(self.messages, connection)
        assistant = {'role': 'assistant', 'content': str(reply), '_provider_context': reply.continuation}
        cases = [([{'role': 'user', 'content': 'changed'}] + [assistant], connection),
                 (self.messages + [{**assistant, 'content': 'changed'}], connection),
                 (self.messages + [{**assistant, '_provider_context': reply.continuation + '!'}], connection),
                 (self.messages + [{'role': 'assistant', 'content': str(reply)}], connection),
                 (self.messages + [assistant], replace(connection, api_key='changed-secret'))]
        self.opener.reset_mock()
        for messages, selected in cases:
            with self.subTest(model=selected.model), self.assertRaises(ProviderError):
                generate_reply(messages, selected)
        self.opener.assert_not_called()

    def test_kimi_k27_does_not_disable_thinking_and_k26_does(self):
        for model in ('kimi-k2.7-code', 'kimi-k2.7-code-highspeed'):
            reply = generate_reply(self.messages, replace(self.connection, provider='kimi', model=model))
            self.assertTrue(reply.continuation)
            self.assertNotIn('thinking', self.payload())
            self.assertNotIn('reasoning_effort', self.payload())
        generate_reply(self.messages, replace(self.connection, provider='kimi', model='kimi-k2.6'))
        self.assertEqual(self.payload()['thinking'], {'type': 'disabled'})

    def test_other_providers_never_receive_kimi_continuation(self):
        generate_reply(self.messages + [{'role': 'assistant', 'content': 'public',
                                        '_provider_context': 'opaque-private-token'}], self.connection)
        self.assertNotIn('opaque-private-token', json.dumps(self.payload()))
