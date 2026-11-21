"""Local encrypted settings. Loading never contacts a model provider."""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

CONFIG_VERSION = 1
ENDPOINTS = {
    'deepseek': 'https://api.deepseek.com/v1',
    'kimi': 'https://api.moonshot.cn/v1',
    'gateway': 'https://ai-gateway.vercel.sh/v1',
    # QwenCloud has no safe default region; base_url resolves this explicitly.
    'qwen': None,
}
# Official QwenCloud pay-as-you-go regions, checked 2026-10-26:
# https://docs.qwencloud.com/developer-guides/getting-started/regions
QWEN_REGIONS = {
    'ap-southeast-1': '新加坡',
    'cn-beijing': '北京',
    'eu-central-1': '法兰克福',
    'cn-hongkong': '香港',
    'ap-northeast-1': '东京',
    'us-east-1': '弗吉尼亚',
}
MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z')
MAX_CONFIG_BYTES = 32 * 1024


class ConnectionError(Exception):
    """Safe configuration error; no credential or original exception echo."""


@dataclass(frozen=True)
class ProviderConnection:
    provider: str
    model: str
    api_key: str = field(repr=False)
    service_region: str = 'local-development'
    reasoning_effort: str = ''
    service_platform: str = ''

    def __post_init__(self):
        if self.provider not in ('', *ENDPOINTS):
            raise ConnectionError('请选择已支持的模型服务。')
        if not isinstance(self.model, str) or (self.model and not MODEL_ID.fullmatch(self.model)):
            raise ConnectionError('模型名称格式无效，请使用服务商模型列表返回的名称。')
        if (not isinstance(self.api_key, str) or len(self.api_key) > 4096
                or any(ord(char) < 33 or ord(char) > 126 for char in self.api_key)):
            raise ConnectionError('模型服务密钥格式无效。')
        if (not isinstance(self.service_region, str) or not self.service_region.strip()
                or len(self.service_region) > 80 or any(ord(char) < 32 for char in self.service_region)):
            raise ConnectionError('请填写有效的服务地区。')
        if not isinstance(self.service_platform, str):
            raise ConnectionError('请选择有效的模型平台。')
        if self.provider == 'qwen' and self.service_region not in QWEN_REGIONS:
            raise ConnectionError('请选择创建千问密钥时的平台区域 ID；不会自动猜测区域。')
        if self.provider == 'qwen' and self.service_platform not in ('', 'qwencloud', 'bailian'):
            raise ConnectionError('请选择千问密钥所属平台。')
        if self.provider != 'qwen' and self.service_platform:
            raise ConnectionError('此服务不支持千问平台设置。')
        if self.provider == 'qwen' and self.service_platform == 'bailian' and self.service_region != 'cn-beijing':
            raise ConnectionError('阿里云百炼当前仅支持已确认的华北2（北京）连接。')
        if self.provider == 'qwen' and self.api_key.startswith('sk-sp-'):
            raise ConnectionError('当前千问连接支持按量付费密钥，不支持 Token Plan 专用密钥。')
        if self.reasoning_effort not in ('', 'none', 'low', 'medium', 'high', 'max'):
            raise ConnectionError('推理强度设置无效。')

    @property
    def configured(self):
        return bool(self.provider and self.model and self.api_key and self.service_region)

    @property
    def base_url(self):
        if self.provider == 'qwen':
            if self.service_platform == 'bailian':
                return 'https://dashscope.aliyuncs.com/compatible-mode/v1'
            # Only an allowlisted region can affect the destination host.
            return f'https://{self.service_region}.maas.qwencloudapi.com/compatible-mode/v1'
        # Other providers retain their disclosure-only region labels.
        try:
            return ENDPOINTS[self.provider]
        except KeyError:
            raise ConnectionError('模型服务尚未配置。') from None

    @property
    def fingerprint(self):
        identity = {'version': CONFIG_VERSION, 'provider': self.provider, 'model': self.model,
                    'region': self.service_region, 'reasoning_effort': self.reasoning_effort,
                    'key_digest': hashlib.sha256(self.api_key.encode('utf-8')).hexdigest()}
        if self.provider == 'qwen' and self.service_platform == 'bailian':
            identity['service_platform'] = 'bailian'
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode('utf-8')).hexdigest()

    @property
    def provider_label(self):
        if self.provider == 'qwen':
            return '千问 · 阿里云百炼' if self.service_platform == 'bailian' else '千问 · QwenCloud'
        return {'deepseek': 'DeepSeek', 'kimi': 'Kimi', 'gateway': 'AI Gateway'}.get(self.provider, '')


