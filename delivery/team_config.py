"""Role routing for the local text team; reading configuration never calls a provider."""
import json
import os
from pathlib import Path
import tempfile

from django.conf import settings
from django.utils.crypto import salted_hmac

from .catalog import chat_available
from .connections import ConnectionError, get_connection, load_named_connection


ROLES = (
    ('proposer', '方案成员'),
    ('challenger', '独立提案成员'),
    ('reviewer', '交叉审查成员'),
    ('synthesizer', '综合交付成员'),
    ('verifier', '最终核验成员'),
)
SOURCES = ('current', 'deepseek', 'kimi', 'gateway', 'qwen')
FLOW_VERSION = 'team-decision-v1-group-chat-v10'
MAX_CALLS = 5
MODE_MAX_CALLS = {'text': 5, 'files': 7}
ADAPTIVE_MODE_MAX_CALLS = {'text': 8, 'files': 10}
DECISION_MAX_CALLS = 14
FILE_EXTRA_ROLES = (('repair', '修订成员'), ('recheck', '再次核验成员'))
MAX_OUTPUT_TOKENS = 4096
MAX_ROUTING_BYTES = 8192


def routing_path():
    return Path(getattr(settings, 'TEAM_ROUTING_PATH', Path(settings.DATA_DIR) / 'team-routing.json'))


def validate_routes(routes):
    if not isinstance(routes, dict) or set(routes) - {role for role, _ in ROLES}:
        raise ConnectionError('团队角色配置无效，请重新配置。')
    if any(not isinstance(source, str) or source not in SOURCES for source in routes.values()):
        raise ConnectionError('团队成员必须选择当前连接或已支持的命名连接。')
    return {role: routes.get(role, 'current') for role, _ in ROLES}


def load_routes():
    override = getattr(settings, 'TEAM_ROLE_CONNECTIONS', None)
    if override is not None:
        return validate_routes(override)
    try:
        with routing_path().open('rb') as stream:
            raw = stream.read(MAX_ROUTING_BYTES + 1)
    except FileNotFoundError:
        return validate_routes({})
    except OSError:
        raise ConnectionError('无法读取本地团队配置。') from None
    try:
        if len(raw) > MAX_ROUTING_BYTES:
            raise ValueError
        document = json.loads(raw)
        if not isinstance(document, dict) or set(document) != {'version', 'roles'} or document['version'] != 1:
            raise ValueError
        return validate_routes(document['roles'])
    except (ValueError, TypeError, RecursionError):
        raise ConnectionError('本地团队配置格式无效，请重新配置。') from None


def resolve_team(routes=None):
    routes = load_routes() if routes is None else validate_routes(routes)
    connections = {}
    result = {}
    for role, _ in ROLES:
        source = routes[role]
        if source not in connections:
            connection = get_connection() if source == 'current' else load_named_connection(source)
            if connection is None:
                raise ConnectionError(f'团队指定的 {source} 连接尚未保存，请先配置该服务。')
            connections[source] = connection
        result[role] = connections[source]
    return result


def team_revision(connections):
    if set(connections) != {role for role, _ in ROLES}:
        raise ConnectionError('团队成员配置不完整，请重新配置。')
    if not all(connection.configured for connection in connections.values()):
        return ''
    from .execution import policy_fingerprint
    identity = {'flow_version': FLOW_VERSION, 'max_calls': MAX_CALLS, 'delivery_modes': MODE_MAX_CALLS,
                'adaptive_modes': ADAPTIVE_MODE_MAX_CALLS, 'decision_max_calls': DECISION_MAX_CALLS,
                'execution_policy': policy_fingerprint(),
                'max_output_tokens': MAX_OUTPUT_TOKENS,
                'roles': {role: connections[role].fingerprint for role, _ in ROLES}}
    return salted_hmac('roundtable.public-team-version', json.dumps(identity, sort_keys=True),
                       algorithm='sha256').hexdigest()


def team_summary(connections=None):
    try:
        connections = resolve_team() if connections is None else connections
        revision = team_revision(connections)
        # Repeated roles on one connection share the same acceptance result.
        readiness = {}
        roles = []
        for role, label in ROLES:
            connection = connections[role]
            if connection.fingerprint not in readiness:
                readiness[connection.fingerprint] = chat_available(connection)
            roles.append({'role': role, 'label': label, 'provider': connection.provider, 'provider_label': connection.provider_label,
                          'model': connection.model, 'ready': readiness[connection.fingerprint]})
        configured = all(connection.configured for connection in connections.values())
        available = configured and all(item['ready'] for item in roles)
        return {'configured': configured,
                'readiness': 'available' if available else 'pending' if configured else 'unconfigured',
                'revision': revision, 'roles': roles, 'max_calls': MAX_CALLS,
                'delivery_modes': [{'id': key, 'max_calls': value} for key, value in MODE_MAX_CALLS.items()],
                'default_workflow': 'decision_v1', 'decision_max_calls': DECISION_MAX_CALLS,
                'adaptive_modes': [{'id': key, 'max_calls': value} for key, value in ADAPTIVE_MODE_MAX_CALLS.items()],
                'max_output_tokens': MAX_OUTPUT_TOKENS,
                'notice': ('文字讨论先确认问题，再独立分析、互相质疑并回应，必要时追加一轮；最多14次调用。多文件任务最多10次调用（包含一次修订）；每次最多4096 tokens，计入每小时20次额度。模型分工仍待实测优选。'
                           if available else '任务规划与分工流程已接入；参与模型仍须分别通过真实验收，未验收时不会启动。当前模型分工尚未经过效果比较。')}
    except ConnectionError as error:
        return {'configured': False, 'readiness': 'pending', 'revision': '',
                'delivery_modes': [{'id': key, 'max_calls': value} for key, value in MODE_MAX_CALLS.items()],
                'default_workflow': 'decision_v1', 'decision_max_calls': DECISION_MAX_CALLS,
                'adaptive_modes': [{'id': key, 'max_calls': value} for key, value in ADAPTIVE_MODE_MAX_CALLS.items()],
                'roles': [{'role': role, 'label': label, 'provider': '', 'model': '', 'ready': False}
                          for role, label in ROLES],
                'max_calls': MAX_CALLS, 'max_output_tokens': MAX_OUTPUT_TOKENS,
                'notice': str(error)}


def save_routes(routes):
    routes = validate_routes(routes)
    target = routing_path()
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix='.team-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'version': 1, 'roles': routes}, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError:
        raise ConnectionError('无法保存团队配置，请检查本地数据目录权限。') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return routes
