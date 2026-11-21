"""End-to-end deletion and restore testing against disposable synthetic snapshots."""
import json
import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TransactionTestCase

from accounts.models import DeviceSession
from delivery.models import AgentStep, QualityRound, TeamRun
from operations.backups import create_snapshot, check_snapshot
from operations.deletion_backups import export_ledger, inspect_snapshot, sanitized_snapshot
from workspace.deletion import record_deletion, record_fingerprint
from workspace.models import Conversation, DataPolicy, Memory, Project, RecordDeletionReceipt, SourceMaterial, Task


class SanitizedBackupTests(TransactionTestCase):
    databases = '__all__'

    def test_new_restore_copy_applies_account_and_single_item_deletions(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-sanitized-') as temporary:
            root = Path(temporary)
            User = get_user_model()
            password = 'Synthetic-Backup-Test-785!'
            owner = User.objects.create_user(email='owner@example.test', password=password)
            deleted = User.objects.create_user(email='deleted@example.test', password=password)
            project = Project.objects.create(owner=owner, title='synthetic project')
            preserved_chat = Conversation.objects.create(owner=owner, project=project, title='keep this chat')
            removed_chat = Conversation.objects.create(owner=owner, title='remove this chat')
            deleted_chat = Conversation.objects.create(owner=deleted, title='remove this account data')
            memory = Memory.objects.create(owner=owner, content='remove this memory')
            historical = Memory.objects.create(owner=owner, content='previously recorded deletion')
            record_deletion('memory', historical.pk)
            source = SourceMaterial.objects.create(owner=owner, conversation=preserved_chat, kind='file',
                name='synthetic.txt', media_type='text/plain', content='remove this source', byte_size=5, sha256='0' * 64)
            quality = QualityRound.objects.create(owner=owner, case_id='synthetic', case_revision='x')
            DataPolicy.objects.create(user=owner, retention_days=30, auto_cleanup=True)
            queued_chat = Conversation.objects.create(owner=owner, title='do not replay')
            execution_id = uuid.uuid4()
            task = Task.objects.create(owner=owner, conversation=queued_chat, goal='do not replay', status='running', execution_id=execution_id)
            run = TeamRun.objects.create(task=task, execution_id=execution_id, status='queued', background=True,
                                         team_revision='synthetic', input_context=[{'role': 'user', 'content': 'synthetic'}])
            AgentStep.objects.create(run=run, position=0, role='proposer', label='synthetic', provider='none', model='none')
            owner_client = Client()
            owner_client.force_login(owner)
            DeviceSession.objects.create(user=owner, session_key=owner_client.session.session_key)

            self.client.force_login(deleted)
            deleted_session_key = self.client.session.session_key
            DeviceSession.objects.create(user=deleted, session_key=deleted_session_key)
            source_file = root / 'source.sqlite3'
            connection.ensure_connection()
            with closing(sqlite3.connect(source_file)) as target:
                connection.connection.backup(target)
            original = root / 'original'
            create_snapshot(source_file, original)
            original_bytes = (original / 'snapshot.sqlite3').read_bytes()
            # Simulate a restored current database or an older external ledger
            # that lacks a tombstone already present inside the backup.
            RecordDeletionReceipt.objects.filter(object_digest=record_fingerprint('memory', historical.pk)).delete()

            account_result = self.client.post('/api/data/delete-account', json.dumps({
                'password': password, 'confirmation': '删除我的账号', 'acknowledge_backup': True}), content_type='application/json')
            self.assertEqual(account_result.status_code, 200)
            self.client.force_login(owner)
            response = self.client.delete(f'/api/conversations/{removed_chat.pk}', json.dumps({
                'password': password, 'confirmation': '删除此对话'}), content_type='application/json')
            self.assertEqual(response.status_code, 204)
            self.assertEqual(self.client.delete(f'/api/memories/{memory.pk}').status_code, 204)
            self.assertEqual(self.client.delete(f'/api/sources/{source.pk}').status_code, 200)
            self.assertEqual(self.client.delete(f'/api/quality/rounds/{quality.pk}').status_code, 200)
            self.assertEqual(self.client.delete(f'/api/projects/{project.pk}', json.dumps({
                'password': password, 'confirm_title': project.title}), content_type='application/json').status_code, 204)
            self.assertEqual(RecordDeletionReceipt.objects.count(), 5)
            ledger = root / 'latest-ledger.txt'
            export_ledger(ledger)
            before = inspect_snapshot(original, ledger_file=ledger)
            self.assertEqual((len(before['matching_receipts']), len(before['matching_record_receipts'])), (1, 5))

            clean = root / 'clean'
            # Django's test runner restricts dynamically-added database aliases;
            # explicitly permit only the predictable disposable copy alias.
            fake_uuid = uuid.uuid4()
            alias = 'sanitized_' + fake_uuid.hex
            previous_databases = self.__class__.databases
            self.__class__.databases = previous_databases | {alias}
            try:
                with patch('operations.deletion_backups.uuid', SimpleNamespace(UUID=uuid.UUID, uuid4=lambda: fake_uuid)):
                    result = sanitized_snapshot(original, clean, ledger_file=ledger)
            finally:
                self.__class__.databases = previous_databases
            self.assertEqual(result['removed'], {'accounts': 1, 'records': 6})
            self.assertEqual((original / 'snapshot.sqlite3').read_bytes(), original_bytes)
            self.assertEqual(check_snapshot(clean)['status'], 'passed')
            self.assertEqual(inspect_snapshot(clean, ledger_file=ledger)['status'], 'no_recorded_matches')
            with closing(sqlite3.connect(clean / 'snapshot.sqlite3')) as restored:
                self.assertEqual(restored.execute('SELECT COUNT(*) FROM accounts_user WHERE id=?', (deleted.pk,)).fetchone()[0], 0)
                self.assertEqual(restored.execute('SELECT COUNT(*) FROM django_session WHERE session_key=?', (deleted_session_key,)).fetchone()[0], 0)
                self.assertEqual(restored.execute('SELECT project_id FROM workspace_conversation WHERE id=?', (preserved_chat.pk.hex,)).fetchone()[0], None)
                self.assertEqual(restored.execute('SELECT COUNT(*) FROM workspace_conversation WHERE id=?', (deleted_chat.pk.hex,)).fetchone()[0], 0)
                self.assertEqual(restored.execute('SELECT COUNT(*) FROM django_session').fetchone()[0], 0)
                self.assertEqual(restored.execute('SELECT auto_cleanup FROM workspace_datapolicy WHERE user_id=?', (owner.pk,)).fetchone()[0], 0)
                self.assertEqual(restored.execute('SELECT status FROM delivery_teamrun WHERE id=?', (run.pk.hex,)).fetchone()[0], 'failed')
                self.assertEqual(restored.execute('SELECT status FROM workspace_task WHERE id=?', (task.pk.hex,)).fetchone()[0], 'failed')
                self.assertEqual(restored.execute('SELECT COUNT(*) FROM workspace_memory WHERE id=?', (historical.pk.hex,)).fetchone()[0], 0)
