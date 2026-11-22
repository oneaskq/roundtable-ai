"""Bounded public retrieval using short, validated keywords."""
import hashlib
import http.client
import json
import os
import queue
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from workspace.web_sources import SourceFetchError, fetch_public_source
from .provider import NoRedirect

TIMEOUT = 15
MAX_RESULTS = 2
MAX_EXCERPT = 1800
_SLOTS = threading.BoundedSemaphore(3)
_PRIVATE = re.compile(r'(?i)https?://|[\w.+-]+@[\w.-]+\.\w+|(?<!\d)1[3-9]\d[ -]?\d{4}[ -]?\d{4}(?!\d)|'
                      r'(?<!\d)\d{13,19}X?(?!\d)|(?:sk-|gh[pousr]_|github_pat_|(?-i:AKIA|ASIA|LTAI))[\w-]{12,}|BEGIN .*PRIVATE KEY|'
                      r'(?:[a-z]:[\\/]|\\\\[^\s\\]+\\|/(?:home|Users)/)\S+|'
                      r'(?:手机号|学号|身份证号|家庭住址|微信号|电子邮箱|银行卡号|我的名字|我叫)\s*[:：是为]?\s*\S+|'
                      r'(?:api[_ -]?key|access[_ -]?token|password|密码|密钥)\s*[:=：]\s*\S+')

_DECLINED = re.compile(r'(?:别|不要|不用|无需|不必|禁止|不允许|不希望|不想)\s*(?:再|额外|主动)?'
                       r'(?:联网|搜索|检索|查证|上网|查资料|查了)|(?:只|仅)(?:用|依据|根据)(?:我给的|已有|现有|所给|本轮)(?:资料|材料)'
                       r'|\b(?:do not|don[\'’]t|no need to|never)\s+(?:search|browse|look up)\b', re.I)


def declines_public_search(text):
    return bool(_DECLINED.search(text))


def suggested_query(draft):
    """Check a model suggestion again when resuming an older stored draft."""
    try:
        return approved_query(draft.get('public_search_query'), True)
    except ValueError:
        return ''


def set_automatic_search(state, draft, query):
    if not (draft.get('public_search_query') or draft.get('public_search_requested')):
        return
    provider = draft.get('public_search_provider')
    if query:
        state['public_search_query'] = query
        state['public_search_provider'] = provider
    else:
        state['public_research'] = {'provider': provider or '', 'status': 'invalid_query', 'sources': []}


def approved_query(value, consent):
    if value is None or value == '':
        return ''
    if not isinstance(value, str) or type(consent) is not bool or not consent:
        raise ValueError('公开检索前请确认将发送的关键词。')
    if len(value) > 160 or any(not c.isprintable() for c in value) or _PRIVATE.search(unicodedata.normalize('NFKC', value)):
        raise ValueError('请使用最多 160 字的公开关键词，移除网址、联系方式及密钥。')
    return value.strip()


def search_provider_name():
    return 'Brave Search' if os.environ.get('BRAVE_SEARCH_API_KEY', '').strip() else '维基百科'


def _get_json(url, headers, deadline):
    # Only fixed service origins are constructed by the two callers. Never
    # forward an API credential or a query to a redirect destination.
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    request = urllib.request.Request(url, headers={
        'User-Agent': 'Roundtable/1.0 (https://github.com/oneaskq/roundtable-ai)',
        'Accept': 'application/json', **headers})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=min(6, remaining)) as response:
        raw = response.read(192 * 1024 + 1)
        if len(raw) > 192 * 1024 or response.headers.get_content_type() != 'application/json':
            raise ValueError('invalid API response')
    data = json.loads(raw)
    if not isinstance(data, dict) or 'error' in data:
        raise ValueError('invalid API response')
    return data


def _source(index, name, url, content, retrieved_at):
    excerpt = content.strip()[:MAX_EXCERPT]
    return {'reference': f'W{index}', 'kind': 'web', 'name': name[:180], 'url': url,
            'content': excerpt, 'sha256': hashlib.sha256(excerpt.encode()).hexdigest(),
            'retrieved_at': retrieved_at}


