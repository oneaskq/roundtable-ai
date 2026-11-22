"""Older stored runs and new runs use the same public discussion rendering."""
import json
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase

from workspace.models import Conversation, Task
from .models import AgentStep, TeamInterjection, TeamRun
from .team_views import serialize_run


class DiscussionPresentationTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(email='presentation@example.test', password='ExamplePass123!')
        conversation = Conversation.objects.create(owner=user, title='讨论')
        task = Task.objects.create(owner=user, conversation=conversation, goal='选择方案', status='waiting')
        self.run = TeamRun.objects.create(task=task, execution_id=uuid.uuid4(), team_revision='test',
            workflow='decision_v1', status='completed', decision_state={'analyses': [
                {'participant': 'p1', 'stance': 'conditional', 'stance_reason': '需要预算',
                 'claims': [{'proposal': '方案A'}]}]})

    def step(self, role, output, position):
        return AgentStep.objects.create(run=self.run, role=role, position=position, label=role,
            provider='deepseek', model='synthetic', status='completed', output=json.dumps(output, ensure_ascii=False))

    def test_empty_rounds_are_not_messages_but_steps_remain_available(self):
        self.step('review_p1', {'findings': []}, 1)
        self.step('response_p1', {'responses': [], 'stance': 'conditional', 'stance_subject': '方案A',
                                  'stance_reason': '预算需要进一步确认', 'note': '补充内容'}, 2)
        data = serialize_run(TeamRun.objects.get(pk=self.run.pk))
        self.assertEqual(data['discussion'], [])
        self.assertEqual([step['role'] for step in data['steps']], ['review_p1', 'response_p1'])

    def test_real_findings_responses_and_changed_stance_are_retained(self):
        self.step('review_p1', {'findings': [{'reason': '预算未核实'}]}, 1)
        self.step('response_p2', {'responses': [{'reason': '需要复核', 'decision': 'reject', 'changed': True}]}, 2)
        self.step('response_p1', {'responses': [], 'stance': 'oppose', 'stance_subject': '方案A',
                                  'stance_reason': '预算不足'}, 3)
        discussion = serialize_run(self.run)['discussion']
        self.assertEqual(len(discussion), 3)
        self.assertIn('预算未核实', discussion[0]['content'])
        self.assertIn('不接受', discussion[1]['content'])
        self.assertIn('已调整观点', discussion[1]['content'])
        self.assertIn('反对', discussion[2]['content'])
        self.assertEqual(discussion[2]['role'], 'opinion_update')
        self.assertEqual(discussion[2]['label'], '意见更新')

    def test_malformed_and_unrecognized_content_is_not_silently_suppressed(self):
        self.step('review_p1', {'findings': None}, 1)
        self.step('review_p2', {'findings': [], 'note': '需要额外核实'}, 2)
        data = serialize_run(self.run)
        content = [item['content'] for item in data['discussion']]
        self.assertIn('格式未识别', content[0])
        self.assertEqual(len(content), 1)
        self.assertIn('需要额外核实', data['steps'][1]['output'])

    def test_missing_initial_opinion_does_not_invent_change(self):
        self.step('response_p2', {'responses': [], 'stance': 'oppose', 'stance_reason': '可能超预算'}, 1)
        data = serialize_run(self.run)
        self.assertEqual(data['discussion'], [])
        self.assertIn('可能超预算', data['steps'][0]['output'])

    def test_old_failed_turn_shows_a_whole_reason_and_keeps_all_nine_findings(self):
        self.run.status = 'failed'
        reasons = [f'条件 {i} 需要进一步确认。除非拿到原始报价，否则结论待定。' for i in range(9)]
        self.step('review_p1', {'findings': [{'claim_id': 'UNKNOWN', 'reason': s} for s in reasons]}, 1)
        item = serialize_run(self.run)['discussion'][0]
        self.assertEqual(item['turn'], {'text': reasons[0], 'detail_label': '完整核验（9 条）'})
        for reason in reasons:
            self.assertIn(reason, item['content'])

    def test_replies_link_to_actual_claim_or_challenge_and_skip_unlinked_findings(self):
        state = self.run.decision_state
        state['participants'] = [{'id': 'p1', 'label': 'DeepSeek', 'provider': 'deepseek', 'model': 'synthetic'},
                                 {'id': 'p2', 'label': 'Kimi', 'provider': 'kimi', 'model': 'synthetic'}]
        state['analyses'] = [{'participant': 'p2', 'claims': [{'id': 'P2C1', 'author': 'p2', 'text': '可以先试一次。'}]}]
        state['reviews'] = [{'id': 'R1', 'reviewer': 'p1', 'author': 'p2', 'claim_id': 'P2C1', 'reason': '先确定试用成本。'}]
        state['unlinked_review_counts'] = {'review_p1': 1, 'extra_review': 1}
        self.step('review_p1', {'findings': [{'claim_id': 'BAD', 'reason': '错误编号', 'important': True},
                                             {'claim_id': ' p2c1 ', 'reason': '先确定试用成本。'}]}, 1)
        self.step('response_p2', {'responses': [{'finding_id': 'R1', 'reason': '我会先问报价。', 'decision': 'accept'}]}, 2)
        self.step('extra_review', {'findings': [{'claim_id': 'P2C1', 'reason': '不是本次追加核验的对象。'}]}, 3)
        review, response, extra = serialize_run(self.run)['discussion']
        self.assertEqual(review['turn']['text'], '先确定试用成本。')
        self.assertEqual(review['turn']['reply_to'], {'kind': 'member', 'label': 'Kimi', 'text': '可以先试一次。'})
        self.assertIn('错误编号', review['content'])
        self.assertEqual(response['turn']['reply_to'], {'kind': 'member', 'label': 'DeepSeek', 'text': '先确定试用成本。'})
        self.assertEqual(response['turn']['decision'], '接受质疑')
        self.assertFalse(extra['turn']['text'])
        self.assertNotIn('reply_to', extra['turn'])
        self.assertIn('不是本次追加核验的对象。', extra['content'])

    def test_only_state_linked_user_replies_become_messages_even_for_empty_stages(self):
        note = TeamInterjection.objects.create(run=self.run, client_request_id=uuid.uuid4(),
            content='请给出已有的结论。', target_provider='kimi', status='included')
        state = self.run.decision_state
        reply = '现在更倾向按月付费，报价还在核对。'
        state['user_replies'] = {role: {'text': reply, 'interjection_ids': [str(note.id)]}
                                  for role in ['review_p1', 'synthesizer']}
        state['user_replies']['review_p3'] = {'text': '未关联其他轮的回复', 'interjection_ids': [str(uuid.uuid4())]}
        self.step('review_p1', {'findings': []}, 1)
        self.step('review_p2', {'findings': [], 'user_reply': '未关联的答复'}, 2)
        self.step('review_p3', {'findings': []}, 3)
        self.step('synthesizer', {'recommendation': '汇总建议'}, 4)
        items = [item for item in serialize_run(self.run)['discussion'] if item['kind'] == 'assistant']
        self.assertEqual([item['role'] for item in items], ['review_p1', 'synthesizer'])
        for item in items:
            self.assertEqual(item['content'], reply)
            self.assertEqual(item['turn']['reply_to'], {'kind': 'user', 'label': '你', 'text': note.content})

    def test_long_legacy_text_is_kept_whole_and_fenced_json_uses_the_worker_parser(self):
        reason = '原文中的条件和假设都需要保留。' * 18 + '所以目前不能下结论。'
        step = self.step('review_p1', {'findings': [{'reason': reason}]}, 1)
        step.output = '```json\n' + step.output + '\n```'
        step.save(update_fields=['output'])
        data = serialize_run(self.run)
        self.assertEqual(data['discussion'][0]['turn']['text'], '')
        self.assertIn(reason, data['discussion'][0]['content'])
        self.assertEqual(data['steps'][0]['output'], step.output)

    def test_direct_mentions_link_only_to_the_note_for_the_addressed_member(self):
        for position, length, target in [(1, 18, 'deepseek'), (2, 22, 'deepseek'), (3, 18, 'kimi')]:
            note = TeamInterjection.objects.create(run=self.run, client_request_id=uuid.uuid4(),
                content=f'第 {position} 个问题', target_provider=target, status='answered')
            step = self.step('mention_' + note.id.hex[:length], {}, position)
            step.output = '我会先核对导师的时间。'
            step.save(update_fields=['output'])
        messages = [item for item in serialize_run(self.run)['discussion'] if item['kind'] == 'assistant']
        for index, item in enumerate(messages[:2], 1):
            self.assertEqual(item['turn']['reply_to']['text'], f'第 {index} 个问题')
            self.assertEqual(item['turn']['text'], item['content'])
        self.assertNotIn('turn', messages[2])
