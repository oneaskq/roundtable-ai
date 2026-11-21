"""Pseudonymous identifiers to find deleted accounts in older snapshots."""
from datetime import datetime, timezone
import uuid

from django.conf import settings
from django.utils.crypto import salted_hmac


def identity_key_id():
    return salted_hmac('roundtable-account-deletion-key-v1', 'identifier',
                       secret=settings.SECRET_KEY, algorithm='sha256').hexdigest()


def account_fingerprint(user_id, joined_at):
    if isinstance(joined_at, str):
        joined_at = datetime.fromisoformat(joined_at.replace('Z', '+00:00'))
    if not isinstance(joined_at, datetime):
        raise ValueError('Invalid account creation timestamp')
    if joined_at.tzinfo is None:
        joined_at = joined_at.replace(tzinfo=timezone.utc)
    joined = joined_at.astimezone(timezone.utc).isoformat(timespec='microseconds')
    return salted_hmac('roundtable-account-deletion-v1', f'{user_id}|{joined}',
                       secret=settings.SECRET_KEY, algorithm='sha256').hexdigest()


RECORD_KINDS = frozenset({'project', 'conversation', 'memory', 'source', 'quality_round'})


def record_fingerprint(kind, object_id):
    if kind not in RECORD_KINDS:
        raise ValueError('Unsupported deletion record kind')
    canonical_id = str(uuid.UUID(str(object_id)))
    return salted_hmac('roundtable-record-deletion-v1', f'{kind}|{canonical_id}',
                       secret=settings.SECRET_KEY, algorithm='sha256').hexdigest()


def record_deletion(kind, object_id):
    from .models import RecordDeletionReceipt
    digest = record_fingerprint(kind, object_id)
    receipt, _ = RecordDeletionReceipt.objects.get_or_create(
        object_digest=digest, defaults={'kind': kind, 'identity_key_id': identity_key_id()})
    if receipt.kind != kind or receipt.identity_key_id != identity_key_id():
        raise ValueError('Deletion record key mismatch')
    return receipt