def _wiki(query, deadline):
    language = 'zh' if re.search(r'[\u3400-\u9fff]', query) else 'en'
    host = f'https://{language}.wikipedia.org'
    params = {'action': 'query', 'format': 'json', 'formatversion': 2, 'generator': 'search',
              'gsrsearch': query, 'gsrlimit': MAX_RESULTS, 'gsrnamespace': 0,
              'prop': 'extracts|info', 'exintro': 1, 'explaintext': 1, 'exlimit': MAX_RESULTS, 'exchars': MAX_EXCERPT}
    data = _get_json(host + '/w/api.php?' + urlencode(params), {}, deadline)
    pages = data.get('query', {}).get('pages', [])
    sources = []
    for page in sorted(pages, key=lambda item: item.get('index', 0))[:MAX_RESULTS]:
        title, content = page.get('title'), page.get('extract')
        if not isinstance(title, str) or not isinstance(content, str) or not content.strip():
            continue
        url = host + '/wiki/' + quote(title.replace(' ', '_'), safe='')
        revision = page.get('lastrevid')
        if type(revision) is int and revision > 0:
            url += '?' + urlencode({'oldid': revision})
        item = _source(len(sources) + 1, title, url, content, datetime.now(timezone.utc).isoformat())
        item['attribution'] = '维基百科条目作者 · CC BY-SA 4.0'
        sources.append(item)
    return sources, False


def _brave(query, deadline, key):
    params = urlencode({'q': query, 'count': MAX_RESULTS})
    data = _get_json('https://api.search.brave.com/res/v1/web/search?' + params,
                     {'X-Subscription-Token': key}, deadline)
    sources = []
    unreadable = False
    seen = set()
    for item in data.get('web', {}).get('results', [])[:MAX_RESULTS]:
        url = item.get('url')
        if not isinstance(url, str) or url in seen:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return sources, True
        seen.add(url)
        try:
            page = fetch_public_source(url, timeout=min(5, remaining))
        except SourceFetchError:
            unreadable = True
            continue
        if any(s['url'] == page['source_url'] for s in sources):
            continue
        sources.append(_source(len(sources) + 1, page['name'], page['source_url'], page['content'],
                               page['metadata']['extracted_at']))
    return sources, unreadable


def lookup_public_sources(query, approved_provider):
    """The deadline includes DNS, connection, search and reading result pages."""
    key = os.environ.get('BRAVE_SEARCH_API_KEY', '').strip()
    info = {'provider': 'Brave Search' if key else '维基百科', 'status': 'unavailable',
            'requested_at': datetime.now(timezone.utc).isoformat(), 'sources': []}
    if approved_provider != info['provider']:
        info['status'] = 'provider_changed'
        return info, []
    try:
        query = approved_query(query, True)
    except ValueError:
        query = ''
    if not query:
        info['status'] = 'invalid_query'
        return info, []
    if not _SLOTS.acquire(blocking=False):
        return info, []
    result = queue.Queue(maxsize=1)
    deadline = time.monotonic() + TIMEOUT

    def retrieve():
        try:
            sources, unreadable = _brave(query, deadline, key) if key else _wiki(query, deadline)
            status = ('partial' if unreadable else 'found') if sources else ('unreadable' if unreadable else 'empty')
            result.put((status, sources))
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError):
            result.put(('unavailable', []))
        finally:
            _SLOTS.release()

    threading.Thread(target=retrieve, daemon=True, name='roundtable-public-search').start()
    try:
        info['status'], sources = result.get(timeout=TIMEOUT)
    except queue.Empty:
        return info, []
    info['sources'] = [{k: s[k] for k in ('name', 'url', 'retrieved_at')} for s in sources]
    return info, sources


def research_note(info):
    if not info:
        return '仅使用本轮对话和选定资料，未联网搜索。'
    provider = info['provider']
    count = len(info['sources'])
    if info['status'] in ('found', 'partial'):
        note = f'通过{provider}读取了 {count} 个公开来源。'
        if info['status'] == 'partial':
            note += '部分结果正文无法读取。'
        if provider == '维基百科':
            note += '范围限于百科背景；最新价格、招聘条件等仍需核对发布方原文。'
        return note
    if info['status'] == 'empty':
        return f'{provider}没有返回可用资料，本轮建议缺少本次检索的外部依据。'
    if info['status'] == 'unreadable':
        return f'{provider}返回了搜索结果，但未能读取网页正文，搜索摘要未用作证据。'
    if info['status'] == 'provider_changed':
        return '公开检索服务已变更，本轮没有发送关键词。'
    if info['status'] == 'invalid_query':
        return '缺少适合公开的检索关键词，本轮没有发送检索请求。'
    return f'{provider}检索未完成，本轮仅使用对话和已选资料。'
