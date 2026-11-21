"""Persistence and download tests for real source files, with no code execution."""
from io import BytesIO
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.test import TestCase

from workspace.models import Artifact, Conversation, Task
from workspace.views import export_data
from .artifact_bundles import save_bundle_artifact
from .bundles import BundleError
from .provider import ProviderError


class BundleArtifactTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='bundle@example.test', password='Fixture-pass-123!')
        self.other = get_user_model().objects.create_user(email='other-bundle@example.test', password='Fixture-pass-123!')
        self.client.force_login(self.user)
        self.conversation = Conversation.objects.create(owner=self.user, title='文件修改')
        self.task = Task.objects.create(owner=self.user, conversation=self.conversation, goal='初版', status='completed')
        self.bundle = {'summary': '两份可编辑源码文件。', 'files': [
            {'path': '说明.md', 'content': '# 学习计划\n\n按需修改。\n'},
            {'path': 'src/plan.py', 'content': 'def daily_minutes():\n    return 50\n'},
        ]}

    def save(self, **kwargs):
        return save_bundle_artifact(self.task, self.user, self.bundle, {'passed': True}, **kwargs)

    def test_zip_roundtrip_preserves_unicode_paths_and_exact_source(self):
        artifact = self.save()
        response = self.client.get(f'/api/artifacts/{artifact.id}/download')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/zip')
        self.assertTrue(response['Content-Disposition'].startswith('attachment;'))
        with ZipFile(BytesIO(response.content)) as archive:
            self.assertCountEqual(archive.namelist(), ['说明.md', 'src/plan.py'])
            for source in self.bundle['files']:
                self.assertEqual(archive.read(source['path']).decode('utf-8'), source['content'])

    def test_each_revision_is_immutable_and_history_spans_conversation_tasks(self):
        first = self.save(review_status='draft')
        self.task = Task.objects.create(owner=self.user, conversation=self.conversation, goal='修改版', status='completed')
        self.bundle['files'][0]['content'] = '# 修改后的计划\n'
        second = self.save()
        self.assertEqual((first.revision, second.revision), (1, 2))
        first.refresh_from_db()
        self.assertIn('按需修改', first.files[0]['content'])
        # Distinct revisions may be saved within one system clock tick.
        Artifact.objects.filter(pk=second.pk).update(
            created_at=first.created_at, updated_at=first.updated_at)
        history = self.client.get(f'/api/conversations/{self.conversation.id}/artifacts').json()['artifacts']
        self.assertEqual([item['id'] for item in history], [str(second.id), str(first.id)])
        self.assertEqual([item['review_status'] for item in history], ['ready', 'draft'])
        self.assertNotIn('files', history[0])
        detail = self.client.get(f'/api/artifacts/{first.id}/detail').json()['artifact']
        self.assertEqual(detail['files'][0]['content'], '# 学习计划\n\n按需修改。\n')
        self.assertEqual(detail['review_status'], 'draft')

    def test_static_failure_cannot_be_promoted_by_forged_report(self):
        self.bundle['files'][1]['content'] = 'def broken(:\n'
        with self.assertRaises(ProviderError):
            self.save()
        self.assertFalse(Artifact.objects.exists())
        draft = self.save(review_status='draft')
        self.assertEqual(draft.review_status, 'draft')
        self.assertFalse(draft.validation['passed'])
        self.assertTrue(any(check['status'] == 'failed' for check in draft.validation['checks']))

    def test_source_html_is_only_served_as_attachment_and_json_text(self):
        artifact = Artifact.objects.create(owner=self.user, task=self.task, name='源码.zip', kind='bundle',
            content='HTML 源码，不在页面执行', files=[{'path': 'index.html',
            'content': '<!doctype html><html><script>window.alert("fixture")</script></html>'}])
        response = self.client.get(f'/api/artifacts/{artifact.id}/files/0')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/plain; charset=utf-8')
        self.assertTrue(response['Content-Disposition'].startswith('attachment;'))
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response['Cache-Control'], 'no-store')
        detail = self.client.get(f'/api/artifacts/{artifact.id}/detail')
        self.assertEqual(detail['Content-Type'], 'application/json')
        self.assertEqual(detail.json()['artifact']['files'][0]['content'], artifact.files[0]['content'])
        self.assertEqual(self.client.get(f'/api/artifacts/{artifact.id}/files/1').status_code, 404)

    def test_corrupt_persisted_paths_are_rejected_before_download_or_preview(self):
        artifact = self.save()
        Artifact.objects.filter(pk=artifact.pk).update(files=[{'path': '../outside.py', 'content': 'print(1)'}])
        for suffix in ('download', 'detail', 'files/0'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.client.get(f'/api/artifacts/{artifact.id}/{suffix}').status_code, 409)
        with self.assertRaises(BundleError):
            save_bundle_artifact(self.task, self.user,
                {'summary': 'bad', 'files': [{'path': 'C:/outside.py', 'content': 'print(1)'}]}, {})

    def test_foreign_users_cannot_save_list_preview_or_download(self):
        artifact = self.save()
        with self.assertRaises(ProviderError):
            save_bundle_artifact(self.task, self.other, self.bundle, {})
        self.client.force_login(self.other)
        for suffix in ('download', 'detail', 'files/0'):
            self.assertEqual(self.client.get(f'/api/artifacts/{artifact.id}/{suffix}').status_code, 404)
        self.assertEqual(self.client.get(f'/api/conversations/{self.conversation.id}/artifacts').status_code, 404)
        self.assertEqual(self.client.get(f'/api/tasks/{self.task.id}/artifacts').status_code, 404)
        self.assertEqual(export_data(self.other)['artifacts'], [])

    def test_export_includes_files_and_checks_but_ordinary_detail_is_metadata_only(self):
        artifact = self.save()
        exported = export_data(self.user)['artifacts'][0]
        self.assertEqual(exported['files'], artifact.files)
        self.assertEqual(exported['validation'], artifact.validation)
        self.assertEqual(exported['revision'], 1)
        self.assertEqual(exported['review_status'], 'ready')
        ordinary = self.client.get(f'/api/conversations/{self.conversation.id}').json()['tasks'][0]['artifacts'][0]
        self.assertEqual(ordinary['kind'], 'bundle')
        self.assertEqual(ordinary['file_count'], 2)
        self.assertNotIn('files', ordinary)
        self.assertNotIn('content', ordinary)

    def test_legacy_text_download_and_anonymous_denial_remain_compatible(self):
        artifact = Artifact.objects.create(owner=self.user, task=self.task, name='旧成果.md', content='原始文本')
        response = self.client.get(f'/api/artifacts/{artifact.id}/download')
        self.assertEqual(response.content.decode('utf-8'), '原始文本')
        detail = self.client.get(f'/api/artifacts/{artifact.id}/detail').json()['artifact']
        self.assertEqual(detail['kind'], 'text')
        self.assertEqual(detail['content'], '原始文本')
        self.assertEqual(detail['files'], [])
        self.assertEqual(self.client.get(f'/api/artifacts/{artifact.id}/files/0').status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(f'/api/artifacts/{artifact.id}/download').status_code, 401)
        self.assertEqual(self.client.get(f'/api/artifacts/{artifact.id}/detail').status_code, 401)
        self.assertEqual(self.client.get(f'/api/conversations/{self.conversation.id}/artifacts').status_code, 401)
