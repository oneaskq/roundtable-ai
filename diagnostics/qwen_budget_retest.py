"""Preview or run one explicitly authorized, bounded Qwen acceptance batch.

Running without --execute is read-only and does not load saved connections.
Execution generates a signed report but never approves or enables capabilities.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import urllib.request
from uuid import uuid4


MAX_CALLS = 5
MAX_OUTPUT_TOKENS = 4096
MODEL = 'qwen3.7-plus'
PLAN = {'service': '阿里云百炼北京', 'model': MODEL, 'generation_calls_max': MAX_CALLS,
        'output_tokens_max_per_call': MAX_OUTPUT_TOKENS, 'inputs': '仅虚构验收样例',
        'automatic_retry': False, 'approve_or_enable': False}


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.stream = self.path.open('x', encoding='utf-8', newline='\n')

    def write(self, event, **data):
        record = {'event': event, 'at': datetime.now(timezone.utc).isoformat(), **data}
        self.stream.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + '\n')
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def close(self):
        self.stream.close()


class BoundedGeneration:
    """Record before dispatch, enforce the agreed limit, and never retry."""
    def __init__(self, generate, expected, current_connection, journal, *, max_calls=MAX_CALLS):
        if type(max_calls) is not int or not 1 <= max_calls <= 6:
            raise ValueError('Invalid maximum call count')
        self.generate = generate
        self.expected = expected
        self.current_connection = current_connection
        self.journal = journal
        self.attempts = 0
        self.failed = False
        self.max_calls = max_calls

    def __call__(self, *args, **kwargs):
        from delivery.provider import ProviderError

        if self.failed or self.attempts >= self.max_calls:
            raise ProviderError('本轮已停止或达到调用上限；没有自动重试。')
        current = self.current_connection()
        if (current is None or current.fingerprint != self.expected.fingerprint or
                kwargs.get('connection') != self.expected or
                kwargs.get('max_output_tokens') != MAX_OUTPUT_TOKENS or
                not callable(kwargs.get('on_text'))):
            self.failed = True
            raise ProviderError('连接或调用边界已变化，本轮已停止。')
        self.attempts += 1
        number = self.attempts
        try:
            self.journal.write('request_started', attempt=number, model=MODEL,
                               output_tokens_max=MAX_OUTPUT_TOKENS)
        except OSError:
            self.failed = True
            raise
        try:
            result = self.generate(*args, **kwargs)
        except Exception as error:
            from delivery.provider import safe_provider_diagnostic
            self.failed = True
            diagnostic = safe_provider_diagnostic(error)
            event = 'response_rejected' if diagnostic == 'structured_validation_failed' else 'request_failed'
            self.journal.write(event, attempt=number, diagnostic_code=diagnostic)
            raise
        try:
            self.journal.write('response_received', attempt=number)
        except OSError:
            self.failed = True
            raise
        return result


def current_server_version():
    # Local health only; no model credentials or user content are sent.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            'http://127.0.0.1:8910/health/', timeout=3) as response:
        data = json.loads(response.read(4096))
    if data.get('application') != 'roundtable-internal':
        return None
    return data.get('version')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--acknowledge-paid-calls', action='store_true')
    parser.add_argument('--experimental-structured', action='store_true')
    options = parser.parse_args()
    if not options.execute:
        print(json.dumps({'mode': 'preview', 'experimental_structured': options.experimental_structured,
                          **PLAN}, ensure_ascii=False))
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

    # The isolated probe may assess staged source while the user's existing
    # webpage keeps running. It never enables capabilities; activation must
    # wait until the live webpage and worker use the tested source version.
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
            raise RuntimeError('已保存的百炼北京连接或模型与本轮计划不一致；未发送请求。')
        journal = Journal(directory / f'qwen-budget-retest-{uuid4().hex}.jsonl')
        journal.write('batch_started', source_version=APP_VERSION, live_web_version=live_version,
                      experimental_structured_mode=options.experimental_structured, **PLAN)
        guarded = BoundedGeneration(original_generate, connection,
                                    lambda: load_named_connection('qwen'), journal)
        acceptance.generate_reply = guarded
        with override_settings(QWEN_STRUCTURED_HANZI_ENABLED=options.experimental_structured):
            path, report = acceptance.run_acceptance(connection=connection, stop_on_check_failure=True)
        journal.write('batch_finished', generation_attempts=guarded.attempts,
                      report_id=report['report_id'], passed=report['passed'])
        print(json.dumps({'generation_attempts': guarded.attempts, 'report': str(path),
                          'journal': str(journal.path), 'passed': report['passed'],
                          'source_version': APP_VERSION, 'live_web_version': live_version,
                          'experimental_structured': options.experimental_structured,
                          'manual_review_required': True, 'enabled': False}, ensure_ascii=False))
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
        # Upstream exception bodies and local connection details may be secret.
        print('复测未完成；请检查本机调用记录。没有自动重试。', file=sys.stderr)
        raise SystemExit(1) from None
