"""Canonical validation for acceptance cases supplied through an owner's API."""
import hashlib
import json

from .provider import ProviderError


def normalize_cases(cases):
    """Copy bounded UTF-8 cases without changing significant input/output text."""
    if not isinstance(cases, list) or len(cases) > 5:
        raise ProviderError('验收用例必须是列表，最多 5 个；空列表可清除用例。')
    normalized = []
    for case in cases:
        if not isinstance(case, dict) or set(case) != {'stdin', 'expected_stdout'}:
            raise ProviderError('每个用例只能包含 stdin 和 expected_stdout。')
        result = {}
        for field in ('stdin', 'expected_stdout'):
            value = case[field]
            if not isinstance(value, str) or len(value) > 2000:
                raise ProviderError('每个用例的输入和预期输出必须是文字，各最多 2000 字符。')
            try:
                value.encode('utf-8')
            except UnicodeError:
                raise ProviderError('验收用例必须是有效的 UTF-8 文字。') from None
            result[field] = value
        normalized.append(result)
    return normalized


def case_revision(cases):
    return hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest()
