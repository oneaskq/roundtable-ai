"""Synthetic structured-output tests; no provider requests are sent."""
import json
from io import BytesIO
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .connections import ProviderConnection
from .provider import ProviderError, STRUCTURED_HANZI_INSTRUCTION, generate_reply


PROMPT = ('为第一次去自习室的学生写三条中文建议，主题分别是计划、安静、休息。'
          '只输出三行，分别以1.、2.、3.开头，总计120至180个汉字，不加标题。')
ANSWER = '\n'.join(f'{index}. ' + '字' * 40 + '。' for index in range(1, 4))


@override_settings(QWEN_STRUCTURED_HANZI_ENABLED=True)
class QwenStructuredLengthTests(SimpleTestCase):
    def setUp(self):
        self.qwen = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-secret',
                                       'cn-beijing', service_platform='bailian')
        self.messages = [{'role': 'user', 'content': PROMPT}]

    def test_stream_buffers_json_and_exposes_only_a_validated_answer(self):
        visible = []
        raw = json.dumps({'answer': ANSWER}, ensure_ascii=False)

        def stream(_connection, payload, callback, **_kwargs):
            schema = payload['response_format']['json_schema']['schema']
            self.assertTrue(payload['response_format']['json_schema']['strict'])
            self.assertEqual(schema['required'], ['answer'])
            self.assertNotIn('minLength', schema['properties']['answer'])
            self.assertEqual(schema['properties']['answer']['maxLength'], 4096)
            callback(raw[:15])
            callback(raw[15:])
            self.assertEqual(visible, [])
            return {'role': 'assistant', 'content': raw}

        with patch('delivery.provider._stream_reply', side_effect=stream) as request:
            result = generate_reply(self.messages, self.qwen, max_output_tokens=4096, on_text=visible.append)
        self.assertEqual(str(result), ANSWER)
        self.assertEqual(visible, [ANSWER])
        self.assertEqual(result.raw_content, raw)
        self.assertEqual(result.response_format['type'], 'json_schema')
        self.assertIn(STRUCTURED_HANZI_INSTRUCTION, result.effective_system_instruction)
        self.assertEqual(request.call_count, 1)

    def test_malformed_extra_duplicate_or_out_of_bounds_response_is_not_published(self):
        values = ('not json', json.dumps({'answer': ANSWER, 'extra': 'x'}),
                  '{"answer":"x","answer":"y"}',
                  json.dumps({'answer': ANSWER.replace('字', '', 1)}, ensure_ascii=False),
                  json.dumps({'answer': ANSWER + '字' * 61}, ensure_ascii=False),
                  json.dumps({'answer': ANSWER + '！' * 4096}, ensure_ascii=False),
                  json.dumps({'answer': ANSWER + '\U00020000'}, ensure_ascii=False),
                  '{"answer":"\\ud800"}')
        for raw in values:
            with self.subTest(raw=raw[:30]):
                visible = []
                with patch('delivery.provider._stream_reply', return_value={'content': raw}) as request:
                    with self.assertRaises(ProviderError) as caught:
                        generate_reply(self.messages, self.qwen, max_output_tokens=4096, on_text=visible.append)
                self.assertEqual(visible, [])
                self.assertEqual(request.call_count, 1)
                self.assertEqual(caught.exception.raw_content, raw)
                self.assertEqual(caught.exception.response_format['type'], 'json_schema')
                self.assertEqual(caught.exception.diagnostic_code, 'structured_validation_failed')

    def test_nonstreaming_uses_the_same_contract_and_returns_original_answer(self):
        raw = json.dumps({'answer': ANSWER}, ensure_ascii=False)
        result = {'choices': [{'message': {'content': raw}, 'finish_reason': 'stop'}]}
        with patch('delivery.provider._request', return_value=result) as request:
            reply = generate_reply(self.messages, self.qwen, max_output_tokens=4096)
        self.assertEqual(reply, ANSWER)
        self.assertEqual(reply.raw_content, raw)
        payload = request.call_args.args[2]
        self.assertEqual(payload['messages'][-1]['content'], PROMPT)
        self.assertEqual(payload['response_format']['type'], 'json_schema')

    def test_hanzi_limit_does_not_count_punctuation_or_numbering(self):
        answer = '\n'.join(f'{index}. ' + '字' * 55 + '，内容。' for index in range(1, 4))
        self.assertGreater(len(answer), 180)
        raw = json.dumps({'answer': answer}, ensure_ascii=False)
        result = {'choices': [{'message': {'content': raw}, 'finish_reason': 'stop'}]}
        with patch('delivery.provider._request', return_value=result):
            self.assertEqual(generate_reply(self.messages, self.qwen, max_output_tokens=4096), answer)

    def test_real_stream_framing_does_not_expose_the_json_wrapper(self):
        raw = json.dumps({'answer': ANSWER}, ensure_ascii=False)
        def frame(value):
            return ('data: ' + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
                    + '\n\n').encode('utf-8')
        data = (frame({'choices': [{'delta': {'content': raw[:30]}, 'finish_reason': None}]}) +
                frame({'choices': [{'delta': {'content': raw[30:]}, 'finish_reason': 'stop'}]}) +
                frame('[DONE]'))
        visible = []
        with patch('delivery.provider.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = BytesIO(data)
            reply = generate_reply(self.messages, self.qwen, max_output_tokens=4096, on_text=visible.append)
            request = opener.return_value.open.call_args.args[0]
            payload = json.loads(request.data)
        self.assertEqual(reply, ANSWER)
        self.assertEqual(visible, [ANSWER])
        self.assertTrue(payload['stream'])
        self.assertEqual(payload['response_format']['type'], 'json_schema')

    def test_ambiguous_requests_and_other_providers_keep_the_regular_stream(self):
        deepseek = ProviderConnection('deepseek', 'deepseek-flash', 'synthetic-secret', 'test')
        cases = ((self.qwen, '写三条，每条50字，总计120至180个汉字。'),
                 (self.qwen, '写三条，总计120至900个汉字。'), (deepseek, PROMPT))
        for connection, prompt in cases:
            with self.subTest(provider=connection.provider, prompt=prompt):
                seen = []
                def stream(_connection, payload, callback, **_kwargs):
                    self.assertNotIn('response_format', payload)
                    callback('普通')
                    return {'content': '普通'}
                with patch('delivery.provider._stream_reply', side_effect=stream):
                    reply = generate_reply([{'role': 'user', 'content': prompt}], connection,
                                           max_output_tokens=4096, on_text=seen.append)
                self.assertEqual(reply, '普通')
                self.assertEqual(seen, ['普通'])
                self.assertIsNone(reply.raw_content)

    @override_settings(QWEN_STRUCTURED_HANZI_ENABLED=False)
    def test_experimental_transport_is_disabled_by_default(self):
        def stream(_connection, payload, callback, **_kwargs):
            self.assertNotIn('response_format', payload)
            callback('普通')
            return {'content': '普通'}
        visible = []
        with patch('delivery.provider._stream_reply', side_effect=stream):
            reply = generate_reply(self.messages, self.qwen, max_output_tokens=4096, on_text=visible.append)
        self.assertEqual(reply, '普通')
        self.assertEqual(visible, ['普通'])
