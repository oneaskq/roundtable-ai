import json
import os
from unittest.mock import patch

from django.test import TestCase

from accounts.models import MFADevice, User


class ReadinessApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('ordinary@example.test', 'synthetic-user-password')
        self.admin = User.objects.create_superuser('admin@example.test', 'synthetic-admin-password')

    def test_only_verified_administrators_can_read_the_report(self):
        path = '/api/release-readiness'
        self.assertEqual(self.client.get(path).status_code, 401)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(path).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(path).status_code, 403)
        session = self.client.session
        session['mfa_verified'] = True
        session.save()
        self.assertEqual(self.client.get(path).status_code, 403)
        MFADevice.objects.create(user=self.admin, enabled=True, encrypted_secret='synthetic-not-a-real-secret')
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertFalse(response.json()['report']['ready'])
        self.assertGreaterEqual(response.json()['report']['counts']['pending'], 9)

    def test_report_does_not_call_network_or_expose_private_configuration(self):
        self.client.force_login(self.admin)
        MFADevice.objects.create(user=self.admin, enabled=True, encrypted_secret='synthetic-not-a-real-secret')
        session = self.client.session
        session['mfa_verified'] = True
        session.save()
        private_value = 'synthetic-private-environment-value'
        with patch.dict(os.environ, {'DJANGO_SECRET_KEY': private_value}), \
                patch('socket.create_connection', side_effect=AssertionError('Network not permitted')):
            response = self.client.get('/api/release-readiness')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(private_value, json.dumps(response.json()))
        self.assertFalse(response.json()['report']['ready'])