def _cipher():
    try:
        return Fernet(settings.MFA_ENCRYPTION_KEY)
    except (AttributeError, ValueError, TypeError):
        raise ConnectionError('本地加密配置无效，请联系管理员修复。') from None


def _path():
    return Path(getattr(settings, 'PROVIDER_CONNECTION_PATH',
                        Path(settings.DATA_DIR) / 'provider-connection.json'))


def save_connection(connection):
    """Encrypt the entire record and atomically replace the local file."""
    _save_connection_at(connection, _path())


def _save_connection_at(connection, target):
    if not isinstance(connection, ProviderConnection) or not connection.configured:
        raise ConnectionError('请先填写模型服务、模型名称和密钥。')
    record = {'version': CONFIG_VERSION, 'provider': connection.provider, 'model': connection.model,
              'api_key': connection.api_key, 'service_region': connection.service_region,
              'reasoning_effort': connection.reasoning_effort, 'service_platform': connection.service_platform}
    encrypted = _cipher().encrypt(json.dumps(record, ensure_ascii=False).encode('utf-8'))
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='wb', dir=target.parent,
                                         prefix='.provider-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError:
        raise ConnectionError('无法保存模型连接，请检查本地数据目录权限。') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def load_connection():
    """Only read the local file; invalid records fail closed, never fall back."""
    return _load_connection_at(_path())


def _load_connection_at(target):
    try:
        with target.open('rb') as stream:
            encrypted = stream.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ConnectionError('无法读取本地模型连接。') from None
    if len(encrypted) > MAX_CONFIG_BYTES:
        raise ConnectionError('本地模型连接文件无效，请重新配置。')
    try:
        record = json.loads(_cipher().decrypt(encrypted))
        if not isinstance(record, dict) or record.pop('version', None) != CONFIG_VERSION:
            raise ValueError
        return ProviderConnection(**record)
    except (InvalidToken, ValueError, TypeError, KeyError, RecursionError):
        raise ConnectionError('本地模型连接无法解密或格式无效，请重新配置。') from None


def _named_path(provider):
    if not isinstance(provider, str) or provider not in ENDPOINTS:
        raise ConnectionError('请选择已支持的模型服务。')
    directory = Path(getattr(settings, 'NAMED_CONNECTIONS_DIR',
                             Path(settings.DATA_DIR) / 'provider-connections'))
    return directory / (provider + '.json')


def save_named_connection(connection):
    """Save one provider without selecting or changing the active connection."""
    if not isinstance(connection, ProviderConnection):
        raise ConnectionError('请先填写模型服务、模型名称和密钥。')
    _save_connection_at(connection, _named_path(connection.provider))


def load_named_connection(provider):
    """Read one allowlisted provider; missing named records have no fallback."""
    connection = _load_connection_at(_named_path(provider))
    if connection is not None and connection.provider != provider:
        raise ConnectionError('保存的模型服务与名称不匹配，请重新配置。')
    return connection


def get_connection():
    """Saved settings override explicit environment fallback; no model is chosen."""
    saved = load_connection()
    if saved is not None:
        return saved
    provider = getattr(settings, 'AI_PROVIDER', os.environ.get('ROUNDTABLE_AI_PROVIDER', ''))
    model = getattr(settings, 'AI_MODEL', os.environ.get('ROUNDTABLE_AI_MODEL', ''))
    gateway_key = getattr(settings, 'AI_GATEWAY_API_KEY', '')
    if not provider and (model or gateway_key):
        provider = 'gateway'
    key = {
        'deepseek': lambda: os.environ.get('DEEPSEEK_API_KEY', ''),
        'kimi': lambda: os.environ.get('KIMI_API_KEY', ''),
        'gateway': lambda: gateway_key,
        'qwen': lambda: os.environ.get('QWEN_API_KEY', ''),
    }.get(provider, lambda: '')()
    return ProviderConnection(
        provider, model, key, getattr(settings, 'SERVICE_REGION', 'local-development'),
        getattr(settings, 'AI_REASONING_EFFORT', os.environ.get('ROUNDTABLE_AI_REASONING_EFFORT', '')),
        getattr(settings, 'QWEN_PLATFORM', os.environ.get('ROUNDTABLE_QWEN_PLATFORM', '')) if provider == 'qwen' else '',
    )
