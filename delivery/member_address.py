"""Recognize a single addressee without treating every model name as a mention."""
import re

from .provider import ProviderError

_NAMES = {'通义千问': 'qwen', '千问': 'qwen', 'qwen': 'qwen', 'kimi': 'kimi',
          'deepseek': 'deepseek', '深度求索': 'deepseek',
          'ai gateway': 'gateway'}
_NAME = '|'.join(map(re.escape, _NAMES))
_START = re.compile(rf'^(?:那(?:么)?[，, ]*|请问[，, ]*|我想问(?:问)?[，, ]*)?'
                    rf'(?P<at>[@＠]?)\s*(?P<name>{_NAME})(?P<rest>.*)$', re.I | re.S)
_GROUP = re.compile(r'请(?:整个|所有)?团队|全(?:队|体)|大家(?:都|来|一起|怎么看|讨论|回答)|'
                    r'(?:你们|各位|所有成员|其他成员)(?:一起|都|来|分别|怎么看|讨论|回答)|'
                    r'一起(?:讨论|回答|比较|分析)|交叉(?:审查|核验)|\b(?:everyone|whole team)\b', re.I)
_DIRECT = re.compile(r'^(?:你(?:呢|好|们怎么看|觉得|怎么|认为|来|能|会|对|有|说|的(?:看法|意见))|'
                     r'您|请(?:问|帮|说|给|分析|比较|评价|回答)|怎么看|有(?:什么看法|补充吗)|能否|能不能|帮我|说说|谈谈|'
                     r'(?:what|how|please|can you|could you)\b)', re.I)


def addressed_provider(text):
    if not isinstance(text, str):
        return ''
    line = text.strip()
    match = _START.match(line)
    if not match or _GROUP.search(line):
        return ''
    rest = match['rest']
    # Addresses must be separate words. Avoid emails, URL parts and products
    # whose model family is only the start of their name.
    if rest and (rest[0].isascii() and (rest[0].isalnum() or rest[0] in '_.@/-')):
        return ''
    question = rest.strip(' \t，,：:！？!?。')
    if re.match(rf'^(?:和|与|、|以及|and\b|[@＠])\s*[@＠]?(?:{_NAME})', question, re.I):
        return ''
    if re.search(rf'[@＠](?:{_NAME})(?![a-z0-9_-])', rest, re.I):
        return ''
    if not match['at'] and question not in ('呢', '吗') and not _DIRECT.match(question):
        return ''
    return _NAMES[match['name'].lower()]


def member_connection(connections, provider, participants=None):
    if participants is None:
        choices = connections.values()
    else:
        choices = [connections[p['role']] for p in participants if p.get('role') in connections
                   and p.get('provider') == provider and connections[p['role']].model == p.get('model')]
    matches = {c.fingerprint: c for c in choices if c.configured and c.provider == provider}
    if not matches:
        raise ProviderError('指定成员不在本轮可用团队中。')
    if len(matches) != 1:
        raise ProviderError('该服务商对应多个模型，无法唯一确定成员。')
    return next(iter(matches.values()))
