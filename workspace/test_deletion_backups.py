import json
import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from operations.backups import BackupError, create_snapshot
from operations.deletion_backups import export_ledger, inspect_snapshot, load_ledger, load_ledger_bundle, sanitized_snapshot
from workspace.deletion import account_fingerprint, identity_key_id, record_fingerprint
from workspace.models import AccountDeletionReceipt, RecordDeletionReceipt


class DeletionBackupTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-deletion-backup-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.joined = timezone.now()
        self.receipt = AccountDeletionReceipt.objects.create(identity_digest=account_fingerprint(42, self.joined),
                                                              identity_key_id=identity_key_id())
        self.source = self.root / 'synthetic.sqlite3'
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute('CREATE TABLE accounts_user (id INTEGER PRIMARY KEY, date_joined TEXT)')
            conn.execute('INSERT INTO accounts_user VALUES (?, ?)', (42, self.joined.isoformat()))
            conn.commit()

    def test_inspection_finds_known_deleted_account_without_modifying_snapshot(self):
        snapshot = self.root / 'snapshot'
        create_snapshot(self.source, snapshot)
        original = (snapshot / 'snapshot.sqlite3').read_bytes()
        result = inspect_snapshot(snapshot)
        self.assertEqual(result['status'], 'recorded_accounts_found')
        self.assertEqual(result['matching_receipts'], [str(self.receipt.pk)])
        self.assertEqual((snapshot / 'snapshot.sqlite3').read_bytes(), original)

    def test_separate_signed_ledger_survives_loss_of_current_database_receipts(self):
        output = self.root / 'signed-ledger.txt'
        export_ledger(output)
        self.assertEqual(len(load_ledger(output)), 1)
        self.assertNotIn('email', output.read_text(encoding='utf-8'))
        AccountDeletionReceipt.objects.all().delete()
        self.assertEqual(inspect_snapshot(self.source)['status'], 'no_receipts')
        result = inspect_snapshot(self.source, ledger_file=output)
        self.assertEqual(result['matching_receipts'], [str(self.receipt.pk)])
        with self.assertRaises(BackupError):
            export_ledger(output)
        output.write_text(output.read_text(encoding='utf-8') + 'tampered', encoding='utf-8')
        with self.assertRaises(BackupError):
            inspect_snapshot(self.source, ledger_file=output)

    def test_missing_table_and_malformed_snapshot_fail_closed(self):
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute('DROP TABLE accounts_user')
            conn.commit()
        with self.assertRaises(BackupError):
            inspect_snapshot(self.source)
        with self.assertRaises(CommandError):
            call_command('check_backup_deletions', snapshot=str(self.source))

    def test_no_match_does_not_claim_complete_erasure(self):
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute('DELETE FROM accounts_user')
            conn.commit()
        result = inspect_snapshot(self.source)
        self.assertEqual(result['status'], 'no_recorded_matches')
        self.assertIn('未修改备份', result['notice'])

    def test_ledger_checks_deleted_individual_items_in_backup(self):
        object_id = uuid.uuid4()
        receipt = RecordDeletionReceipt.objects.create(kind='memory', object_digest=record_fingerprint('memory', object_id),
                                                       identity_key_id=identity_key_id())
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute('CREATE TABLE workspace_memory (id TEXT PRIMARY KEY)')
            conn.execute('INSERT INTO workspace_memory VALUES (?)', (object_id.hex,))
            conn.commit()
        ledger = self.root / 'signed-records.txt'
        export_ledger(ledger)
        accounts, records = load_ledger_bundle(ledger)
        self.assertEqual((len(accounts), len(records)), (1, 1))
        result = inspect_snapshot(self.source, ledger_file=ledger)
        self.assertEqual(result['matching_record_receipts'], [str(receipt.pk)])

    def test_stale_ledger_fails_before_creating_a_sanitized_copy(self):
        ledger = self.root / 'old-ledger.txt'
        export_ledger(ledger)
        RecordDeletionReceipt.objects.create(kind='memory', object_digest=record_fingerprint('memory', uuid.uuid4()),
                                             identity_key_id=identity_key_id())
        output = self.root / 'must-not-exist'
        with self.assertRaises(BackupError):
            sanitized_snapshot(self.source, output, ledger_file=ledger)
        self.assertFalse(output.exists())

    @override_settings(SECRET_KEY='different-synthetic-key')
    def test_changed_signing_key_cannot_be_mistaken_for_a_clean_backup(self):
        with self.assertRaises(BackupError):
            inspect_snapshot(self.source)
