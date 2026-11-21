"""Shared source limits and public metadata; never fetch or invoke models."""
import hashlib
import json


MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_CHARS = 12000
MAX_TOTAL_SOURCE_CHARS = 16000
MAX_SOURCES = 8
SUPPORTED_EXTENSIONS = ('.txt', '.md', '.csv', '.json', '.docx', '.pdf', '.png', '.jpg', '.jpeg')


def source_limits():
    return {'max_files': MAX_SOURCES, 'max_bytes': MAX_SOURCE_BYTES,
            'max_chars': MAX_SOURCE_CHARS, 'max_total_chars': MAX_TOTAL_SOURCE_CHARS,
            'supported_extensions': list(SUPPORTED_EXTENSIONS)}


def sources_revision(sources):
    """All selections participate, including an explicitly excluded last source."""
    rows = sorted((str(item.id), item.sha256, bool(item.included)) for item in sources)
    return hashlib.sha256(json.dumps(rows, separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()


def serialize_source(source, include_content=False):
    data = {'id': str(source.id), 'conversation_id': str(source.conversation_id),
            'kind': source.kind, 'name': source.name, 'media_type': source.media_type,
            'source_url': source.source_url, 'byte_size': source.byte_size,
            'char_count': len(source.content), 'sha256': source.sha256,
            'included': source.included, 'metadata': source.metadata,
            'created_at': source.created_at.isoformat(), 'updated_at': source.updated_at.isoformat()}
    if include_content:
        data['content'] = source.content
    return data
