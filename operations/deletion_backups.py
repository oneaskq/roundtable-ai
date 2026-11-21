"""Read-only backup inspection and separately signed deletion-receipt ledgers."""
import os
import re
import sqlite3
import uuid
import copy
import json
from contextlib import closing
from datetime import datetime, timezone as utc_timezone
from io import StringIO

from django.core import signing
from django.utils import timezone

from operations.backups import (MANIFEST_NAME, MAX_SNAPSHOT_BYTES, SNAPSHOT_NAME, BackupError, _check_database,
                                _connect_readonly, _digest, _ordinary_path, check_snapshot, create_snapshot)
from workspace.deletion import RECORD_KINDS, account_fingerprint, identity_key_id, record_fingerprint
from workspace.models import AccountDeletionReceipt, RecordDeletionReceipt


LEDGER_SALT = 'roundtable-backup-deletions-v1'
MAX_LEDGER_BYTES = 4 * 1024 * 1024
MAX_ROWS = 50000
RECORD_TABLES = {'project': 'workspace_project', 'conversation': 'workspace_conversation',
                 'memory': 'workspace_memory', 'source': 'workspace_sourcematerial',
                 'quality_round': 'delivery_qualityround'}


def _validate_receipts(rows):
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise BackupError('注销记录清单无效或过大。')
    result = {}
    for item in rows:
        if not isinstance(item, dict) or set(item) != {'id', 'digest', 'key_id'}:
            raise BackupError('注销记录清单格式无效。')
        try:
            receipt_id = str(uuid.UUID(item['id']))
        except (TypeError, ValueError, AttributeError):
            raise BackupError('注销记录清单格式无效。') from None
        digest = item['digest']
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or digest in result:
            raise BackupError('注销记录清单格式无效。')
        if item['key_id'] != identity_key_id():
            raise BackupError('注销记录使用的签名密钥不同；无法确认备份状态。')
        result[digest] = receipt_id
    return result


def current_receipts():
    rows = list(AccountDeletionReceipt.objects.order_by('id').values('id', 'identity_digest', 'identity_key_id')[:MAX_ROWS + 1])
    return [{'id': str(row['id']), 'digest': row['identity_digest'], 'key_id': row['identity_key_id']} for row in rows]


def _validate_record_receipts(rows):
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise BackupError('单项删除记录无效或过多。')
    result = {}
    for item in rows:
        if not isinstance(item, dict) or set(item) != {'id', 'kind', 'digest', 'key_id'} or item['kind'] not in RECORD_KINDS:
            raise BackupError('单项删除记录格式无效。')
        try:
            receipt_id = str(uuid.UUID(item['id']))
        except (TypeError, ValueError, AttributeError):
            raise BackupError('单项删除记录格式无效。') from None
        digest = item['digest']
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or digest in result:
            raise BackupError('单项删除记录格式无效。')
        if item['key_id'] != identity_key_id():
            raise BackupError('单项删除记录使用的签名密钥不同；无法确认备份状态。')
        result[digest] = {'id': receipt_id, 'kind': item['kind']}
    return result


def current_record_receipts():
    rows = RecordDeletionReceipt.objects.order_by('id').values('id', 'kind', 'object_digest', 'identity_key_id')[:MAX_ROWS + 1]
    return [{'id': str(row['id']), 'kind': row['kind'], 'digest': row['object_digest'],
             'key_id': row['identity_key_id']} for row in rows]


def serialize_ledger():
    rows = current_receipts()
    record_rows = current_record_receipts()
    _validate_receipts(rows)
    _validate_record_receipts(record_rows)
    payload = {'schema_version': 2, 'exported_at': timezone.now().isoformat(), 'receipts': rows,
               'record_receipts': record_rows}
    token = signing.dumps(payload, salt=LEDGER_SALT, compress=True)
    if len(token.encode('utf-8')) > MAX_LEDGER_BYTES:
        raise BackupError('注销记录清单过大。')
    return token, len(rows) + len(record_rows)


