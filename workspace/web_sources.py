"""Read bounded public HTTPS text without executing a page or using a model.

The destination is resolved once, checked in full, and pinned at the socket
layer. TLS still verifies the original hostname. Nothing here follows page
links, loads resources, uses environment proxies, or sends account cookies.
"""
import codecs
import hashlib
import http.client
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import certifi


MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SOURCE_CHARS = 12000
MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 15
_DNS_SLOTS = threading.BoundedSemaphore(4)
_DOMAIN_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
_BLOCKS = frozenset({'address', 'article', 'blockquote', 'br', 'dd', 'div', 'dl', 'dt',
                     'figcaption', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'li',
                     'main', 'ol', 'p', 'pre', 'section', 'table', 'tr', 'ul'})
_IGNORED = frozenset({'head', 'title', 'script', 'style', 'nav', 'footer', 'noscript',
                      'svg', 'form', 'template', 'iframe', 'canvas', 'object', 'embed',
                      'input', 'button', 'select', 'textarea', 'aside'})
_VOID = frozenset({'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
                   'meta', 'param', 'source', 'track', 'wbr'})
_CHARSETS = frozenset({'utf-8', 'utf-8-sig', 'gb18030', 'gbk', 'gb2312', 'big5',
                       'cp1252', 'iso8859-1', 'ascii'})


class SourceFetchError(Exception):
    """Safe product-facing error; never contains response bodies or URLs."""


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SourceFetchError('网页读取超时，请稍后重试。')
    return remaining


def _normalize_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise SourceFetchError('请填写不超过2048字符的公开HTTPS网页地址。')
    value = value.strip()
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) or '\\' in value:
        raise SourceFetchError('网页地址包含无效字符。')
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() != 'https' or not parts.netloc or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)):
            raise ValueError
        hostname = (parts.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
        if len(hostname) > 253 or '.' not in hostname or '%' in hostname:
            raise ValueError
        labels = hostname.split('.')
        if (any(not _DOMAIN_LABEL.fullmatch(label) for label in labels)
                or labels[-1].isdigit() or hostname.endswith(('.local', '.localhost', '.internal'))):
            raise ValueError
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError
        path = quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
        if re.search(r'%(?![a-fA-F0-9]{2})', path + query):
            raise ValueError
        result = urlunsplit(('https', hostname, path, query, ''))
        if len(result) > 2048:
            raise ValueError
        return result, hostname, path + ('?' + query if query else '')
    except (ValueError, UnicodeError):
        raise SourceFetchError('仅支持不含登录凭据、使用标准443端口的公开HTTPS域名地址。') from None


def _is_public_address(value):
    try:
        address = ipaddress.ip_address(value)
        if (not address.is_global or address.is_private or address.is_loopback
                or address.is_link_local or address.is_multicast or address.is_reserved
                or address.is_unspecified):
            return False
        if isinstance(address, ipaddress.IPv6Address):
            # Avoid address translation/tunnelling to a destination that was not
            # independently resolved and checked, even with a public prefix.
            if address.ipv4_mapped is not None or address.sixtofour is not None or address.teredo is not None:
                return False
            if address in ipaddress.ip_network('64:ff9b::/96') or address in ipaddress.ip_network('64:ff9b:1::/48'):
                return False
        return True
    except ValueError:
        return False


def _resolve_public(hostname, deadline):
    # getaddrinfo has no portable timeout. Bound waiting and the number of
    # outstanding OS resolver threads; a stalled resolver cannot accumulate
    # an unbounded thread per web request.
    if not _DNS_SLOTS.acquire(blocking=False):
        raise SourceFetchError('网页地址解析繁忙，请稍后重试。')
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put((True, socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)))
        except OSError:
            result.put((False, None))
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=resolve, daemon=True, name='roundtable-public-source-dns').start()
    try:
        success, records = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise SourceFetchError('网页地址解析超时，请稍后重试。') from None
    if not success or not records or len(records) > 32:
        raise SourceFetchError('无法解析该公开网页地址。')
    addresses = []
    for family, socktype, protocol, _, sockaddr in records:
        if (family not in (socket.AF_INET, socket.AF_INET6) or socktype != socket.SOCK_STREAM
                or not _is_public_address(sockaddr[0])
                or (family == socket.AF_INET6 and (len(sockaddr) != 4 or sockaddr[3] != 0))):
            raise SourceFetchError('该地址包含非公开网络目标，不能读取。')
        addresses.append((family, protocol, sockaddr))
    # Pin one validated address; no reconnect or DNS fallback after connection.
    return sorted(addresses, key=lambda entry: entry[0] != socket.AF_INET)[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, address, deadline):
        context = ssl.create_default_context()
        # Keep OS trust and hostname verification, and supply Mozilla's public
        # root bundle for Python runtimes with an incomplete local root store.
        context.load_verify_locations(cafile=certifi.where())
        context.set_alpn_protocols(['http/1.1'])
        super().__init__(hostname, port=443, timeout=_remaining(deadline), context=context)
        self._address = address
        self._deadline = deadline
        self._transport = None

    def connect(self):
        family, protocol, sockaddr = self._address
        raw = socket.socket(family, socket.SOCK_STREAM, protocol)
        self._transport = raw
        try:
            raw.settimeout(_remaining(self._deadline))
            raw.connect(sockaddr)
            raw.settimeout(_remaining(self._deadline))
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            self._transport = self.sock
        except BaseException:
            raw.close()
            raise

    def abort(self):
        transport = self._transport
        if transport is not None:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                transport.close()
            except OSError:
                pass


