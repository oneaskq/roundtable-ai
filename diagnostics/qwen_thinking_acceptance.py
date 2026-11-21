"""Preview or perform one separately authorized, bounded thinking-mode acceptance.

Only synthetic examples are sent. The saved connection is not changed, and a
passing report still requires human review and a matching live configuration.
"""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from uuid import uuid4


from .qwen_budget_retest import (BoundedGeneration, Journal, MAX_OUTPUT_TOKENS,
                                    MODEL, current_server_version)


PLAN = {'service': '阿里云百炼北京', 'model': MODEL, 'generation_calls_max': 5,
        'output_tokens_max_per_call': MAX_OUTPUT_TOKENS, 'thinking_budget_tokens': 2048,
        'inputs': '仅四项虚构验收样例', 'stop_on_check_failure': True,
        'automatic_retry': False, 'save_connection': False, 'approve_or_enable': False}


def verify_source(connection, path):
    """A signed, passing writing diagnostic must match this exact saved connection."""
    from django.core import signing
    from delivery import acceptance

    path = Path(path)
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('oversized source')
        document = json.loads(path.read_text(encoding='utf-8'))
        payload = document['probe']
        signing.Signer(salt='roundtable.qwen-thinking-probe.v1').unsign(
            acceptance._canonical(payload) + ':' + document['signature'])
        if (payload.get('schema_version') != 1 or payload.get('kind') != 'preliminary_qwen_thinking_writing' or
                payload.get('approvable') is not False or payload.get('passed') is not True or
                payload.get('thinking_observed') is not True or payload.get('mode') != 'isolated_thinking_diagnostic' or
                payload.get('provider') != connection.provider or payload.get('model') != connection.model or
                payload.get('service_platform') != connection.service_platform or
                payload.get('service_region') != connection.service_region or
                payload.get('saved_connection_fingerprint') != connection.fingerprint or
                payload.get('prompt') != acceptance.WRITING_PROMPT or
                payload.get('system_instruction') != acceptance._probe_instruction(
                    'chat.writing', [acceptance.WRITING_PROMPT]) or
                payload.get('parameters') != {'enable_thinking': True, 'thinking_budget': 2048,
                                               'preserve_thinking': False,
                                               'max_output_tokens': MAX_OUTPUT_TOKENS}):
            raise ValueError('source mismatch')
        output = payload.get('output')
        if not isinstance(output, str) or not output.strip() or len(output) > 16000:
            raise ValueError('invalid source output')
        checks = acceptance._checks('chat.writing', [output])
        if not checks or not all(check['passed'] for check in checks):
            raise ValueError('source check failed')
    except (OSError, ValueError, TypeError, KeyError, signing.BadSignature):
        raise ValueError('既有诊断未通过、已变化或与当前连接不符；未发送请求。') from None
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--acknowledge-paid-calls', action='store_true')
    parser.add_argument('--source', type=Path, help='Signed local report from the preceding synthetic diagnostic')
    options = parser.parse_args()
    if not options.execute:
        print(json.dumps({'mode': 'preview', **PLAN}, ensure_ascii=False))
        return 0
    if not options.acknowledge_paid_calls:
        parser.error('真实请求可能计费；须先获得明确授权并传入 --acknowledge-paid-calls。')

    if options.source is None:
        parser.error('--source is required for execution.')

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from django.test import override_settings
    from delivery import acceptance
    from delivery.connections import load_named_connection
    from operations.local_lock import LocalProcessLock
    from operations.version import APP_VERSION

    try:
        live_version = current_server_version()
    except (OSError, ValueError, TypeError, AttributeError):
        live_version = None
    directory = acceptance.report_directory()
    directory.mkdir(parents=True, exist_ok=True)
    lock = LocalProcessLock(directory / 'qwen-budget-retest.lock')
    if not lock.acquire():
        raise RuntimeError('已有千问复测在运行；未发送请求。')
    journal = None
    original_generate = acceptance.generate_reply
    try:
        saved = load_named_connection('qwen')
        if (not saved or not saved.configured or saved.model != MODEL or
                saved.service_platform != 'bailian' or saved.service_region != 'cn-beijing' or
                saved.reasoning_effort not in ('', 'none')):
            raise ValueError('connection does not match the plan')
        verify_source(saved, options.source)
        test_connection = replace(saved, reasoning_effort='low')

        def current_test_connection():
            current = load_named_connection('qwen')
            if current is None or current.fingerprint != saved.fingerprint:
                return None
            return replace(current, reasoning_effort='low')

        journal = Journal(directory / f'qwen-thinking-acceptance-{uuid4().hex}.jsonl')
        journal.write('batch_started', source_version=APP_VERSION, live_web_version=live_version, **PLAN)
        guarded = BoundedGeneration(original_generate, test_connection, current_test_connection, journal, max_calls=5)
        acceptance.generate_reply = guarded
        with override_settings(QWEN_STRUCTURED_HANZI_ENABLED=False):
            path, report = acceptance.run_acceptance(connection=test_connection, stop_on_check_failure=True)
        journal.write('batch_finished', generation_attempts=guarded.attempts,
                      report_id=report['report_id'], passed=report['passed'])
        print(json.dumps({'generation_attempts': guarded.attempts, 'report': str(path),
                          'journal': str(journal.path), 'passed': report['passed'],
                          'source_version': APP_VERSION, 'live_web_version': live_version,
                          'saved_connection_changed': False, 'manual_review_required': True,
                          'enabled': False}, ensure_ascii=False))
        return 0 if report['passed'] else 2
    finally:
        acceptance.generate_reply = original_generate
        if journal is not None:
            journal.close()
        lock.release()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('思考模式验收未完成；请检查本机调用记录。没有自动重试。', file=sys.stderr)
        raise SystemExit(1) from None
