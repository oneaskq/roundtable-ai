import json
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .models import ComposerDraft, Conversation, Project
from .views import export_data


class ComposerDraftTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='draft-owner@example.test', password='example-password')
        self.other = get_user_model().objects.create_user(email='draft-other@example.test', password='example-password')
        self.conversation = Conversation.objects.create(owner=self.user, title='Synthetic')
        self.foreign = Conversation.objects.create(owner=self.other, title='Foreign')
        self.project = Project.objects.create(owner=self.user, title='Synthetic project')
        self.foreign_project = Project.objects.create(owner=self.other, title='Foreign project')
        self.writer = str(uuid.uuid4())
        self.client.force_login(self.user)

    def save(self, content, *, conversation=None, project=None, sequence=1, writer=None, base=None):
        return self.client.post('/api/drafts', json.dumps({'conversation_id': str(conversation.id) if conversation else None,
            'project_id': str(project.id) if project else None, 'content': content,
            'writer_id': writer or self.writer, 'sequence': sequence, 'base_revision': base}), content_type='application/json')

    def test_drafts_survive_new_login_are_owner_scoped_and_exported(self):
        self.assertEqual(self.save('新草稿\n  ', project=self.project).status_code, 200)
        self.assertEqual(self.save('对话草稿', conversation=self.conversation).status_code, 200)
        self.client.logout()
        self.client.force_login(self.user)
        rows = self.client.get('/api/drafts').json()['drafts']
        self.assertCountEqual([d['content'] for d in rows], ['新草稿\n  ', '对话草稿'])
        self.assertCountEqual([d['content'] for d in export_data(self.user)['drafts']], ['新草稿\n  ', '对话草稿'])
        self.assertEqual([d['content'] for d in export_data(self.user, self.project)['drafts']], ['新草稿\n  '])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get('/api/drafts').json()['drafts'], [])
        self.assertEqual(self.save('不可覆盖', conversation=self.conversation).status_code, 404)
        self.assertEqual(self.save('不可选择', project=self.project).status_code, 404)

    def test_stale_same_page_request_cannot_resurrect_sent_text(self):
        first = self.save('未发送', sequence=1).json()['revision']
        cleared = self.save('', sequence=3, base=first).json()['revision']
        result = self.save('过期的旧内容', sequence=2)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()['draft']['updated_at'], cleared)
        self.assertEqual(self.client.get('/api/drafts').json()['drafts'], [])
        self.assertEqual(self.client.get('/api/drafts?include_cleared=1').json()['drafts'][0]['updated_at'], cleared)

    def test_old_tab_cannot_overwrite_other_writer_until_choosing_a_version(self):
        first = self.save('A 第 2 版', sequence=2).json()['revision']
        other = self.save('B 已保存版', writer=str(uuid.uuid4()), base=first).json()['revision']
        delayed = self.save('A 迟到的第 1 版')
        self.assertEqual(delayed.status_code, 409)
        self.assertEqual(delayed.json()['draft']['content'], 'B 已保存版')
        self.assertEqual(delayed.json()['draft']['updated_at'], other)
        self.assertGreater(other, first)
        chosen = self.save('A 手动合并', sequence=3, base=other).json()['revision']
        replay = self.save('A 手动合并', sequence=3, base=other)
        self.assertEqual(replay.json(), {'saved': True, 'revision': chosen})
        self.assertEqual(ComposerDraft.objects.get().content, 'A 手动合并')

    def test_older_page_without_version_cannot_overwrite_existing_draft(self):
        body = {'content': '待同步', 'conversation_id': None, 'project_id': None,
                'writer_id': self.writer, 'sequence': 1}
        first = self.client.post('/api/drafts', body, content_type='application/json').json()['revision']
        other = self.save('其他标签的新版本', base=first,
                          writer=str(uuid.uuid4()))
        self.assertEqual(other.status_code, 200)
        retry = self.client.post('/api/drafts', body, content_type='application/json')
        self.assertEqual(retry.status_code, 409)
        self.assertEqual(retry.json()['draft']['content'], '其他标签的新版本')

    def test_selected_project_survives_without_message_text(self):
        self.save('', project=self.project)
        restored = self.client.get('/api/drafts').json()['drafts'][0]
        self.assertEqual(restored['project_id'], str(self.project.id))

    def test_invalid_drafts_and_foreign_references_are_rejected(self):
        self.assertEqual(self.save('x' * 12001).status_code, 400)
        self.assertEqual(self.save('内容', sequence=True).status_code, 400)
        self.assertEqual(self.save('内容', writer='bad').status_code, 400)
        self.assertEqual(self.save('内容', conversation=self.foreign).status_code, 404)
        self.assertEqual(self.save('内容', project=self.foreign_project).status_code, 404)
        self.assertEqual(self.save('内容', conversation=self.conversation, project=self.project).status_code, 400)
        self.assertFalse(ComposerDraft.objects.exists())

    def test_conversation_and_account_deletion_cascade(self):
        self.save('对话草稿', conversation=self.conversation)
        self.save('新草稿', sequence=2)
        self.conversation.delete()
        self.assertEqual(list(ComposerDraft.objects.values_list('content', flat=True)), ['新草稿'])
        self.user.delete()
        self.assertFalse(ComposerDraft.objects.exists())

    def test_recent_draft_edit_updates_conversation_retention_timestamp(self):
        past = timezone.now() - timezone.timedelta(days=40)
        Conversation.objects.filter(id=self.conversation.id).update(updated_at=past)
        self.assertEqual(self.save('正在编辑', conversation=self.conversation).status_code, 200)
        self.conversation.refresh_from_db()
        self.assertGreater(self.conversation.updated_at, past)
