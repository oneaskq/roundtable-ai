"""Encrypted local-only SMTP settings; loading never opens a network connection."""
import json
import os
import re
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

MAX_BYTES = 32768


class MailConfigError(Exception):
    pass


def _validate(record):
    fields = {'version', 'host', 'port', 'username', 'password', 'sender', 'tls', 'ssl'}
    if not isinstance(record, dict) or set(record) != fields or type(record['version']) is not int or record['version'] != 1:
        raise MailConfigError('邮件连接格式无效。')
    host, port = record['host'], record['port']
    if (not isinstance(host, str) or len(host) > 253 or
            not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', host) or
            '.' not in host or host.casefold().endswith(('.localhost', '.local', '.test', '.invalid')) or
            type(port) is not int or not 1 <= port <= 65535):
        raise MailConfigError('邮件服务器地址或端口无效。')
    if any(not isinstance(record[name], str) or not record[name] or len(record[name]) > 1024 or
           any(ord(char) < 32 for char in record[name]) for name in ('username', 'password', 'sender')):
        raise MailConfigError('邮件账号、授权密码或发信地址无效。')
    try:
        validate_email(record['sender'])
    except ValidationError:
        raise MailConfigError('发信邮箱地址无效。') from None
    if type(record['tls']) is not bool or type(record['ssl']) is not bool or record['tls'] == record['ssl']:
        raise MailConfigError('邮件连接必须使用 TLS 或 SSL。')
    return record


def load_local_mail(path, key):
    try:
        target = Path(path)
        with target.open('rb') as stream:
            encrypted = stream.read(MAX_BYTES + 1)
        if len(encrypted) > MAX_BYTES:
            raise MailConfigError('本地邮件连接文件过大。')
        return _validate(json.loads(Fernet(key).decrypt(encrypted)))
    except FileNotFoundError:
        return None
    except MailConfigError:
        raise
    except (OSError, InvalidToken, ValueError, TypeError, UnicodeError, RecursionError):
        raise MailConfigError('本地邮件连接无法读取，请重新配置。') from None


def save_local_mail(path, key, record):
    _validate(record)
    try:
        encrypted = Fernet(key).encrypt(json.dumps(record, ensure_ascii=False).encode('utf-8'))
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='wb', dir=target.parent, prefix='.mail-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        raise MailConfigError('无法保存邮件连接，请检查本机数据目录权限。') from None
