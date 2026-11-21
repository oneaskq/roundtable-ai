import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from workspace.models import Conversation, Message, Task
from .models import AgentStep, TeamInterjection, TeamRun


class RunStatusTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='status@example.test', password='testpass')
        self.other = get_user_model().objects.create_user(email='status-other@example.test', password='testpass')
        self.client.force_login(self.user)
        self.conversation = Conversation.objects.create(owner=self.user, title='状态')
        Message.objects.create(conversation=self.conversation, role='user', content='长对话内容' * 1000)
        self.task = Task.objects.create(owner=self.user, conversation=self.conversation, goal='讨论', status='running')
        self.run = TeamRun.objects.create(task=self.task, execution_id=uuid.uuid4(), status='running', team_revision='a')
        self.step = AgentStep.objects.create(run=self.run, position=1, role='clarifier', label='提问', provider='test', model='test', status='running')
        self.url = f'/api/conversations/{self.conversation.id}/runs/{self.run.id}/status'

    def token(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'no-store')
        return response.json()['version']

    def test_version_tracks_discussion_and_terminal_state_without_full_history(self):
        first = self.token()
        self.assertEqual(first, self.token())
        # Heartbeats are internal and should not cause a full reload.
        TeamRun.objects.filter(pk=self.run.pk).update(updated_at=timezone.now())
        self.assertEqual(first, self.token())
        AgentStep.objects.filter(pk=self.step.pk).update(status='completed', output='一条回复', public_order=1)
        step = self.token()
        self.assertNotEqual(first, step)
        item = TeamInterjection.objects.create(run=self.run, client_request_id=uuid.uuid4(), content='补充', status='pending')
        added = self.token()
        self.assertNotEqual(step, added)
        TeamInterjection.objects.filter(pk=item.pk).update(status='included')
        included = self.token()
        self.assertNotEqual(added, included)
        TeamRun.objects.filter(pk=self.run.pk).update(decision_state={'phase': 'awaiting_input'}, status='awaiting_input')
        self.assertNotEqual(included, self.token())
        Task.objects.filter(pk=self.task.pk).update(status='paused')
        response = self.client.get(self.url)
        self.assertEqual(response.json()['task_status'], 'paused')
        full = self.client.get(f'/api/conversations/{self.conversation.id}')
        self.assertLess(len(response.content), len(full.content))
        self.assertLess(len(response.content), 250)

    def test_other_owner_and_unknown_run_cannot_read_status(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(f'/api/conversations/{self.conversation.id}/runs/{uuid.uuid4()}/status').status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 401)
