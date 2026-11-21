"""Preview or run an authorized Qwen structured-length probe and acceptance batch.

One preliminary writing probe is followed by the standard five-call acceptance
only if it passes. The script never approves or enables capabilities.
"""
import argparse
import json
import os
import sys
from uuid import uuid4


from .qwen_budget_retest import (BoundedGeneration, Journal, MAX_OUTPUT_TOKENS,
                                     MODEL, current_server_version)


PLAN = {'service': '阿里云百炼北京', 'model': MODEL, 'generation_calls_max': 6,
        'output_tokens_max_per_call': MAX_OUTPUT_TOKENS, 'inputs': '仅虚构验收样例',
        'sequence': '1次写作定点测试；通过后最多5次完整验收',
        'automatic_retry': False, 'approve_or_enable': False}


def save_probe(directory, result, source_version, connection):
    """Keep a signed preliminary result distinct from approvable reports."""
    from django.core import signing
    from django.utils import timezone
    from delivery.acceptance import _canonical

    document = {'schema_version': 1, 'kind': 'preliminary_structured_writing_probe',
                'approvable': False, 'source_version': source_version,
                'provider': connection.provider, 'model': connection.model,
                'service_region': connection.service_region, 'service_platform': connection.service_platform,
                'connection_fingerprint': connection.fingerprint,
                'completed_at': timezone.now().isoformat(), 'result': result}
    wrapper = {'probe': document,
               'signature': signing.Signer(salt='roundtable.qwen-structured-probe.v1').signature(_canonical(document))}
    path = directory / f'qwen-structured-probe-{uuid4().hex}.json'
    with path.open('x', encoding='utf-8') as stream:
        json.dump(wrapper, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--acknowledge-paid-calls', action='store_true')
    options = parser.parse_args()
    if not options.execute:
        print(json.dumps({'mode': 'preview', **PLAN}, ensure_ascii=False))
        return 0
    if not options.acknowledge_paid_calls:
        parser.error('真实请求可能计费；须先获得明确授权并传入 --acknowledge-paid-calls。')

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
    lock = LocalProcessLock(directory / 'qwen-budget-retest.lock')
    if not lock.acquire():
        raise RuntimeError('已有千问复测在运行；未发送请求。')
    journal = None
    original_generate = acceptance.generate_reply
    try:
        connection = load_named_connection('qwen')
        if (not connection or not connection.configured or connection.model != MODEL or
                connection.service_platform != 'bailian' or connection.service_region != 'cn-beijing'):
            raise RuntimeError('百炼北京连接或模型与本轮计划不一致；未发送请求。')
        journal = Journal(directory / f'qwen-structured-retest-{uuid4().hex}.jsonl')
        journal.write('batch_started', source_version=APP_VERSION, live_web_version=live_version,
                      experimental_structured_mode=True, **PLAN)
        guarded = BoundedGeneration(original_generate, connection,
                                    lambda: load_named_connection('qwen'), journal, max_calls=6)
        acceptance.generate_reply = guarded
        # This switch lives only in this isolated process. The running service
        # remains unchanged and experimental reports cannot be approved while
        # the live configuration keeps the feature disabled.
        with override_settings(QWEN_STRUCTURED_HANZI_ENABLED=True):
            probe = acceptance._probe(connection, 'chat.writing', [acceptance.WRITING_PROMPT])
            probe_path = save_probe(directory, probe, APP_VERSION, connection)
            journal.write('preliminary_probe_finished', passed=probe['passed'], probe_file=probe_path.name)
            report_path = None
            report = None
            if probe['passed']:
                report_path, report = acceptance.run_acceptance(connection=connection, stop_on_check_failure=True)
        passed = bool(report and report['passed'])
        journal.write('batch_finished', generation_attempts=guarded.attempts,
                      report_id=report['report_id'] if report else None, passed=passed)
        print(json.dumps({'generation_attempts': guarded.attempts, 'probe': str(probe_path),
                          'probe_passed': probe['passed'], 'report': str(report_path) if report_path else None,
                          'journal': str(journal.path), 'passed': passed, 'source_version': APP_VERSION,
                          'live_web_version': live_version, 'manual_review_required': True,
                          'enabled': False}, ensure_ascii=False))
        return 0 if passed else 2
    finally:
        acceptance.generate_reply = original_generate
        if journal is not None:
            journal.close()
        lock.release()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('复测未完成；请检查本机调用记录。没有自动重试。', file=sys.stderr)
        raise SystemExit(1) from None
