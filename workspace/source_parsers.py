"""Bounded local text extraction. Original file bytes are never stored."""
import csv
import hashlib
import io
import json
import stat
import zipfile
import zlib
from pathlib import PurePosixPath
from xml.etree import ElementTree

from django.core.files.uploadhandler import MemoryFileUploadHandler, StopUpload

from .sources import MAX_SOURCE_BYTES, MAX_SOURCE_CHARS, SUPPORTED_EXTENSIONS


class SourceParseError(Exception):
    pass


class BoundedMemoryUploadHandler(MemoryFileUploadHandler):
    """Keep file bytes off disk, including rejected oversized multipart uploads."""
    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        self.activated = True
        self.total_received = 0

    def receive_data_chunk(self, raw_data, start):
        self.total_received += len(raw_data)
        if self.total_received > MAX_SOURCE_BYTES:
            self.request.source_upload_too_large = True
            self.file.close()
            raise StopUpload(connection_reset=True)
        return super().receive_data_chunk(raw_data, start)


def _safe_name(name):
    # Treat a filename as a display label, never as a local path.
    name = str(name or '').replace('\\', '/').rsplit('/', 1)[-1]
    if (not name or len(name) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise SourceParseError('文件名无效或超过200字符。')
    return name


def _bounded_text(text):
    if not text.strip():
        raise SourceParseError('没有提取到可用正文，请换一份清晰的材料，或粘贴文字。')
    if len(text) > MAX_SOURCE_CHARS:
        raise SourceParseError('正文超过12000字符，请拆分后上传；未截断或保存原文。')
    if any(ord(char) < 32 and char not in '\n\r\t' for char in text) or '\x7f' in text:
        raise SourceParseError('文件包含非文字控制字符，请转换为UTF-8文本后上传。')
    try:
        text.encode('utf-8')
    except UnicodeError:
        raise SourceParseError('文件含有无效文字编码。') from None
    return text


def _json_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError('duplicate key')
        data[key] = value
    return data


def _invalid_number(value):
    raise ValueError('non-finite JSON')


def _xml(raw):
    # Office XML is UTF-8/UTF-16. Null-stripping detects entity declarations in
    # either encoding before the parser can expand an entity or load a DTD.
    declarations = raw.replace(b'\x00', b'').upper()
    if b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:
        raise SourceParseError('不支持包含DTD或实体声明的Word文件。')
    try:
        return ElementTree.fromstring(raw)
    except (ElementTree.ParseError, ValueError, RecursionError):
        raise SourceParseError('Word正文结构无效，请重新导出为DOCX。') from None


def _docx(raw):
    word = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    content_types = '{http://schemas.openxmlformats.org/package/2006/content-types}'
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > 100:
                raise SourceParseError('Word文件结构过大，请拆分后上传。')
            names = set()
            expanded = 0
            for item in entries:
                path = PurePosixPath(item.filename)
                folded = item.filename.casefold()
                if (not item.filename or '\\' in item.filename or path.is_absolute()
                        or any(part in ('..', '.') for part in item.filename.split('/'))
                        or ':' in item.filename or folded in names
                        or stat.S_ISLNK(item.external_attr >> 16)):
                    raise SourceParseError('Word文件包含不支持的内部路径。')
                names.add(folded)
                if item.flag_bits & 1:
                    raise SourceParseError('不支持加密的Word文件。')
                if ('vbaproject' in folded or folded.startswith(('word/activex/', 'word/embeddings/'))
                        or folded.endswith(('.exe', '.dll', '.bin'))):
                    raise SourceParseError('不支持含宏、活动内容或嵌入对象的Word文件。')
                expanded += item.file_size
                if (item.file_size > 2 * 1024 * 1024 or expanded > 8 * 1024 * 1024
                        or item.file_size > max(item.compress_size, 1) * 200):
                    raise SourceParseError('Word文件解压大小或压缩比例超过限制。')
                if item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise SourceParseError('Word文件采用不支持的压缩格式。')
            if '[content_types].xml' not in names or 'word/document.xml' not in names:
                raise SourceParseError('文件不是支持的DOCX正文格式。')
            document = None
            valid_type = False
            for item in entries:
                if item.filename.endswith(('.xml', '.rels')):
                    with archive.open(item) as stream:
                        member = stream.read(2 * 1024 * 1024 + 1)
                    if len(member) > 2 * 1024 * 1024:
                        raise SourceParseError('Word文件正文结构超过限制。')
                    root = _xml(member)
                    if item.filename == '[Content_Types].xml':
                        valid_type = any(node.tag == content_types + 'Override'
                            and node.get('PartName') == '/word/document.xml'
                            and node.get('ContentType') == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
                            for node in root)
                        if any('macro' in value.lower() for node in root for value in node.attrib.values()):
                            raise SourceParseError('不支持带宏的Word文件。')
                    if item.filename == 'word/document.xml':
                        document = root
            if not valid_type or document is None or document.tag != word + 'document':
                raise SourceParseError('Word正文格式不受支持。')
            paragraphs = []
            length = 0
            for paragraph in document.iter(word + 'p'):
                pieces = []
                for node in paragraph.iter():
                    if node.tag == word + 't':
                        pieces.append(node.text or '')
                    elif node.tag == word + 'tab':
                        pieces.append('\t')
                    elif node.tag in (word + 'br', word + 'cr'):
                        pieces.append('\n')
                text = ''.join(pieces)
                length += len(text) + 1
                if length > MAX_SOURCE_CHARS + 1:
                    raise SourceParseError('Word正文超过12000字符，请拆分后上传。')
                paragraphs.append(text)
            return '\n'.join(paragraphs), len(paragraphs)
    except SourceParseError:
        raise
    except (zipfile.BadZipFile, RuntimeError, ValueError, OSError, EOFError, KeyError, zlib.error):
        raise SourceParseError('Word文件损坏或格式不受支持。') from None


def parse_uploaded_source(uploaded):
    name = _safe_name(uploaded.name)
    extension = PurePosixPath(name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise SourceParseError('支持 TXT、MD、CSV、JSON、DOCX、PDF、PNG 和 JPG，也可以直接粘贴文字。')
    if uploaded.size > MAX_SOURCE_BYTES:
        raise SourceParseError('文件超过2MiB，请拆分后上传。')
    raw = uploaded.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise SourceParseError('文件超过2MiB，请拆分后上传。')
    media = {'.txt': 'text/plain', '.md': 'text/markdown', '.csv': 'text/csv',
             '.json': 'application/json', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
             '.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}[extension]
    metadata = {'extraction': 'local', 'format': extension[1:], 'original_stored': False}
    if extension in ('.pdf', '.png', '.jpg', '.jpeg'):
        from .visual_sources import extract_document
        text, details = extract_document(raw, extension)
        metadata.update(details)
    elif extension == '.docx':
        text, count = _docx(raw)
        metadata.update({'paragraph_count': count,
            'scope': '仅提取正文段落和表格中的文字；不读取图片、页眉页脚、批注、脚注或外部链接内容。'})
    else:
        try:
            text = raw.decode('utf-8-sig', errors='strict')
        except UnicodeDecodeError:
            raise SourceParseError('请将文字文件保存为UTF-8编码后上传。') from None
        _bounded_text(text)
        if extension == '.json':
            try:
                json.loads(text, object_pairs_hook=_json_object, parse_constant=_invalid_number)
            except (ValueError, RecursionError):
                raise SourceParseError('JSON格式无效、字段重复或嵌套过深。') from None
        elif extension == '.csv':
            try:
                rows = list(csv.reader(io.StringIO(text, newline=''), strict=True))
                if not rows or any(len(row) != len(rows[0]) for row in rows):
                    raise ValueError
                metadata.update({'row_count': len(rows), 'column_count': len(rows[0])})
            except (csv.Error, ValueError):
                raise SourceParseError('CSV格式无效或各行列数不一致。') from None
    text = _bounded_text(text)
    return {'name': name, 'content': text, 'media_type': media, 'source_url': '',
            'byte_size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'metadata': metadata}
