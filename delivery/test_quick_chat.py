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

    def start(self, content, *, calls=14, count=None, expected_status=202):
        self.message.content = content
        self.message.save(update_fields=['content'])
        self.task.goal = content
        self.task.save()
        body = {'background': True, 'client_request_id': str(uuid.uuid4()), 'confirm_cost': True,
                'expected_team': self.revision, 'workflow': 'decision_v1', 'delivery_mode': 'text',
                'expected_max_calls': calls, 'expected_task': str(self.task.id),
                'expected_task_updated_at': self.task.updated_at.isoformat(),
                'expected_sources': sources_revision(SourceMaterial.objects.filter(conversation=self.conversation))}
        if count:
            body['participant_count'] = count
        result = self.client.post(f'/api/conversations/{self.conversation.id}/collaborate',
                                  json.dumps(body), content_type='application/json')
        self.assertEqual(result.status_code, expected_status, result.content)

    def test_address_syntax_is_narrow_and_group_requests_are_preserved(self):
        from .member_address import addressed_provider
        for text, provider in [('千问呢？', 'qwen'), ('那 Kimi 呢', 'kimi'),
                               ('@DeepSeek 你怎么看？', 'deepseek'), ('Kimi，你觉得可行吗？', 'kimi')]:
            self.assertEqual(addressed_provider(text), provider, text)
        for text in ['千问的接口价格是多少', 'Kimi 和千问呢？', 'Kimi.ai 靠谱吗', 'https://example.org/@Kimi',
                     'a@kimi.example', '@Kimi 请整个团队一起分析', '@Kimi @千问 你们怎么看', '资料里写着千问呢']:
            self.assertEqual(addressed_provider(text), '', text)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_direct_member_round_calls_only_named_provider_with_prior_visible_history(self, generate):
        Message.objects.create(conversation=self.conversation, role='assistant', content='DeepSeek：方案 A 的通勤更长')
        generate.return_value = '{"reply":"如果每周返校，先核对通勤时间。"}'
        self.start('千问呢？', calls=1)
        process_next_run()
        run = TeamRun.objects.get()
        self.assertEqual((run.status, run.max_calls, run.calls_used), ('completed', 1, 1))
        self.assertEqual((run.steps.get().provider, run.decision_state['response_kind']), ('qwen', 'direct_member'))
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(generate.call_args.kwargs['connection'].provider, 'qwen')
        self.assertIn('通勤更长', generate.call_args.args[0][0]['content'])
        self.assertIn('核对通勤时间', Message.objects.get(pk=run.decision_state['result_message_id']).content)

    def test_direct_address_outside_selected_team_or_one_call_consent_for_full_team_is_rejected(self):
        self.start('千问呢', count=2, expected_status=409)
        self.start('请整个团队聊聊', calls=1, expected_status=400)
        self.assertFalse(TeamRun.objects.exists())

    @patch('delivery.decision_team_engine.generate_reply')
    def test_direct_member_failure_does_not_fall_back_or_claim_a_reply(self, generate):
        from .provider import ProviderError
        generate.side_effect = ProviderError('模拟网络超时')
        self.start('@Kimi 你怎么看？')
        process_next_run()
        run = TeamRun.objects.get()
        self.assertEqual((run.status, run.calls_used, generate.call_count), ('failed', 1, 1))
        self.assertEqual(run.steps.get().provider, 'kimi')
        self.assertFalse(Message.objects.filter(conversation=self.conversation, role='assistant').exists())

    @patch('delivery.team_views.generate_reply')
    @patch('delivery.decision_team_engine.generate_reply')
    def test_named_followup_during_quick_group_goes_to_requested_member_only(self, first, target):
        def posted(messages, **kwargs):
            step = AgentStep.objects.get(status='running')
            payload = {'run_id': str(step.run_id), 'content': '千问呢？', 'client_request_id': str(uuid.uuid4())}
            endpoint = f'/api/conversations/{self.conversation.id}/team-messages'
            for status in (201, 200):
                response = self.client.post(endpoint, json.dumps(payload), content_type='application/json')
                self.assertEqual(response.status_code, status, response.content)
            return '{"mode":"quick","quick_reply":"先听大家意见。"}'
        first.side_effect = posted
        target.return_value = '我的看法是先明确时间。'
        self.start('hello')
        process_next_run()
        run = TeamRun.objects.get()
        self.assertEqual((run.status, run.calls_used), ('completed', 2))
        self.assertEqual(first.call_count, 1)
        self.assertEqual(target.call_count, 1)
        self.assertEqual(target.call_args.kwargs['connection'].provider, 'qwen')
        mention = run.interjections.get()
        self.assertEqual((mention.status, mention.target_provider), ('answered', 'qwen'))
        self.assertFalse(run.steps.filter(provider='kimi', status='completed').exists())

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
    def test_ordinary_followup_keeps_referent_for_the_first_member(self, generate):
        self.message.content = '给我两个名字'
        self.message.save(update_fields=['content'])
        Message.objects.create(conversation=self.conversation, role='assistant', content='River 和 Cloud')
        question = '把第二个名字译成法语'
        self.message = Message.objects.create(conversation=self.conversation, role='user', content=question)

        def answer(messages, **kwargs):
            packet = json.loads(messages[0]['content'])
            if AgentStep.objects.get(status='running').role == 'clarifier':
                self.assertEqual(packet['preceding_public_reply'], 'River 和 Cloud')
                self.assertEqual(packet['conversation'][-1]['content'], question)
                return json.dumps({'mode': 'quick', 'quick_reply': 'Cloud 可以译作 Nuage。'})
            return json.dumps({'reply': 'Nuage。'})

        generate.side_effect = answer
        self.start(question)
        process_next_run()
        run = TeamRun.objects.get()
        self.assertEqual(run.status, 'completed')
        self.assertIn('Cloud 可以译作 Nuage', Message.objects.get(pk=run.decision_state['result_message_id']).content)

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
