"""Decision workflow tests use synthetic model outputs and an isolated test DB."""
import json
import hashlib
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from workspace.models import Conversation, Message, SourceMaterial, Task
from workspace.sources import sources_revision
from workspace.views import export_data
from .background import process_next_run
from .connections import ProviderConnection
from .decision_team_engine import (_calculate, confirm_brief, make_state, prepare_brief,
                                   report_references, run_decision)
from .models import AgentStep, TeamInterjection, TeamRun
from .provider import ProviderError


def connections():
    a = ProviderConnection('deepseek', 'synthetic-a', 'synthetic-key')
    b = ProviderConnection('kimi', 'synthetic-b', 'synthetic-key')
    c = ProviderConnection('qwen', 'synthetic-c', 'synthetic-key', 'cn-beijing')
    return {'proposer': a, 'challenger': b, 'reviewer': c, 'synthesizer': a, 'verifier': b}


def fixture(stage, *, extra=False, bad_quote=False, missing_response=False):
    if stage == 'clarifier_peer':
        return {'question': ''}
    if stage == 'clarifier':
        return {'goal': '选择方案', 'metrics': '总成本', 'constraints': '预算1000元',
                'preferences': '', 'assumptions': '', 'unknowns': '成本与速度的优先级未知',
                'questions': ['成本和速度哪个更重要？']}
    if stage.startswith('analysis_'):
        part = stage[-2:]
        return {'recommendation': f'{part} 的条件建议', 'stance': 'conditional', 'stance_reason': '取决于偏好',
                'claims': [{'text': f'{part} 主张：六个月费用为500元', 'proposal': '方案A',
                            'metric': '总成本', 'evidence': []}], 'unknowns': ['优先级未定']}
    if stage.startswith('review_'):
        part = stage[-2:]
        target = {'p1': 'P2C1', 'p2': 'P3C1', 'p3': 'P1C1'}[part]
        finding = {'claim_id': target, 'categories': ['fact', 'reasoning'], 'verdict': 'refuted',
                   'reason': '金额需要复核', 'impact': '总成本', 'important': part == 'p3', 'evidence': []}
        if part == 'p3' and not bad_quote:
            finding['calculation'] = {'expression': '100*6', 'scope': '仅计算每月100元乘6个月'}
        if bad_quote:
            finding['evidence'] = [{'source': 'S1', 'quote': '不存在的引文', 'scope': '报价'}]
        return {'findings': [finding]}
    if stage.startswith('response_'):
        part = stage[-2:]
        finding = {'p1': 'R3', 'p2': 'R1', 'p3': 'R2'}[part]
        if missing_response and part == 'p1':
            return {'responses': [], 'stance': 'conditional', 'stance_subject': '方案A', 'stance_reason': '取决于总价'}
        item = {'finding_id': finding, 'decision': 'accept', 'reason': '接受计算纠错',
                'changed': True, 'change_basis': 'corrected_reasoning', 'new_evidence': [],
                'important_new_evidence': False, 'new_evidence_impact': ''}
        if extra and part == 'p1':
            item.update(new_evidence=[{'source': 'S1', 'quote': '额外费用200元', 'scope': '报价范围'}],
                        important_new_evidence=True, new_evidence_impact='可能影响预算')
        return {'responses': [item], 'stance': 'conditional', 'stance_subject': '方案A', 'stance_reason': '取决于总价'}
    if stage == 'extra_review':
        return {'findings': [{'claim_id': 'P1C1', 'categories': ['fact'], 'verdict': 'unverified',
                              'reason': '追加费用尚需确认', 'impact': '预算', 'important': True,
                              'evidence': []}]}
    if stage == 'extra_response':
        return {'stance': 'conditional', 'stance_subject': '方案A', 'stance_reason': '需要核对报价',
                'responses': [{'finding_id': 'R4', 'decision': 'undetermined', 'reason': '需要核对报价',
                               'changed': False, 'change_basis': 'none',
                               'new_evidence': [{'source': 'S1', 'quote': '报价有效30天', 'scope': '有效期'}],
                               'important_new_evidence': True, 'new_evidence_impact': '有效期影响决定'}]}
    if stage == 'synthesizer':
        return {'recommendation': '优先成本时先确认方案A总价；优先速度时另行比较。',
                'conditions': ['先核实费用和偏好'], 'basis': ['计算结果需结合报价范围'],
                'next_steps': ['确认总价'], 'unknowns': ['偏好尚未确定']}
    raise AssertionError(stage)


