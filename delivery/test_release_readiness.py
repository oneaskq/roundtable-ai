import json
import os
import secrets
import sqlite3
import tempfile
from contextlib import closing
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from operations.backups import BackupError, check_snapshot, create_snapshot
from operations.production import production_values
from operations.readiness import build_release_report


def production_environment():
    return {
        'ROUNDTABLE_ENV': 'production', 'DJANGO_SECRET_KEY': secrets.token_urlsafe(64),
        'MFA_ENCRYPTION_KEY': Fernet.generate_key().decode(), 'DJANGO_ALLOWED_HOSTS': 'roundtable.example.org',
        'PUBLIC_BASE_URL': 'https://roundtable.example.org', 'POSTGRES_DB': 'synthetic-db',
        'POSTGRES_USER': 'synthetic-user', 'POSTGRES_PASSWORD': secrets.token_urlsafe(24),
        'POSTGRES_HOST': 'db.example.org', 'EMAIL_HOST': 'smtp.example.org',
        'EMAIL_HOST_USER': 'synthetic-mail-user', 'EMAIL_HOST_PASSWORD': secrets.token_urlsafe(24),
        'DEFAULT_FROM_EMAIL': 'roundtable@example.org',
    }


class ReleaseReadinessTests(SimpleTestCase):
    def test_production_configuration_does_not_forge_real_acceptance(self):
        env = production_environment()
        values = production_values(env)
        values['RELEASE_EVIDENCE'] = {'all': 'passed', 'model_quality': True}
        values['EXECUTION_VERIFIED'] = True
        report = build_release_report(SimpleNamespace(**values), env,
                                      implemented_capabilities={'chat.qa'}, required_capabilities={'chat.qa'})
        self.assertEqual(report['counts']['failed'], 0)
        self.assertGreaterEqual(report['counts']['pending'], 9)
        self.assertFalse(report['ready'])
        rendered = json.dumps(report)
        for key in ('DJANGO_SECRET_KEY', 'MFA_ENCRYPTION_KEY', 'POSTGRES_PASSWORD', 'EMAIL_HOST_PASSWORD'):
            self.assertNotIn(env[key], rendered)

    def test_local_configuration_and_missing_capabilities_block_release(self):
        report = build_release_report(SimpleNamespace(DEBUG=True, ENVIRONMENT='development'), {},
                                      implemented_capabilities={'chat.qa'}, required_capabilities={'chat.qa', 'video.generate'})
        rows = {item['id']: item for item in report['checks']}
        for key in ('environment', 'secret_source', 'public_origin', 'transport', 'database', 'mail_configuration', 'capability_implementation'):
            self.assertEqual(rows[key]['status'], 'failed')
        self.assertIn('1/2', rows['capability_implementation']['detail'])

    def test_mismatched_runtime_key_and_test_hash_are_rejected(self):
        env = production_environment()
        values = production_values(env)
        values['SECRET_KEY'] = 'another-private-key'
        values['PASSWORD_HASHERS'] = ['django.contrib.auth.hashers.MD5PasswordHasher']
        report = build_release_report(SimpleNamespace(**values), env)
        rows = {item['id']: item for item in report['checks']}
        self.assertEqual(rows['secret_source']['status'], 'failed')
        self.assertEqual(rows['password_hashing']['status'], 'failed')
        self.assertNotIn('another-private-key', json.dumps(report))

    def test_preflight_command_is_readonly_and_strict_mode_fails_closed(self):
        output = StringIO()
        with patch('socket.create_connection', side_effect=AssertionError('No network')):
            call_command('check_release', as_json=True, stdout=output)
        report = json.loads(output.getvalue())
        self.assertFalse(report['ready'])
        with self.assertRaises(CommandError) as error:
            call_command('check_release', strict=True, stdout=StringIO())
        self.assertEqual(error.exception.returncode, 2)


class ProductionSettingsTests(SimpleTestCase):
    def test_production_opt_in_requires_all_credentials_and_no_local_fallback(self):
        env = production_environment()
        for missing in ('ROUNDTABLE_ENV', 'DJANGO_SECRET_KEY', 'MFA_ENCRYPTION_KEY',
                        'POSTGRES_PASSWORD', 'EMAIL_HOST_PASSWORD', 'PUBLIC_BASE_URL'):
            incomplete = dict(env)
            incomplete.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ImproperlyConfigured):
                production_values(incomplete)

    def test_wildcards_local_origins_credentials_and_database_tls_are_rejected(self):
        cases = ({'DJANGO_ALLOWED_HOSTS': '*'}, {'DJANGO_ALLOWED_HOSTS': '127.0.0.1'},
                 {'PUBLIC_BASE_URL': 'http://roundtable.example.org'},
                 {'PUBLIC_BASE_URL': 'https://user:private@roundtable.example.org'},
                 {'PUBLIC_BASE_URL': 'https://roundtable.example.org/path'},
                 {'PGSSLMODE': 'disable'}, {'EMAIL_USE_TLS': '1', 'EMAIL_USE_SSL': '1'},
                 {'EMAIL_PORT': 'private-value'}, {'DEFAULT_FROM_EMAIL': 'test@localhost'})
        for override in cases:
            with self.subTest(override=override), self.assertRaises(ImproperlyConfigured) as error:
                production_values({**production_environment(), **override})
            self.assertNotIn('private-value', str(error.exception))

    def test_no_implicit_proxy_trust_or_release_promotion(self):
        env = production_environment()
        values = production_values(env)
        self.assertIsNone(values['SECURE_PROXY_SSL_HEADER'])
        self.assertEqual(values['RELEASE_STAGE'], 'internal')
        self.assertEqual(values['DATABASES']['default']['OPTIONS']['sslmode'], 'verify-full')
        self.assertEqual(production_values({**env, 'ROUNDTABLE_TRUST_PROXY_TLS': '1'})['SECURE_PROXY_SSL_HEADER'],
                         ('HTTP_X_FORWARDED_PROTO', 'https'))


class BackupSnapshotTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-backup-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'synthetic.sqlite3'
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute('CREATE TABLE synthetic (id INTEGER PRIMARY KEY, value TEXT)')
            connection.execute('INSERT INTO synthetic VALUES (1, ?)', ('private synthetic contents',))
            connection.commit()

    def test_snapshot_and_disposable_restore_leave_source_unchanged(self):
        before = self.source.read_bytes()
        result = create_snapshot(self.source, self.root / 'new-backup')
        checked = check_snapshot(self.root / 'new-backup')
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(checked['status'], 'passed')
        self.assertEqual(before, self.source.read_bytes())
        self.assertNotIn('private synthetic contents', json.dumps(result) + json.dumps(checked))
        self.assertEqual(sorted(path.name for path in (self.root / 'new-backup').iterdir()), ['manifest.json', 'snapshot.sqlite3'])
        self.assertIn('temporary_restore', checked['checks'])

    def test_never_overwrites_existing_destination_or_missing_source(self):
        output = self.root / 'backup'
        create_snapshot(self.source, output)
        before = (output / 'snapshot.sqlite3').read_bytes()
        with self.assertRaises(BackupError):
            create_snapshot(self.source, output)
        self.assertEqual(before, (output / 'snapshot.sqlite3').read_bytes())
        with self.assertRaises(BackupError):
            create_snapshot(self.root / 'missing.sqlite3', self.root / 'new')
        self.assertFalse((self.root / 'missing.sqlite3').exists())
        self.assertFalse((self.root / 'new').exists())

    def test_corrupted_snapshot_and_manifest_path_injection_rejected(self):
        output = self.root / 'backup'
        create_snapshot(self.source, output)
        snapshot = output / 'snapshot.sqlite3'
        original = snapshot.read_bytes()
        snapshot.write_bytes(original + b'corruption')
        with self.assertRaises(BackupError):
            check_snapshot(output)
        snapshot.write_bytes(original)
        manifest_path = output / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['file'] = '../synthetic.sqlite3'
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(BackupError):
            check_snapshot(output)

    def test_fake_integrity_claim_and_duplicate_manifest_fields_rejected(self):
        output = self.root / 'backup'
        create_snapshot(self.source, output)
        manifest_path = output / 'manifest.json'
        document = manifest_path.read_text()
        manifest_path.write_text(document[:-1] + ', "passed": true}')
        with self.assertRaises(BackupError):
            check_snapshot(output)
        manifest_path.write_text(document[:-1] + ', "schema_version": 1}')
        with self.assertRaises(BackupError):
            check_snapshot(output)

    def test_invalid_database_fails_without_creating_output(self):
        bad = self.root / 'bad.sqlite3'
        bad.write_text('synthetic invalid data')
        with self.assertRaises(BackupError):
            create_snapshot(bad, self.root / 'bad-backup')
        self.assertFalse((self.root / 'bad-backup').exists())

    def test_redirected_path_is_rejected_before_read_or_create(self):
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaises(BackupError):
                create_snapshot(self.source, self.root / 'redirected')
        self.assertFalse((self.root / 'redirected').exists())

    def test_timeout_leaves_no_valid_manifest_and_never_replaces_source(self):
        before = self.source.read_bytes()
        # The first two calls establish integrity-check and backup deadlines.
        with patch('operations.backups.time.monotonic', side_effect=[0, 0, 100]):
            with self.assertRaises(BackupError):
                create_snapshot(self.source, self.root / 'timeout')
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse((self.root / 'timeout' / 'manifest.json').exists())

    def test_foreign_key_failure_prevents_snapshot(self):
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute('CREATE TABLE child (parent INTEGER REFERENCES synthetic(id))')
            connection.execute('INSERT INTO child VALUES (99)')
            connection.commit()
        with self.assertRaises(BackupError):
            create_snapshot(self.source, self.root / 'invalid-foreign-key')
        self.assertFalse((self.root / 'invalid-foreign-key').exists())

    def test_commands_need_explicit_paths_and_do_not_expose_database_contents(self):
        output = StringIO()
        call_command('backup_snapshot', source=str(self.source), output=str(self.root / 'backup'), stdout=output)
        self.assertEqual(json.loads(output.getvalue())['status'], 'passed')
        output = StringIO()
        call_command('check_backup', snapshot=str(self.root / 'backup'), stdout=output)
        self.assertEqual(json.loads(output.getvalue())['scope'], 'disposable_sqlite_restore')
        self.assertNotIn('private synthetic contents', output.getvalue())