def export_ledger(output_file):
    output = _ordinary_path(output_file, exists=False)
    if output.exists() or not output.parent.is_dir():
        raise BackupError('输出必须是已有目录下尚不存在的新文件，不能覆盖旧文件。')
    token, count = serialize_ledger()
    try:
        with output.open('x', encoding='utf-8') as stream:
            stream.write(token)
        os.chmod(output, 0o600)
    except OSError:
        raise BackupError('无法创建注销记录清单；没有覆盖已有文件。') from None
    return {'status': 'exported', 'receipts': count,
            'notice': '请单独保管清单及签名密钥。旧清单不含之后发生的注销；它不会删除备份。'}


def load_ledger_bundle(ledger_file):
    path = _ordinary_path(ledger_file, exists=True)
    try:
        if path.stat().st_size > MAX_LEDGER_BYTES:
            raise BackupError('注销记录清单过大。')
        payload = signing.loads(path.read_text(encoding='utf-8'), salt=LEDGER_SALT)
        if not isinstance(payload, dict) or payload.get('schema_version') not in (1, 2):
            raise BackupError('注销记录清单格式无效。')
        expected = {'schema_version', 'exported_at', 'receipts'}
        if payload['schema_version'] == 2:
            expected.add('record_receipts')
        if set(payload) != expected or not isinstance(payload['exported_at'], str):
            raise BackupError('注销记录清单格式无效。')
        _validate_receipts(payload['receipts'])
        records = payload.get('record_receipts', [])
        _validate_record_receipts(records)
        return payload['receipts'], records
    except (OSError, UnicodeError, signing.BadSignature, ValueError, TypeError):
        raise BackupError('注销记录清单读取或签名校验失败；无法确认备份状态。') from None


def load_ledger(ledger_file):
    return load_ledger_bundle(ledger_file)[0]


def inspect_snapshot(snapshot_path, *, ledger_file=None):
    target = _ordinary_path(snapshot_path, exists=False)
    if target.is_dir():
        check_snapshot(target)
        target = _ordinary_path(target / SNAPSHOT_NAME, exists=True)
    else:
        target = _ordinary_path(target, exists=True)
    if target.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise BackupError('备份文件过大，无法检查。')
    accounts, records = load_ledger_bundle(ledger_file) if ledger_file else (current_receipts(), current_record_receipts())
    known = _validate_receipts(accounts)
    known_records = _validate_record_receipts(records)
    if ledger_file:
        # When the current database is available, never sanitize using a ledger
        # that omits deletions already recorded here. An offline restore still
        # requires the operator to supply the newest separately saved ledger.
        live_accounts = _validate_receipts(current_receipts())
        live_records = _validate_record_receipts(current_record_receipts())
        if not set(live_accounts).issubset(known) or not set(live_records).issubset(known_records):
            raise BackupError('删除清单缺少当前已记录的项目，请先导出最新清单。')
    matches = set()
    record_matches = set()
    try:
        with closing(_connect_readonly(target)) as source:
            _check_database(source)
            rows = source.execute('SELECT id, date_joined FROM accounts_user LIMIT ?', (MAX_ROWS + 1,)).fetchall()
            if len(rows) > MAX_ROWS:
                raise BackupError('备份账号数量超过本工具限制，无法确认备份状态。')
            for user_id, joined_at in rows:
                digest = account_fingerprint(user_id, joined_at)
                if digest in known:
                    matches.add(known[digest])
            kinds = {record['kind'] for record in known_records.values()}
            for kind in kinds:
                table = RECORD_TABLES[kind]
                if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
                    raise BackupError('备份缺少待核对项目的数据表；无法确认备份状态。')
                rows = source.execute(f'SELECT id FROM "{table}" LIMIT ?', (MAX_ROWS + 1,)).fetchall()
                if len(rows) > MAX_ROWS:
                    raise BackupError('备份项目数量超过本工具限制，无法确认备份状态。')
                for (object_id,) in rows:
                    record = known_records.get(record_fingerprint(kind, object_id))
                    if record and record['kind'] == kind:
                        record_matches.add(record['id'])
    except (sqlite3.Error, OSError, ValueError, TypeError):
        raise BackupError('无法读取备份中的账号记录；不能认为备份已清理。') from None
    status = ('recorded_accounts_found' if matches else 'recorded_records_found' if record_matches
              else 'no_recorded_matches' if known or known_records else 'no_receipts')
    return {'status': status, 'receipts_checked': len(known), 'matching_receipts': sorted(matches),
            'record_receipts_checked': len(known_records), 'matching_record_receipts': sorted(record_matches),
            'notice': '只检查这份备份和已记录的删除项，未修改备份。旧清单、上线前的单项删除、其他副本及外部服务可能不在范围内。'}


