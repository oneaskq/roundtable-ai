import json
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from workspace.models import Conversation, Message, Task, SourceMaterial
from workspace.sources import sources_revision
from .models import TeamRun
from .decision_team_engine import make_state, run_decision
from .test_decision_team import connections, fixture


class ParticipantCountTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(email='count@example.test', password='ExamplePass123!')
        self.client.force_login(user)
        self.conversation = Conversation.objects.create(owner=user, title='讨论')
        Message.objects.create(conversation=self.conversation, role='user', content='比较方案')
        self.task = Task.objects.create(owner=user, conversation=self.conversation, goal='比较方案', status='waiting')
        self.url = f'/api/conversations/{self.conversation.id}/collaborate'
        self.body = {'background': True, 'client_request_id': str(uuid.uuid4()), 'confirm_cost': True,
                     'expected_team': 'a' * 64, 'workflow': 'decision_v1', 'delivery_mode': 'text',
                     'expected_max_calls': 14, 'expected_task': str(self.task.id),
                     'expected_task_updated_at': self.task.updated_at.isoformat(),
                     'expected_sources': sources_revision(SourceMaterial.objects.filter(conversation=self.conversation))}
        for path, value in [('delivery.team_views.resolve_team', connections()),
                            ('delivery.team_views.team_summary', {'readiness': 'available', 'revision': 'a' * 64})]:
            patcher = patch(path, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, body):
        return self.client.post(self.url, json.dumps(body), content_type='application/json')

    def test_two_participants_are_frozen_and_replay_cannot_change_count(self):
        body = {**self.body, 'participant_count': 2}
        self.assertEqual(self.post(body).status_code, 202)
        run = TeamRun.objects.get()
        self.assertEqual(len(run.decision_state['participants']), 2)
        self.assertEqual(run.max_calls, 14)
        self.assertEqual(run.steps.filter(role__startswith='analysis_').count(), 2)
        self.assertEqual(self.post(body).status_code, 202)
        self.assertEqual(self.post({**body, 'participant_count': 3}).status_code, 409)

    def test_invalid_count_and_unconfirmed_budget_do_not_queue(self):
        for count in (True, 1, 4, '2'):
            self.assertEqual(self.post({**self.body, 'participant_count': count}).status_code, 400)
        self.assertEqual(self.post({**self.body, 'participant_count': 2, 'expected_max_calls': 11}).status_code, 400)
        self.assertFalse(TeamRun.objects.exists())

    def test_two_members_complete_independent_review_and_response(self):
        team = connections()
        state = make_state(team, [], 2)
        state.update(phase='confirmed', brief={'goal': '比较方案', 'unknowns': '', 'assumptions': ''})
        current_stage = ''
        stages = []

        def before(stage, label, connection):
            nonlocal current_stage
            current_stage = stage
            stages.append(stage)

        def generate(messages, **kwargs):
            if current_stage.startswith('review_'):
                target = 'P2C1' if current_stage.endswith('p1') else 'P1C1'
                value = {'findings': [{'claim_id': target, 'categories': ['reasoning'],
                                       'verdict': 'unverified', 'reason': '需要核实',
                                       'impact': '成本', 'important': True, 'evidence': []}]}
            elif current_stage.startswith('response_'):
                finding = 'R2' if current_stage.endswith('p1') else 'R1'
                value = {'stance': 'conditional', 'stance_reason': '取决于成本',
                         'responses': [{'finding_id': finding, 'decision': 'undetermined', 'reason': '需要核实',
                                        'changed': False, 'change_basis': 'none', 'new_evidence': [],
                                        'important_new_evidence': False, 'new_evidence_impact': ''}]}
            else:
                value = fixture(current_stage)
            return json.dumps(value, ensure_ascii=False)

        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            result = run_decision([{'role': 'user', 'content': '比较方案'}], team, state,
                                  before, lambda *_: None, lambda _: None)
        self.assertEqual(state['phase'], 'completed')
        self.assertEqual(len(state['reviews']), 2)
        self.assertEqual(len(state['responses']), 2)
        self.assertNotIn('analysis_p3', stages)
        self.assertIn('重要分歧', result)
