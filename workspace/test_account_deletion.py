import json
import uuid

from django.contrib.sessions.models import Session
from django.core import signing
from django.test import Client, TestCase

from accounts.models import DeviceSession, MFADevice, SecurityEvent, User
from delivery.models import QualityRound, QualitySample, RequestBudget, TeamRun
from operations.deletion_backups import LEDGER_SALT
from .deletion import account_fingerprint, identity_key_id
from .models import (AccountDeletionReceipt, Artifact, Conversation, DataPolicy, DeletionRequest,
                     Memory, Message, Project, SourceMaterial, Task)


PASSWORD = 'Synthetic-only-password-3948!'


class AccountDeletionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('delete@example.test', PASSWORD)
        self.other = User.objects.create_user('other@example.test', PASSWORD)
        self.client.force_login(self.owner)
        self.project = Project.objects.create(owner=self.owner, title='synthetic project')
        self.conversation = Conversation.objects.create(owner=self.owner, project=self.project, title='synthetic conversation')
        self.task = Task.objects.create(owner=self.owner, conversation=self.conversation, goal='synthetic goal', status='waiting')
        Message.objects.create(conversation=self.conversation, role='user', content='synthetic private message')
        Memory.objects.create(owner=self.owner, content='synthetic private memory')
        Artifact.objects.create(owner=self.owner, task=self.task, name='synthetic', content='synthetic artifact')
        SourceMaterial.objects.create(owner=self.owner, conversation=self.conversation, kind='file', name='synthetic.txt',
                                      media_type='text/plain', content='synthetic source', byte_size=16, sha256='0' * 64)
        item = QualityRound.objects.create(owner=self.owner, case_id='synthetic', case_revision='0' * 64)
        QualitySample.objects.create(round=item, position=1, model_label='synthetic', output='synthetic result', output_sha256='0' * 64)
        DataPolicy.objects.create(user=self.owner)
        DeletionRequest.objects.create(user=self.owner)
        SecurityEvent.objects.create(user=self.owner, kind='synthetic', message='synthetic')
        RequestBudget.objects.create(user=self.owner)
        self.other_conversation = Conversation.objects.create(owner=self.other, title='other private work')
        self.url = '/api/data/delete-account'
        self.body = {'password': PASSWORD, 'confirmation': '删除我的账号', 'acknowledge_backup': True}

    def post(self, changes=None, client=None):
        return (client or self.client).post(self.url, json.dumps({**self.body, **(changes or {})}), content_type='application/json')

    def test_deletes_only_owner_live_records_and_retains_pseudonymous_backup_receipt(self):
        other_session = Client()
        other_session.force_login(self.owner)
        key = other_session.session.session_key
        DeviceSession.objects.create(user=self.owner, session_key=key)
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['backup_status'], 'pending')
        self.assertFalse(User.objects.filter(pk=self.owner.pk).exists())
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())
        self.assertFalse(Conversation.objects.filter(pk=self.conversation.pk).exists())
        self.assertFalse(Task.objects.filter(pk=self.task.pk).exists())
        self.assertFalse(QualityRound.objects.filter(owner_id=self.owner.pk).exists())
        self.assertFalse(SecurityEvent.objects.filter(user_id=self.owner.pk).exists())
        self.assertFalse(Session.objects.filter(session_key=key).exists())
        self.assertFalse(other_session.get('/api/auth/me').json()['authenticated'])
        self.assertTrue(Conversation.objects.filter(pk=self.other_conversation.pk).exists())
        receipt = AccountDeletionReceipt.objects.get(pk=response.json()['receipt'])
        self.assertEqual(receipt.backup_status, 'pending')
        self.assertNotIn(self.owner.email, receipt.identity_digest)
        self.assertFalse(self.client.get('/api/auth/me').json()['authenticated'])

    def test_requires_exact_confirmation_backup_acknowledgement_and_password(self):
        for change in ({'confirmation': 'delete'}, {'acknowledge_backup': False}, {'password': 'wrong'}, {'user_id': self.other.pk}):
            self.assertEqual(self.post(change).status_code, 400)
        self.assertTrue(User.objects.filter(pk=self.owner.pk).exists())
        self.assertFalse(AccountDeletionReceipt.objects.exists())

    def test_a_restored_account_can_reuse_its_existing_deletion_receipt(self):
        receipt = AccountDeletionReceipt.objects.create(
            identity_digest=account_fingerprint(self.owner.pk, self.owner.date_joined), identity_key_id=identity_key_id())
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['receipt'], str(receipt.pk))
        self.assertEqual(AccountDeletionReceipt.objects.count(), 1)
        self.assertFalse(User.objects.filter(pk=self.owner.pk).exists())

    def test_queued_or_running_work_blocks_deletion(self):
        execution = uuid.uuid4()
        self.task.status = 'running'
        self.task.execution_id = execution
        self.task.save(update_fields=['status', 'execution_id'])
        run = TeamRun.objects.create(task=self.task, execution_id=execution, team_revision='synthetic', status='queued')
        self.assertEqual(self.post().status_code, 409)
        run.status = 'running'
        run.save(update_fields=['status'])
        self.assertEqual(self.post().status_code, 409)
        self.assertTrue(User.objects.filter(pk=self.owner.pk).exists())

    def test_last_administrator_cannot_be_deleted(self):
        self.owner.is_staff = self.owner.is_superuser = True
        self.owner.save(update_fields=['is_staff', 'is_superuser'])
        device = MFADevice.objects.create(user=self.owner, enabled=True, encrypted_secret='synthetic')
        session = self.client.session
        session['mfa_verified'] = True
        session.save()
        self.assertEqual(self.post().status_code, 409)
        self.other.is_staff = self.other.is_superuser = True
        self.other.save(update_fields=['is_staff', 'is_superuser'])
        self.assertEqual(self.post().status_code, 200)
        self.assertTrue(User.objects.filter(pk=self.other.pk, is_superuser=True).exists())

    def test_wrong_password_is_rate_limited(self):
        for _ in range(5):
            self.assertEqual(self.post({'password': 'wrong'}).status_code, 400)
        self.assertEqual(self.post().status_code, 429)
        self.assertTrue(User.objects.filter(pk=self.owner.pk).exists())

    def test_mfa_and_csrf_are_enforced(self):
        MFADevice.objects.create(user=self.owner, enabled=True, encrypted_secret='synthetic')
        self.assertEqual(self.post().status_code, 403)
        session = self.client.session
        session['mfa_verified'] = True
        session.save()
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.owner)
        session = browser.session
        session['mfa_verified'] = True
        session.save()
        self.assertEqual(self.post(client=browser).status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.owner.pk).exists())

    def test_conversation_deletion_is_scoped_and_removes_its_related_records(self):
        body = {'password': PASSWORD, 'confirmation': '删除此对话'}
        target = f'/api/conversations/{self.conversation.id}'
        foreign = f'/api/conversations/{self.other_conversation.id}'
        self.assertEqual(self.client.delete(foreign, json.dumps(body), content_type='application/json').status_code, 404)
        self.assertEqual(self.client.delete(target, json.dumps({**body, 'password': 'wrong'}), content_type='application/json').status_code, 400)
        response = self.client.delete(target, json.dumps(body), content_type='application/json')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Conversation.objects.filter(pk=self.conversation.pk).exists())
        self.assertFalse(Task.objects.filter(pk=self.task.pk).exists())
        self.assertFalse(SourceMaterial.objects.filter(conversation_id=self.conversation.pk).exists())
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())
        self.assertTrue(Memory.objects.filter(owner=self.owner).exists())
        self.assertTrue(Conversation.objects.filter(pk=self.other_conversation.pk).exists())
        self.assertFalse(AccountDeletionReceipt.objects.exists())

    def test_conversation_deletion_does_not_race_a_running_task(self):
        self.task.status = 'running'
        self.task.execution_id = uuid.uuid4()
        self.task.save(update_fields=['status', 'execution_id'])
        body = {'password': PASSWORD, 'confirmation': '删除此对话'}
        response = self.client.delete(f'/api/conversations/{self.conversation.id}', json.dumps(body), content_type='application/json')
        self.assertEqual(response.status_code, 409)
        self.assertTrue(Conversation.objects.filter(pk=self.conversation.pk).exists())

    def test_backup_receipts_and_signed_ledger_require_verified_administrator(self):
        receipt = AccountDeletionReceipt.objects.create(
            identity_digest=account_fingerprint(self.owner.pk, self.owner.date_joined), identity_key_id=identity_key_id())
        status_url = '/api/data/deletion-backups'
        ledger_url = '/api/data/deletion-backups/ledger'
        self.assertEqual(self.client.get(status_url).status_code, 403)
        self.assertEqual(self.client.get(ledger_url).status_code, 403)
        self.owner.is_superuser = self.owner.is_staff = True
        self.owner.save(update_fields=['is_superuser', 'is_staff'])
        self.assertEqual(self.client.get(status_url).status_code, 403)
        MFADevice.objects.create(user=self.owner, enabled=True, encrypted_secret='synthetic')
        session = self.client.session
        session['mfa_verified'] = True
        session.save()
        response = self.client.get(status_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['pending_count'], 1)
        self.assertEqual(response.json()['receipts'][0]['id'], str(receipt.pk))
        self.assertNotIn(receipt.identity_digest, response.content.decode())
        ledger = self.client.get(ledger_url)
        self.assertEqual(ledger.status_code, 200)
        self.assertEqual(ledger['Cache-Control'], 'no-store')
        self.assertEqual(signing.loads(ledger.content.decode(), salt=LEDGER_SALT)['receipts'][0]['id'], str(receipt.pk))
