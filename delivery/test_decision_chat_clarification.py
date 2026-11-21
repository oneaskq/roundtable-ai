import json
import uuid
import hashlib
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from workspace.models import Conversation, Message, SourceMaterial, Task
from workspace.sources import sources_revision
from .background import process_next_run
from .decision_team_engine import _sources
from .models import AgentStep, TeamRun
from .test_decision_team import connections, fixture


class ChatClarificationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='clarify@example.test', password='ExamplePass123!')
        self.client.force_login(self.user)
        self.conversation = Conversation.objects.create(owner=self.user, title='讨论')
        Message.objects.create(conversation=self.conversation, role='user', content='选择方案')
        self.task = Task.objects.create(owner=self.user, conversation=self.conversation, goal='选择方案', status='waiting')
        self.team = connections()
        for path, value in [('delivery.team_views.resolve_team', self.team),
                            ('delivery.team_views.team_summary', {'readiness': 'available', 'revision': 'a' * 64})]:
            patcher = patch(path, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.url = f'/api/conversations/{self.conversation.id}/confirm-decision'

    def start(self, peer_question='', on_peer=None):
        body = {'background': True, 'client_request_id': str(uuid.uuid4()), 'confirm_cost': True,
                'expected_team': 'a' * 64, 'workflow': 'decision_v1', 'delivery_mode': 'text',
                'expected_max_calls': 14, 'expected_task': str(self.task.id),
                'expected_task_updated_at': self.task.updated_at.isoformat(),
                'expected_sources': sources_revision(SourceMaterial.objects.filter(conversation=self.conversation))}
        response = self.client.post(f'/api/conversations/{self.conversation.id}/collaborate',
                                    json.dumps(body), content_type='application/json')
        self.assertEqual(response.status_code, 202)
        def generate(messages, **kwargs):
            stage = AgentStep.objects.get(status='running').role
            if stage == 'clarifier_peer' and on_peer:
                on_peer()
            value = {'question': peer_question} if stage == 'clarifier_peer' else fixture(stage)
            return json.dumps(value, ensure_ascii=False)
        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            process_next_run()
        return TeamRun.objects.get()

    def answer(self, run, text):
        return self.client.post(self.url, json.dumps({'run_id': str(run.id), 'answer': text,
                                'confirm_continue': True, 'expected_max_calls': run.max_calls}),
                                content_type='application/json')

    def test_answer_is_recorded_once_in_frozen_context_and_not_as_confirmed_draft(self):
        run = self.start()
        self.assertEqual(self.answer(run, '速度更重要，预算改为 800 元').status_code, 202)
        self.assertEqual(self.answer(run, '速度更重要，预算改为 800 元').status_code, 200)
        self.assertEqual(self.answer(run, '不同的回答').status_code, 409)
        run.refresh_from_db()
        self.assertEqual(run.interjections.count(), 1)
        self.assertEqual(run.interjections.first().status, 'included')
        self.assertIn({'role': 'user', 'content': '速度更重要，预算改为 800 元'}, run.input_context)
        self.assertFalse(run.decision_state['brief_user_confirmed'])
        source_map = _sources(run.input_context, run.decision_state)
        self.assertNotIn('B1', source_map)
        self.assertTrue(any('预算改为 800 元' in source['content'] for source in source_map.values()))
        self.assertEqual(run.calls_used, 2)
        self.assertEqual(run.max_calls, 14)

    def test_skip_is_idempotent_and_does_not_invent_answer(self):
        run = self.start()
        self.assertEqual(self.answer(run, '').status_code, 202)
        self.assertEqual(self.answer(run, '').status_code, 200)
        run.refresh_from_db()
        self.assertFalse(run.interjections.exists())
        self.assertTrue(run.decision_state['brief']['unknowns'])
        self.assertEqual(AgentStep.objects.filter(run=run, status='completed').count(), 2)

    def test_interjection_arriving_during_clarification_stays_pending_until_answer(self):
        run = self.start()
        url = f'/api/conversations/{self.conversation.id}/team-messages'
        body = json.dumps({'run_id': str(run.id), 'client_request_id': str(uuid.uuid4()),
                           'target_provider': '', 'content': '预算还有硬上限'})
        self.assertEqual(self.client.post(url, body, content_type='application/json').status_code, 201)
        self.assertEqual(self.client.post(url, body, content_type='application/json').status_code, 200)
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used, run.interjections.count()), ('awaiting_input', 2, 1))
        self.assertEqual(self.answer(run, '偏好成本').status_code, 202)
        run.refresh_from_db()
        self.assertTrue(any('预算还有硬上限' in item['content'] for item in run.input_context))

    def test_answer_reaches_all_independent_analysts_without_peer_outputs(self):
        run = self.start()
        self.assertEqual(self.answer(run, '预算 800 元，优先速度').status_code, 202)
        packets = {}

        def generate(messages, **kwargs):
            stage = AgentStep.objects.get(status='running').role
            packets[stage] = json.loads(messages[0]['content'])
            return json.dumps(fixture(stage), ensure_ascii=False)

        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            process_next_run()
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(packets['analysis_p1'], packets['analysis_p2'])
        self.assertEqual(packets['analysis_p2'], packets['analysis_p3'])
        self.assertIn('预算 800 元，优先速度', json.dumps(packets['analysis_p1'], ensure_ascii=False))
        self.assertNotIn('p1 的条件建议', json.dumps(packets['analysis_p2'], ensure_ascii=False))

    def test_no_questions_continues_without_claiming_user_confirmed_draft(self):
        body = {'background': True, 'client_request_id': str(uuid.uuid4()), 'confirm_cost': True,
                'expected_team': 'a' * 64, 'workflow': 'decision_v1', 'delivery_mode': 'text',
                'expected_max_calls': 14, 'expected_task': str(self.task.id),
                'expected_task_updated_at': self.task.updated_at.isoformat(),
                'expected_sources': sources_revision(SourceMaterial.objects.filter(conversation=self.conversation))}
        response = self.client.post(f'/api/conversations/{self.conversation.id}/collaborate',
                                    json.dumps(body), content_type='application/json')
        self.assertEqual(response.status_code, 202)
        packets = {}

        def generate(messages, **kwargs):
            stage = AgentStep.objects.get(status='running').role
            packets[stage] = json.loads(messages[0]['content'])
            value = fixture(stage)
            if stage == 'clarifier':
                value['questions'] = []
            return json.dumps(value, ensure_ascii=False)

        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            process_next_run()
        run = TeamRun.objects.get()
        self.assertEqual(run.status, 'completed')
        self.assertFalse(run.steps.filter(role='clarifier_peer').exists())
        self.assertFalse(run.decision_state['brief_user_confirmed'])
        self.assertNotIn('confirmed_at', run.decision_state['brief'])
        self.assertNotIn('B1', [source['reference'] for source in packets['analysis_p1']['sources']])
        self.assertEqual(packets['analysis_p1'], packets['analysis_p2'])

    def test_first_question_is_visible_while_peer_is_still_working(self):
        from .team_views import serialize_run
        snapshots = []
        def read_pending_discussion():
            run = TeamRun.objects.get()
            snapshots.append((run.status, [(item['role'], item['content'])
                              for item in serialize_run(run)['discussion'] if item['content']]))

        run = self.start('多久需要决定？', on_peer=read_pending_discussion)
        self.assertEqual(snapshots, [('running', [('clarifier', '成本和速度哪个更重要？')])])
        self.assertEqual(run.status, 'awaiting_input')
        self.assertEqual([item['role'] for item in serialize_run(run)['discussion'] if item['content']],
                         ['clarifier', 'clarifier_peer'])

    def test_duplicate_peer_question_is_not_attributed_as_second_question(self):
        from .team_views import serialize_run
        run = self.start('成本和速度哪个更重要？')
        discussion = serialize_run(run)['discussion']
        self.assertEqual(len(run.decision_state['brief_draft']['questions']), 1)
        self.assertEqual([item['role'] for item in discussion if item['content']], ['clarifier'])

    def test_skipped_questions_remain_visible_as_unresolved(self):
        run = self.start('方案需要多久实施？')
        self.assertEqual(self.answer(run, '').status_code, 202)
        def generate(messages, **kwargs):
            return json.dumps(fixture(AgentStep.objects.get(status='running').role), ensure_ascii=False)
        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            process_next_run()
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertTrue(any('成本和速度' in item for item in run.decision_state['unresolved']))
        self.assertTrue(any('多久实施' in item for item in run.decision_state['unresolved']))

    def test_legacy_waiting_run_without_peer_step_resumes(self):
        run = self.start()
        run.steps.filter(role='clarifier_peer').delete()
        state = dict(run.decision_state)
        for key in ('clarification_calls', 'clarification_questions', 'clarify_peer_enabled'):
            state.pop(key, None)
        TeamRun.objects.filter(pk=run.pk).update(calls_used=1, decision_state=state)
        run.refresh_from_db()
        self.assertEqual(self.answer(run, '成本优先').status_code, 202)
        def generate(messages, **kwargs):
            return json.dumps(fixture(AgentStep.objects.get(status='running').role), ensure_ascii=False)
        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            process_next_run()
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used), ('completed', 11))

    def test_no_findings_skips_responses_preserves_initial_views_and_late_user_input(self):
        run = self.start()
        self.assertEqual(self.answer(run, '优先速度').status_code, 202)
        packets = {}

        def generate(messages, **kwargs):
            stage = AgentStep.objects.get(status='running').role
            packets[stage] = json.loads(messages[0]['content'])
            if stage == 'review_p3':
                response = self.client.post(f'/api/conversations/{self.conversation.id}/team-messages',
                    json.dumps({'run_id': str(run.id), 'content': '预算现在改为 500 元', 'target_provider': '',
                                'client_request_id': str(uuid.uuid4())}), content_type='application/json')
                self.assertEqual(response.status_code, 201)
            value = {'findings': []} if stage.startswith('review_') else fixture(stage)
            return json.dumps(value, ensure_ascii=False)

        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            process_next_run()
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used), ('completed', 9))
        self.assertEqual(run.steps.filter(role__startswith='review_', status='completed').count(), 3)
        self.assertEqual(run.steps.filter(role__startswith='response_', status='skipped').count(), 3)
        self.assertFalse(any(key.startswith('response_') for key in packets))
        self.assertEqual(run.decision_state['final_opinions'], [])
        self.assertEqual(len(run.decision_state['analyses']), 3)
        self.assertIn('预算现在改为 500 元', json.dumps(packets['synthesizer'], ensure_ascii=False))
        self.assertTrue(run.decision_state['user_updates_during_discussion'])
        self.assertTrue(any('重新开展独立分析' in item for item in run.decision_state['unresolved']))
        result = Message.objects.get(conversation=self.conversation, role='assistant').content
        self.assertIn('独立分析时的初始建议，未作回应后表态', result)
        self.assertIn('p1 的条件建议', result)

    def test_only_actual_findings_trigger_a_response_and_final_opinion(self):
        run = self.start()
        self.assertEqual(self.answer(run, '优先速度').status_code, 202)
        packets = {}

        def generate(messages, **kwargs):
            stage = AgentStep.objects.get(status='running').role
            packets[stage] = json.loads(messages[0]['content'])
            value = {'findings': []} if stage in ('review_p2', 'review_p3') else fixture(stage)
            return json.dumps(value, ensure_ascii=False)

        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            process_next_run()
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used), ('completed', 10))
        self.assertEqual(run.steps.get(role='response_p2').status, 'completed')
        self.assertEqual(run.steps.filter(role__in=['response_p1', 'response_p3'], status='skipped').count(), 2)
        self.assertEqual([x['participant'] for x in run.decision_state['final_opinions']], ['p2'])
        self.assertEqual(len(run.decision_state['responses']), 1)
        result = Message.objects.get(conversation=self.conversation, role='assistant').content
        self.assertIn('p1 的条件建议', result)
        self.assertIn('p3 的条件建议', result)

    def test_empty_extra_review_does_not_fabricate_an_extra_response(self):
        content = '额外费用200元；报价有效30天'
        SourceMaterial.objects.create(owner=self.user, conversation=self.conversation, kind='file',
            name='报价', media_type='text/plain', content=content, byte_size=len(content.encode()),
            sha256=hashlib.sha256(content.encode()).hexdigest(), included=True, metadata={})
        run = self.start()
        self.assertEqual(self.answer(run, '优先速度').status_code, 202)
        stages = []

        def generate(messages, **kwargs):
            stage = AgentStep.objects.get(status='running').role
            stages.append(stage)
            value = {'findings': []} if stage == 'extra_review' else fixture(stage, extra=True)
            return json.dumps(value, ensure_ascii=False)

        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            process_next_run()
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used), ('completed', 13))
        self.assertEqual(run.steps.get(role='extra_review').status, 'completed')
        self.assertEqual(run.steps.get(role='extra_response').status, 'skipped')
        self.assertNotIn('extra_response', stages)
        self.assertEqual(run.decision_state['extra_round']['responses'], [])
        self.assertEqual(len(run.decision_state['final_opinions']), 3)

    def test_two_real_questioners_and_extra_review_fit_confirmed_budget(self):
        from .team_views import serialize_run
        content = '额外费用200元；报价有效30天'
        SourceMaterial.objects.create(owner=self.user, conversation=self.conversation, kind='file',
            name='报价', media_type='text/plain', content=content, byte_size=len(content.encode()),
            sha256=hashlib.sha256(content.encode()).hexdigest(), included=True, metadata={})
        run = self.start('方案需要多久实施？')
        self.assertEqual(run.calls_used, 2)
        discussion = [item for item in serialize_run(run)['discussion'] if item['content']]
        self.assertEqual([item['role'] for item in discussion], ['clarifier', 'clarifier_peer'])
        self.assertNotEqual(discussion[0]['provider'], discussion[1]['provider'])
        self.assertIn('成本和速度', discussion[0]['content'])
        self.assertNotIn('多久实施', discussion[0]['content'])
        self.assertIn('多久实施', discussion[1]['content'])
        self.assertEqual(self.answer(run, '优先速度，期限两周').status_code, 202)
        packets = {}

        def generate(messages, **kwargs):
            stage = AgentStep.objects.get(status='running').role
            packets[stage] = json.loads(messages[0]['content'])
            if stage == 'analysis_p1':
                response = self.client.post(f'/api/conversations/{self.conversation.id}/team-messages',
                    json.dumps({'run_id': str(run.id), 'content': '请千问考虑维护成本', 'target_provider': 'qwen',
                                'client_request_id': str(uuid.uuid4())}), content_type='application/json')
                self.assertEqual(response.status_code, 201)
            return json.dumps(fixture(stage, extra=True), ensure_ascii=False)

        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate), \
             patch('delivery.team_views.generate_reply') as mention:
            process_next_run()
        mention.assert_not_called()
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used, run.max_calls), ('completed', 14, 14))
        self.assertEqual(packets['analysis_p1'], packets['analysis_p2'])
        self.assertIn('期限两周', json.dumps(packets['analysis_p3'], ensure_ascii=False))
        self.assertIn('考虑维护成本', json.dumps(packets['review_p1'], ensure_ascii=False))
        self.assertEqual(run.interjections.get(target_provider='qwen').status, 'included')
        self.assertIn('extra_review', packets)
        self.assertIn('extra_response', packets)
