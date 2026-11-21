"""Bounded text-file bundles and deterministic checks, with no code execution.

All names are archive-relative and safe on Windows as well as POSIX. The module
never writes to disk, imports generated modules, executes code, or contacts a
network service. A successful report means only the listed static checks passed.
"""
import ast
import csv
from html.parser import HTMLParser
import io
import json
import unicodedata
import zipfile


MAX_FILES = 12
MAX_FILE_CHARS = 30000
MAX_TOTAL_CHARS = 100000
MAX_SUMMARY_CHARS = 2000
MAX_PATH_CHARS = 240
MAX_RAW_CHARS = 700000
MAX_ZIP_BYTES = 500000
ALLOWED_EXTENSIONS = frozenset({'.md', '.txt', '.csv', '.json', '.html', '.css', '.js', '.py'})
WINDOWS_RESERVED = frozenset({'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} |
                             {f'{prefix}{number}' for prefix in ('COM', 'LPT') for number in range(1, 10)})
CHECK_SCOPE = ('静态检查阶段仅检查文件路径、UTF-8 文本格式和明确列出的静态解析项目；'
               '本阶段不执行生成的代码、计算表格公式、进行浏览器渲染、联网、安装依赖或功能测试。另有运行验收时，以对应记录为准。')


class BundleError(ValueError):
    """Safe, user-readable rejection of an invalid bundle."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('non-finite JSON number')


def _json_loads(raw):
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _utf8(value):
    try:
        return value.encode('utf-8')
    except UnicodeError:
        raise BundleError('成果文本包含无效字符，请重新生成。') from None


def _safe_path(path):
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
        raise BundleError('成果文件路径为空或超过长度限制。')
    _utf8(path)
    if ('\\' in path or path.startswith('/') or any(character in '<>:"|?*' for character in path)
            or any(unicodedata.category(character) in {'Cc', 'Cf', 'Cs'} for character in path)):
        raise BundleError('成果文件路径必须是安全的相对路径。')
    segments = path.split('/')
    normalized = []
    for segment in segments:
        # Hidden directories and dotfiles are not needed in the supported output
        # formats and could become configuration or credential files on extraction.
        if (not segment or segment in {'.', '..'} or segment.startswith('.')
                or segment != segment.strip() or segment.endswith('.')):
            raise BundleError('成果文件路径包含不允许的目录或文件名。')
        comparable = unicodedata.normalize('NFKC', segment)
        if (comparable in {'.', '..'} or comparable.startswith('.') or comparable.endswith(('.', ' '))
                or '/' in comparable or '\\' in comparable
                or any(character in '<>:"|?*' for character in comparable)):
            raise BundleError('成果文件路径包含不允许的目录或文件名。')
        stem = comparable.split('.', 1)[0].rstrip(' .').upper()
        if stem in WINDOWS_RESERVED:
            raise BundleError('成果文件名不能使用系统保留名称。')
        normalized.append(comparable.casefold())
    extension = '.' + segments[-1].rsplit('.', 1)[-1].lower() if '.' in segments[-1] else ''
    if extension not in ALLOWED_EXTENSIONS:
        raise BundleError('成果只支持 Markdown、纯文本、CSV、JSON、HTML、CSS、JavaScript 和 Python 源文件。')
    return '/'.join(normalized)


def validate_bundle(bundle):
    """Return an independent, strictly validated public bundle without truncation."""
    if not isinstance(bundle, dict) or set(bundle) != {'summary', 'files'}:
        raise BundleError('成果必须包含 summary 和 files，不能包含其他字段。')
    summary, files = bundle['summary'], bundle['files']
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY_CHARS:
        raise BundleError('成果说明不能为空，且不能超过 2000 字符。')
    _utf8(summary)
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise BundleError('每份成果须包含 1 至 12 个文件。')
    result, seen, total = [], set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'content'}:
            raise BundleError('每个成果文件必须只包含 path 和 content。')
        key = _safe_path(item['path'])
        content = item['content']
        if not isinstance(content, str) or len(content) > MAX_FILE_CHARS:
            raise BundleError('每个成果文件必须是文本，且不能超过 30000 字符。')
        _utf8(content)
        total += len(content)
        if total > MAX_TOTAL_CHARS:
            raise BundleError('本轮成果文件总内容不能超过 100000 字符。')
        if key in seen:
            raise BundleError('成果文件路径重复，包括仅大小写或兼容字符不同的名称。')
        if any(key.startswith(prior + '/') or prior.startswith(key + '/') for prior in seen):
            raise BundleError('成果文件路径存在文件与目录冲突。')
        seen.add(key)
        result.append({'path': item['path'], 'content': content})
    return {'summary': summary, 'files': result}


def parse_bundle(raw):
    """Read a strict JSON response, rejecting duplicate keys and unsafe paths."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_RAW_CHARS:
        raise BundleError('成果 JSON 为空或超过允许大小。')
    _utf8(raw)
    try:
        bundle = _json_loads(raw)
    except (ValueError, TypeError, RecursionError):
        raise BundleError('成果 JSON 格式无效或包含重复字段，请重新生成。') from None
    return validate_bundle(bundle)


class _BasicHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = 0

    def handle_starttag(self, tag, attrs):
        self.tags += 1

    def handle_startendtag(self, tag, attrs):
        self.tags += 1


def check_bundle(bundle):
    """Check text formats without executing model-produced code or formulas."""
    bundle = validate_bundle(bundle)
    checks = []

    def add(path, check, status, detail):
        checks.append({'path': path, 'check': check, 'status': status, 'detail': detail})

    for item in bundle['files']:
        path, content = item['path'], item['content']
        extension = '.' + path.rsplit('.', 1)[-1].lower()
        add(path, '路径与文本格式', 'passed', '安全相对路径，内容可编码为 UTF-8。')
        if extension == '.py':
            try:
                tree = ast.parse(content, filename=path, mode='exec')
                # Compilation checks statement placement too (e.g. a top-level
                # return). It creates a code object but does not execute it.
                compile(tree, path, 'exec', dont_inherit=True, optimize=0)
            except (SyntaxError, ValueError, RecursionError, MemoryError) as error:
                line = getattr(error, 'lineno', None)
                add(path, 'Python 静态语法', 'failed',
                    f'第 {line} 行未通过 Python 语法检查。' if line else '未通过 Python 静态语法检查。')
            else:
                add(path, 'Python 静态语法', 'passed', 'AST 解析与静态编译通过；本项检查未执行代码。')
            add(path, 'Python 运行与测试', 'not_run', '本阶段未安装依赖或运行程序；如另有运行验收，以对应记录为准。')
        elif extension == '.json':
            try:
                _json_loads(content)
            except (ValueError, TypeError, RecursionError):
                add(path, 'JSON 解析', 'failed', 'JSON 格式无效、字段重复或含非标准数值。')
            else:
                add(path, 'JSON 解析', 'passed', 'JSON 解析通过，未发现重复字段或非标准数值。')
        elif extension == '.csv':
            try:
                rows = [row for row in csv.reader(io.StringIO(content, newline=''), strict=True) if row]
                if not rows or any(len(row) != len(rows[0]) for row in rows):
                    raise ValueError()
            except (csv.Error, ValueError):
                add(path, 'CSV 读取与列数', 'failed', 'CSV 为空、引号格式无效或各行列数不一致。')
            else:
                add(path, 'CSV 读取与列数', 'passed', f'已读取 {len(rows)} 行、每行 {len(rows[0])} 列；未计算公式。')
        elif extension == '.html':
            parser = _BasicHTMLParser()
            try:
                parser.feed(content)
                parser.close()
                if not parser.tags:
                    raise ValueError()
            except (ValueError, RecursionError):
                add(path, 'HTML 基本解析', 'failed', '未能读取有效的 HTML 起始标签。')
            else:
                add(path, 'HTML 基本解析', 'passed', f'已读取 {parser.tags} 个起始标签；此检查不验证标签闭合或页面效果。')
            add(path, '浏览器运行与渲染', 'not_run', '没有执行页面脚本、加载资源或检查视觉效果。')
        elif extension in {'.js', '.css'}:
            add(path, '源码语法与功能', 'not_run', '目前只验证文本与路径，尚未实施此语言的语法或运行检查。')
    return {'passed': not any(item['status'] == 'failed' for item in checks),
            'checks': checks, 'scope': CHECK_SCOPE}


def zip_bundle(bundle):
    """Create a reproducible ZIP entirely in memory, without extra hidden files."""
    bundle = validate_bundle(bundle)
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode='w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for item in sorted(bundle['files'], key=lambda entry: entry['path'].casefold()):
            info = zipfile.ZipInfo(item['path'], date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, _utf8(item['content']))
    raw = output.getvalue()
    if len(raw) > MAX_ZIP_BYTES:
        raise BundleError('成果压缩包超过允许大小。')
    return raw
