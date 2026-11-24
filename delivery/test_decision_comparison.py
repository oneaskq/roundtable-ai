"""Structured comparison is optional and never presented as verified evidence."""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from .decision_team_engine import _calculation, _evidence, make_state, render_result, report_references, run_decision
from .provider import ProviderError
from .review_groups import finding_groups, matching_steps
from .test_decision_team import connections, fixture


class ComparisonTests(SimpleTestCase):
    def test_reference_ids_distinguish_source_and_scope_even_with_the_same_text_or_calculation(self):
        state = {'evidence': []}
        source = {'kind':'file','name':'合同','sha256':'a'*64,'content':'两个项目'}
        library = {'S1':source,'S2':{**source,'name':'另一份合同'}}
        quote = {'source':'S1','quote':'两个项目','scope':'岗位职责'}
        self.assertEqual(_evidence([quote, {**quote,'scope':'整个部门'}, {**quote,'source':'S2'}, quote],
                                   state, library), ['E1','E2','E3'])
        first = _calculation({'expression':'2*4','scope':'8小时'}, state)
        second = _calculation({'expression':'2*4','scope':'8天'}, state)
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['key'], second['key'])  # Rewording scope alone cannot qualify as new evidence.

    def test_three_reviewers_of_matching_offer_claims_merge_only_with_the_same_evidence_and_response(self):
        team = connections()
        state = make_state(team, [{'reference':'S1','kind':'file','name':'乙的录用通知',
                                   'sha256':'a' * 64,'content':'乙可独立负责一个项目'}])
        state.update(phase='confirmed', brief={'goal':'比较两份实习','unknowns':'','assumptions':''})
        quote = {'source':'S1','quote':'独立负责一个项目','scope':'岗位职责'}
        review_text = '岗位职责可从乙的录用通知中核对。'
        stage, steps = '', []
        def before(current, *_):
            nonlocal stage
            stage = current
        def generate(*args, **kwargs):
            value = fixture(stage)
            if stage.startswith('analysis_'):
                value['claims'][0].update(text='乙允许独立负责项目', proposal='乙', metric='岗位职责', evidence=[quote])
            elif stage.startswith('review_'):
                row = value['findings'][0]
                row.pop('calculation', None)
                row.update(reason=review_text, important=True, categories=['fact'], verdict='supported', evidence=[quote])
            return json.dumps(value, ensure_ascii=False)
        def after(role, output):
            steps.append(SimpleNamespace(role=role, status='completed', output=output))
        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            result = run_decision([{'role':'user','content':'请比较'}], team, state, before, after, lambda _: None)
        self.assertEqual([len(g) for g in finding_groups(state)], [3])
        self.assertEqual(result.count(review_text), 1)
        self.assertEqual(len(state['reviews']), 3)
        self.assertEqual(len(state['responses']), 3)
        self.assertEqual(len({row['claim_id'] for row in state['reviews']}), 3)
        keys = matching_steps(state, steps)
        self.assertEqual(len({keys['review_' + p['id']] for p in state['participants']}), 1)
        self.assertEqual(len({keys['response_' + p['id']] for p in state['participants']}), 1)
        self.assertIn('## 成员核验记录', result)
        links = [r for r in report_references(state) if r['section'] == '成员核验记录']
        grouped = next(r for r in links if r['line'] in result)
        self.assertEqual(grouped['ids'], ['E1'])
        self.assertEqual(len(links), 4)  # Three original archived lines and the exact grouped line.
        self.assertTrue(all(p['label'] in grouped['line'] for p in state['participants']))
        archived = deepcopy(state)
        archived.pop('review_section_title')
        self.assertIn('## 重要分歧与核验', render_result(archived))
        self.assertIn({**grouped, 'section': '重要分歧与核验'}, report_references(archived))
        for section, field, other in [('claim','text','另一项主张'),('claim','metric','不同指标'),
            ('claim','proposal','甲'),('claim','evidence_ids',[]),('finding','impact','待遇'),('finding','verdict','refuted'),
            ('finding','categories',['preference']),('finding','reason','有不同理由'),('finding','evidence_ids',[]),
            ('response','decision','reject'),('response','changed',False),('response','reason','另外的回应')]:
            with self.subTest(section=section, field=field):
                changed = deepcopy(state)
                row = {'claim':changed['analyses'][0]['claims'][0], 'finding':changed['reviews'][0],
                       'response':changed['responses'][0]}[section]
                row[field] = other
                self.assertEqual(sorted(len(g) for g in finding_groups(changed)), [1, 2])
                self.assertEqual(len([k for k in matching_steps(changed, steps) if k.startswith('review_')]), 2)

    def run_with_options(self, options=None, **summary_fields):
        team = connections()
        state = make_state(team, [])
        state.update(phase='confirmed', brief={'goal': '选择方案', 'unknowns': '', 'assumptions': ''})
        stage = ''

        def before(current, label, connection):
            nonlocal stage
            stage = current

        def generate(messages, **kwargs):
            value = fixture(stage)
            if stage == 'synthesizer':
                value.update(summary_fields)
                if options is not None:
                    value['options'] = options
            return json.dumps(value, ensure_ascii=False)

        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            result = run_decision([{'role': 'user', 'content': '比较方案'}], team, state,
                                  before, lambda *_: None, lambda _: None)
        return state, result

    def test_comparison_is_persisted_and_displayed_with_conditions(self):
        state, result = self.run_with_options([
            {'name': '方案A', 'tradeoffs': '成本较低但耗时较长', 'conditions': '报价尚待确认'},
            {'name': '方案B', 'tradeoffs': '速度较快', 'conditions': '预算允许时'},
        ])
        self.assertEqual(len(state['summary']['options']), 2)
        self.assertIn('## 方案比较', result)
        self.assertIn('报价尚待确认', result)
        self.assertIn('由模型整理', result)
        self.assertLess(result.index('## 主要依据'), result.index('## 方案比较'))

    def test_legacy_output_without_options_remains_valid(self):
        state, result = self.run_with_options()
        self.assertEqual(state['summary']['options'], [])
        self.assertNotIn('## 方案比较', result)

    def test_short_answer_keeps_unique_conditions_and_skips_an_empty_next_step(self):
        advice = '先与供应商确认总价。'
        state, result = self.run_with_options(recommendation=advice, conditions=['报价仍有效', '报价仍有效'],
                                              next_steps=[], unknowns=['是否含税', '是否含税'])
        self.assertTrue(result.startswith('## 当前建议与条件\n' + advice + '\n\n'))
        self.assertEqual(result.count('报价仍有效'), 1)
        self.assertEqual(state['summary']['unknowns'], ['是否含税'])
        self.assertNotIn('## 下一步', result)

    def test_report_prioritizes_action_and_combines_only_matching_opinions(self):
        state, _ = self.run_with_options()
        state['final_opinions'][-1]['reason'] = '需要先拿到报价'
        result = render_result(state)
        self.assertLess(result.index('## 下一步'), result.index('## 主要依据'))
        self.assertEqual(result.count('：有条件支持（方案A）；取决于总价'), 1)
        self.assertIn('DeepSeek、Kimi：', result)
        self.assertIn('需要先拿到报价', result)
        links = report_references(state)
        ref = next(link for link in links if link['section'] == '成员核验记录')
        self.assertIn(ref['line'] + '（依据 ' + '、'.join(ref['ids']) + '）', result)
        self.assertTrue(ref['legacy_match'])
        checked = next(f for f in state['reviews'] if f['evidence_ids'])
        state['reviews'].append({**checked, 'evidence_ids': []})
        overlapping = next(link for link in report_references(state) if link['line'] == ref['line'])
        self.assertFalse(overlapping['legacy_match'])

    def test_malformed_comparison_is_not_published(self):
        with self.assertRaises(ProviderError):
            self.run_with_options([{'name': '', 'tradeoffs': '未知'}])
