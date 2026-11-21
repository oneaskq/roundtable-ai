"""Exercise brief replies without contacting an external model."""
import hashlib
import json
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from workspace.models import Conversation, Message, SourceMaterial, Task
from workspace.sources import sources_revision
from .background import process_next_run
from .models import AgentStep, TeamInterjection, TeamRun
from .test_decision_team import connections, fixture


class QuickChatTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='quick@example.test', password='ExamplePass123!')
        self.client.force_login(self.user)
        self.conversation = Conversation.objects.create(owner=self.user, title='聊天')
        self.message = Message.objects.create(conversation=self.conversation, role='user', content='hello')
        self.task = Task.objects.create(owner=self.user, conversation=self.conversation, goal='hello', status='waiting')
        self.revision = 'a' * 64
        self.team_patch = patch('delivery.team_views.resolve_team', return_value=connections())
        self.summary_patch = patch('delivery.team_views.team_summary', return_value={'readiness': 'available', 'revision': self.revision})
        self.team_patch.start()
        self.summary_patch.start()
        self.addCleanup(self.team_patch.stop)
        self.addCleanup(self.summary_patch.stop)

    def start(self, content):
        self.message.content = content
        self.message.save(update_fields=['content'])
        self.task.goal = content
        self.task.save()
        body = {'background': True, 'client_request_id': str(uuid.uuid4()), 'confirm_cost': True,
                'expected_team': self.revision, 'workflow': 'decision_v1', 'delivery_mode': 'text',
                'expected_max_calls': 14, 'expected_task': str(self.task.id),
                'expected_task_updated_at': self.task.updated_at.isoformat(),
                'expected_sources': sources_revision(SourceMaterial.objects.filter(conversation=self.conversation))}
        result = self.client.post(f'/api/conversations/{self.conversation.id}/collaborate',
                                  json.dumps(body), content_type='application/json')
        self.assertEqual(result.status_code, 202, result.content)

    def assert_quick_result(self, content, answer, generate):
        def generate_member(messages, **kwargs):
            step = AgentStep.objects.get(status='running')
            value = {'mode': 'quick', 'quick_reply': answer} if step.role == 'clarifier' else {'reply': step.provider + ' 的回复'}
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generate_member
        self.start(content)
        process_next_run()
        run = TeamRun.objects.get()
        self.assertEqual((run.status, run.calls_used, run.decision_state.get('response_kind')), ('completed', 3, 'quick_group'))
        self.assertIn(answer, Message.objects.get(conversation=self.conversation, role='assistant').content)
        self.assertEqual(generate.call_count, 3)
        from .team_views import serialize_run
        discussion = serialize_run(run)['discussion']
        self.assertEqual({item['provider'] for item in discussion}, {'deepseek', 'kimi', 'qwen'})
        self.assertEqual(len(discussion), 3)
        self.assertTrue(run.decision_state.get('result_message_id'))
        self.assertFalse(run.steps.filter(status='pending').exists())
        self.assertEqual(run.steps.filter(status='completed').count(), 3)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_greeting_receives_each_members_natural_reply(self, generate):
        self.assert_quick_result('hello', '你好！', generate)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_simple_question_can_receive_direct_answer(self, generate):
        self.assert_quick_result('1+1等于几？', '等于 2。', generate)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_failed_member_keeps_earlier_reply_without_inventing_others(self, generate):
        from .provider import ProviderError
        from .team_views import serialize_run
        def answer(messages, **kwargs):
            step = AgentStep.objects.get(status='running')
            if step.role == 'clarifier':
                return json.dumps({'mode': 'quick', 'quick_reply': '你好！'}, ensure_ascii=False)
            raise ProviderError('模拟连接失败')
        generate.side_effect = answer
        self.start('hello')
        process_next_run()
        run = TeamRun.objects.get()
        self.assertEqual((run.status, run.calls_used), ('failed', 2))
        self.assertEqual([item['provider'] for item in serialize_run(run)['discussion']], ['deepseek'])
        self.assertFalse(Message.objects.filter(conversation=self.conversation, role='assistant').exists())

    @patch('delivery.decision_team_engine.generate_reply')
    def test_explicit_complex_question_cannot_be_bypassed_by_quick_classification(self, generate):
        def reply(messages, **kwargs):
            stage = AgentStep.objects.get(status='running').role
            value = {'mode': 'quick', 'quick_reply': '随便选一个。'} if stage == 'clarifier' else fixture(stage)
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = reply
        text = '请比较两个方案的风险'
        self.start(text)
        process_next_run()
        run = TeamRun.objects.get()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.steps.filter(role__startswith='analysis_', status='completed').count(), 3)
        self.assertEqual(run.decision_state['brief_draft']['goal'], text)
        self.assertNotIn('随便选一个', Message.objects.get(conversation=self.conversation, role='assistant').content)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_attached_source_prevents_quick_bypass(self, generate):
        content = '模拟资料'
        SourceMaterial.objects.create(owner=self.user, conversation=self.conversation, kind='file',
            name='资料', media_type='text/plain', content=content, byte_size=len(content.encode()),
            sha256=hashlib.sha256(content.encode()).hexdigest(), included=True, metadata={})
        def reply(messages, **kwargs):
            stage = AgentStep.objects.get(status='running').role
            value = {'mode': 'quick', 'quick_reply': '你好！'} if stage == 'clarifier' else fixture(stage)
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = reply
        self.start('hello')
        process_next_run()
        run = TeamRun.objects.get()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.steps.filter(role__startswith='analysis_', status='completed').count(), 3)
        self.assertNotIn('你好！', Message.objects.get(conversation=self.conversation, role='assistant').content)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_new_interjection_during_quick_call_is_not_lost_or_answered_by_stale_reply(self, generate):
        def reply(messages, **kwargs):
            step = AgentStep.objects.get(status='running')
            payload = {'run_id': str(step.run_id), 'content': '我还有补充', 'target_provider': '',
                       'client_request_id': str(uuid.uuid4())}
            result = self.client.post(f'/api/conversations/{self.conversation.id}/team-messages',
                                      json.dumps(payload), content_type='application/json')
            self.assertEqual(result.status_code, 201)
            return json.dumps({'mode': 'quick', 'quick_reply': '你好！'}, ensure_ascii=False)
        generate.side_effect = reply
        self.start('hello')
        process_next_run()
        run = TeamRun.objects.get()
        self.assertNotEqual(run.status, 'completed')
        self.assertEqual(TeamInterjection.objects.get().status, 'pending')
        self.assertFalse(Message.objects.filter(conversation=self.conversation, role='assistant').exists())
