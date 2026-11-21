"""Bind internal execution receipts to the exact artifact bytes being saved."""
import json

from django.utils.crypto import constant_time_compare, salted_hmac

from .bundles import validate_bundle
from .provider import ProviderError


def _payload(bundle, report):
    return json.dumps({'bundle': validate_bundle(bundle), 'report': report},
                      sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def seal_execution(bundle, report):
    return {'report': report, 'signature': salted_hmac('roundtable.execution-report.v1',
            _payload(bundle, report), algorithm='sha256').hexdigest()}


def verified_execution(bundle, receipt):
    if not isinstance(receipt, dict) or set(receipt) != {'report', 'signature'}:
        raise ProviderError('运行记录格式无效，不能用于交付验收。')
    expected = seal_execution(bundle, receipt['report'])['signature']
    if not isinstance(receipt['signature'], str) or not constant_time_compare(expected, receipt['signature']):
        raise ProviderError('运行记录与本版文件不一致，不能用于交付验收。')
    return receipt['report']
