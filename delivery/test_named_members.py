import json
import uuid
from dataclasses import replace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from workspace.models import Conversation, Message, Task
from .background import process_next_run
from .catalog import connection_revision
from .models import AgentStep, TeamRun
from .team_config import select_team, team_summary
from .test_adaptive_team import plan_fixture
from .test_decision_team import connections, fixture


class NamedMembersTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(email='roster@example.test', password='ExamplePass123!')
        self.client.force_login(user)
        self.conversation = Conversation.objects.create(owner=user)
        self.task = Task.objects.create(owner=user, conversation=self.conversation, goal='比较两种方案', status='waiting')
        Message.objects.create(conversation=self.conversation, role='user', content=self.task.goal)
        self.team = connections()
        self.base = f'/api/conversations/{self.conversation.pk}'
        self.ids = [connection_revision(self.team[role]) for role in ('challenger', 'reviewer')]
        # The standard synthesizer is not accepted and must never see this round.
        self.denied = {'deepseek'}
        self.enterContext(patch('delivery.team_config.chat_available', side_effect=lambda c: c.provider not in self.denied))
        self.enterContext(patch('delivery.team_views.resolve_team', return_value=self.team))
        self.revision = team_summary(self.team)['revision']
        self.body = {'background': True, 'client_request_id': str(uuid.uuid4()), 'confirm_cost': True,
                     'expected_team': self.revision, 'workflow': 'decision_v1', 'delivery_mode': 'text',
                     'expected_max_calls': 14, 'expected_task': str(self.task.pk),
                     'expected_task_updated_at': self.task.updated_at.isoformat(), 'participant_ids': self.ids}

    def submit(self, **overrides):
        return self.client.post(self.base + '/collaborate', {**self.body, **overrides}, content_type='application/json')

    def mention(self, run, name):
        return self.client.post(self.base + '/team-messages',
            {'run_id': str(run.pk), 'client_request_id': str(uuid.uuid4()), 'content': '这个条件怎么理解？',
             'target_provider': name, 'intent': 'clarification_mention'}, content_type='application/json')

    def test_choices_deduplicate_roles_and_distinguish_models_from_the_same_provider(self):
        other_kimi = replace(self.team['challenger'], model='another-kimi-model')
        self.team['verifier'] = other_kimi
        summary = team_summary(self.team)
        self.assertEqual([m['model'] for m in summary['members']], ['synthetic-a', 'synthetic-b', 'synthetic-c', other_kimi.model])
        self.assertEqual(summary['default_member_ids'], self.ids + [connection_revision(other_kimi)])
        self.assertFalse(summary['members'][0]['ready'])
        self.assertNotIn(other_kimi.fingerprint, json.dumps(summary))

    def test_reassigning_a_verifier_uses_a_different_selected_member(self):
        # The default verifier is excluded, while the original writer is kept.
        retained = [connection_revision(self.team[role]) for role in ('proposer', 'reviewer')]
        projected = select_team(self.team, retained)
        self.assertEqual(projected['synthesizer'].provider, 'deepseek')
        self.assertEqual(projected['verifier'].provider, 'qwen')
        self.assertEqual(projected['challenger'].provider, 'qwen')

    def test_frozen_roster_applies_to_clarification_followups_and_worker(self):
        response = self.submit()
        self.assertEqual(response.status_code, 202, response.content)
        run = TeamRun.objects.get()
        members = response.json()['run']['decision']['selected_members']
        self.assertEqual([m['id'] for m in members], self.ids)
        self.assertEqual({p['provider'] for p in run.decision_state['participants']}, {'kimi', 'qwen'})
        self.assertEqual(set(run.steps.values_list('provider', flat=True)), {'kimi', 'qwen'})
        self.assertTrue(self.submit().json()['replayed'])
        self.assertEqual(self.submit(participant_ids=list(reversed(self.ids))).status_code, 409)

        def generate(messages, **kwargs):
            self.assertIn(kwargs['connection'].provider, {'kimi', 'qwen'})
            stage = AgentStep.objects.get(status='running').role
            return json.dumps(fixture(stage), ensure_ascii=False)

        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            self.assertEqual(process_next_run(), run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, 'awaiting_input')
        self.assertEqual(self.mention(run, 'deepseek').status_code, 400)
        self.assertEqual(self.mention(run, 'qwen').status_code, 201)
        with patch('delivery.decision_team_engine.generate_reply', return_value='{"reply":"已收到。"}') as reply:
            self.assertEqual(process_next_run(), run.pk)
            self.assertEqual(reply.call_args.kwargs['connection'].provider, 'qwen')
        answer = self.client.post(self.base + '/confirm-decision',
            {'run_id': str(run.pk), 'answer': '按实际预算比较', 'confirm_continue': True,
             'expected_max_calls': 14}, content_type='application/json')
        self.assertEqual(answer.status_code, 202, answer.content)

    def test_file_planner_synthesis_and_verification_all_use_the_selected_roster(self):
        self.assertEqual(self.submit(workflow='adaptive', delivery_mode='files', expected_max_calls=10).status_code, 202)
        bundle = {'summary': '说明文件。', 'files': [{'path': 'README.md', 'content': '使用说明。'}]}
        with patch('delivery.adaptive_team_engine.generate_reply',
                   side_effect=[json.dumps(plan_fixture()), '提案甲', '提案乙', '审查']) as planning, \
             patch('delivery.file_team_engine.generate_reply',
                   side_effect=[json.dumps(bundle), '{"approved":true,"issues":[]}']) as files:
            process_next_run()
        run = TeamRun.objects.get()
        self.assertEqual(run.status, 'completed', run.error)
        observed = [call.kwargs['connection'].provider for call in planning.call_args_list + files.call_args_list]
        self.assertEqual(set(observed), {'kimi', 'qwen'})
        self.assertEqual(run.max_calls, 10)

    def test_member_rotation_after_enqueue_stops_before_a_provider_call(self):
        self.assertEqual(self.submit().status_code, 202)
        self.team['reviewer'] = replace(self.team['reviewer'], api_key='rotated-synthetic-key')
        with patch('delivery.decision_team_engine.generate_reply') as provider:
            process_next_run()
        provider.assert_not_called()
        self.assertEqual(TeamRun.objects.get().status, 'failed')

    def test_invalid_or_unaccepted_selections_cannot_enqueue(self):
        other = connection_revision(self.team['proposer'])
        for ids in (None, self.ids[:1], self.ids * 2, [self.ids[0]] * 2, [{}, self.ids[0]], 'kimi'):
            self.assertEqual(self.submit(participant_ids=ids).status_code, 400)
        self.assertEqual(self.submit(participant_count=2).status_code, 400)
        self.assertEqual(self.submit(expected_team='stale').status_code, 409)
        self.assertEqual(self.submit(participant_ids=['0' * 64, self.ids[0]]).status_code, 409)
        self.assertEqual(self.submit(participant_ids=[other, self.ids[0]]).status_code, 503)
        self.assertFalse(TeamRun.objects.exists())

    def test_addressing_an_excluded_member_rejects_the_one_call_submission(self):
        self.task.goal = '@DeepSeek 帮我核对预算'
        self.task.save(update_fields=['goal'])
        result = self.submit(expected_max_calls=1)
        self.assertEqual(result.status_code, 409, result.content)
        self.assertEqual(result.json()['code'], 'member_unavailable')
        self.assertFalse(TeamRun.objects.exists())
