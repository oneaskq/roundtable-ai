import json
import hashlib
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from workspace.models import Conversation, Message, SourceMaterial, Task
from workspace.sources import serialize_source, sources_revision
from .background import process_next_run
from .models import AgentStep, TeamRun
from .decision_team_engine import make_state, prepare_brief
from .report_followups import resolve_report, same_selected_sources
from .test_decision_team import connections, fixture


class ReportFollowupTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(email='reader@example.test')
        self.client.force_login(self.owner)
        self.chat = Conversation.objects.create(owner=self.owner)

    def report(self, body, *, chat=None, kind='discussion', sources=None, advice=None):
        chat = chat or self.chat
        task = Task.objects.create(conversation=chat, owner=chat.owner, goal='比较', status='completed')
        msg = Message.objects.create(conversation=chat, role='assistant', content=body)
        TeamRun.objects.create(task=task, execution_id=uuid.uuid4(), team_revision='a' * 64,
            workflow='decision_v1', status='completed', source_snapshot=sources or [], decision_state={'response_kind': kind,
                'result_message_id': str(msg.pk), 'summary': {'recommendation': advice or body}, 'evidence': [{'id': 'E1', 'type': 'calculation',
                    'status': 'executed', 'expression': '100*6', 'result': '600'}]})
        return msg

    def test_receipt_after_report_is_saved_once_without_a_new_task_or_model_call(self):
        self.report('先向供应商核实报价。')
        url = f'/api/conversations/{self.chat.id}'
        data = {'content': '可以的。', 'client_message_id': str(uuid.uuid4())}
        for expected in (201, 200):
            response = self.client.post(url, data, content_type='application/json')
            self.assertEqual(response.status_code, expected)
            self.assertEqual(response.json()['task']['status'], 'completed')
        self.assertEqual(self.chat.messages.filter(role='user').count(), 1)
        self.assertEqual(self.chat.tasks.count(), 1)
        self.assertFalse(TeamRun.objects.exclude(status='completed').exists())
        next_post = self.client.post(url, {'content':'好的，帮我写一封询价邮件。'}, content_type='application/json')
        self.assertEqual(next_post.json()['task']['status'], 'waiting')

    def test_new_material_keeps_a_short_followup_actionable(self):
        self.report('先核对报价。')
        run = TeamRun.objects.get()
        run.source_revision = sources_revision([])
        run.save(update_fields=['source_revision'])
        SourceMaterial.objects.create(conversation=self.chat, owner=self.owner, kind='file', name='补充报价',
                                      content='报价已含税', byte_size=15, sha256='a' * 64)
        response = self.client.post(f'/api/conversations/{self.chat.id}', {'content':'可以的'}, content_type='application/json')
        self.assertEqual(response.json()['task']['status'], 'waiting')

    @patch('delivery.decision_team_engine.generate_reply')
    def test_yes_to_an_offer_keeps_the_preceding_question_for_the_clarifier(self, generate):
        advice = '需要我帮你起草询价邮件吗？'
        body = '## 当前建议与条件\n' + advice + '\n\n## 下一步\n核对总价。'
        self.report(body, advice=advice)
        result = self.client.post(f'/api/conversations/{self.chat.id}', {'content':'可以的'}, content_type='application/json')
        self.assertEqual(result.json()['task']['status'], 'waiting')
        team = connections()
        generate.return_value = json.dumps({'mode':'quick', 'quick_reply':'询价邮件草稿'}, ensure_ascii=False)
        context = [{'role':'assistant','content':body},{'role':'user','content':'可以的'}]
        prepare_brief(context, team, make_state(team, []), lambda *_: None, lambda *_: None)
        packet = json.loads(generate.call_args.args[0][0]['content'])
        self.assertEqual(packet['preceding_question_or_offer'], advice)

    def test_reference_respects_branch_conversation_and_explicit_older_quote(self):
        other = Conversation.objects.create(owner=self.owner)
        self.report('私有资料', chat=other)
        first = self.report('第一份建议')
        obsolete = self.report('被编辑的建议')
        obsolete.superseded_by = first
        obsolete.save(update_fields=['superseded_by'])
        second = self.report('第二份建议')
        self.assertIsNone(resolve_report(self.chat, '帮我比较这两份预算'))
        self.assertEqual(resolve_report(self.chat, '你刚才的第二条为什么成立？')['message_id'], str(second.pk))
        self.assertEqual(resolve_report(self.chat, '那第二点呢？')['message_id'], str(second.pk))
        self.assertEqual(resolve_report(self.chat, '如果预算只有500元呢？')['message_id'], str(second.pk))
        self.assertIsNone(resolve_report(self.chat, '为什么天空是蓝色的？'))
        quote = '引用第 1 份圆桌报告：\n> 第一份建议\n\n为什么？'
        selected = resolve_report(self.chat, quote)
        self.assertEqual((selected['message_id'], selected['user_selected_text']), (str(first.pk), '第一份建议'))
        self.assertTrue(selected['selection_in_report'])
        self.assertFalse(resolve_report(self.chat, quote.replace('第一份建议', '新增预算只有500元'))['selection_in_report'])
        self.report('群聊客套回应', kind='quick_group')
        self.assertIsNone(resolve_report(self.chat, '为什么这么说？'))
        self.assertEqual(resolve_report(self.chat, '上一份报告有什么依据？')['report'], '第二份建议')

    def test_api_followup_packet_preserves_attribution_and_current_evidence(self):
        self.report('旧方案文本\n第二项历史依据（依据 E1）')
        text = '引用第 1 份圆桌报告：\n> 第二项历史依据\n\n有什么证据？'
        Message.objects.create(conversation=self.chat, role='user', content=text)
        task = Task.objects.create(conversation=self.chat, owner=self.owner, goal=text, status='waiting')
        body = {'background': True, 'client_request_id': str(uuid.uuid4()), 'confirm_cost': True,
                'expected_team': 'a'*64, 'workflow': 'decision_v1', 'delivery_mode': 'text',
                'expected_max_calls': 14, 'expected_task': str(task.pk), 'expected_task_updated_at': task.updated_at.isoformat()}
        seen = {}
        def generate(messages, **kwargs):
            stage = AgentStep.objects.get(status='running').role
            seen[stage] = json.loads(messages[0]['content'])
            if stage == 'analysis_p1':
                run = task.runs.get()
                payload = {'run_id': str(run.pk), 'client_request_id': str(uuid.uuid4()), 'target_provider': '',
                           'content': '引用第 1 份圆桌报告：\n> 第二项历史依据\n\n这一段是否有原始材料？'}
                submitted = self.client.post(f'/api/conversations/{self.chat.pk}/team-messages', payload,
                                             content_type='application/json')
                self.assertEqual(submitted.status_code, 201)
            result = fixture(stage)
            if stage == 'clarifier':
                result['questions'] = []
            return json.dumps(result, ensure_ascii=False)
        with patch('delivery.team_views.resolve_team', return_value=connections()), \
             patch('delivery.team_views.team_summary', return_value={'readiness':'available', 'revision':'a'*64}), \
             patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            resp = self.client.post(f'/api/conversations/{self.chat.pk}/collaborate', body, content_type='application/json')
            self.assertEqual(resp.status_code, 202, resp.content)
            process_next_run()
        for stage in ('clarifier', 'analysis_p1', 'analysis_p2', 'analysis_p3', 'review_p1', 'synthesizer'):
            packet = seen[stage]
            self.assertIn('旧方案文本', packet['followup_report']['report'])
            self.assertEqual(packet['followup_report']['prior_evidence'][0]['result'], '600')
        analysis = seen['analysis_p1']
        self.assertEqual(analysis['conversation'][0]['content'], '有什么证据？')
        self.assertNotIn('第二项历史依据', json.dumps(analysis['sources'], ensure_ascii=False))
        self.assertNotIn('E1', [s['reference'] for s in analysis['sources']])
        review = seen['review_p1']
        self.assertNotIn('第二项历史依据', json.dumps(review['sources'], ensure_ascii=False))
        self.assertTrue(any(entry['member'] == '此前圆桌报告' and entry['content'] == '第二项历史依据'
                            for entry in review['discussion']))

    def test_source_question_after_twelve_calls_uses_default_group_and_existing_material(self):
        from .models import RequestBudget
        content = '周五提交。'
        source = SourceMaterial.objects.create(owner=self.owner, conversation=self.chat, name='报名通知',
            kind='file', media_type='text/plain', content=content, byte_size=len(content.encode()),
            sha256=hashlib.sha256(content.encode()).hexdigest(), included=True)
        old = self.report('建议周五前提交（依据 E1）', sources=[dict(serialize_source(source), reference='S1')])
        RequestBudget.objects.create(user=self.owner, attempts=12)
        question = '这条建议对应哪一份通知？'
        Message.objects.create(conversation=self.chat, role='user', content=question)
        task = Task.objects.create(conversation=self.chat, owner=self.owner, goal=question, status='waiting')
        body = {'background': True, 'client_request_id': str(uuid.uuid4()), 'confirm_cost': True,
                'expected_team': 'a'*64, 'workflow': 'decision_v1', 'delivery_mode': 'text',
                'expected_max_calls': 14, 'expected_task': str(task.pk), 'expected_task_updated_at': task.updated_at.isoformat(),
                'expected_sources': sources_revision([source])}
        peers = []
        def generate(messages, **kwargs):
            stage = AgentStep.objects.get(status='running').role
            packet = json.loads(messages[0]['content'])
            self.assertEqual(packet['followup_report']['message_id'], str(old.pk))
            self.assertEqual(packet['sources'][0]['name'], '报名通知')
            self.assertEqual(packet['sources'][0]['content'], content)
            peers.append(kwargs['connection'].provider)
            return json.dumps({'mode': 'quick', 'quick_reply': '报名通知。'} if stage == 'clarifier'
                              else {'reply': '报名通知写的是周五提交。'}, ensure_ascii=False)
        with patch('delivery.team_views.resolve_team', return_value=connections()), \
             patch('delivery.team_views.team_summary', return_value={'readiness': 'available', 'revision':'a'*64}), \
             patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            response = self.client.post(f'/api/conversations/{self.chat.pk}/collaborate', body, content_type='application/json')
            self.assertEqual(response.status_code, 202, response.content)
            process_next_run()
        run = task.runs.get()
        self.assertEqual((run.status, run.calls_used, RequestBudget.objects.get(user=self.owner).attempts),
                         ('completed', 3, 15))
        self.assertEqual(len(set(peers)), 3)
        self.assertEqual(resolve_report(self.chat, '这条建议的出处是什么？')['message_id'], str(old.pk))

    def test_changed_source_new_conditions_and_requests_to_recheck_require_discussion(self):
        source = {'id': 'a', 'reference': 'S1', 'sha256': '1'*64, 'name': '旧材料', 'kind': 'file'}
        for changed in ([], [{**source, 'sha256': '2'*64}], [{**source, 'id': 'b'}]):
            self.assertFalse(same_selected_sources([source], changed))
        team = connections()
        mock = json.dumps({'mode': 'quick', 'quick_reply': '候选回复。'})
        with patch('delivery.decision_team_engine.generate_reply', return_value=mock):
            for prompt, same in [('这条建议对应哪一份通知？', False), ('请重新核验这条建议', True),
                                 ('如果预算只有500元呢？', True), ('请重新比较两个方案', True),
                                 ('这条建议对应哪一份通知？另外帮我比较其他城市的要求。', True)]:
                with self.subTest(prompt=prompt, same=same):
                    state = make_state(team, [source])
                    state['followup_report'] = {'message_id': str(uuid.uuid4()), 'same_selected_sources': same}
                    draft = prepare_brief([{'role':'user', 'content':prompt}], team, state, lambda *_: None, lambda *_: None)
                    self.assertEqual(draft['mode'], 'discussion')
