import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image

from .source_parsers import SourceParseError, parse_uploaded_source


def text_pdf(text):
    """Minimal one-page PDF with a selectable text layer."""
    content = b'BT /F1 12 Tf 30 100 Td (' + text.encode('ascii') + b') Tj ET'
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Count 1 /Kids [3 0 R] >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 200] /Contents 4 0 R '
        b'/Resources << /Font << /F1 5 0 R >> >> >>',
        f'<< /Length {len(content)} >>\nstream\n'.encode() + content + b'\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    output = bytearray(b'%PDF-1.4\n')
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f'{number} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(output)
    output.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets:
        output.extend(f'{offset:010} 00000 n \n'.encode())
    output.extend(f'trailer << /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode())
    return bytes(output)


class VisualSourceTests(SimpleTestCase):
    def test_pdf_text_layer_is_preserved_without_ocr(self):
        result = parse_uploaded_source(SimpleUploadedFile('offer.pdf', text_pdf('Internship A - 6 months')))
        self.assertEqual(result['content'], 'Internship A - 6 months')
        self.assertEqual(result['media_type'], 'application/pdf')
        self.assertFalse(result['metadata']['ocr'])
        self.assertFalse(result['metadata']['original_stored'])

    def test_image_extension_must_match_the_actual_format(self):
        data = io.BytesIO()
        Image.new('RGB', (10, 10), color='white').save(data, format='PNG')
        with self.assertRaises(SourceParseError):
            parse_uploaded_source(SimpleUploadedFile('offer.jpg', data.getvalue()))
