"""Preview or run one authorized Qwen thinking-mode writing diagnostic.

This synthetic diagnostic does not modify saved connections or enable a capability.
It records the final answer and only a boolean indicating whether thinking was
observed; private reasoning text is never saved or shown.
"""
import argparse
import json
import os
import sys
from uuid import uuid4


from .qwen_budget_retest import BoundedGeneration, Journal, MAX_OUTPUT_TOKENS, MODEL


THINKING_BUDGET = 2048
PLAN = {'service': '阿里云百炼北京', 'model': MODEL, 'generation_calls_max': 1,
        'output_tokens_max_per_call': MAX_OUTPUT_TOKENS, 'thinking_budget_tokens': THINKING_BUDGET,
        'inputs': '仅原验收虚构写作题', 'automatic_retry': False,
        'save_connection': False, 'approve_or_enable': False}


def thinking_generation(messages, *, connection, max_output_tokens, system_instruction, on_text):
    """Use the normal bounded transport with thinking enabled only for this probe."""
    from delivery.acceptance import WRITING_PROMPT, _probe_instruction
    from delivery.provider import ProviderError, _payload, _prepare_messages, _stream_reply

    if (connection.provider != 'qwen' or connection.model != MODEL or
            connection.service_platform != 'bailian' or connection.service_region != 'cn-beijing' or
            connection.reasoning_effort not in ('', 'none') or
            messages != [{'role': 'user', 'content': WRITING_PROMPT}] or
            max_output_tokens != MAX_OUTPUT_TOKENS or not callable(on_text) or
            system_instruction != _probe_instruction('chat.writing', [WRITING_PROMPT])):
        raise ProviderError('本次诊断与既定的虚构测试计划不一致。')
    prepared = _prepare_messages(messages, connection)
    prepared[0] = {'role': 'system', 'content': system_instruction}
    payload = _payload(connection, prepared, max_output_tokens)
    payload.update(enable_thinking=True, thinking_budget=THINKING_BUDGET, preserve_thinking=False)
    message = _stream_reply(connection, payload, on_text, preserve_reasoning=True)
    thinking_observed = bool(message.pop('reasoning_content', ''))
    content = message.get('content')
    if not isinstance(content, str) or not content.strip() or len(content) > 16000:
        raise ProviderError('思考模式未返回完整的有效文字。')
    try:
        content.encode('utf-8')
    except UnicodeError:
        raise ProviderError('模型响应文字格式无效。') from None
    return {'output': content, 'thinking_observed': thinking_observed}


def save_result(directory, connection, system_instruction, output, thinking_observed, checks):
    from django.core import signing
    from django.utils import timezone
    from delivery.acceptance import WRITING_PROMPT, _canonical

    passed = thinking_observed and bool(checks) and all(check['passed'] for check in checks)
    payload = {'schema_version': 1, 'kind': 'preliminary_qwen_thinking_writing', 'approvable': False,
               'provider': connection.provider, 'model': connection.model,
               'service_platform': connection.service_platform, 'service_region': connection.service_region,
               'saved_connection_fingerprint': connection.fingerprint,
               'mode': 'isolated_thinking_diagnostic', 'completed_at': timezone.now().isoformat(),
               'prompt': WRITING_PROMPT, 'system_instruction': system_instruction,
               'parameters': {'enable_thinking': True, 'thinking_budget': THINKING_BUDGET,
                              'preserve_thinking': False, 'max_output_tokens': MAX_OUTPUT_TOKENS},
               'output': output, 'thinking_observed': thinking_observed, 'checks': checks,
               'passed': passed, 'manual_review_required': True}
    document = {'probe': payload,
                'signature': signing.Signer(salt='roundtable.qwen-thinking-probe.v1').signature(_canonical(payload))}
    path = directory / f'qwen-thinking-probe-{uuid4().hex}.json'
    with path.open('x', encoding='utf-8') as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    return path, payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--acknowledge-paid-call', action='store_true')
    options = parser.parse_args()
    if not options.execute:
        print(json.dumps({'mode': 'preview', **PLAN}, ensure_ascii=False))
        return 0
    if not options.acknowledge_paid_call:
        parser.error('真实请求可能计费；须先获得明确授权并传入 --acknowledge-paid-call。')

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from delivery import acceptance
    from delivery.connections import load_named_connection
    from delivery.provider import ProviderError, safe_provider_diagnostic
    from operations.local_lock import LocalProcessLock
    from operations.version import APP_VERSION

    directory = acceptance.report_directory()
    directory.mkdir(parents=True, exist_ok=True)
    lock = LocalProcessLock(directory / 'qwen-budget-retest.lock')
    if not lock.acquire():
        raise RuntimeError('已有千问复测在运行；未发送请求。')
    journal = None
    try:
        connection = load_named_connection('qwen')
        if (not connection or not connection.configured or connection.model != MODEL or
                connection.service_platform != 'bailian' or connection.service_region != 'cn-beijing' or
                connection.reasoning_effort not in ('', 'none')):
            raise ValueError('connection does not match the plan')
        instruction = acceptance._probe_instruction('chat.writing', [acceptance.WRITING_PROMPT])
        journal = Journal(directory / f'qwen-thinking-probe-{uuid4().hex}.jsonl')
        journal.write('batch_started', source_version=APP_VERSION, **PLAN)
        guarded = BoundedGeneration(thinking_generation, connection,
                                    lambda: load_named_connection('qwen'), journal, max_calls=1)
        try:
            response = guarded([{'role': 'user', 'content': acceptance.WRITING_PROMPT}],
                               connection=connection, max_output_tokens=MAX_OUTPUT_TOKENS,
                               system_instruction=instruction, on_text=lambda _fragment: None)
        except ProviderError as error:
            journal.write('probe_stopped', diagnostic_code=safe_provider_diagnostic(error))
            print(json.dumps({'generation_attempts': guarded.attempts, 'passed': False, 'probe': None,
                              'diagnostic_code': safe_provider_diagnostic(error),
                              'journal': str(journal.path), 'enabled': False}, ensure_ascii=False))
            return 2
        output = acceptance._redact(response['output'], connection)
        checks = acceptance._checks('chat.writing', [output])
        path, result = save_result(directory, connection, instruction, output,
                                   response['thinking_observed'], checks)
        journal.write('probe_finished', passed=result['passed'], probe_file=path.name)
        print(json.dumps({'generation_attempts': guarded.attempts, 'passed': result['passed'],
                          'thinking_observed': result['thinking_observed'], 'probe': str(path),
                          'journal': str(journal.path), 'manual_review_required': True,
                          'enabled': False}, ensure_ascii=False))
        return 0 if result['passed'] else 2
    finally:
        if journal is not None:
            journal.close()
        lock.release()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('思考模式诊断未完成；请检查本机调用记录。没有自动重试。', file=sys.stderr)
        raise SystemExit(1) from None