def _read_response(connection, target, deadline):
    # Socket timeouts alone are inactivity limits. The watchdog also stops a
    # peer that keeps sending tiny header/body chunks past the total deadline.
    watchdog = threading.Timer(_remaining(deadline), connection.abort)
    watchdog.daemon = True
    watchdog.start()
    try:
        connection.request('GET', target, headers={
            'User-Agent': 'Roundtable/0.8 (public text reader)',
            'Accept': 'text/html, text/plain', 'Accept-Encoding': 'identity', 'Connection': 'close',
        })
        response = connection.getresponse()
        try:
            _remaining(deadline)
            if response.status in (301, 302, 303, 307, 308):
                return response.status, response.getheader('Location', ''), None, b''
            if response.status != 200:
                raise SourceFetchError('网页未返回可读取的正文，请确认链接可以公开访问。')
            content_type = Message()
            content_type['content-type'] = response.getheader('Content-Type', '')
            media_type = content_type.get_content_type()
            if media_type not in ('text/html', 'text/plain') or not response.getheader('Content-Type'):
                raise SourceFetchError('此链接不是支持的HTML或纯文字网页。')
            if response.getheader('Content-Encoding', '').strip().lower() not in ('', 'identity'):
                raise SourceFetchError('网页返回了压缩内容，本轮无法读取；请使用可直接读取的文字页面。')
            if response.getheader('Transfer-Encoding', '').strip().lower() not in ('', 'chunked'):
                raise SourceFetchError('网页传输格式暂不支持。')
            declared_length = response.getheader('Content-Length')
            if declared_length is not None and (not declared_length.isdigit()
                    or int(declared_length) > MAX_RESPONSE_BYTES):
                raise SourceFetchError('网页超过1MiB读取上限或长度无效，请选择较短页面。')
            chunks = []
            total = 0
            while True:
                if connection.sock is not None:
                    connection.sock.settimeout(_remaining(deadline))
                else:
                    _remaining(deadline)
                chunk = response.read1(min(16384, MAX_RESPONSE_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise SourceFetchError('网页超过1MiB读取上限，请选择较短页面。')
                chunks.append(chunk)
            _remaining(deadline)
            raw = b''.join(chunks)
            if declared_length is not None and len(raw) != int(declared_length):
                raise SourceFetchError('网页正文传输不完整，请稍后重试。')
            return response.status, '', content_type, raw
        finally:
            response.close()
    finally:
        watchdog.cancel()
        connection.close()


class _TextHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.body = []
        self.title = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        ignored = bool(self.stack and self.stack[-1][1]) or tag in _IGNORED
        ignored = ignored or 'hidden' in attrs or str(attrs.get('aria-hidden', '')).lower() == 'true'
        if not ignored and tag in _BLOCKS:
            self.body.append('\n')
        if tag not in _VOID:
            if len(self.stack) >= 128:
                raise SourceFetchError('网页结构过于复杂，无法可靠提取文字。')
            self.stack.append((tag, ignored))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        found = next((index for index in range(len(self.stack) - 1, -1, -1)
                      if self.stack[index][0] == tag), None)
        if found is not None:
            ignored = self.stack[found][1]
            del self.stack[found:]
            if not ignored and tag in _BLOCKS:
                self.body.append('\n')

    def handle_data(self, data):
        text = re.sub(r'\s+', ' ', data)
        if self.stack and self.stack[-1][0] == 'title':
            if sum(map(len, self.title)) < 200:
                self.title.append(text[:200])
        elif not self.stack or not self.stack[-1][1]:
            self.body.append(text)


def _decode(raw, content_type):
    charset = content_type.get_content_charset()
    if not charset and content_type.get_content_type() == 'text/html':
        match = re.search(br'<meta\b[^>]{0,300}\bcharset\s*=\s*[\"\x27]?([a-zA-Z0-9_-]{1,30})', raw[:4096], re.I)
        charset = match.group(1).decode('ascii') if match else None
    try:
        encoding = codecs.lookup(charset or 'utf-8-sig').name
        if encoding not in _CHARSETS:
            raise ValueError
        text = raw.decode(encoding)
        if any(ord(char) < 32 and char not in '\n\r\t' for char in text):
            raise ValueError
        return text
    except (LookupError, UnicodeError, ValueError):
        raise SourceFetchError('网页文字编码不支持或正文无效，未保存不完整内容。') from None


def fetch_public_source(url, *, timeout=FETCH_TIMEOUT_SECONDS):
    """Return source text and provenance, never a fact-check or model output."""
    deadline = time.monotonic() + min(timeout, FETCH_TIMEOUT_SECONDS)
    current, _, _ = _normalize_url(url)
    seen = set()
    try:
        for hop in range(MAX_REDIRECTS + 1):
            current, hostname, target = _normalize_url(current)
            if current in seen:
                raise SourceFetchError('网页重定向形成循环，已停止读取。')
            seen.add(current)
            address = _resolve_public(hostname, deadline)
            connection = _PinnedHTTPSConnection(hostname, address, deadline)
            status, location, content_type, raw = _read_response(connection, target, deadline)
            if status in (301, 302, 303, 307, 308):
                if not location or hop == MAX_REDIRECTS:
                    raise SourceFetchError('网页重定向过多或目标无效，已停止读取。')
                current, _, _ = _normalize_url(urljoin(current, location))
                continue
            decoded = _decode(raw, content_type)
            name = hostname
            if content_type.get_content_type() == 'text/html':
                parser = _TextHTMLParser()
                parser.feed(decoded)
                parser.close()
                content = '\n'.join(line.strip() for line in ''.join(parser.body).splitlines() if line.strip())
                title = re.sub(r'\s+', ' ', ''.join(parser.title)).strip()
                name = title[:200] or hostname
            else:
                content = decoded.replace('\r\n', '\n').replace('\r', '\n').strip()
            if not content:
                raise SourceFetchError('页面没有可提取的正文；本轮不执行网页脚本，也不进行图片识别。')
            if len(content) > MAX_SOURCE_CHARS:
                raise SourceFetchError('网页正文超过12000字符，请选择较短页面；未截断或保存正文。')
            _remaining(deadline)
            return {
                'name': name, 'content': content, 'media_type': content_type.get_content_type(),
                'source_url': current, 'byte_size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                'metadata': {
                    'host': hostname, 'extracted_at': datetime.now(timezone.utc).isoformat(),
                    'redirects': hop, 'char_count': len(content), 'extraction': 'public_https_text',
                    'notice': '仅提取网页文字；未执行页面脚本，也未核验网页陈述。',
                },
            }
    except SourceFetchError:
        raise
    except (OSError, http.client.HTTPException, ValueError, UnicodeError, RecursionError):
        raise SourceFetchError('公开网页读取失败或超时；未自动重试，也未保存不完整内容。') from None
