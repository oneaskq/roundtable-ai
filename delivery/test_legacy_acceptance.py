"""The old single-reply endpoint cannot bypass saved owner acceptance cases."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from workspace.models import Artifact, Conversation, Message, Task
from workspace.views import lock_conversation
from .acceptance_cases import case_revision
from .models import RequestBudget


CASES = [{'stdin': 'a\n', 'expected_stdout': 'a\n'}]


class LegacyAcceptanceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='legacy-cases@example.test', password='ExamplePass123!')
        self.client.force_login(self.user)
        self.conversation = Conversation.objects.create(owner=self.user)
        self.task = Task.objects.create(owner=self.user, conversation=self.conversation, goal='保留用户需求', status='waiting')
        Message.objects.create(conversation=self.conversation, role='user', content=self.task.goal)
        self.url = f'/api/conversations/{self.conversation.pk}/respond'
        self.case_url = f'/api/conversations/{self.conversation.pk}/acceptance-cases'
        self.connection = self.mock('delivery.views.current_connection', return_value=SimpleNamespace(fingerprint='synthetic'))
        self.mock('delivery.views.chat_available', return_value=True)
        self.mock('delivery.views.connection_revision', return_value='legacy-consent')
        self.mock('workspace.views.readiness', return_value={'available_tools': []})
        self.generate = self.mock('delivery.views.generate_reply', return_value='离线普通回复')

    def mock(self, target, **kwargs):
        patched = patch(target, **kwargs)
        self.addCleanup(patched.stop)
        return patched.start()

    def post(self):
        return self.client.post(self.url, json.dumps({'confirm_cost': True, 'expected_connection': 'legacy-consent'}),
                                content_type='application/json')

    def save_cases(self):
        return self.client.put(self.case_url, json.dumps({'cases': CASES, 'expected_revision': case_revision([])}),
                               content_type='application/json')

    def test_saved_cases_reject_before_provider_configuration_or_budget(self):
        self.assertEqual(self.save_cases().status_code, 200)
        response = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'acceptance_mode_required')
        self.assertIn('多文件团队协作', response.json()['error'])
        self.generate.assert_not_called()
        self.connection.assert_not_called()
        self.assertFalse(RequestBudget.objects.exists())
        self.assertFalse(self.task.events.exists())
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.goal), ('waiting', '保留用户需求'))

    def test_start_rereads_cases_after_shared_lock(self):
        def concurrently_saved(owner, conversation_id):
            Conversation.objects.filter(pk=conversation_id).update(acceptance_cases=CASES)
            return lock_conversation(owner, conversation_id)
        with patch('delivery.views.lock_conversation', side_effect=concurrently_saved):
            self.assertEqual(self.post().status_code, 400)
        self.generate.assert_not_called()
        self.assertFalse(RequestBudget.objects.exists())

    def test_no_cases_retains_legacy_response_and_running_edit_is_rejected(self):
        def replied(*args, **kwargs):
            self.assertEqual(self.save_cases().status_code, 409)
            return '离线普通回复'
        self.generate.side_effect = replied
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        self.generate.assert_called_once()
        self.assertEqual(RequestBudget.objects.get().attempts, 1)
        self.assertEqual(Artifact.objects.get().content, '离线普通回复')
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.acceptance_cases, [])

    def test_out_of_band_cases_before_sending_stop_external_call(self):
        lock_calls = 0
        def changed_before_send(owner, conversation_id):
            nonlocal lock_calls
            lock_calls += 1
            if lock_calls == 2:
                Conversation.objects.filter(pk=conversation_id).update(acceptance_cases=CASES)
            return lock_conversation(owner, conversation_id)
        with patch('delivery.views.lock_conversation', side_effect=changed_before_send):
            response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'acceptance_changed')
        self.generate.assert_not_called()
        self.assertFalse(Artifact.objects.exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'failed')

    def test_out_of_band_cases_during_provider_call_prevent_late_writeback(self):
        def changed(*args, **kwargs):
            Conversation.objects.filter(pk=self.conversation.pk).update(acceptance_cases=CASES)
            return '不能保存的回复'
        self.generate.side_effect = changed
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'acceptance_changed')
        self.assertFalse(Message.objects.filter(role='assistant').exists())
        self.assertFalse(Artifact.objects.exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'failed')

    def test_pause_then_edit_during_provider_call_keeps_pause_and_discards_reply(self):
        def paused(*args, **kwargs):
            response = self.client.post(f'/api/tasks/{self.task.pk}/transition', json.dumps({'action': 'pause'}),
                                        content_type='application/json')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.save_cases().status_code, 200)
            return '不能保存的回复'
        self.generate.side_effect = paused
        self.assertEqual(self.post().status_code, 409)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'paused')
        self.assertFalse(Message.objects.filter(role='assistant').exists())
        self.assertFalse(Artifact.objects.exists())