class DecisionEngineTests(TestCase):
    def setUp(self):
        self.connections = connections()
        self.context = [{'role': 'user', 'content': '每月100元，共6个月；预算1000元。'},
                        {'role': 'assistant', 'content': '旧团队的答案不应进入本轮独立分析。'}]
        self.sources = [{'reference': 'S1', 'kind': 'file', 'name': '报价', 'url': '',
                         'sha256': 'a' * 64, 'content': '额外费用200元；报价有效30天'}]
        self.state = make_state(self.connections, self.sources)
        self.stage = ''
        self.packets = {}
        self.calls = []

    def before(self, stage, label, connection):
        self.stage = stage
        self.calls.append(stage)

    def after(self, stage, output):
        pass

    @patch('delivery.decision_team_engine.lookup_public_sources')
    @patch('delivery.decision_team_engine.generate_reply')
    def test_only_selected_public_keywords_leave_and_fetched_source_reaches_review(self, generate, lookup):
        def generated(messages, **kwargs):
            self.packets[self.stage] = json.loads(messages[0]['content'])
            value = fixture(self.stage)
            if self.stage == 'review_p1':
                value['findings'][0]['evidence'] = [
                    {'source': 'W1', 'quote': '日期未定', 'scope': '尚未确认日期'}]
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()
        self.state['public_search_query'] = '公开通知'
        self.state['public_search_provider'] = '维基百科'
        source = {'name': '条目', 'content': '日期未定', 'url': 'https://example.org/article',
                  'kind': 'web', 'reference': 'W1', 'sha256': 'b' * 64,
                  'retrieved_at': '2026-01-01T00:00:00+00:00'}
        lookup.return_value = ({'provider': '维基百科', 'status': 'found', 'sources': [source]}, [source])
        report = run_decision(self.context, self.connections, self.state, self.before, self.after,
                              lambda _: None, before_discussion=lambda *args: None)
        lookup.assert_called_once_with('公开通知', '维基百科')
        self.assertIn('百科背景', self.packets['analysis_p1']['research_scope'])
        self.assertIn('W1', [s['reference'] for s in self.packets['review_p1']['sources']])
        self.assertIn(source['url'], str(report))
        citation = next(e for e in self.state['evidence'] if e.get('source') == 'W1')
        self.assertEqual((citation['url'], citation['excerpt'], citation['retrieved_at']),
                         (source['url'], '日期未定', source['retrieved_at']))

    @patch('delivery.decision_team_engine.lookup_public_sources')
    @patch('delivery.decision_team_engine.generate_reply')
    def test_legacy_skipped_lookup_remains_skipped_during_execution(self, generate, lookup):
        generate.side_effect = self.generator
        self.prepared()
        self.state['brief_draft']['public_search_query'] = '建议但未获授权'
        report = run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        lookup.assert_not_called()
        self.assertIn('未联网搜索', str(report))

    def generator(self, messages, **kwargs):
        self.packets[self.stage] = json.loads(messages[0]['content'])
        return json.dumps(fixture(self.stage), ensure_ascii=False)

    def prepared(self):
        draft = prepare_brief(self.context, self.connections, self.state, self.before, self.after)
        self.state['brief_draft'] = draft
        self.state['brief'] = confirm_brief({**{k: draft[k] for k in
            ('goal', 'metrics', 'constraints', 'preferences', 'assumptions', 'unknowns')}, 'answers': ''}, draft)
        self.state['phase'] = 'confirmed'

    @patch('delivery.decision_team_engine.generate_reply')
    def test_independent_inputs_freeze_then_late_user_message_reaches_review(self, generate):
        generate.side_effect = self.generator
        self.prepared()
        def discussion(stage, public, history):
            entry = {'role': 'user', 'content': '补充：费用包含税费'}
            if entry not in public:
                public.append(entry)
        result = run_decision(self.context, self.connections, self.state, self.before, self.after,
                              lambda _: None, before_discussion=discussion)
        packets = [self.packets[f'analysis_p{i}'] for i in (1, 2, 3)]
        self.assertEqual(packets[0], packets[1])
        self.assertEqual(packets[1], packets[2])
        self.assertNotIn('补充：费用包含税费', json.dumps(packets, ensure_ascii=False))
        self.assertNotIn('旧团队的答案', json.dumps(packets, ensure_ascii=False))
        self.assertNotIn('旧团队的答案', json.dumps(self.packets['clarifier'], ensure_ascii=False))
        self.assertIn('补充：费用包含税费', json.dumps(self.packets['review_p1'], ensure_ascii=False))
        self.assertEqual(self.packets['synthesizer']['conversation'][-1], {'role': 'user', 'content': '补充：费用包含税费'})
        self.assertEqual(self.state['phase'], 'completed')
        self.assertIn('待核实主张', result)
        self.assertIn('意见分布', result)
        self.assertIn('成本和速度哪个更重要', json.dumps(self.state['brief'], ensure_ascii=False))
        self.assertEqual(len(self.calls), 11)
        calc = next(e for e in self.state['evidence'] if e['type'] == 'calculation')
        self.assertEqual((calc['status'], calc['result']), ('executed', '600'))

    @patch('delivery.decision_team_engine.generate_reply')
    def test_visible_reply_is_only_supplied_to_clarifier_and_is_not_independent_evidence(self, generate):
        old_reply = self.context[-1]['content']
        self.context += [{'role': 'assistant', 'content': '【此前团队待复核材料，尚未核验】草稿'},
                         {'role': 'user', 'content': '请比较刚才的选项'},
                         {'role': 'assistant', 'content': '【用户选定的参考资料，由系统提取】'}]
        generate.side_effect = self.generator
        self.prepared()
        run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        self.assertEqual(self.packets['clarifier']['preceding_public_reply'], old_reply)
        for stage in ('analysis_p1', 'analysis_p2', 'analysis_p3'):
            self.assertNotIn(old_reply, json.dumps(self.packets[stage], ensure_ascii=False))

    @patch('delivery.decision_team_engine.generate_reply')
    def test_interjection_can_be_cited_as_user_statement_in_later_review(self, generate):
        def generated(messages, **kwargs):
            packet = json.loads(messages[0]['content'])
            self.packets[self.stage] = packet
            value = fixture(self.stage)
            if self.stage == 'review_p1':
                value['findings'][0]['verdict'] = 'supported'
                value['findings'][0]['evidence'] = [
                    {'source': 'U2', 'quote': '费用包含税费', 'scope': '用户补充的费用条件'}]
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()

        def interject(stage, public, history):
            entry = {'role': 'user', 'content': '补充：费用包含税费'}
            if entry not in public:
                public.append(entry)

        run_decision(self.context, self.connections, self.state, self.before, self.after,
                     lambda _: None, before_discussion=interject)
        source = next(s for s in self.packets['review_p1']['sources'] if s['reference'] == 'U2')
        self.assertEqual(source['content'], '补充：费用包含税费')
        evidence = next(e for e in self.state['evidence'] if e['source'] == 'U2')
        self.assertEqual((evidence['type'], evidence['excerpt']), ('user', '费用包含税费'))
        self.assertEqual(self.state['reviews'][0]['verdict'], 'supported')
        self.assertNotIn('费用包含税费', json.dumps(self.packets['analysis_p1'], ensure_ascii=False))

    @patch('delivery.decision_team_engine.generate_reply')
    def test_invented_sources_and_model_agreement_never_become_verified(self, generate):
        def generated(messages, **kwargs):
            return json.dumps(fixture(self.stage, bad_quote=True), ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()
        run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        self.assertEqual({f['verdict'] for f in self.state['reviews']}, {'unverified'})
        self.assertEqual(self.state['evidence'], [])

    @patch('delivery.decision_team_engine.generate_reply')
    def test_reviews_link_only_to_eligible_peers_without_losing_other_findings(self, generate):
        def generated(messages, **kwargs):
            self.packets[self.stage] = json.loads(messages[0]['content'])
            value = fixture(self.stage, extra=True)
            if self.stage == 'review_p1':
                valid = value['findings'][0]
                value['findings'] = [{'claim_id': 'P1C1', 'reason': '不可采用的自我质疑'},
                                     {'claim_id': ['P3C1']}, {**valid, 'claim_id': ' p2c1 '}]
            if self.stage == 'extra_review':
                value['findings'].insert(0, {'claim_id': 'P3C1', 'reason': '偏离本次追加核验'})
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()
        run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        p1, p2, p3 = [p['label'] for p in self.state['participants']]
        self.assertEqual(self.packets['review_p1']['review_targets'], {'P2C1': p2, 'P3C1': p3})
        self.assertEqual(self.packets['response_p2']['response_targets'], {'R1': p1})
        self.assertEqual(self.state['unlinked_review_counts'], {'review_p1': 2, 'extra_review': 1})
        self.assertEqual([(f['id'], f['claim_id']) for f in self.state['reviews']],
                         [('R1', 'P2C1'), ('R2', 'P3C1'), ('R3', 'P1C1')])
        self.assertEqual(self.packets['extra_review']['review_targets'], {'P1C1': p1})
        self.assertEqual([f['id'] for f in self.state['extra_round']['reviews']], ['R4'])
        self.assertNotIn('不可采用的自我质疑', json.dumps(self.packets['synthesizer'], ensure_ascii=False))

    @patch('delivery.decision_team_engine.generate_reply')
    def test_user_question_reaches_next_member_if_first_omits_reply(self, generate):
        def interject(stage, public, history):
            if stage == 'review_p1':
                for key, text, target, deferred in [('a', '如果我只能周末做呢？', '', False),
                        ('b', '已经点名并且回答过的问题', 'deepseek', False), ('c', '预算也减半', 'kimi', True)]:
                    public.append({'role': 'user', 'content': text})
                    history.append({'member': '用户', 'content': text, 'interjection_id': key,
                                    'target_provider': target, 'reply_deferred': deferred})

        def generated(messages, **kwargs):
            self.packets[self.stage] = json.loads(messages[0]['content'])
            value = fixture(self.stage)
            if self.stage != 'review_p1':
                value['user_reply'] = '那周末先用一半预算试一次。'
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()
        run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None,
                     before_discussion=interject)
        stages = [stage for stage, packet in self.packets.items() if 'new_user_messages' in packet]
        self.assertEqual(stages, ['review_p1', 'review_p2'])
        for stage in stages:
            self.assertEqual([msg['interjection_id'] for msg in self.packets[stage]['new_user_messages']], ['a', 'c'])
        self.assertEqual(self.state['user_replies'], {
            'review_p2': {'text': '那周末先用一半预算试一次。', 'interjection_ids': ['a', 'c']}})
        self.assertEqual(len(self.calls), 11)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_unlinked_reviews_and_unanswered_synthesis_remain_explicitly_unresolved(self, generate):
        def interject(stage, public, history):
            if stage == 'synthesizer':
                public.append({'role': 'user', 'content': '预算能否再减半？'})
                history.append({'member': '用户', 'content': '预算能否再减半？', 'interjection_id': 'last'})

        def generated(messages, **kwargs):
            value = fixture(self.stage)
            if self.stage.startswith('review_'):
                value['findings'] = [{'claim_id': '不存在'}]
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()
        run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None,
                     before_discussion=interject)
        self.assertEqual(self.state['phase'], 'completed')
        self.assertEqual(self.state['reviews'], [])
        self.assertEqual(self.state['responses'], [])
        self.assertNotIn('未提出', self.state['stop_reason'])
        self.assertIn('未能对应', self.state['stop_reason'])
        self.assertFalse(self.state.get('user_replies'))
        self.assertTrue(any('1 条用户插话未收到直接答复' in note for note in self.state['unresolved']))

    @patch('delivery.decision_team_engine.generate_reply')
    def test_exact_quote_without_applicable_scope_does_not_validate_a_claim(self, generate):
        def generated(messages, **kwargs):
            value = fixture(self.stage, bad_quote=True)
            if self.stage.startswith('review_'):
                value['findings'][0]['evidence'] = [{'source': 'S1', 'quote': '额外费用200元', 'scope': ''}]
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()
        run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        self.assertEqual({f['verdict'] for f in self.state['reviews']}, {'unverified'})
        self.assertEqual(self.state['evidence'], [])

    @patch('delivery.decision_team_engine.generate_reply')
    def test_summary_cannot_attach_real_evidence_to_an_unrelated_claim(self, generate):
        def generated(messages, **kwargs):
            value = fixture(self.stage)
            if self.stage == 'synthesizer':
                value['basis'] = [
                    {'text': '完全无关的收益已核实', 'status': 'refuted', 'evidence_ids': ['E1']},
                    {'text': 'p1 主张：六个月费用为500元', 'status': 'refuted', 'evidence_ids': ['E1']},
                ]
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()
        run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        self.assertEqual(self.state['summary']['basis'][0]['status'], 'unverified')
        self.assertEqual(self.state['summary']['basis'][1]['status'], 'refuted')

    @patch('delivery.decision_team_engine.generate_reply')
    def test_summary_claim_id_uses_reviewed_text_and_only_its_actual_evidence(self, generate):
        original = 'p1 主张：六个月费用为500元'
        rewrite = '合成器改写且扩大范围的摘要'
        def generated(messages, **kwargs):
            value = fixture(self.stage)
            if self.stage == 'synthesizer':
                self.assertIn('若省略 claim_id', kwargs['system_instruction'])
                value['basis'] = [
                    {'claim_id': 'P1C1', 'text': rewrite, 'status': 'refuted', 'evidence_ids': ['E1', 'E999']},
                    {'claim_id': 'P1C1', 'text': '核验方向错误', 'status': 'supported', 'evidence_ids': ['E1']},
                    {'claim_id': 'P2C1', 'text': original, 'status': 'refuted', 'evidence_ids': ['E1']},
                    {'claim_id': 'P0C0', 'text': original, 'status': 'refuted', 'evidence_ids': ['E1']},
                    {'claim_id': ['P1C1'], 'text': original, 'status': 'refuted', 'evidence_ids': ['E1']},
                    {'claim_id': 'P1C1', 'text': original, 'status': 'refuted', 'evidence_ids': ['E999']},
                ]
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()
        result = run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        valid, *invalid = self.state['summary']['basis']
        self.assertEqual(valid, {'claim_id': 'P1C1', 'text': original, 'status': 'refuted', 'evidence_ids': ['E1']})
        self.assertNotIn(rewrite, result)
        self.assertTrue(all(item['status'] == 'unverified' and item['evidence_ids'] == [] for item in invalid))

    @patch('delivery.decision_team_engine.generate_reply')
    def test_conflicting_reviews_keep_both_sides_in_summary_and_citations(self, generate):
        self.sources[0]['content'] += '；套餐费用500元'

        def generated(messages, **kwargs):
            value = fixture(self.stage, extra=True)
            if self.stage == 'extra_review':
                value['findings'][0].update(verdict='supported', evidence=[
                    {'source': 'S1', 'quote': '套餐费用500元', 'scope': '套餐报价'}])
            if self.stage == 'synthesizer':
                value['basis'] = [{'claim_id': 'P1C1', 'text': '核验结果一致', 'status': 'refuted',
                                   'evidence_ids': ['E1']}]
            return json.dumps(value, ensure_ascii=False)

        generate.side_effect = generated
        self.prepared()
        report = run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        basis, = self.state['summary']['basis']
        self.assertEqual((basis['claim_id'], basis['status']), ('P1C1', 'unverified'))
        sides = basis['evidence_by_verdict']
        self.assertEqual(sides['supported'], self.state['extra_round']['reviews'][0]['evidence_ids'])
        self.assertEqual(sides['refuted'], ['E1'])
        self.assertNotIn('核验结果一致', report)
        self.assertIn('支持和反驳依据并存', self.state['summary']['recommendation'])
        row, = [r for r in report_references(self.state) if r['section'] == '主要依据']
        self.assertEqual(row['ids'], sides['supported'] + sides['refuted'])
        self.assertIn('支持依据 ' + sides['supported'][0] + '，反驳依据 E1', row['line'])
        self.assertIn(row['line'], report)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_summarizer_cannot_claim_support_by_inventing_evidence_ids(self, generate):
        def generated(messages, **kwargs):
            value = fixture(self.stage)
            if self.stage == 'synthesizer':
                value['basis'] = [{'text': '模型声称的依据', 'status': 'supported', 'evidence_ids': ['E999']}]
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()
        result = run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        self.assertEqual(self.state['summary']['basis'][0]['status'], 'unverified')
        self.assertIn('[暂无法核实] 模型声称的依据', result)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_final_opinion_uses_the_post_review_position_instead_of_initial_vote(self, generate):
        def generated(messages, **kwargs):
            value = fixture(self.stage)
            if self.stage == 'response_p1':
                value['stance'] = 'oppose'
                value['stance_reason'] = '纠正计算后不再支持'
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()
        result = run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        self.assertIn('DeepSeek：反对（方案A）；纠正计算后不再支持', result)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_important_new_evidence_survives_when_remaining_budget_is_reserved_for_synthesis(self, generate):
        generate.side_effect = lambda *a, **kw: json.dumps(fixture(self.stage, extra=True), ensure_ascii=False)
        self.prepared()
        skipped = []
        result = run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None,
                              allow_extra_round=lambda: False, skip_optional=lambda: skipped.append(True))
        self.assertEqual(skipped, [True])
        self.assertNotIn('extra_review', self.calls)
        self.assertEqual(self.calls[-1], 'synthesizer')
        self.assertIn('重要新依据仍待追加核验', result)
        self.assertEqual(self.state['phase'], 'completed')

    @patch('delivery.decision_team_engine.generate_reply')
    def test_only_one_extra_round_for_traceable_important_new_evidence(self, generate):
        generate.side_effect = lambda messages, **kwargs: json.dumps(fixture(self.stage, extra=True), ensure_ascii=False)
        self.prepared()
        result = run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        self.assertEqual(self.calls.count('extra_review'), 1)
        self.assertEqual(self.calls.count('extra_response'), 1)
        self.assertEqual(len(self.calls), 13)
        self.assertIn('达到最多一轮追加讨论', result)
        self.assertEqual(self.state['extra_round']['trigger']['finding_id'], 'R3')

    @patch('delivery.decision_team_engine.generate_reply')
    def test_missing_response_stops_without_fabricating_a_result(self, generate):
        generate.side_effect = lambda messages, **kwargs: json.dumps(fixture(self.stage, missing_response=True), ensure_ascii=False)
        self.prepared()
        with self.assertRaises(ProviderError):
            run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        self.assertNotIn('synthesizer', self.calls)
        self.assertNotEqual(self.state['phase'], 'completed')

    def test_calculator_rejects_code_and_bounds(self):
        self.assertEqual(_calculate('100 * 6'), '600')
        self.assertEqual(_calculate('1 / 2'), '0.5')
        for value in ('__import__("os")', '2 ** 999999', '1/0', '10**10', '1e99'):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                _calculate(value)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_changed_opinion_without_new_evidence_or_corrected_reasoning_is_rejected(self, generate):
        def generated(messages, **kwargs):
            value = fixture(self.stage)
            if self.stage == 'response_p1':
                value['responses'][0]['change_basis'] = 'none'
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()
        with self.assertRaises(ProviderError):
            run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        self.assertNotIn('synthesizer', self.calls)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_model_claiming_an_unperformed_test_is_not_published_as_a_result(self, generate):
        def generated(messages, **kwargs):
            value = fixture(self.stage)
            if self.stage == 'synthesizer':
                value['basis'] = ['已通过功能测试']
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()
        with self.assertRaises(ProviderError):
            run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        self.assertNotEqual(self.state['phase'], 'completed')

    @patch('delivery.decision_team_engine.generate_reply')
    def test_member_saying_it_looked_something_up_stops_before_review(self, generate):
        def generated(messages, **kwargs):
            value = fixture(self.stage)
            if self.stage == 'analysis_p2':
                value['recommendation'] = '我查了一下，官网写的是每周三天。'
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()
        with self.assertRaises(ProviderError):
            run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        self.assertNotIn('review_p1', self.calls)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_the_users_own_situation_and_quoted_words_complete_the_round(self, generate):
        question = '我已上线一个小程序，已通过功能测试。要不要迁移到云服务？'
        self.context = [{'role': 'user', 'content': question}]
        def generated(messages, **kwargs):
            value = fixture(self.stage)
            if self.stage == 'clarifier':
                value.update(goal='我已上线一个小程序，想判断是否迁移到云服务',
                             assumptions='已通过功能测试的版本不再改动',
                             assignments=[{'member': f'p{index}', 'angle': angle, 'focus': '我已上线一个小程序'}
                                          for index, angle in enumerate(('feasibility', 'resources', 'alternatives'), 1)])
            elif self.stage == 'analysis_p1':
                # json.dumps escapes these quotes; the member is quoting the user.
                value['recommendation'] = '你说"已通过功能测试"，那就先别改核心模块。'
            elif self.stage == 'review_p2':
                value['findings'][0]['reason'] = '你的小程序已上线，已通过功能测试，迁移前要先做回滚预案。'
            return json.dumps(value, ensure_ascii=False)
        generate.side_effect = generated
        self.prepared()
        self.assertEqual(self.state['brief']['goal'], '我已上线一个小程序，想判断是否迁移到云服务')
        self.assertEqual(self.state['participants'][0]['assignment']['focus'], '我已上线一个小程序')
        run_decision(self.context, self.connections, self.state, self.before, self.after, lambda _: None)
        self.assertEqual(self.state['phase'], 'completed')

    @patch('delivery.decision_team_engine.generate_reply')
    def test_time_limit_stops_before_dispatching_a_late_mention_or_review(self, generate):
        generate.side_effect = self.generator
        self.prepared()
        discussion_calls = []
        with patch('delivery.decision_team_engine.MAX_DISCUSSION_SECONDS', 7), \
                patch('delivery.decision_team_engine.time.monotonic', side_effect=iter(range(100))):
            with self.assertRaises(ProviderError):
                run_decision(self.context, self.connections, self.state, self.before, self.after,
                             lambda _: None, before_discussion=lambda *args: discussion_calls.append(args))
        self.assertEqual(discussion_calls, [])
        self.assertNotIn('review_p1', self.calls)


