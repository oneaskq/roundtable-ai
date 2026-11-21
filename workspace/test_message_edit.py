import json
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from delivery.background import process_next_run
from delivery.models import TeamRun
from delivery.test_decision_team import connections
from .models import Conversation, Message, Task
from .views import export_data


class MessageEditTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='editor@example.test', password='ExamplePass123!')
        self.other = get_user_model().objects.create_user(email='other-edit@example.test', password='ExamplePass123!')
        self.client.force_login(self.user)
        self.conversation = Conversation.objects.create(owner=self.user, title='旧问题')
        self.first = Message.objects.create(conversation=self.conversation, role='user', content='旧问题')
        self.old_task = Task.objects.create(owner=self.user, conversation=self.conversation, goal='旧问题', status='completed')
        self.old_answer = Message.objects.create(conversation=self.conversation, role='assistant', content='旧答案')
        self.next_message = Message.objects.create(conversation=self.conversation, role='user', content='旧分支补充')
        self.pending = Task.objects.create(owner=self.user, conversation=self.conversation, goal='旧分支补充', status='waiting')
        self.request_id = str(uuid.uuid4())
        self.body = {'content': '新问题', 'expected_content': '旧问题', 'client_edit_id': self.request_id}
        self.url = f'/api/messages/{self.first.id}/edit'

    def post(self, body=None):
        return self.client.post(self.url, json.dumps(body or self.body), content_type='application/json')

    def test_edit_branches_without_destroying_original_or_later_replies_and_is_idempotent(self):
        response = self.post()
        self.assertEqual(response.status_code, 201, response.content)
        result = response.json()
        revised = Message.objects.get(pk=result['message']['id'])
        self.first.refresh_from_db()
        self.old_answer.refresh_from_db()
        self.next_message.refresh_from_db()
        self.old_task.refresh_from_db()
        self.pending.refresh_from_db()
        self.assertEqual(self.first.content, '旧问题')
        self.assertEqual(revised.edited_from_id, self.first.id)
        self.assertEqual({m.superseded_by_id for m in (self.first, self.old_answer, self.next_message)}, {revised.id})
        self.assertEqual(self.pending.status, 'cancelled')
        self.assertEqual(self.old_task.superseded_by_message_id, revised.id)
        self.assertEqual(list(Message.objects.filter(conversation=self.conversation, superseded_by__isnull=True)
                              .values_list('content', flat=True)), ['新问题'])
        self.assertEqual(Task.objects.get(edit_message=revised).status, 'waiting')
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(Message.objects.filter(edit_request_id=self.request_id).count(), 1)
        exported = export_data(self.user)
        self.assertEqual(len(exported['messages']), 4)
        self.assertTrue(any(m['content'] == '旧答案' and m['superseded_by'] for m in exported['messages']))

    def test_other_user_cannot_edit_and_stale_requests_do_not_branch(self):
        self.client.force_login(self.other)
        self.assertEqual(self.post().status_code, 404)
        self.client.force_login(self.user)
        stale = {**self.body, 'expected_content': '不是原文'}
        self.assertEqual(self.post(stale).status_code, 409)
        self.assertEqual(Message.objects.filter(conversation=self.conversation).count(), 3)

    def test_running_task_prevents_edit_without_changing_messages(self):
        self.pending.status = 'running'
        self.pending.execution_id = uuid.uuid4()
        self.pending.save()
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(Message.objects.filter(conversation=self.conversation).count(), 3)
        self.assertFalse(Message.objects.filter(superseded_by__isnull=False).exists())

    def test_equal_clock_ticks_preserve_message_order_and_edit_branch(self):
        conversation = Conversation.objects.create(owner=self.user, title='同时钟')
        tick = timezone.now()
        # Reverse UUID order so a timestamp tie would visibly reverse history.
        with patch('django.utils.timezone.now', return_value=tick):
            first = Message.objects.create(id=uuid.UUID(int=3), conversation=conversation, role='user', content='最初问题')
            answer = Message.objects.create(id=uuid.UUID(int=2), conversation=conversation, role='assistant', content='旧回答')
            followup = Message.objects.create(id=uuid.UUID(int=1), conversation=conversation, role='user', content='后续补充')
            Task.objects.create(owner=self.user, conversation=conversation, goal='后续补充', status='waiting')
            response = self.client.post(f'/api/messages/{first.id}/edit', json.dumps({
                'content': '修订问题', 'expected_content': first.content, 'client_edit_id': str(uuid.uuid4()),
            }), content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertLess(first.created_at, answer.created_at)
        self.assertLess(answer.created_at, followup.created_at)
        self.assertEqual(list(Message.objects.filter(conversation=conversation, superseded_by__isnull=True)
                              .values_list('content', flat=True)), ['修订问题'])

    @patch('delivery.decision_team_engine.generate_reply')
    def test_new_model_run_only_receives_current_branch(self, generate):
        response = self.post()
        self.assertEqual(response.status_code, 201)
        task = Task.objects.get(edit_message_id=response.json()['message']['id'])
        team = connections()
        revision = 'a' * 64
        captured = []
        def reply(messages, **kwargs):
            captured.append(json.loads(messages[0]['content']))
            from delivery.models import AgentStep
            step = AgentStep.objects.get(status='running')
            return json.dumps({'mode': 'quick', 'quick_reply': '这是新的回答。'} if step.role == 'clarifier'
                              else {'reply': '这是另一位成员的新回答。'}, ensure_ascii=False)
        generate.side_effect = reply
        with patch('delivery.team_views.resolve_team', return_value=team), \
                patch('delivery.team_views.team_summary', return_value={'readiness': 'available', 'revision': revision}):
            body = {'background': True, 'client_request_id': str(uuid.uuid4()), 'confirm_cost': True,
                    'expected_team': revision, 'workflow': 'decision_v1', 'delivery_mode': 'text',
                    'expected_max_calls': 14, 'expected_task': str(task.id),
                    'expected_task_updated_at': task.updated_at.isoformat()}
            self.assertEqual(self.client.post(f'/api/conversations/{self.conversation.id}/collaborate',
                json.dumps(body), content_type='application/json').status_code, 202)
            process_next_run()
        run = TeamRun.objects.get(task=task)
        self.assertEqual((run.status, run.calls_used), ('completed', 3))
        packet = json.dumps(captured, ensure_ascii=False)
        self.assertIn('新问题', packet)
        self.assertNotIn('旧问题', packet)
        self.assertNotIn('旧分支补充', packet)
        self.assertNotIn('旧答案', packet)
