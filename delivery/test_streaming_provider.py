"""Real SSE framing exercised against synthetic, in-memory provider responses."""
from io import BytesIO
import json
import urllib.error
from unittest.mock import patch

from django.test import SimpleTestCase

from .connections import ProviderConnection
from .provider import ProviderError, _prepare_messages, generate_reply


def event(value):
    return ('data: ' + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)) + '\n\n').encode('utf-8')


class StreamingProviderTests(SimpleTestCase):
    def setUp(self):
        self.connection = ProviderConnection('deepseek', 'deepseek-flash', 'synthetic-secret', 'cn')
        self.messages = [{'role': 'user', 'content': '虚构测试'}]

    def run_stream(self, data, callback, connection=None):
        with patch('delivery.provider.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = BytesIO(data)
            reply = generate_reply(self.messages, connection=connection or self.connection,
                                   max_output_tokens=128, on_text=callback)
            request = opener.return_value.open.call_args.args[0]
            self.assertTrue(json.loads(request.data)['stream'])
            self.assertEqual(opener.return_value.open.call_count, 1)
            return reply

    def test_content_arrives_incrementally_without_private_reasoning(self):
        data = (b': ping\n\n' + event({'choices': [{'index': 0, 'delta': {'role': 'assistant',
                'reasoning_content': 'private-thought'}, 'finish_reason': None}]}) +
                event({'choices': [{'index': 0, 'delta': {'content': '第一'}, 'finish_reason': None}]}) +
                event({'choices': [{'index': 0, 'delta': {'content': '段'}, 'finish_reason': 'stop'}]}) +
                event({'choices': [], 'usage': {'completion_tokens': 2}}) + event('[DONE]'))
        seen = []
        reply = self.run_stream(data, seen.append)
        self.assertEqual(seen, ['第一', '段'])
        self.assertEqual(str(reply), '第一段')
        self.assertNotIn('private-thought', str(reply))

    def test_incomplete_or_truncated_stream_never_counts_as_complete(self):
        data = event({'choices': [{'delta': {'content': '未完成'}, 'finish_reason': None}]})
        seen = []
        with self.assertRaisesMessage(ProviderError, '没有自动重试'):
            self.run_stream(data, seen.append)
        self.assertEqual(seen, ['未完成'])
        data += event({'choices': [{'delta': {}, 'finish_reason': 'length'}]}) + event('[DONE]')
        with self.assertRaisesMessage(ProviderError, '输出上限'):
            self.run_stream(data, lambda _piece: None)

    def test_error_and_tool_call_events_are_not_exposed(self):
        for data in [event({'error': {'message': 'synthetic-secret'}}),
                     event({'choices': [{'delta': {'tool_calls': [{'id': 'x'}]}, 'finish_reason': None}]})]:
            with self.subTest(data=data), self.assertRaises(ProviderError) as caught:
                self.run_stream(data, lambda _piece: None)
            self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_http_error_records_only_the_status_category(self):
        error = urllib.error.HTTPError('https://fake.invalid', 400, 'private', {},
                                       BytesIO(b'synthetic-secret'))
        with patch('delivery.provider.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(ProviderError) as caught:
                generate_reply(self.messages, self.connection, max_output_tokens=128, on_text=lambda _part: None)
        self.assertEqual(caught.exception.diagnostic_code, 'http_400')
        self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_qwen_uses_bounded_non_thinking_stream(self):
        connection = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-secret', 'cn-beijing',
                                        service_platform='bailian')
        data = event({'choices': [{'delta': {'content': '好'}, 'finish_reason': 'stop'}]}) + event('[DONE]')
        seen = []
        self.assertEqual(self.run_stream(data, seen.append, connection), '好')
        self.assertEqual(seen, ['好'])

    def test_qwen_thinking_is_bounded_and_never_exposes_reasoning(self):
        connection = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-secret', 'cn-beijing',
                                        'low', 'bailian')
        data = (event({'choices': [{'delta': {'reasoning_content': 'private-thought'}, 'finish_reason': None}]}) +
                event({'choices': [{'delta': {'content': '公开答案'}, 'finish_reason': 'stop'}]}) + event('[DONE]'))
        seen = []
        with patch('delivery.provider.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = BytesIO(data)
            reply = generate_reply(self.messages, connection, max_output_tokens=4096, on_text=seen.append)
            payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertTrue(payload['enable_thinking'])
        self.assertEqual(payload['thinking_budget'], 2048)
        self.assertEqual(payload['max_completion_tokens'], 4086)
        self.assertFalse(payload['preserve_thinking'])
        self.assertEqual(seen, ['公开答案'])
        self.assertEqual(str(reply), '公开答案')
        self.assertTrue(reply.thinking_observed)
        self.assertEqual(reply.continuation, '')
        self.assertNotIn('private-thought', repr(reply.__dict__))

    def test_qwen_thinking_budget_shrinks_with_small_output_limit(self):
        connection = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-secret', 'cn-beijing',
                                        'low', 'bailian')
        data = event({'choices': [{'delta': {'content': '好'}, 'finish_reason': 'stop'}]}) + event('[DONE]')
        with patch('delivery.provider.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = BytesIO(data)
            reply = generate_reply(self.messages, connection, max_output_tokens=128, on_text=lambda _part: None)
            payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(payload['thinking_budget'], 54)
        self.assertFalse(reply.thinking_observed)
        with patch('delivery.provider.urllib.request.build_opener') as opener:
            with self.assertRaises(ProviderError):
                generate_reply(self.messages, connection, max_output_tokens=127, on_text=lambda _part: None)
        opener.assert_not_called()

    def test_kimi_reasoning_stays_private_but_continuation_is_preserved(self):
        connection = ProviderConnection('kimi', 'kimi-k3.1-pro', 'synthetic-secret', 'test-region')
        data = (event({'choices': [{'delta': {'reasoning_content': 'private'}, 'finish_reason': None}]}) +
                event({'choices': [{'delta': {'content': '可见回复'}, 'finish_reason': 'stop'}]}) + event('[DONE]'))
        seen = []
        reply = self.run_stream(data, seen.append, connection)
        self.assertEqual(seen, ['可见回复'])
        self.assertTrue(reply.continuation)
        restored = _prepare_messages(self.messages + [{'role': 'assistant', 'content': str(reply),
                                                       '_provider_context': reply.continuation}], connection)
        self.assertEqual(restored[-1]['reasoning_content'], 'private')

    def test_callback_interruption_stops_reading_without_retry(self):
        data = event({'choices': [{'delta': {'content': '一'}, 'finish_reason': None}]}) + event('[DONE]')
        def stop(_piece):
            raise RuntimeError('synthetic cancellation')
        with self.assertRaisesMessage(RuntimeError, 'synthetic cancellation'):
            self.run_stream(data, stop)
