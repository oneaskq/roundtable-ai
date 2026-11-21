"""Explicit SQLite snapshots and disposable restore checks. Never replaces a live database.

The manifest verifies accidental corruption, not provenance or authenticity. A snapshot
contains private application data and must remain in access-controlled storage.
Reference: https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup
"""
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
SNAPSHOT_NAME = 'snapshot.sqlite3'
MANIFEST_NAME = 'manifest.json'
MAX_SECONDS = 30


class BackupError(Exception):
    """Safe operational error, excluding database contents, SQL, or credentials."""


def _ordinary_path(value, *, exists):
    path = Path(value).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise BackupError('快照路径不能包含符号链接。')
    # Reject junctions and other redirected Windows filesystem locations too.
    if path.resolve() != path:
        raise BackupError('快照路径必须是规范的实际路径。')
    if exists and not path.is_file():
        raise BackupError('源文件不存在或不是普通文件。')
    return path


def _digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _connect_readonly(path):
    with path.open('rb') as stream:
        if stream.read(16) != b'SQLite format 3\x00':
            raise BackupError('输入不是完整的 SQLite 数据库文件。')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    connection.execute('PRAGMA trusted_schema=OFF')
    connection.execute('PRAGMA query_only=ON')
    return connection


def _limited_backup(source, target):
    deadline = time.monotonic() + MAX_SECONDS
    page_size = source.execute('PRAGMA page_size').fetchone()[0]

    def progress(status, remaining, total):
        if time.monotonic() > deadline or total * page_size > MAX_SNAPSHOT_BYTES:
            raise BackupError('备份超出时间或大小限制；未替换任何已有数据库。')

    source.backup(target, pages=256, progress=progress, sleep=.05)


def _check_database(connection):
    deadline = time.monotonic() + MAX_SECONDS
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    if connection.execute('PRAGMA integrity_check').fetchone() != ('ok',):
        raise BackupError('SQLite 完整性检查未通过。')
    if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise BackupError('SQLite 外键检查未通过。')
    return connection.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('table','index','view','trigger')").fetchone()[0]


def create_snapshot(source_file, output_directory):
    """Read explicit source in read-only mode, exclusively create a new snapshot directory."""
    source = _ordinary_path(source_file, exists=True)
    output = _ordinary_path(output_directory, exists=False)
    if source.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise BackupError('源数据库超过此本地快照工具的大小限制。')
    if output.exists() or not output.parent.is_dir():
        raise BackupError('输出必须是已有父目录下尚不存在的新目录；不能覆盖旧快照。')
    snapshot = output / SNAPSHOT_NAME
    try:
        # Source is opened and checked before any output directory is created.
        with closing(_connect_readonly(source)) as original:
            _check_database(original)
            output.mkdir(mode=0o700)
            with snapshot.open('xb'):
                pass
            os.chmod(snapshot, 0o600)
            with closing(sqlite3.connect(snapshot)) as target:
                target.execute('PRAGMA trusted_schema=OFF')
                _limited_backup(original, target)
                _check_database(target)
        if snapshot.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise BackupError('生成快照超过大小限制。')
        manifest = {'schema_version': 1, 'engine': 'sqlite3', 'file': SNAPSHOT_NAME,
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'bytes': snapshot.stat().st_size, 'sha256': _digest(snapshot),
                    'scope': 'sqlite_snapshot_integrity_only'}
        with (output / MANIFEST_NAME).open('x', encoding='utf-8') as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
        os.chmod(output / MANIFEST_NAME, 0o600)
        return {'status': 'passed', 'scope': 'local_sqlite_snapshot', 'manifest': manifest,
                'notice': '已生成新快照，原库未替换。此文件包含私密数据；本检查不能证明生产恢复能力。'}
    except BackupError:
        raise
    except (OSError, sqlite3.Error, ValueError):
        raise BackupError('无法完成快照；原数据库未替换。请检查路径、权限和数据库完整性。') from None


def check_snapshot(snapshot_directory):
    """Check hash, restore only into a disposable new file, then inspect the restored copy."""
    directory = _ordinary_path(snapshot_directory, exists=False)
    snapshot = _ordinary_path(directory / SNAPSHOT_NAME, exists=True)
    manifest_path = _ordinary_path(directory / MANIFEST_NAME, exists=True)
    try:
        if manifest_path.stat().st_size > 4096 or snapshot.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise BackupError('快照或清单超过大小限制。')

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        manifest = json.loads(manifest_path.read_text(encoding='utf-8'), object_pairs_hook=unique_object)
        if (not isinstance(manifest, dict) or set(manifest) != {
            'schema_version', 'engine', 'file', 'created_at', 'bytes', 'sha256', 'scope'} or
            manifest['schema_version'] != 1 or manifest['engine'] != 'sqlite3' or
            manifest['file'] != SNAPSHOT_NAME or manifest['scope'] != 'sqlite_snapshot_integrity_only' or
            not isinstance(manifest['bytes'], int) or isinstance(manifest['bytes'], bool) or
            manifest['bytes'] != snapshot.stat().st_size or
            not isinstance(manifest['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', manifest['sha256']) or
            manifest['sha256'] != _digest(snapshot)):
            raise BackupError('快照清单或内容校验失败；未进行恢复。')
        timestamp = datetime.fromisoformat(manifest['created_at'])
        if timestamp.tzinfo is None or timestamp > datetime.now(timezone.utc):
            raise BackupError('快照时间记录无效。')
        with closing(_connect_readonly(snapshot)) as source:
            count = _check_database(source)
            with tempfile.TemporaryDirectory(prefix='roundtable-restore-check-') as temp:
                restored_file = Path(temp) / 'restored.sqlite3'
                with closing(sqlite3.connect(restored_file)) as restored:
                    restored.execute('PRAGMA trusted_schema=OFF')
                    _limited_backup(source, restored)
                    if _check_database(restored) != count:
                        raise BackupError('临时恢复后的结构检查失败。')
        return {'status': 'passed', 'scope': 'disposable_sqlite_restore',
                'sha256': manifest['sha256'], 'bytes': manifest['bytes'],
                'checks': ['manifest_shape', 'sha256', 'sqlite_integrity', 'foreign_keys', 'temporary_restore'],
                'notice': '只恢复到新建临时文件并检查后清理；未替换原库。哈希验证不证明来源，不能代替生产 PostgreSQL 恢复演练。'}
    except BackupError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        raise BackupError('快照验证失败；未替换任何已有数据库。') from None
