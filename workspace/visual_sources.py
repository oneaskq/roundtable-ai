"""Extract PDF and screenshot text locally in a time-limited child process."""
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading


_slots = threading.BoundedSemaphore(2)


def extract_document(raw, extension):
    from .source_parsers import SourceParseError
    from .sources import MAX_SOURCE_CHARS
    if not _slots.acquire(timeout=1):
        raise SourceParseError('其他材料正在读取，请稍后重试。')
    try:
        proc = subprocess.run(
            [sys.executable, '-I', '-X', 'utf8', str(Path(__file__).resolve()), extension, str(MAX_SOURCE_CHARS)],
            input=raw, capture_output=True, timeout=45, check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if proc.returncode:
            raise SourceParseError('未能读取这份材料，请换一份文件或粘贴文字。')
        payload = json.loads(proc.stdout)
        if 'error' in payload:
            raise SourceParseError(payload['error'])
        return payload['text'], payload['metadata']
    except subprocess.TimeoutExpired:
        raise SourceParseError('材料读取超时，请分成较短的文件或粘贴文字。') from None
    finally:
        _slots.release()


class DocumentError(Exception):
    pass


def _recognize(image):
    # Both supported OCR adapters read only local image bytes from stdin.
    if os.name == 'nt':
        engine = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        # Use a fixed, source-controlled command. User bytes go only to stdin.
        script = Path(__file__).with_name('windows_ocr.ps1').read_text(encoding='utf-8')
        command = [str(engine), '-NoProfile', '-NonInteractive', '-Command', script]
    else:
        engine = shutil.which('tesseract')
        if not engine:
            raise DocumentError('本机没有可用的文字识别，请粘贴图片中的文字。')
        langs = subprocess.run([engine, '--list-langs'], capture_output=True, timeout=4).stdout.split()
        selected = [lang for lang in ('chi_sim', 'eng') if lang.encode() in langs]
        if not selected:
            raise DocumentError('本机缺少中文或英文识别语言包，请粘贴文字。')
        command = [engine, 'stdin', 'stdout', '-l', '+'.join(selected)]
    png = io.BytesIO()
    image.convert('RGB').save(png, format='PNG')
    output = subprocess.run(command, input=png.getvalue(), capture_output=True, timeout=15,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if output.returncode:
        raise DocumentError('本机文字识别暂不可用，请粘贴文字或上传带正文的 PDF。')
    text = output.stdout.decode('utf-8-sig').replace('\r\n', '\n').strip()
    # Windows separates detected Chinese characters as if they were words.
    cjk = r'\u3400-\u9fff，。；：、！？（）'
    return re.sub(rf'(?<=[{cjk}]) +(?=[{cjk}])', '', text)


def _read(raw, extension, limit):
    from PIL import Image, ImageOps
    if extension != '.pdf':
        with Image.open(io.BytesIO(raw), formats=['PNG' if extension == '.png' else 'JPEG']) as image:
            if image.width * image.height > 24_000_000 or max(image.size) > 10000:
                raise DocumentError('图片尺寸过大，请裁剪需要讨论的部分再上传。')
            text = _recognize(ImageOps.exif_transpose(image))
        metadata = {'ocr': True, 'scope': '本机识别的图片文字，请核对金额、姓名等关键内容。'}
    else:
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(raw) as document:
            count = len(document)
            if not 1 <= count <= 40:
                raise DocumentError('每份 PDF 最多 40 页，请选取需要讨论的页面。')
            pages, ocr_pages, chars = [], [], 0
            for index in range(count):
                page = document[index]
                try:
                    text_page = page.get_textpage()
                    try:
                        if text_page.count_chars() > limit + 1:
                            raise DocumentError('PDF 正文超过 12000 字符，请拆分后上传。')
                        part = text_page.get_text_bounded().strip()
                    finally:
                        text_page.close()
                    if not part and next(page.get_objects(), None) is not None:
                        width, height = page.get_size()
                        if not all(math.isfinite(n) and n > 0 for n in (width, height)):
                            raise DocumentError('PDF 页面大小无效，请重新导出。')
                        bitmap = page.render(scale=min(2.4, 2400 / max(width, height)))
                        try:
                            part = _recognize(bitmap.to_pil())
                        finally:
                            bitmap.close()
                        ocr_pages.append(index + 1)
                        if not part:
                            raise DocumentError(f'第 {index + 1} 页未识别到文字，请检查该页或单独粘贴正文。')
                    if part:
                        chars += len(part) + (2 if pages else 0)
                        if chars > limit:
                            raise DocumentError('PDF 正文超过 12000 字符，请拆分后上传。')
                        pages.append(part)
                finally:
                    page.close()
        text = '\n\n'.join(pages)
        metadata = {'page_count': count, 'ocr': bool(ocr_pages), 'ocr_pages': ocr_pages,
                    'scope': ('扫描页使用本机文字识别，请核对金额等关键内容。' if ocr_pages
                              else '提取 PDF 正文文字，图表与版面请以原件为准。')}
    if not text:
        raise DocumentError('没有识别到文字，请上传清晰的材料或直接粘贴文字。')
    if len(text) > limit:
        raise DocumentError('提取文字超过 12000 字符，请拆分后上传。')
    return {'text': text, 'metadata': metadata}


if __name__ == '__main__':
    try:
        result = _read(sys.stdin.buffer.read(), sys.argv[1], int(sys.argv[2]))
    except DocumentError as exc:
        result = {'error': str(exc)}
    except subprocess.TimeoutExpired:
        result = {'error': '文字识别超时，请裁剪图片或直接粘贴文字。'}
    except Exception:
        result = {'error': '无法读取文件，请检查文件格式、是否加密，或直接粘贴文字。'}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
