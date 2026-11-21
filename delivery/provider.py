"""Bounded server-only calls; no default model or automatic live probes.

Official protocols checked on 2026-10-26:
https://api-docs.deepseek.com/api/create-chat-completion/
https://api-docs.deepseek.com/api/list-models/
https://platform.kimi.com/docs/api/list-models
https://platform.kimi.com/docs/api/models-overview
https://platform.kimi.com/docs/guide/use-thinking-models
https://docs.qwencloud.com/api-reference/chat/openai-chat
https://help.aliyun.com/zh/model-studio/beijing-access-information
https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions
https://docs.qwencloud.com/developer-guides/text-generation/thinking
https://docs.qwencloud.com/developer-guides/clients-and-developer-tools/deepseek-harness
"""
import base64
import hashlib
import http.client
import json
import re
import time
import urllib.error
import urllib.request
import unicodedata

from cryptography.fernet import InvalidToken
from django.conf import settings

from .connections import ConnectionError, MODEL_ID, ProviderConnection, _cipher, get_connection
from .text_guidance import explicit_hanzi_list_bounds

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_HYDRATED_BYTES = 4 * 1024 * 1024
MAX_CONTINUATION_CHARS = 3 * 1024 * 1024
# This is protocol support, not a live availability catalog. Only documented
# hybrid-thinking models with max_completion_tokens are admitted. Thinking-only
# snapshots (including qwen3.7-max-2026-05-17) are not part of this adapter.
# New models need review; users select exact IDs from their region's console.
QWEN_TEXT_MODELS = frozenset({
    'qwen3.8-max', 'qwen3.8-max-0902', 'qwen3.8-flash',
    'qwen3.7-max', 'qwen3.7-max-2026-05-20', 'qwen3.7-max-2026-06-08',
    'qwen3.7-plus', 'qwen3.7-plus-2026-05-26',
    'qwen3.7-flash', 'qwen3.7-flash-2026-07-15',
    'qwen3.6-plus', 'qwen3.6-plus-2026-04-02',
    'qwen3.6-flash', 'qwen3.6-flash-2026-04-16',
    'qwen3.5-plus', 'qwen3.5-plus-2026-02-15',
    'qwen3.5-flash', 'qwen3.5-flash-2026-02-23',
})
SYSTEM_MESSAGE = {
    'role': 'system',
    'content': '你是圆桌的主助手。准确回答用户问题。当前没有执行工具，不得声称已搜索、运行程序或生成文件；明确未核实的信息。',
}
QWEN_STRUCTURED_HANZI_MODELS = frozenset({'qwen3.7-plus', 'qwen3.7-plus-2026-05-26'})
QWEN_THINKING_MODELS = QWEN_STRUCTURED_HANZI_MODELS
QWEN_THINKING_BUDGET = 2048
STRUCTURED_HANZI_INSTRUCTION = (
    '本次使用 JSON 结构化传输。只返回含 answer 字段的对象；answer 必须是严格按照用户要求'
    '完成的最终文本原文，不添加代码围栏、额外标题或解释。'
)
_HANZI = re.compile(r'[\u4e00-\u9fff]')


class ProviderError(Exception):
    def __init__(self, message, *, diagnostic_code=''):
        super().__init__(message)
        self.diagnostic_code = diagnostic_code


def safe_provider_diagnostic(error):
    """Return only an allowlisted error category, never an upstream response body."""
    code = getattr(error, 'diagnostic_code', '')
    return code if isinstance(code, str) and (re.fullmatch(r'http_[1-5]\d\d', code) or
        code in {'transport_error', 'invalid_stream', 'structured_validation_failed',
                 'output_limit', 'deadline'}) else ''


class ProviderReply(str):
    """Public answer with an encrypted server-only continuation token."""
    def __new__(cls, content, continuation='', *, raw_content=None,
                effective_system_instruction=None, response_format=None, thinking_observed=None):
        result = super().__new__(cls, content)
        result.continuation = continuation
        result.raw_content = raw_content
        result.effective_system_instruction = effective_system_instruction
        result.response_format = response_format
        result.thinking_observed = thinking_observed
        return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_error(error):
    messages = {400: '模型服务未接受请求，请检查模型名称与参数。',
                401: '模型服务认证失败，请检查本地密钥。',
                402: '模型服务余额或预算不足。',
                403: '模型服务尚未完成账号验证或访问授权。',
                404: '模型或接口不可用，请重新读取模型列表。',
                413: '本次上下文超过模型服务允许的大小。',
                422: '模型服务未接受请求，请检查模型名称与参数。',
                429: '模型服务请求过于频繁，请稍后再试。'}
    message = ('模型服务返回了重定向，已停止发送以保护密钥。' if 300 <= error.code < 400
               else messages.get(error.code, '模型服务暂时不可用，请稍后再试。'))
    error.close()  # Error bodies may echo private text or credentials; never read them.
    return ProviderError(message, diagnostic_code=f'http_{error.code}' if type(error.code) is int else '')


