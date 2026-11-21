"""Local Gmail configuration tests; SMTP transport is always mocked."""
import json
import smtplib
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from accounts.local_mail import MailConfigError, load_local_mail, save_local_mail
from accounts.models import User


TEST_KEY = Fernet.generate_key().decode('ascii')
TEST_SECRET = 'abcdefghijklmnop'
TEST_SENDER = 'roundtable-test-fixture' + '@gmail.com'
RECORD = {'version': 1, 'host': 'smtp.gmail.com', 'port': 587, 'username': TEST_SENDER,
          'password': TEST_SECRET, 'sender': TEST_SENDER, 'tls': True, 'ssl': False}


@override_settings(ROOT_URLCONF='accounts.test_urls', DEBUG=True, LOCAL_MAIL_CONFIG_ENABLED=True,
                   MFA_ENCRYPTION_KEY=TEST_KEY, EMAIL_DELIVERY_ENABLED=False,
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   PUBLIC_BASE_URL='http://127.0.0.1:8910')
class LocalMailTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-mail-settings-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'connection.bin'
        self.settings_override = override_settings(LOCAL_EMAIL_PATH=self.path)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def test_encrypted_file_and_key_mismatch(self):
        save_local_mail(self.path, TEST_KEY, RECORD)
        self.assertNotIn(TEST_SECRET.encode(), self.path.read_bytes())
        self.assertEqual(load_local_mail(self.path, TEST_KEY), RECORD)
        with self.assertRaises(MailConfigError):
            load_local_mail(self.path, Fernet.generate_key())

    @patch('accounts.views.send_mail', return_value=1)
    def test_saved_gmail_configuration_is_loaded_without_restart(self, send):
        user = User.objects.create_user(email='recipient@example.test', password='Synthetic-Password-975!')
        self.assertFalse(self.client.get('/api/auth/me').json()['recovery_available'])
        save_local_mail(self.path, TEST_KEY, RECORD)
        status = self.client.get('/api/auth/me').json()
        self.assertTrue(status['recovery_available'])
        self.assertEqual(status['recovery_scope'], 'this_computer')
        response = self.client.post('/api/auth/password-reset/request',
            json.dumps({'email': user.email}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('仅可在安装圆桌的这台电脑', send.call_args.args[1])
        self.assertIn('http://127.0.0.1:8910/#reset_uid=', send.call_args.args[1])
        self.assertEqual(send.call_args.kwargs['connection'].host, 'smtp.gmail.com')

    def test_local_link_is_not_enabled_for_public_or_other_local_hosts(self):
        save_local_mail(self.path, TEST_KEY, RECORD)
        for url in ('http://127.0.0.1:8911', 'http://192.168.1.2:8910', 'http://example.com:8910'):
            with self.subTest(url=url), override_settings(PUBLIC_BASE_URL=url):
                self.assertFalse(self.client.get('/api/auth/me').json()['recovery_available'])
        with override_settings(DEBUG=False):
            self.assertFalse(self.client.get('/api/auth/me').json()['recovery_available'])

    @patch('delivery.management.commands.configure_recovery_mail.sys.stdin.isatty', return_value=True)
    @patch('delivery.management.commands.configure_recovery_mail.getpass.getpass', return_value=TEST_SECRET)
    @patch('builtins.input', side_effect=[TEST_SENDER, 'Y'])
    @patch('delivery.management.commands.configure_recovery_mail.smtplib.SMTP')
    def test_wizard_verifies_login_without_sending_mail(self, smtp, _input, _secret, _tty):
        call_command('configure_recovery_mail', stdout=StringIO())
        client = smtp.return_value.__enter__.return_value
        client.login.assert_called_once_with(TEST_SENDER, TEST_SECRET)
        client.sendmail.assert_not_called()
        self.assertNotIn(TEST_SECRET.encode(), self.path.read_bytes())
        self.assertEqual(load_local_mail(self.path, TEST_KEY), RECORD)

    @patch('delivery.management.commands.configure_recovery_mail.sys.stdin.isatty', return_value=True)
    @patch('delivery.management.commands.configure_recovery_mail.getpass.getpass', return_value=TEST_SECRET)
    @patch('builtins.input', side_effect=[TEST_SENDER, 'Y'])
    @patch('delivery.management.commands.configure_recovery_mail.smtplib.SMTP', side_effect=smtplib.SMTPException('synthetic failure'))
    def test_failed_verification_preserves_existing_configuration(self, _smtp, _input, _secret, _tty):
        save_local_mail(self.path, TEST_KEY, RECORD)
        original = self.path.read_bytes()
        with self.assertRaises(CommandError):
            call_command('configure_recovery_mail', stdout=StringIO())
        self.assertEqual(self.path.read_bytes(), original)