def sanitized_snapshot(snapshot_path, output_directory, *, ledger_file=None):
    """Create a new, migrated SQLite copy with recorded deletions applied.

    This never overwrites the live DB or the input backup. On failure there is no
    valid manifest in the new output directory, so it cannot look like a verified
    snapshot. The caller must still manage older copies and external services.
    """
    from django.contrib.auth import get_user_model
    from django.contrib.sessions.models import Session
    from django.core.management import call_command
    from django.db import connections, transaction
    from accounts.models import DeviceSession, LoginAttempt
    from accounts.views import attempt_key
    from delivery.models import AgentStep, QualityRound, TeamRun
    from workspace.models import Conversation, DataPolicy, Memory, Project, SourceMaterial, Task

    accounts, records = load_ledger_bundle(ledger_file) if ledger_file else (current_receipts(), current_record_receipts())
    known = _validate_receipts(accounts)
    known_records = _validate_record_receipts(records)
    if ledger_file and (not set(_validate_receipts(current_receipts())).issubset(known) or
                        not set(_validate_record_receipts(current_record_receipts())).issubset(known_records)):
        raise BackupError('删除清单缺少当前已记录的项目，请先导出最新清单。')
    source = _ordinary_path(snapshot_path, exists=False)
    if source.is_dir():
        check_snapshot(source)
        source = _ordinary_path(source / SNAPSHOT_NAME, exists=True)
    else:
        source = _ordinary_path(source, exists=True)
    output = _ordinary_path(output_directory, exists=False)
    create_snapshot(source, output)
    manifest_file = output / MANIFEST_NAME
    manifest_file.unlink()  # An incomplete copy must never pass check_snapshot.
    database_file = output / SNAPSHOT_NAME
    alias = 'sanitized_' + uuid.uuid4().hex
    config = copy.deepcopy(connections.databases['default'])
    config.update({'ENGINE': 'django.db.backends.sqlite3', 'NAME': str(database_file),
                   'OPTIONS': {'timeout': 20}, 'CONN_MAX_AGE': 0})
    connections.databases[alias] = config
    counts = {'accounts': 0, 'records': 0}
    try:
        call_command('migrate', database=alias, interactive=False, verbosity=0, stdout=StringIO(), stderr=StringIO())
        models = {'conversation': Conversation, 'source': SourceMaterial, 'project': Project,
                  'memory': Memory, 'quality_round': QualityRound}
        with transaction.atomic(using=alias):
            # A backup can contain newer tombstones than an old external ledger.
            # Preserve and apply both sets; an incompatible key fails closed.
            saved_accounts = [{'id': str(row['id']), 'digest': row['identity_digest'], 'key_id': row['identity_key_id']}
                              for row in AccountDeletionReceipt.objects.using(alias).values(
                                  'id', 'identity_digest', 'identity_key_id')[:MAX_ROWS + 1]]
            saved_records = [{'id': str(row['id']), 'kind': row['kind'], 'digest': row['object_digest'], 'key_id': row['identity_key_id']}
                             for row in RecordDeletionReceipt.objects.using(alias).values(
                                 'id', 'kind', 'object_digest', 'identity_key_id')[:MAX_ROWS + 1]]
            saved_known = _validate_receipts(saved_accounts)
            saved_known_records = _validate_record_receipts(saved_records)
            accounts.extend(row for row in saved_accounts if row['digest'] not in known)
            records.extend(row for row in saved_records if row['digest'] not in known_records)
            known.update(saved_known)
            known_records.update(saved_known_records)
            _validate_receipts(accounts)
            _validate_record_receipts(records)
            User = get_user_model()
            if User.objects.using(alias).count() > MAX_ROWS:
                raise BackupError('备份账号数量超过本工具限制。')
            for user in list(User.objects.using(alias).all()):
                if account_fingerprint(user.pk, user.date_joined) not in known:
                    continue
                session_keys = DeviceSession.objects.using(alias).filter(user_id=user.pk).values_list('session_key', flat=True)
                Session.objects.using(alias).filter(session_key__in=session_keys).delete()
                keys = [attempt_key(scope, str(user.pk)) for scope in (
                    'account-deletion', 'password-change', 'mfa', 'mfa-setup', 'mfa-enable', 'mfa-recovery-rotate')]
                keys.append(attempt_key('login-email', user.email))
                LoginAttempt.objects.using(alias).filter(key__in=keys).delete()
                user.delete(using=alias)
                counts['accounts'] += 1
            for kind, model in models.items():
                if not any(record['kind'] == kind for record in known_records.values()):
                    continue
                if model.objects.using(alias).count() > MAX_ROWS:
                    raise BackupError('备份项目数量超过本工具限制。')
                ids = list(model.objects.using(alias).values_list('pk', flat=True))
                for object_id in ids:
                    matched = known_records.get(record_fingerprint(kind, object_id))
                    if matched and matched['kind'] == kind:
                        model.objects.using(alias).filter(pk=object_id).delete()
                        counts['records'] += 1
            # A restored copy carries the same tombstones for future snapshots.
            for item in accounts:
                AccountDeletionReceipt.objects.using(alias).get_or_create(identity_digest=item['digest'],
                    defaults={'id': item['id'], 'identity_key_id': item['key_id']})
            for item in records:
                RecordDeletionReceipt.objects.using(alias).get_or_create(object_digest=item['digest'],
                    defaults={'id': item['id'], 'kind': item['kind'], 'identity_key_id': item['key_id']})
            # Restoring an old snapshot must not revive sessions, consent for
            # automatic deletion, or previously sent/queued paid model calls.
            now = timezone.now()
            Session.objects.using(alias).all().delete()
            DeviceSession.objects.using(alias).filter(revoked_at__isnull=True).update(revoked_at=now)
            DataPolicy.objects.using(alias).filter(auto_cleanup=True).update(auto_cleanup=False)
            interrupted = TeamRun.objects.using(alias).filter(status__in=['queued', 'running'])
            AgentStep.objects.using(alias).filter(run__in=interrupted, status='running').update(
                status='failed', error='恢复备份时已中断；不会自动重试。', updated_at=now)
            AgentStep.objects.using(alias).filter(run__in=interrupted, status='pending').update(status='skipped', updated_at=now)
            interrupted.update(status='failed', error='从备份恢复时已停止；不会自动重试，已发出的请求可能计费。',
                               input_context=[], finished_at=now, updated_at=now)
            Task.objects.using(alias).filter(status='running').update(status='failed', execution_id=None, updated_at=now)
        connections[alias].close()
        with closing(sqlite3.connect(database_file)) as db:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('VACUUM')
            _check_database(db)
        if database_file.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise BackupError('清理后的快照超过大小限制。')
        manifest = {'schema_version': 1, 'engine': 'sqlite3', 'file': SNAPSHOT_NAME,
                    'created_at': datetime.now(utc_timezone.utc).isoformat(), 'bytes': database_file.stat().st_size,
                    'sha256': _digest(database_file), 'scope': 'sqlite_snapshot_integrity_only'}
        with manifest_file.open('x', encoding='utf-8') as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
        os.chmod(manifest_file, 0o600)
        check_snapshot(output)
        checked = inspect_snapshot(output, ledger_file=ledger_file)
        if checked['matching_receipts'] or checked['matching_record_receipts']:
            raise BackupError('清理副本仍包含已记录的删除项目，未生成可用快照。')
        return {'status': 'passed', 'scope': 'new_local_sqlite_copy', 'removed': counts,
                'notice': '仅清理新副本中有记录的项目，已关闭旧会话、自动清理和未完成任务；原备份、旧清单未覆盖的删除及外部副本仍需处理。恢复还可能回滚密码和验证器设置，必须另行核对。'}
    except BackupError:
        if manifest_file.exists():
            manifest_file.unlink()
        raise
    except Exception as error:
        # The output has no valid manifest unless all work reached verification.
        if manifest_file.exists():
            manifest_file.unlink()
        raise BackupError(f'无法生成已清理的备份副本（{type(error).__name__}）；原库和原备份均未修改。') from None
    finally:
        connections[alias].close()
        del connections.databases[alias]
        try:
            delattr(connections._connections, alias)
        except AttributeError:
            pass
