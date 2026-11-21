"""Older stored runs and new runs use the same public discussion rendering."""
import json
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase

from workspace.models import Conversation, Task
from .models import AgentStep, TeamRun
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