def _connection(connection, *, require_model=True):
    try:
        connection = get_connection() if connection is None else connection
        if (not isinstance(connection, ProviderConnection) or not connection.provider
                or not connection.api_key or (require_model and not connection.model)):
            raise ProviderError('模型服务尚未配置。')
        return connection
    except ConnectionError as error:
        raise ProviderError(str(error)) from None


def _request(connection, suffix, payload=None, *, timeout=50):
    try:
        request = urllib.request.Request(
            connection.base_url + suffix,
            data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Authorization': f'Bearer {connection.api_key}', 'Content-Type': 'application/json'},
            method='GET' if payload is None else 'POST',
        )
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError('模型响应超过本次允许大小。')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except urllib.error.HTTPError as error:
        raise _http_error(error) from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise ProviderError('模型服务连接失败或超时；本次没有自动重试。') from None
    except ConnectionError as error:
        raise ProviderError(str(error)) from None
    except (ValueError, KeyError, IndexError, TypeError, RecursionError):
        raise ProviderError('模型响应格式无效；本次未标记完成。') from None


def list_models(connection=None):
    """Explicit read-only discovery. The caller chooses a returned model ID."""
    connection = _connection(connection, require_model=False)
    if connection.provider == 'qwen':
        # Official docs explicitly say this endpoint has no GET /models API.
        raise ProviderError('千问 QwenCloud 不提供模型名单接口；请在所选区域的官方模型目录确认名称。')
    result = _request(connection, '/models', timeout=20)
    try:
        data = result['data']
        if not isinstance(data, list) or len(data) > 10000:
            raise ValueError
        ids = []
        for model in data:
            identifier = model['id']
            if not isinstance(identifier, str) or not MODEL_ID.fullmatch(identifier):
                raise ValueError
            if identifier not in ids:
                ids.append(identifier)
        return ids
    except (ValueError, TypeError, KeyError):
        raise ProviderError('模型列表格式无效，请稍后重新读取。') from None


def _preserves_thinking(connection):
    return connection.provider == 'kimi' and (
        connection.model.startswith('kimi-k3') or connection.model.startswith('kimi-k2.7-code'))


def qwen_thinking_enabled(connection):
    """Only the specifically supported Beijing Bailian connection may opt in."""
    return (connection.provider == 'qwen' and connection.service_platform == 'bailian' and
            connection.service_region == 'cn-beijing' and connection.model in QWEN_THINKING_MODELS and
            connection.reasoning_effort == 'low')


