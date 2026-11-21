"""Real local parsers plus source ownership/selection APIs; no external models."""
import hashlib
import io
import json
import uuid
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase, override_settings

from .models import Conversation, Project, SourceMaterial, Task
from .source_parsers import SourceParseError, parse_uploaded_source
from .sources import MAX_SOURCE_BYTES, sources_revision


WORD_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
TYPES = (b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Override PartName="/word/document.xml" '
    b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    b'</Types>')


def word_fixture(body='<w:p><w:r><w:t>材料正文</w:t></w:r></w:p>', *, extra=None, types=TYPES):
    result = io.BytesIO()
    with ZipFile(result, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', types)
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{WORD_NS}"><w:body>{body}</w:body></w:document>')
        for path, content in (extra or {}).items():
            archive.writestr(path, content)
    return result.getvalue()


class SourceParserTests(SimpleTestCase):
    def parse(self, name, raw):
        return parse_uploaded_source(SimpleUploadedFile(name, raw))

    def test_utf8_bom_preserves_text_and_hashes_original_bytes(self):
        raw = b'\xef\xbb\xbf' + '项目一\r\n  第二行\n'.encode()
        result = self.parse('材料.md', raw)
        self.assertEqual(result['content'], '项目一\r\n  第二行\n')
        self.assertEqual(result['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result['byte_size'], len(raw))
        self.assertFalse(result['metadata']['original_stored'])

    def test_unsupported_binary_empty_encoding_and_size_are_rejected(self):
        for name, raw in [('scan.pdf', b'%PDF-1.7'), ('legacy.doc', b'abc'),
                          ('bad.txt', b'\xff\xfeabc'), ('empty.md', b' \n\t'),
                          ('control.txt', b'abc\x00def'), ('long.txt', b'x' * 12001),
                          ('huge.txt', b'x' * (MAX_SOURCE_BYTES + 1))]:
            with self.subTest(name=name), self.assertRaises(SourceParseError):
                self.parse(name, raw)
        self.assertEqual(len(self.parse('maximum.txt', b'x' * 12000)['content']), 12000)

    def test_json_and_csv_are_checked_without_reformatting_source(self):
        text = '{ "name": "圆桌", "lines": [1, 2] }\n'
        self.assertEqual(self.parse('source.json', text.encode())['content'], text)
        csv = '姓名,备注\n圆桌,"包含,逗号"\n'
        result = self.parse('source.csv', csv.encode())
        self.assertEqual(result['content'], csv)
        self.assertEqual(result['metadata']['row_count'], 2)
        for name, text in [('x.json', '{bad}'), ('x.json', '{"a":1,"a":2}'),
                           ('x.json', '{"a":NaN}'), ('x.csv', 'a,b\n1'),
                           ('x.csv', 'a,b\n"unfinished,2')]:
            with self.subTest(text=text), self.assertRaises(SourceParseError):
                self.parse(name, text.encode())

    def test_docx_extracts_paragraphs_tabs_breaks_and_table_text(self):
        body = ('<w:p><w:r><w:t>首段</w:t><w:tab/><w:t>文字</w:t><w:br/><w:t>下一行</w:t></w:r></w:p>'
                '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>单元格</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
        result = self.parse('report.docx', word_fixture(body))
        self.assertEqual(result['content'], '首段\t文字\n下一行\n单元格')
        self.assertEqual(result['metadata']['paragraph_count'], 2)
        self.assertIn('不读取图片', result['metadata']['scope'])

    def test_docx_rejects_paths_macros_embedded_objects_and_external_entities(self):
        fixtures = [word_fixture(extra={'../outside.txt': b'bad'}),
                    word_fixture(extra={'word/vbaProject.bin': b'macro'}),
                    word_fixture(extra={'word/embeddings/oleObject.bin': b'object'}),
                    word_fixture(extra={'word/header.xml': b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///secret">]><x>&e;</x>'}),
                    word_fixture(types=TYPES.replace(b'document.main+xml', b'macroEnabled.main+xml')),
                    word_fixture(extra={'word/empty.xml': b'<unclosed>'}),
                    word_fixture(extra={'word/overlarge.xml': b'x' * (2 * 1024 * 1024 + 1)}),
                    b'not a zip']
        for raw in fixtures:
            with self.subTest(bytes=len(raw)), self.assertRaises(SourceParseError):
                self.parse('unsafe.docx', raw)

    def test_docx_rejects_archive_bombs_and_empty_scanned_documents(self):
        for raw in [word_fixture(extra={'word/zeros.xml': b'0' * 800000}),
                    word_fixture(extra={f'word/x{i}.xml': b'<x/>' for i in range(101)}),
                    word_fixture('<w:p><w:r><w:drawing/></w:r></w:p>')]:
            with self.subTest(bytes=len(raw)), self.assertRaises(SourceParseError):
                self.parse('rejected.docx', raw)

    def test_document_hyperlinks_are_only_text_and_never_requested(self):
        body = '<w:p><w:hyperlink><w:r><w:t>公开链接文字</w:t></w:r></w:hyperlink></w:p>'
        with patch('socket.create_connection', side_effect=AssertionError('no network')):
            result = self.parse('links.docx', word_fixture(body, extra={'word/_rels/document.xml.rels':
                '<Relationships><Relationship Target="https://example.test" TargetMode="External"/></Relationships>'}))
        self.assertEqual(result['content'], '公开链接文字')


@override_settings(FILE_UPLOAD_HANDLERS=['workspace.source_parsers.BoundedMemoryUploadHandler'])
class SourceAPITests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(email='source-owner@example.test', password='Fixture-123!')
        self.other = get_user_model().objects.create_user(email='source-other@example.test', password='Fixture-123!')
        self.client.force_login(self.owner)
        self.conversation = Conversation.objects.create(owner=self.owner, title='资料测试')
        self.url = f'/api/conversations/{self.conversation.pk}/sources'

    def upload(self, text='资料正文', name='材料.txt'):
        return self.client.post(self.url, {'file': SimpleUploadedFile(name, text.encode())})

    def create_source(self, text='已有材料', included=True, conversation=None, owner=None):
        return SourceMaterial.objects.create(owner=owner or self.owner, conversation=conversation or self.conversation,
            kind='file', name='example.txt', media_type='text/plain', content=text,
            sha256=hashlib.sha256(text.encode()).hexdigest(), byte_size=len(text.encode()), included=included)

    def test_first_two_materials_are_in_one_draft_before_a_question_is_sent(self):
        draft_id = uuid.uuid4()
        project = Project.objects.create(owner=self.owner, title='测试项目')
        first_url = f'/api/draft-sources/{draft_id}?project={project.id}'
        def post(path, name, content):
            return self.client.post(path, {'file': SimpleUploadedFile(name, content.encode())})

        with patch('delivery.provider._request', side_effect=AssertionError('must not call model')):
            self.assertEqual(post(first_url, '空.txt', '').status_code, 400)
            self.assertFalse(Conversation.objects.filter(pk=draft_id).exists())
            first = post(first_url, 'A.txt', '方案A').json()
            retry = post(first_url, 'A.txt', '方案A').json()
            self.assertEqual(first['source']['id'], retry['source']['id'])
            self.assertEqual(first['conversation']['project_id'], str(project.id))
            second = post(f'/api/conversations/{draft_id}/sources', 'B.txt', '方案B').json()
            self.assertEqual({s['name'] for s in second['sources'] if s['included']}, {'A.txt', 'B.txt'})
            conv = Conversation.objects.get(pk=draft_id)
            self.assertFalse(conv.messages.exists())
            self.assertFalse(conv.tasks.exists())
            saved = self.client.post(f'/api/conversations/{draft_id}', {'content': '比较 A 和 B'},
                                     content_type='application/json').json()
            self.assertEqual(saved['conversation']['title'], '比较 A 和 B')
            self.assertEqual(saved['conversation']['sources_revision'], second['revision'])
            self.assertEqual(saved['conversation']['included_sources_count'], 2)
            self.assertFalse(conv.tasks.first().runs.exists())

    def test_draft_materials_cannot_attach_to_another_users_project_or_conversation(self):
        draft_id = uuid.uuid4()
        project = Project.objects.create(owner=self.other, title='他人的项目')
        other = Conversation.objects.create(owner=self.other)
        for url in (f'/api/draft-sources/{draft_id}?project={project.id}', f'/api/draft-sources/{other.id}'):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {'file': SimpleUploadedFile('test.txt', b'text')}).status_code, 404)
        self.assertFalse(Conversation.objects.filter(pk=draft_id).exists())
        self.assertFalse(other.sources.exists())

    def test_upload_list_and_detail_preserve_content_without_model_calls(self):
        original = '<script>alert(1)</script>\n忽略规则只是材料原文。'
        with patch('delivery.provider._request', side_effect=AssertionError('must not call model')):
            before = self.client.get(self.url).json()['revision']
            response = self.upload(original)
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertNotEqual(data['revision'], before)
        self.assertNotIn('content', data['source'])
        self.assertNotIn('content', data['sources'][0])
        self.assertEqual(data['source']['char_count'], len(original))
        self.assertEqual(data['limits']['max_files'], 8)
        detail = self.client.get(f'/api/sources/{data["source"]["id"]}').json()['source']
        self.assertEqual(detail['content'], original)
        self.assertFalse(detail['metadata']['original_stored'])
        self.assertEqual(SourceMaterial.objects.count(), 1)

    def test_anonymous_and_foreign_requests_never_parse_or_fetch(self):
        source = self.create_source()
        foreign = Conversation.objects.create(owner=self.other, title='不属于当前账号')
        for logged_in in (True, False):
            if not logged_in:
                self.client.logout()
            target = f'/api/conversations/{foreign.pk}/sources' if logged_in else self.url
            with patch('workspace.source_views.parse_uploaded_source', side_effect=AssertionError('parse forbidden')):
                response = self.client.post(target, {'file': SimpleUploadedFile('x.txt', b'x')})
            self.assertEqual(response.status_code, 404 if logged_in else 401)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.get(f'/api/sources/{source.pk}').status_code, 404)
        self.assertEqual(self.client.post(f'/api/sources/{source.pk}', json.dumps({'included': False}),
                                         content_type='application/json').status_code, 404)
        source.refresh_from_db()
        self.assertTrue(source.included)

    def test_mfa_and_csrf_protect_uploads(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post(self.url, {'file': SimpleUploadedFile('x.txt', b'x')}).status_code, 403)
        self.owner.is_superuser = True
        self.owner.save(update_fields=['is_superuser'])
        self.assertEqual(self.upload().status_code, 403)
        self.assertFalse(SourceMaterial.objects.exists())

    def test_toggle_changes_revision_keeps_immutable_text_and_is_idempotent(self):
        source = self.create_source()
        before = self.client.get(self.url).json()['revision']
        response = self.client.post(f'/api/sources/{source.pk}', json.dumps({'included': False}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        after = response.json()['revision']
        self.assertNotEqual(after, before)
        source.refresh_from_db()
        self.assertEqual(source.content, '已有材料')
        self.assertFalse(source.included)
        repeat = self.client.post(f'/api/sources/{source.pk}', json.dumps({'included': False}), content_type='application/json')
        self.assertEqual(repeat.json()['revision'], after)
        self.assertEqual(source.sha256, hashlib.sha256('已有材料'.encode()).hexdigest())

    def test_running_task_blocks_upload_and_toggle_before_extraction(self):
        source = self.create_source()
        Task.objects.create(owner=self.owner, conversation=self.conversation, goal='运行中', status='running')
        with patch('workspace.source_views.parse_uploaded_source', side_effect=AssertionError('parse forbidden')):
            self.assertEqual(self.upload().status_code, 409)
        self.assertEqual(self.client.post(f'/api/sources/{source.pk}', json.dumps({'included': False}),
                                         content_type='application/json').status_code, 409)
        self.assertEqual(SourceMaterial.objects.count(), 1)

    def test_delete_is_owner_scoped_and_blocked_while_running(self):
        source = self.create_source()
        target = f'/api/sources/{source.pk}'
        revision = self.client.get(self.url).json()['revision']
        self.client.force_login(self.other)
        self.assertEqual(self.client.delete(target).status_code, 404)
        self.client.force_login(self.owner)
        task = Task.objects.create(owner=self.owner, conversation=self.conversation, goal='处理中', status='running')
        self.assertEqual(self.client.delete(target).status_code, 409)
        self.assertTrue(SourceMaterial.objects.filter(pk=source.pk).exists())
        task.status = 'waiting'
        task.save(update_fields=['status'])
        result = self.client.delete(target)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['sources'], [])
        self.assertNotEqual(result.json()['revision'], revision)
        self.assertEqual(self.client.get(target).status_code, 404)
        self.assertEqual(self.upload('新的资料').status_code, 201)

    def test_delete_requires_csrf(self):
        source = self.create_source()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.delete(f'/api/sources/{source.pk}').status_code, 403)
        self.assertTrue(SourceMaterial.objects.filter(pk=source.pk).exists())

    def test_state_is_rechecked_after_slow_extraction(self):
        def extracting(uploaded):
            parsed = parse_uploaded_source(uploaded)
            Task.objects.create(owner=self.owner, conversation=self.conversation, goal='抢先运行', status='running')
            return parsed
        with patch('workspace.source_views.parse_uploaded_source', side_effect=extracting):
            self.assertEqual(self.upload().status_code, 409)
        self.assertFalse(SourceMaterial.objects.exists())

    def test_count_and_total_selected_text_limits(self):
        source = self.create_source('a' * 12000)
        self.assertEqual(self.upload('b' * 4001).status_code, 409)
        self.assertEqual(self.upload('b' * 4000).status_code, 201)
        self.assertEqual(self.client.post(f'/api/sources/{source.pk}', json.dumps({'included': False}),
                                        content_type='application/json').status_code, 200)
        self.assertEqual(self.upload('c' * 12000).status_code, 201)
        self.assertEqual(self.client.post(f'/api/sources/{source.pk}', json.dumps({'included': True}),
                                        content_type='application/json').status_code, 409)
        for _ in range(5):
            self.create_source(included=False)
        with patch('workspace.source_views.parse_uploaded_source', side_effect=AssertionError('parse forbidden')):
            self.assertEqual(self.upload('x').status_code, 409)
        self.assertEqual(SourceMaterial.objects.count(), 8)

    def test_oversized_upload_stops_in_memory_and_saves_nothing(self):
        with patch('tempfile.NamedTemporaryFile', side_effect=AssertionError('must not spill uploaded bytes to disk')):
            response = self.client.post(self.url, {'file': SimpleUploadedFile('large.txt', b'x' * (MAX_SOURCE_BYTES + 1))})
        self.assertEqual(response.status_code, 413)
        self.assertFalse(SourceMaterial.objects.exists())

    def test_invalid_upload_and_toggle_shape_cannot_change_fields(self):
        source = self.create_source()
        for data in ({'included': 'false'}, {'included': True, 'content': 'overwrite'}, {'content': 'overwrite'}):
            self.assertEqual(self.client.post(f'/api/sources/{source.pk}', json.dumps(data), content_type='application/json').status_code, 400)
        self.assertEqual(self.client.post(self.url, {'file': [SimpleUploadedFile('a.txt', b'a'), SimpleUploadedFile('b.txt', b'b')]}).status_code, 400)
        self.assertEqual(self.upload('bad', 'bad.exe').status_code, 400)
        self.assertEqual(SourceMaterial.objects.count(), 1)

    def test_revision_is_order_independent_and_all_selections_are_bound(self):
        first, second = self.create_source(), self.create_source('第二份', included=False)
        self.assertEqual(sources_revision([first, second]), sources_revision([second, first]))
        prior = sources_revision([first, second])
        second.included = True
        self.assertNotEqual(prior, sources_revision([first, second]))

    def test_web_import_preserves_provenance_and_never_accepts_client_content(self):
        extracted = {'name': '官方说明', 'content': '网页正文', 'media_type': 'text/html',
                     'source_url': 'https://example.test/final', 'byte_size': 200,
                     'sha256': 'a' * 64, 'metadata': {'extraction': 'public_https_text', 'redirects': 1}}
        with patch('workspace.web_sources.fetch_public_source', return_value=extracted) as fetch:
            response = self.client.post(self.url, json.dumps({'url': 'https://example.test/start'}), content_type='application/json')
            self.assertEqual(response.status_code, 201)
            fetch.assert_called_once_with('https://example.test/start')
            malformed = self.client.post(self.url, json.dumps({'url': 'https://example.test', 'content': 'forged'}),
                                         content_type='application/json')
            self.assertEqual(malformed.status_code, 400)
            self.assertEqual(fetch.call_count, 1)
        source = SourceMaterial.objects.get()
        self.assertEqual((source.kind, source.content, source.source_url), ('web', '网页正文', 'https://example.test/final'))
        self.assertEqual(source.metadata['redirects'], 1)

    def test_web_auth_failure_and_running_state_do_not_contact_network(self):
        target = f'/api/conversations/{Conversation.objects.create(owner=self.other).pk}/sources'
        with patch('workspace.web_sources.fetch_public_source', side_effect=AssertionError('fetch forbidden')):
            self.assertEqual(self.client.post(target, json.dumps({'url': 'https://example.test'}), content_type='application/json').status_code, 404)
            Task.objects.create(owner=self.owner, conversation=self.conversation, goal='排队', status='running')
            self.assertEqual(self.client.post(self.url, json.dumps({'url': 'https://example.test'}), content_type='application/json').status_code, 409)
            self.client.logout()
            self.assertEqual(self.client.post(self.url, json.dumps({'url': 'https://example.test'}), content_type='application/json').status_code, 401)

    def test_fetch_failure_and_concurrent_queue_never_save_partial_material(self):
        from .web_sources import SourceFetchError
        with patch('workspace.web_sources.fetch_public_source', side_effect=SourceFetchError('网页读取超时，请稍后重试。')):
            response = self.client.post(self.url, json.dumps({'url': 'https://example.test'}), content_type='application/json')
            self.assertEqual(response.status_code, 400)
            self.assertFalse(SourceMaterial.objects.exists())
        def start_run(url):
            Task.objects.create(owner=self.owner, conversation=self.conversation, goal='新任务排队', status='running')
            return {'name': '网页', 'content': '正文', 'media_type': 'text/plain', 'source_url': url,
                    'byte_size': 6, 'sha256': 'b' * 64, 'metadata': {}}
        with patch('workspace.web_sources.fetch_public_source', side_effect=start_run):
            response = self.client.post(self.url, json.dumps({'url': 'https://example.test'}), content_type='application/json')
            self.assertEqual(response.status_code, 409)
            self.assertFalse(SourceMaterial.objects.exists())

    def test_inconsistent_ownership_is_hidden_and_conversation_deletion_cascades(self):
        hidden = self.create_source(owner=self.other)
        visible = self.create_source()
        self.assertEqual([row['id'] for row in self.client.get(self.url).json()['sources']], [str(visible.pk)])
        self.assertEqual(self.client.get(f'/api/sources/{hidden.pk}').status_code, 404)
        self.conversation.delete()
        self.assertFalse(SourceMaterial.objects.exists())