class DecisionWorkflowTests(TestCase):
    def test_member_reply_quotes_the_visible_finding_even_at_the_end_of_a_long_review(self):
        from types import SimpleNamespace
        from .team_views import quoted_member_turn
        findings = [{'reason': '待核算的支出很长。' * 80} for _ in range(5)]
        findings.append({'reason': '这笔预算漏了交通费。', 'important': True})
        step = SimpleNamespace(id=uuid.uuid4(), role='review_p1', label='交叉核验', provider='kimi',
                               run=SimpleNamespace(workflow='decision_v1', decision_state={}),
                               output=json.dumps({'findings': findings}, ensure_ascii=False))
        quote = quoted_member_turn(SimpleNamespace(reply_to_step_id=step.id, reply_to_step=step))
        self.assertEqual(quote, {'step_id': str(step.id), 'member': 'kimi', 'content': '这笔预算漏了交通费。'})

    def setUp(self):
        self.user = get_user_model().objects.create_user(email='decision@example.test', password='ExamplePass123!')
        self.other = get_user_model().objects.create_user(email='other@example.test', password='ExamplePass123!')
        self.client.force_login(self.user)
        self.conversation = Conversation.objects.create(owner=self.user, title='讨论')
        Message.objects.create(conversation=self.conversation, role='user', content='每月100元，共6个月；预算1000元。')
        self.task = Task.objects.create(owner=self.user, conversation=self.conversation, goal='选择方案', status='waiting')
        self.url = f'/api/conversations/{self.conversation.id}/collaborate'
        self.confirm_url = f'/api/conversations/{self.conversation.id}/confirm-decision'
        self.team = connections()
        self.revision = 'a' * 64
        self.resolve_patch = patch('delivery.team_views.resolve_team', return_value=self.team)
        self.summary_patch = patch('delivery.team_views.team_summary', return_value={'readiness': 'available', 'revision': self.revision})
        self.resolve_patch.start()
        self.summary_patch.start()
        self.addCleanup(self.resolve_patch.stop)
        self.addCleanup(self.summary_patch.stop)
        self.packets = {}

    def submit(self):
        body = {'background': True, 'client_request_id': str(uuid.uuid4()), 'confirm_cost': True,
                'expected_team': self.revision, 'workflow': 'decision_v1', 'delivery_mode': 'text',
                'expected_max_calls': 14, 'expected_task': str(self.task.id),
                'expected_task_updated_at': self.task.updated_at.isoformat(),
                'expected_sources': sources_revision(SourceMaterial.objects.filter(conversation=self.conversation))}
        return self.client.post(self.url, json.dumps(body), content_type='application/json')

    def generate(self, messages, **kwargs):
        step = AgentStep.objects.get(status='running')
        self.packets[step.role] = json.loads(messages[0]['content'])
        return json.dumps(fixture(step.role), ensure_ascii=False)

    def confirm(self, run):
        draft = run.decision_state['brief_draft']
        brief = {k: draft[k] for k in ('goal', 'metrics', 'constraints', 'preferences', 'assumptions', 'unknowns')}
        brief['answers'] = ''
        body = {'run_id': str(run.id), 'brief': brief, 'confirm_continue': True, 'expected_max_calls': 14}
        return self.client.post(self.confirm_url, json.dumps(body), content_type='application/json'), body

    @patch('delivery.provider._request')
    def test_queued_discussion_sends_current_personal_memory_to_members(self, transport):
        fact = '以后回答请先给结论'
        saved = self.client.post('/api/conversations', {'client_message_id': str(uuid.uuid4()), 'content': fact},
                                 content_type='application/json')
        self.assertEqual(saved.status_code, 201)
        self.assertEqual(self.submit().status_code, 202)
        note = self.client.get('/api/memories').json()['memories'][0]
        changed = '每次回答先给出一句建议'
        self.client.patch('/api/memories/' + note['id'], {'content': changed}, content_type='application/json')

        def reply(_connection, _path, payload, **_kwargs):
            packet = json.loads(payload['messages'][-1]['content'])
            self.assertEqual(packet['personal_memories'], [changed])
            return {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content':
                    json.dumps(fixture(AgentStep.objects.get(status='running').role), ensure_ascii=False)}}]}

        transport.side_effect = reply
        self.assertIsNotNone(process_next_run())
        self.assertEqual(transport.call_count, 2)
        run = TeamRun.objects.get(task=self.task)
        self.assertEqual(run.status, 'awaiting_input')
        self.assertNotIn(changed, json.dumps(run.input_context, ensure_ascii=False))

    @patch('delivery.decision_team_engine.generate_reply')
    def test_pause_for_user_confirmation_resume_once_export_and_delete(self, generate):
        generate.side_effect = self.generate
        self.assertEqual(self.submit().status_code, 202)
        self.assertIsNotNone(process_next_run())
        run = TeamRun.objects.get()
        self.task.refresh_from_db()
        self.assertEqual((run.status, self.task.status, run.calls_used), ('awaiting_input', 'waiting', 2))
        self.assertEqual(set(self.packets), {'clarifier', 'clarifier_peer'})
        self.assertFalse(Message.objects.filter(conversation=self.conversation, role='assistant').exists())
        response, body = self.confirm(run)
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(self.client.post(self.confirm_url, json.dumps(body), content_type='application/json').status_code, 200)
        self.assertIsNotNone(process_next_run())
        run.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual((run.status, self.task.status, run.calls_used), ('completed', 'completed', 12))
        self.assertIsNone(process_next_run())
        self.assertEqual(self.packets['analysis_p1'], self.packets['analysis_p2'])
        self.assertEqual(self.packets['analysis_p2'], self.packets['analysis_p3'])
        self.assertEqual(run.decision_state['phase'], 'completed')
        self.assertNotIn('sources', run.decision_state)
        self.assertIn('未决事项', Message.objects.get(conversation=self.conversation, role='assistant').content)
        self.assertEqual(export_data(self.user)['team_runs'][0]['decision']['version'], 'decision-v1')
        self.assertEqual(export_data(self.other)['team_runs'], [])
        self.conversation.delete()
        self.assertFalse(TeamRun.objects.exists())

    @patch('delivery.decision_team_engine.generate_reply')
    def test_stale_task_and_wrong_user_cannot_continue(self, generate):
        generate.side_effect = self.generate
        self.submit()
        process_next_run()
        run = TeamRun.objects.get()
        self.client.force_login(self.other)
        response, _ = self.confirm(run)
        self.assertEqual(response.status_code, 404)
        self.client.force_login(self.user)
        Task.objects.filter(pk=self.task.pk).update(goal='新需求', updated_at=timezone.now() + timezone.timedelta(seconds=2))
        response, _ = self.confirm(run)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'brief_stale')
        self.assertIsNone(process_next_run())
        self.assertEqual(generate.call_count, 2)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_interjection_during_analysis_is_deferred_without_input_leak(self, generate):
        sent = False
        def generated(messages, **kwargs):
            nonlocal sent
            step = AgentStep.objects.get(status='running')
            self.packets[step.role] = json.loads(messages[0]['content'])
            if step.role == 'analysis_p1' and not sent:
                sent = True
                payload = {'run_id': str(step.run_id), 'content': '补充：价格含税', 'target_provider': '',
                           'client_request_id': str(uuid.uuid4())}
                response = self.client.post(f'/api/conversations/{self.conversation.id}/team-messages',
                                            json.dumps(payload), content_type='application/json')
                self.assertEqual(response.status_code, 201)
            return json.dumps(fixture(step.role), ensure_ascii=False)
        generate.side_effect = generated
        self.submit()
        process_next_run()
        run = TeamRun.objects.get()
        self.assertEqual(self.confirm(run)[0].status_code, 202)
        process_next_run()
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(self.packets['analysis_p1'], self.packets['analysis_p2'])
        self.assertEqual(self.packets['analysis_p2'], self.packets['analysis_p3'])
        self.assertNotIn('价格含税', json.dumps(self.packets['analysis_p3'], ensure_ascii=False))
        self.assertIn('价格含税', json.dumps(self.packets['review_p1'], ensure_ascii=False))
        self.assertEqual(TeamInterjection.objects.get().status, 'included')

    @patch('delivery.decision_team_engine.generate_reply')
    def test_changed_requirement_invalidates_brief_and_clears_temporary_sources(self, generate):
        generate.side_effect = self.generate
        self.submit()
        process_next_run()
        run = TeamRun.objects.get()
        response = self.client.post(f'/api/conversations/{self.conversation.id}',
            json.dumps({'content': '预算改为500元'}), content_type='application/json')
        self.assertEqual(response.status_code, 201)
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.input_context, [])
        self.assertNotIn('sources', run.decision_state)
        self.assertEqual(AgentStep.objects.filter(run=run, status='pending').count(), 0)
        self.assertIsNone(process_next_run())
        self.assertEqual(generate.call_count, 2)

    @patch('delivery.decision_team_engine.generate_reply')
    def test_deleting_source_while_awaiting_confirmation_retires_raw_snapshot(self, generate):
        text = '仅用于测试的参考材料。'
        source = SourceMaterial.objects.create(owner=self.user, conversation=self.conversation, kind='file',
            name='模拟材料', media_type='text/plain', content=text, byte_size=len(text.encode()),
            sha256=hashlib.sha256(text.encode()).hexdigest(), included=True, metadata={})
        generate.side_effect = self.generate
        self.assertEqual(self.submit().status_code, 202)
        process_next_run()
        run = TeamRun.objects.get()
        self.assertIn(text, json.dumps(run.decision_state, ensure_ascii=False))
        self.assertEqual(self.client.delete(f'/api/sources/{source.id}').status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.input_context, [])
        self.assertNotIn(text, json.dumps(run.decision_state, ensure_ascii=False))

    @patch('delivery.decision_team_engine.generate_reply')
    def test_pause_during_model_call_discards_late_response(self, generate):
        def generated(messages, **kwargs):
            step = AgentStep.objects.get(status='running')
            if step.role == 'analysis_p1':
                response = self.client.post(f'/api/tasks/{self.task.id}/transition',
                    json.dumps({'action': 'pause'}), content_type='application/json')
                self.assertEqual(response.status_code, 200)
            return json.dumps(fixture(step.role), ensure_ascii=False)
        generate.side_effect = generated
        self.submit()
        process_next_run()
        run = TeamRun.objects.get()
        self.assertEqual(self.confirm(run)[0].status_code, 202)
        process_next_run()
        run.refresh_from_db()
        self.assertEqual((run.status, run.decision_state['phase']), ('paused', 'paused'))
        self.assertEqual(run.steps.get(role='analysis_p1').status, 'failed')
        self.assertFalse(Message.objects.filter(conversation=self.conversation, role='assistant').exists())
        self.assertEqual(run.input_context, [])

    @patch('delivery.decision_team_engine.generate_reply')
    def test_confirmed_resume_has_fresh_queue_window_after_long_user_wait(self, generate):
        generate.side_effect = self.generate
        self.submit()
        process_next_run()
        run = TeamRun.objects.get()
        TeamRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - timezone.timedelta(hours=2))
        run.refresh_from_db()
        self.assertEqual(self.confirm(run)[0].status_code, 202)
        self.assertIsNotNone(process_next_run())
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')

    @patch('delivery.team_views.generate_reply', return_value='点名成员的模拟回应')
    @patch('delivery.decision_team_engine.generate_reply')
    def test_targeted_interjection_costs_one_call_and_is_shown_as_member_reply(self, generate, mention):
        sent = False
        def generated(messages, **kwargs):
            nonlocal sent
            step = AgentStep.objects.get(status='running')
            self.packets[step.role] = json.loads(messages[0]['content'])
            if step.role == 'response_p3' and not sent:
                sent = True
                published = AgentStep.objects.get(run=step.run, role='analysis_p3', status='completed')
                payload = {'run_id': str(step.run_id), 'content': '请千问说明预算', 'target_provider': 'qwen',
                           'reply_to_step': str(published.id),
                           'client_request_id': str(uuid.uuid4())}
                url = f'/api/conversations/{self.conversation.id}/team-messages'
                for invalid in (step.id, uuid.uuid4(), AgentStep.objects.get(run=step.run, role='analysis_p2').id):
                    self.assertEqual(self.client.post(url, json.dumps({**payload, 'reply_to_step': str(invalid)}),
                                                      content_type='application/json').status_code, 400)
                response = self.client.post(url, json.dumps(payload), content_type='application/json')
                self.assertEqual(response.status_code, 201)
                self.assertEqual(self.client.post(url, json.dumps(payload), content_type='application/json').status_code, 200)
                self.assertEqual(self.client.post(url, json.dumps({**payload, 'reply_to_step': None}),
                                                  content_type='application/json').status_code, 409)
            return json.dumps(fixture(step.role), ensure_ascii=False)
        generate.side_effect = generated
        self.submit()
        process_next_run()
        run = TeamRun.objects.get()
        self.assertEqual(self.confirm(run)[0].status_code, 202)
        process_next_run()
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used), ('completed', 13))
        self.assertEqual(TeamInterjection.objects.get().status, 'answered')
        self.assertEqual(mention.call_count, 1)
        turn = json.loads(mention.call_args.args[0][0]['content'])['discussion'][-1]
        self.assertEqual(turn['reply_to']['member'], 'qwen')
        self.assertIn('p3 的条件建议', turn['reply_to']['content'])
        self.assertEqual(turn['content'], '请千问说明预算')
        self.assertNotIn('reply_to', self.packets['analysis_p1'])
        self.assertEqual(self.packets['analysis_p1'], self.packets['analysis_p3'])
        discussion = export_data(self.user)['team_runs'][0]['discussion']
        self.assertTrue(any(item['content'] == '点名成员的模拟回应' for item in discussion))

    @patch('delivery.decision_team_engine.generate_reply')
    def test_provider_failure_retains_partial_work_and_never_claims_completion(self, generate):
        def generated(messages, **kwargs):
            step = AgentStep.objects.get(status='running')
            if step.role == 'review_p2':
                raise ProviderError('模拟模型超时。')
            return json.dumps(fixture(step.role), ensure_ascii=False)
        generate.side_effect = generated
        self.submit()
        process_next_run()
        run = TeamRun.objects.get()
        self.assertEqual(self.confirm(run)[0].status_code, 202)
        process_next_run()
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIn('模拟模型超时', run.decision_state['stop_reason'])
        self.assertIn('本轮没有完成全部核验', run.decision_state['unresolved'][0])
        self.assertFalse(Message.objects.filter(conversation=self.conversation, role='assistant').exists())
        self.assertIsNone(process_next_run())