def _lineage(messages):
    visible = [SYSTEM_MESSAGE] + [{'role': item['role'], 'content': item['content']} for item in messages]
    return hashlib.sha256(json.dumps(visible, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _restore_message(token, visible, prefix, connection):
    if not isinstance(token, str) or not token or len(token) > MAX_CONTINUATION_CHARS:
        raise ProviderError('该会话的模型上下文不完整，请新建会话后继续。')
    try:
        encoded = token.encode('ascii')
        decoded = base64.b64decode(encoded, altchars=b'-_', validate=True)
        if base64.urlsafe_b64encode(decoded) != encoded:
            raise ValueError
        record = json.loads(_cipher().decrypt(encoded))
        raw = record['message']
        if (record['connection'] != connection.fingerprint or record['lineage'] != _lineage(prefix)
                or not isinstance(raw, dict) or raw.get('role') != 'assistant'
                or raw.get('content') != visible['content']):
            raise ValueError
        return raw
    except (ConnectionError, InvalidToken, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        raise ProviderError('该会话的模型上下文已失效，请新建会话后继续。') from None


def _prepare_messages(messages, connection):
    if not isinstance(messages, list) or not messages:
        raise ProviderError('请提供有效的对话内容。')
    if len(messages) > 100:
        raise ProviderError('当前会话超过内测上下文上限，请导出后新建会话。')
    if any(not isinstance(item, dict) or item.get('role') not in ('user', 'assistant')
           or not isinstance(item.get('content'), str)
           or set(item) - {'role', 'content', '_provider_context'} for item in messages):
        raise ProviderError('对话消息格式无效。')
    if sum(len(item['content']) for item in messages) > 60000:
        raise ProviderError('当前会话超过内测上下文上限，请导出后新建会话。')
    prepared = [SYSTEM_MESSAGE.copy()]
    for index, item in enumerate(messages):
        token = item.get('_provider_context', '')
        if item['role'] != 'assistant' and token:
            raise ProviderError('对话消息格式无效。')
        if item['role'] == 'assistant' and _preserves_thinking(connection):
            prepared.append(_restore_message(token, item, messages[:index], connection))
        else:
            prepared.append({'role': item['role'], 'content': item['content']})
    try:
        hydrated_size = len(json.dumps(prepared, ensure_ascii=False).encode('utf-8'))
    except UnicodeError:
        raise ProviderError('对话内容包含无效字符，请检查后重试。') from None
    if hydrated_size > MAX_HYDRATED_BYTES:
        raise ProviderError('当前会话的完整上下文超过内测上限，请新建会话后继续。')
    return prepared


def _payload(connection, messages, max_output_tokens):
    if max_output_tokens is None:
        max_output_tokens = 4096
    if type(max_output_tokens) is not int or not 64 <= max_output_tokens <= 32768:
        raise ProviderError('本次输出长度设置无效。')
    body = {'model': connection.model, 'messages': messages, 'stream': False}
    effort = connection.reasoning_effort
    if connection.provider == 'qwen':
        if connection.model not in QWEN_TEXT_MODELS:
            raise ProviderError('该千问模型尚未适配有界文字对话；请在官方目录选择受支持的 Qwen Max、Plus 或 Flash 模型。')
        if max_output_tokens > 4096:
            raise ProviderError('千问连接每次输出上限为4096 tokens。')
        # max_completion_tokens covers thinking + answer. The official API
        # documents up to 10 tokens of variance, so reserve that margin.
        body.update(max_completion_tokens=max_output_tokens - 10, preserve_thinking=False)
        if effort in ('', 'none'):
            body['enable_thinking'] = False
        elif qwen_thinking_enabled(connection):
            if max_output_tokens < 128:
                raise ProviderError('千问思考模式的输出上限至少为128 tokens。')
            body.update(enable_thinking=True,
                        thinking_budget=min(QWEN_THINKING_BUDGET, max_output_tokens - 74))
        else:
            raise ProviderError('当前千问连接不支持所选思考模式。')
    elif connection.provider == 'kimi' and connection.model.startswith('kimi-k3'):
        if effort not in ('', 'low', 'high', 'max'):
            raise ProviderError('该 Kimi 模型需要 low、high 或 max 推理强度。')
        body.update(reasoning_effort=effort or 'high', max_completion_tokens=max_output_tokens)
    else:
        body['max_tokens'] = max_output_tokens
        if connection.provider == 'deepseek':
            if effort not in ('', 'none', 'low', 'high', 'max'):
                raise ProviderError('该 DeepSeek 连接的推理强度不受支持。')
            if effort in ('', 'none'):
                body['thinking'] = {'type': 'disabled'}
            else:
                body.update(thinking={'type': 'enabled'}, reasoning_effort=effort)
        elif connection.provider == 'kimi':
            if connection.model == 'kimi-k2.6':
                if effort not in ('', 'none'):
                    raise ProviderError('该 Kimi 模型不支持所选推理强度。')
                body['thinking'] = {'type': 'disabled'}
            elif effort:
                raise ProviderError('该 Kimi 模型不支持所选推理强度。')
        elif effort:
            body['reasoning_effort'] = effort
    return body


def _structured_hanzi_format(connection, messages):
    """Use an explicit provider contract only for a narrow, unambiguous request."""
    if (getattr(settings, 'QWEN_STRUCTURED_HANZI_ENABLED', False) is not True or
            connection.provider != 'qwen' or connection.service_platform != 'bailian' or
            connection.model not in QWEN_STRUCTURED_HANZI_MODELS or
            not messages or messages[-1].get('role') != 'user'):
        return None
    bounds = explicit_hanzi_list_bounds(messages[-1].get('content'))
    if bounds is None:
        return None
    _count, _low, high = bounds
    # Avoid large/complex provider grammars. Other requests keep ordinary text.
    # JSON Schema length counts punctuation and digits too, so never impose the
    # user's Hanzi ceiling on all characters. Enforce the original Hanzi bounds
    # on the decoded answer locally instead.
    if high > 600:
        return None
    return {'type': 'json_schema', 'json_schema': {'name': 'bounded_hanzi_answer', 'strict': True,
            'schema': {'type': 'object', 'properties': {'answer': {'type': 'string',
                        'maxLength': 4096}}, 'required': ['answer'],
                       'additionalProperties': False}}}


def _unpack_structured_hanzi(raw, low, high):
    """Check the returned wrapper and actual Hanzi before exposing any text."""
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate field')
            result[key] = value
        return result

    try:
        if not isinstance(raw, str) or len(raw) > 32000:
            raise ValueError('invalid response size')
        document = json.loads(raw, object_pairs_hook=unique_fields)
        if not isinstance(document, dict) or set(document) != {'answer'}:
            raise ValueError('invalid fields')
        answer = document['answer']
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 4096:
            raise ValueError('invalid answer')
        answer.encode('utf-8')
        # The existing acceptance count is U+4E00–U+9FFF. Fail closed on other
        # Han ideographs instead of silently undercounting rare characters.
        if any(not ('\u4e00' <= char <= '\u9fff') and
               unicodedata.name(char, '').startswith(('CJK UNIFIED IDEOGRAPH-', 'CJK COMPATIBILITY IDEOGRAPH-'))
               for char in answer):
            raise ValueError('unsupported Hanzi counting range')
        if not low <= len(_HANZI.findall(answer)) <= high:
            raise ValueError('length outside user bounds')
        return answer
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ProviderError('模型回复未满足指定格式或字数，本次未标记完成；没有自动重试。',
                            diagnostic_code='structured_validation_failed') from None


def _stream_reply(connection, payload, on_text, *, preserve_reasoning=False):
    """Consume provider SSE once; publish only content deltas, never reasoning."""
    payload = {**payload, 'stream': True}
    timeout = 120 if _preserves_thinking(connection) or qwen_thinking_enabled(connection) else 50
    deadline = time.monotonic() + 180
    parts, reasoning, event_lines = [], [], []
    finish = None
    thinking_seen = False
    done = False
    received = 0

    def consume_event(data):
        nonlocal finish, done, thinking_seen
        if data == '[DONE]':
            done = True
            return
        if done:
            raise ValueError('event after done')
        document = json.loads(data)
        if not isinstance(document, dict) or document.get('error'):
            raise ValueError('invalid stream event')
        choices = document.get('choices')
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError('invalid stream choices')
        if not choices:  # Optional usage-only event.
            return
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get('index', 0) != 0:
            raise ValueError('invalid stream choice')
        delta = choice.get('delta') or {}
        if not isinstance(delta, dict) or delta.get('tool_calls') or delta.get('function_call'):
            raise ValueError('unexpected tool call')
        if delta.get('role') not in (None, 'assistant'):
            raise ValueError('invalid role')
        thought, text = delta.get('reasoning_content'), delta.get('content')
        if thought is not None and not isinstance(thought, str):
            raise ValueError('invalid reasoning fragment')
        if thought:
            thinking_seen = True
        if text is not None and not isinstance(text, str):
            raise ValueError('invalid text fragment')
        if finish is not None and (text or thought or choice.get('finish_reason')):
            raise ValueError('content after finish')
        if preserve_reasoning and thought:
            reasoning.append(thought)
        if text:
            text.encode('utf-8')
            parts.append(text)
            on_text(text)
        reason = choice.get('finish_reason')
        if reason is not None:
            if reason == 'length':
                raise ProviderError('模型达到本次输出上限，草稿未完成；本次没有自动重试。',
                                    diagnostic_code='output_limit')
            if reason != 'stop':
                raise ProviderError('模型没有返回完整文本；本次未标记完成。')
            finish = reason

    try:
        request = urllib.request.Request(
            connection.base_url + '/chat/completions',
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Authorization': f'Bearer {connection.api_key}', 'Content-Type': 'application/json',
                     'Accept': 'text/event-stream'}, method='POST')
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            while not done:
                if time.monotonic() > deadline:
                    raise ProviderError('模型回复时间过长，草稿未完成；本次没有自动重试。',
                                        diagnostic_code='deadline')
                line = response.readline(MAX_RESPONSE_BYTES + 1)
                if not line:
                    break
                received += len(line)
                if received > MAX_RESPONSE_BYTES:
                    raise ProviderError('模型响应超过本次允许大小。')
                line = line.decode('utf-8').rstrip('\r\n')
                if not line:
                    if event_lines:
                        consume_event('\n'.join(event_lines))
                        event_lines.clear()
                elif line.startswith('data:'):
                    value = line[5:]
                    event_lines.append(value[1:] if value.startswith(' ') else value)
                elif line.startswith(':') or line.startswith(('event:', 'id:', 'retry:')):
                    continue
                else:
                    raise ValueError('invalid stream line')
        if not done or finish != 'stop' or not ''.join(parts).strip():
            raise ProviderError('模型回复中断，草稿未完成；本次没有自动重试。',
                                diagnostic_code='invalid_stream')
        message = {'role': 'assistant', 'content': ''.join(parts)}
        if qwen_thinking_enabled(connection):
            message['_thinking_observed'] = thinking_seen
        if preserve_reasoning and reasoning:
            message['reasoning_content'] = ''.join(reasoning)
        return message
    except urllib.error.HTTPError as error:
        raise _http_error(error) from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise ProviderError('模型服务连接失败或超时；本次没有自动重试。', diagnostic_code='transport_error') from None
    except (UnicodeError, ValueError, TypeError, KeyError, IndexError, RecursionError):
        raise ProviderError('模型流式响应格式无效；本次未标记完成。', diagnostic_code='invalid_stream') from None


def generate_reply(messages, connection=None, *, max_output_tokens=None, system_instruction=None, on_text=None):
    connection = _connection(connection)
    if system_instruction is not None:
        # Role invocations are fresh exchanges; never transplant private thinking
        # or allow a caller to change the system prompt of an existing lineage.
        if (not isinstance(system_instruction, str) or not system_instruction.strip()
                or len(system_instruction) > 12000 or not isinstance(messages, list)
                or len(messages) != 1 or not isinstance(messages[0], dict)
                or messages[0].get('role') != 'user' or '_provider_context' in messages[0]):
            raise ProviderError('团队角色请求格式无效。')
    prepared = _prepare_messages(messages, connection)
    if system_instruction is not None:
        prepared[0] = {'role': 'system', 'content': system_instruction}
    response_format = _structured_hanzi_format(connection, messages)
    if response_format:
        prepared[0]['content'] += '\n' + STRUCTURED_HANZI_INSTRUCTION
    payload = _payload(connection, prepared, max_output_tokens)
    if response_format:
        payload['response_format'] = response_format
    try:
        if on_text is not None:
            if not callable(on_text):
                raise ProviderError('流式回复处理方式无效。')
            # Never expose raw JSON fragments as a draft. Publish the complete,
            # locally validated answer once instead.
            stream_callback = (lambda _fragment: None) if response_format else on_text
            message = _stream_reply(connection, payload, stream_callback,
                                    preserve_reasoning=_preserves_thinking(connection) and system_instruction is None)
            result = {'choices': [{'message': message, 'finish_reason': 'stop'}]}
        else:
            result = _request(connection, '/chat/completions', payload,
                              timeout=120 if _preserves_thinking(connection) or
                              qwen_thinking_enabled(connection) else 50)
    except ProviderError as error:
        if response_format:
            error.response_format = response_format
            error.effective_system_instruction = prepared[0]['content']
        raise
    try:
        choice = result['choices'][0]
        message = choice['message']
        content = message['content']
        if choice.get('finish_reason') == 'length':
            raise ProviderError('模型达到本次输出上限，未返回完整文本；本次未标记完成。')
        if (choice.get('finish_reason') != 'stop' or not isinstance(content, str) or not content.strip()
                or message.get('tool_calls') or message.get('function_call')):
            raise ProviderError('模型没有返回完整文本；本次未标记完成。')
        content.encode('utf-8')
        raw_content = content if response_format else None
        if response_format:
            _count, low, high = explicit_hanzi_list_bounds(messages[-1]['content'])
            try:
                content = _unpack_structured_hanzi(content, low, high)
            except ProviderError as error:
                error.response_format = response_format
                error.effective_system_instruction = prepared[0]['content']
                error.raw_content = raw_content
                raise
            if on_text is not None:
                on_text(content)
        continuation = ''
        if _preserves_thinking(connection) and system_instruction is None:
            if (message.get('role') != 'assistant'
                    or (message.get('reasoning_content') is not None
                        and not isinstance(message['reasoning_content'], str))):
                raise ValueError
            record = {'connection': connection.fingerprint, 'lineage': _lineage(messages), 'message': message}
            continuation = _cipher().encrypt(json.dumps(record, ensure_ascii=False).encode('utf-8')).decode('ascii')
        thinking_observed = None
        if qwen_thinking_enabled(connection):
            thinking_observed = (message.get('_thinking_observed', False) if on_text is not None else
                                 bool(message.get('reasoning_content')))
        return ProviderReply(content, continuation, raw_content=raw_content,
                             effective_system_instruction=prepared[0]['content'] if response_format else None,
                             response_format=response_format, thinking_observed=thinking_observed)
    except ConnectionError as error:
        raise ProviderError(str(error)) from None
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError):
        raise ProviderError('模型响应格式无效；本次未标记完成。') from None

