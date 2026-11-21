"""Synthetic integration tests: real persistence and validation, no provider access."""
import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from workspace.models import Artifact, Conversation, Message, Task
from workspace.views import export_data
from .artifact_bundles import save_bundle_artifact
from .catalog import IMPLEMENTED
from .connections import get_connection
from .execution_evidence import seal_execution
from .models import AgentStep, CapabilityEvidence, RequestBudget, TeamRun, TeamInterjection
from .provider import ProviderError
from .team_config import team_summary
from .adaptive_team_engine import work_roles
from .team_engine import TeamReviewError, _require_supported_review_claims
from .team_views import discussion_output
from .text_guidance import TEXT_CONSTRAINT_GUIDANCE


def plan_fixture(count=2, tools=None):
    items = [
        {'id': 'worker_1', 'specialty': 'analysis', 'instruction': '根据材料提出方案与约束。', 'depends_on': []},
        {'id': 'worker_2', 'specialty': 'critique', 'instruction': '独立提出替代方案及可验证问题。', 'depends_on': []},
    ]
    if count >= 3:
        items.append({'id': 'worker_3', 'specialty': 'writing', 'instruction': '只根据第一项工作整理清单。', 'depends_on': ['worker_1']})
    if count >= 4:
        items.append({'id': 'worker_4', 'specialty': 'coding', 'instruction': '参考第三项工作提供实现。', 'depends_on': ['worker_3']})
    return {'goal': '交付可以核对的学习计划', 'criteria': ['包含三项可执行任务', '标明待验证事项'],
            'work_items': items, 'required_tools': tools or []}


@override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='synthetic', SERVICE_REGION='test',
                   TEAM_ROLE_CONNECTIONS={}, EXECUTION_ENABLED=False, EXECUTION_IMAGES={})
class AdaptiveTeamTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email='adaptive@example.test', password='ExamplePass123!')
        self.other = get_user_model().objects.create_user(email='adaptive-other@example.test', password='ExamplePass123!')
        self.client.force_login(self.user)
        self.conversation = Conversation.objects.create(owner=self.user, title='离线任务规划')
        Message.objects.create(conversation=self.conversation, role='user', content='写一份学习计划，包含三项可执行任务')
        self.task = Task.objects.create(owner=self.user, conversation=self.conversation, goal='学习计划', status='waiting')
        self.url = f'/api/conversations/{self.conversation.id}/collaborate'
        connection = get_connection()
        for capability in IMPLEMENTED:
            CapabilityEvidence.objects.create(capability=capability, provider=connection.provider,
                model=connection.model, connection_fingerprint=connection.fingerprint,
                service_region=connection.service_region, evidence='Synthetic integration fixture only',
                verified_at=timezone.now(), expires_at=timezone.now() + timezone.timedelta(days=1),
                enabled=True, region_approved=True, compliance_approved=True)
        self.plan = plan_fixture()
        self.approved = '{"approved":true,"issues":[]}'

    def post(self, mode='text', **overrides):
        return self.client.post(self.url, json.dumps({'confirm_cost': True,
            'expected_team': team_summary()['revision'], 'workflow': 'adaptive', 'delivery_mode': mode,
            'expected_max_calls': 10 if mode == 'files' else 8, **overrides}), content_type='application/json')

    def test_duplicate_independent_specialties_use_distinct_configured_connections(self):
        plan = plan_fixture()
        plan['work_items'][1]['specialty'] = 'analysis'
        connections = {role: SimpleNamespace(fingerprint=fingerprint) for role, fingerprint in {
            'proposer': 'model-a', 'challenger': 'model-b', 'reviewer': 'model-c',
            'synthesizer': 'model-a', 'verifier': 'model-b'}.items()}
        self.assertEqual(work_roles(plan['work_items'], connections), ['proposer', 'challenger'])
        connections['challenger'].fingerprint = 'model-a'
        connections['reviewer'].fingerprint = 'model-a'
        self.assertEqual(work_roles(plan['work_items'], connections), ['proposer', 'proposer'])

    def test_public_verifier_display_reflects_model_fields(self):
        yes = discussion_output(SimpleNamespace(role='verifier', output='{"approved":true,"issues":[]}'))
        self.assertIn('通过标志：是', yes)
        self.assertIn('问题：无', yes)
        no = discussion_output(SimpleNamespace(role='verifier', output='{"approved":false,"issues":["遗漏预算"]}'))
        self.assertIn('通过标志：否', no)
        self.assertIn('遗漏预算', no)
        malformed = discussion_output(SimpleNamespace(role='verifier', output='不合格式的回答'))
        self.assertIn('格式未识别', malformed)
        self.assertIn('不合格式的回答', malformed)

    @patch('delivery.team_views.generate_reply', return_value='点名成员的真实回复')
    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_mention_is_answered_by_target_and_included_in_final_context(self, generate, mention):
        answers = iter([json.dumps(self.plan), '方案一', '方案二', '审查', '成果', self.approved])
        request_id = str(uuid.uuid4())
        sent = []
        def respond(messages, **kwargs):
            if generate.call_count == 2:
                run = TeamRun.objects.get()
                payload = {'run_id': str(run.id), 'content': '请回应我的补充',
                           'target_provider': 'gateway', 'client_request_id': request_id}
                url = f'/api/conversations/{self.conversation.id}/team-messages'
                self.assertEqual(self.client.post(url, json.dumps(payload), content_type='application/json').status_code, 201)
                self.assertEqual(self.client.post(url, json.dumps(payload), content_type='application/json').status_code, 200)
            sent.append(json.loads(messages[0]['content']))
            return next(answers)
        generate.side_effect = respond
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        run = TeamRun.objects.get()
        self.assertEqual(run.calls_used, 7)
        self.assertEqual(TeamInterjection.objects.get().status, 'answered')
        self.assertEqual(mention.call_count, 1)
        self.assertIn('点名成员的真实回复', json.dumps(sent[-2], ensure_ascii=False))
        self.assertIn('请回应我的补充', json.dumps(sent[-2], ensure_ascii=False))
        events = export_data(self.user)['team_runs'][0]['discussion']
        self.assertEqual(sum(e['kind'] == 'user' for e in events), 1)
        self.assertTrue(any(e['content'] == '点名成员的真实回复' for e in events))
        self.assertLess(next(e['order'] for e in events if e['kind'] == 'user'),
                        next(e['order'] for e in events if e['content'] == '方案一'))
        self.assertEqual([e['order'] for e in events], sorted(e['order'] for e in events))
        self.assertEqual(export_data(self.other)['team_runs'], [])

    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_late_message_prevents_stale_final_and_is_carried_to_next_round(self, generate):
        answers = iter([json.dumps(self.plan), '方案一', '方案二', '审查', '旧草稿', self.approved])
        def respond(messages, **kwargs):
            if generate.call_count == 6:
                run = TeamRun.objects.get()
                payload = {'run_id': str(run.id), 'content': '最后再加一条要求',
                           'target_provider': '', 'client_request_id': str(uuid.uuid4())}
                self.assertEqual(self.client.post(f'/api/conversations/{self.conversation.id}/team-messages',
                    json.dumps(payload), content_type='application/json').status_code, 201)
            return next(answers)
        generate.side_effect = respond
        self.assertEqual(self.post().status_code, 409)
        self.assertFalse(Message.objects.filter(conversation=self.conversation, role='assistant').exists())
        self.assertEqual(TeamInterjection.objects.get().status, 'pending')
        generate.side_effect = [json.dumps(self.plan), '方案一', '方案二', '审查', '新成果', self.approved]
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(set(TeamInterjection.objects.values_list('status', flat=True)), {'forwarded', 'included'})
        packets = [json.loads(c.args[0][0]['content']) for c in generate.call_args_list]
        self.assertIn('最后再加一条要求', json.dumps(packets[-2], ensure_ascii=False))

    @patch('delivery.team_views.generate_reply', return_value='成员回应补充')
    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_completed_run_accepts_followup_and_retains_export_and_delete(self, generate, mention):
        generate.side_effect = [json.dumps(self.plan), '方案一', '方案二', '审查', '成果', self.approved]
        self.assertEqual(self.post().status_code, 200)
        first = TeamRun.objects.get()
        payload = {'run_id': str(first.id), 'content': '再给一个例子',
                   'target_provider': 'gateway', 'client_request_id': str(uuid.uuid4())}
        url = f'/api/conversations/{self.conversation.id}/team-messages'
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(url, json.dumps(payload), content_type='application/json').status_code, 404)
        self.client.force_login(self.user)
        response = self.client.post(url, json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()['continuation_required'])
        replay = self.client.post(url, json.dumps(payload), content_type='application/json')
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()['continuation_required'])
        self.assertEqual(replay.json()['id'], response.json()['id'])
        self.assertTrue(Task.objects.filter(owner=self.user, conversation=self.conversation, status='waiting').exists())
        self.assertTrue(any(e['content'] == '再给一个例子' for e in export_data(self.user)['team_runs'][0]['discussion']))
        generate.reset_mock()
        generate.side_effect = [json.dumps(self.plan), '新方案一', '新方案二', '新审查', '新成果', self.approved]
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(mention.call_count, 1)
        self.assertIn('再给一个例子', generate.call_args_list[0].args[0][0]['content'])
        self.conversation.delete()
        self.assertFalse(TeamInterjection.objects.exists())

    @patch('delivery.team_views.generate_reply', return_value='已回应文件要求')
    @patch('delivery.file_team_engine.generate_reply')
    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_file_synthesis_receives_targeted_interjection(self, generate, file_generate, mention):
        answers = iter([json.dumps(self.plan), '方案一', '方案二', '审查'])
        def respond(messages, **kwargs):
            if generate.call_count == 4:
                run = TeamRun.objects.get()
                payload = {'run_id': str(run.id), 'content': 'README 说明如何使用',
                           'target_provider': 'gateway', 'client_request_id': str(uuid.uuid4())}
                self.assertEqual(self.client.post(f'/api/conversations/{self.conversation.id}/team-messages',
                    json.dumps(payload), content_type='application/json').status_code, 201)
            return next(answers)
        generate.side_effect = respond
        bundle = {'summary': '已准备文件。', 'files': [{'path': 'README.md', 'content': '使用说明。'}]}
        file_generate.side_effect = [json.dumps(bundle), self.approved]
        response = self.post(mode='files')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(mention.call_count, 1)
        packet = file_generate.call_args_list[0].args[0][0]['content']
        self.assertIn('README 说明如何使用', packet)
        self.assertIn('已回应文件要求', packet)
        self.assertEqual(TeamInterjection.objects.get().status, 'answered')

    def test_unverified_human_review_claim_is_not_a_completed_result(self):
        with self.assertRaises(TeamReviewError):
            _require_supported_review_claims('本轮未运行检查，以上结论均来自对文本中数字的人工复核。')
        _require_supported_review_claims('本轮未经人工复核，仍需人工检查。')
        _require_supported_review_claims('本方案需要由人工复核。')
        _require_supported_review_claims('本轮不含人工复核、审批或验收记录，不得表述为已经人工审核通过。')

    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_human_review_claim_keeps_draft_and_skips_further_paid_calls(self, generate):
        generate.side_effect = [json.dumps(self.plan), '方案1', '方案2', '审查',
                                '本轮未运行检查，以上结论均来自对文本中数字的人工复核。']
        response = self.post()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(TeamRun.objects.get().status, 'needs_review')
        self.assertTrue(Artifact.objects.filter(name='待复核草稿.md').exists())
        self.assertEqual(generate.call_count, 5)

    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_dynamic_plan_persists_and_members_see_prior_public_discussion(self, generate):
        self.plan = plan_fixture(4)
        generate.side_effect = [json.dumps(self.plan), 'root-one', 'root-two', 'dependent-three',
                                'dependent-four', '交叉审查', '最终成果', self.approved]
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        run = TeamRun.objects.get()
        self.assertEqual((run.workflow, run.calls_used, run.max_calls), ('adaptive', 8, 8))
        self.assertEqual(run.plan, self.plan)
        packets = [json.loads(call.args[0][0]['content']) for call in generate.call_args_list]
        self.assertEqual(packets[1]['dependencies'], {})
        self.assertEqual(packets[2]['dependencies'], {})
        self.assertEqual(packets[3]['dependencies'], {'worker_1': 'root-one'})
        self.assertEqual(packets[2]['discussion'][0]['content'], 'root-one')
        self.assertEqual([item['content'] for item in packets[3]['discussion']], ['root-one', 'root-two'])
        self.assertEqual(packets[4]['dependencies'], {'worker_3': 'dependent-three'})
        self.assertEqual(packets[-1]['plan']['criteria'], self.plan['criteria'])
        for index, call in enumerate(generate.call_args_list):
            self.assertEqual(TEXT_CONSTRAINT_GUIDANCE in call.kwargs['system_instruction'],
                             index == len(generate.call_args_list) - 2)
        self.assertEqual(len(export_data(self.user)['team_runs'][0]['plan']['work_items']), 4)
        self.assertEqual(export_data(self.other)['team_runs'], [])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f'/api/conversations/{self.conversation.id}').status_code, 404)

    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_adaptive_synthesizer_receives_the_same_explicit_length_budget(self, generate):
        Message.objects.create(conversation=self.conversation, role='user',
                               content='写四条建议，合计200至320个汉字。')
        generate.side_effect = [json.dumps(self.plan), '方案一', '方案二', '审查', '成果', self.approved]
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        instructions = [call.kwargs['system_instruction'] for call in generate.call_args_list]
        self.assertIn('分成4项时每项平均约65个汉字', instructions[-2])
        self.assertTrue(all('每项平均约65个汉字' not in instruction for instruction in instructions[:-2]))

    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_text_draft_is_visible_before_final_review_and_not_saved_as_final(self, generate):
        answers = iter([json.dumps(self.plan), '方案一', '方案二', '审查', '草稿内容', self.approved])
        def answer(_messages, **kwargs):
            value = next(answers)
            if 'on_text' in kwargs:
                kwargs['on_text']('草稿')
                step = AgentStep.objects.get(role='synthesizer')
                self.assertEqual((step.status, step.output), ('running', '草稿'))
                self.assertFalse(Message.objects.filter(conversation=self.conversation, role='assistant').exists())
                kwargs['on_text']('内容')
            return value
        generate.side_effect = answer
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Message.objects.get(conversation=self.conversation, role='assistant').content, '草稿内容')
        self.assertEqual(AgentStep.objects.get(role='synthesizer').status, 'completed')

    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_stopping_during_live_draft_prevents_completion_and_later_model_calls(self, generate):
        answers = iter([json.dumps(self.plan), '方案一', '方案二', '审查', '草稿内容'])
        def answer(_messages, **kwargs):
            value = next(answers)
            if 'on_text' in kwargs:
                kwargs['on_text']('草稿')
                response = self.client.post(f'/api/tasks/{self.task.id}/transition',
                    json.dumps({'action': 'pause'}), content_type='application/json')
                self.assertEqual(response.status_code, 200)
                kwargs['on_text']('内容')
            return value
        generate.side_effect = answer
        self.post()
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'paused')
        self.assertEqual(generate.call_count, 5)
        self.assertFalse(Message.objects.filter(conversation=self.conversation, role='assistant').exists())

    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_old_limits_unknown_flow_and_cost_refusal_never_dispatch(self, generate):
        self.assertEqual(self.post(expected_max_calls=5).status_code, 400)
        self.assertEqual(self.post(mode='files', expected_max_calls=7).status_code, 400)
        self.assertEqual(self.post(workflow='unbounded').status_code, 400)
        self.assertEqual(self.post(confirm_cost=False).status_code, 400)
        generate.assert_not_called()
        self.assertFalse(TeamRun.objects.exists())

    @patch('delivery.adaptive_team_engine.readiness', return_value={'available_tools': []})
    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_unavailable_required_execution_stops_at_plan(self, generate, ready):
        generate.return_value = json.dumps(plan_fixture(tools=['python_unittest']))
        response = self.post(mode='files')
        self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(TeamRun.objects.get().status, 'failed')
        self.assertFalse(Artifact.objects.exists())

    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_invalid_plan_is_never_scheduled(self, generate):
        bad = plan_fixture()
        bad['work_items'][0]['depends_on'] = ['worker_2']
        bad['work_items'][1]['depends_on'] = ['worker_1']
        generate.return_value = json.dumps(bad)
        self.assertEqual(self.post().status_code, 502)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(AgentStep.objects.count(), 1)
        self.assertFalse(Message.objects.filter(role='assistant').exists())

    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_pause_during_planning_does_not_persist_plan_or_schedule(self, generate):
        def paused(*args, **kwargs):
            Task.objects.filter(pk=self.task.pk).update(status='paused', execution_id=None)
            return json.dumps(self.plan)
        generate.side_effect = paused
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(TeamRun.objects.get().plan, {})
        self.assertEqual(AgentStep.objects.count(), 1)
        self.assertEqual(generate.call_count, 1)

    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_budget_exhaustion_skips_unstarted_work(self, generate):
        RequestBudget.objects.create(user=self.user, attempts=19)
        generate.return_value = json.dumps(self.plan)
        self.assertEqual(self.post().status_code, 429)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(AgentStep.objects.filter(status='skipped').count(), 5)

    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_changed_execution_policy_stops_late_plan(self, generate):
        # Use a scoped setting change active during the post-call guard.
        from django.conf import settings
        original = settings.EXECUTION_ENABLED
        def change(*args, **kwargs):
            settings.EXECUTION_ENABLED = True
            return json.dumps(self.plan)
        generate.side_effect = change
        try:
            self.assertEqual(self.post().status_code, 409)
        finally:
            settings.EXECUTION_ENABLED = original
        self.assertFalse(AgentStep.objects.exclude(output='').exists())

    @patch('delivery.adaptive_team_engine.readiness', return_value={'available_tools': ['python_unittest']})
    @patch('delivery.execution.validate_execution')
    @patch('delivery.file_team_engine.generate_reply')
    @patch('delivery.adaptive_team_engine.generate_reply')
    def test_runtime_unavailable_preserves_draft_and_never_spends_repair(self, plan_generate, file_generate, execute, ready):
        plan_generate.side_effect = [json.dumps(plan_fixture(tools=['python_unittest'])), '方案1', '方案2', '审查']
        bundle = {'summary': '候选成果', 'files': [{'path': 'test_sample.py', 'content': 'import unittest\n'}]}
        file_generate.return_value = json.dumps(bundle)
        execute.return_value = {'passed': False, 'checks': [{'path': '', 'check': 'Python 测试',
            'status': 'not_run', 'detail': '隔离环境不可用'}], 'scope': '未执行任何代码。'}
        response = self.post(mode='files')
        self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(file_generate.call_count, 1)
        artifact = Artifact.objects.get()
        self.assertEqual(artifact.review_status, 'draft')
        self.assertFalse(artifact.validation['execution']['passed'])
        self.assertEqual(TeamRun.objects.get().calls_used, 5)

    def test_execution_evidence_is_bound_to_exact_file_contents(self):
        bundle = {'summary': '候选成果', 'files': [{'path': 'main.py', 'content': 'print(1)'}]}
        report = {'passed': True, 'checks': [], 'scope': 'Synthetic receipt only'}
        validation = {'execution': seal_execution(bundle, report)}
        changed = {'summary': '候选成果', 'files': [{'path': 'main.py', 'content': 'print(2)'}]}
        with self.assertRaises(ProviderError):
            save_bundle_artifact(self.task, self.user, changed, validation)
        validation['execution']['report']['passed'] = False
        with self.assertRaises(ProviderError):
            save_bundle_artifact(self.task, self.user, bundle, validation)
        self.assertFalse(Artifact.objects.exists())

    def test_ready_artifact_requires_every_tool_in_saved_plan(self):
        import uuid
        TeamRun.objects.create(task=self.task, execution_id=uuid.uuid4(), workflow='adaptive',
                               team_revision='synthetic', plan=plan_fixture(tools=['python_unittest']))
        bundle = {'summary': '候选成果', 'files': [{'path': 'main.py', 'content': 'print(1)'}]}
        with self.assertRaises(ProviderError):
            save_bundle_artifact(self.task, self.user, bundle, {})
        wrong_tool = {'passed': True, 'checks': [{'tool': 'javascript_check', 'status': 'passed'}], 'scope': 'Synthetic'}
        with self.assertRaises(ProviderError):
            save_bundle_artifact(self.task, self.user, bundle, {'execution': seal_execution(bundle, wrong_tool)})
        draft = save_bundle_artifact(self.task, self.user, bundle, {}, review_status='draft')
        self.assertEqual(draft.review_status, 'draft')
