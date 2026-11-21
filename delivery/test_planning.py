"""Offline plan validation: no provider, database, tool or secret access."""
import copy
import json
from unittest.mock import patch

from django.test import SimpleTestCase

from .planning import (MAX_PLAN_CHARS, PlanError, PlanToolsUnavailable, parse_plan,
                       plan_prompt, routing_role, schedule, validate_plan)


def sample_plan():
    return {
        'goal': '制作可下载的整数求和程序与测试文件',
        'criteria': ['交付sum.py和test_sum.py，明确空输入和负数输入的预期结果'],
        'work_items': [
            {'id': 'worker_1', 'specialty': 'coding',
             'instruction': '设计整数求和函数及输入约束，交付代码方案。', 'depends_on': []},
            {'id': 'worker_2', 'specialty': 'critique',
             'instruction': '独立分析边界条件并列出可执行测试案例。', 'depends_on': []},
        ],
        'required_tools': [],
    }


class PlanningTests(SimpleTestCase):
    def parse(self, document, **kwargs):
        return parse_plan(json.dumps(document, ensure_ascii=False), **kwargs)

    @patch('delivery.provider._request', side_effect=AssertionError('unexpected external request'))
    def test_valid_plan_is_detached_and_routes_through_existing_roles(self, request):
        source = sample_plan()
        parsed = self.parse(source)
        self.assertEqual(parsed, source)
        self.assertEqual([routing_role(item, index) for index, item in enumerate(schedule(parsed))],
                         ['proposer', 'reviewer'])
        parsed['work_items'][0]['instruction'] = '本地修改不应改变源文档'
        self.assertNotEqual(parsed, source)
        request.assert_not_called()

    def test_forward_dependency_is_sorted_and_independent_roots_stay_first(self):
        plan = sample_plan()
        dependent = {'id': 'worker_3', 'specialty': 'writing',
                     'instruction': '根据两份独立方案起草使用说明。',
                     'depends_on': ['worker_1', 'worker_2']}
        plan['work_items'].insert(0, dependent)
        ordered = schedule(self.parse(plan))
        self.assertEqual([item['id'] for item in ordered], ['worker_1', 'worker_2', 'worker_3'])
        ordered[0]['depends_on'].append('worker_3')
        self.assertEqual(plan['work_items'][1]['depends_on'], [])

    def test_diamond_dependency_and_stable_order(self):
        plan = sample_plan()
        plan['work_items'] += [
            {'id': 'worker_4', 'specialty': 'writing', 'instruction': '整合上一步材料成为最终说明。',
             'depends_on': ['worker_3', 'worker_2']},
            {'id': 'worker_3', 'specialty': 'analysis', 'instruction': '分析第一份提案的复杂度约束。',
             'depends_on': ['worker_1']},
        ]
        self.assertEqual([item['id'] for item in schedule(self.parse(plan))],
                         ['worker_1', 'worker_2', 'worker_3', 'worker_4'])

    def test_cycles_unknown_dependencies_and_loss_of_independence_are_rejected(self):
        cases = []
        single_root = sample_plan()
        single_root['work_items'][1]['depends_on'] = ['worker_1']
        cases.append(single_root)
        missing = sample_plan()
        missing['work_items'][1]['depends_on'] = ['worker_4']
        cases.append(missing)
        self_reference = sample_plan()
        self_reference['work_items'][0]['depends_on'] = ['worker_1']
        cases.append(self_reference)
        duplicate = sample_plan()
        duplicate['work_items'].append({'id': 'worker_3', 'specialty': 'analysis',
                                       'instruction': '分析两份已有材料中的矛盾。',
                                       'depends_on': ['worker_1', 'worker_1']})
        cases.append(duplicate)
        disconnected_cycle = sample_plan()
        disconnected_cycle['work_items'] += [
            {'id': 'worker_3', 'specialty': 'analysis', 'instruction': '分析第二份材料中的潜在问题。',
             'depends_on': ['worker_4']},
            {'id': 'worker_4', 'specialty': 'analysis', 'instruction': '分析第一份材料中的潜在问题。',
             'depends_on': ['worker_3']},
        ]
        cases.append(disconnected_cycle)
        for plan in cases:
            with self.subTest(plan=plan):
                with self.assertRaises(PlanError):
                    self.parse(plan)

    def test_actual_runtime_state_and_delivery_mode_control_tools(self):
        plan = sample_plan()
        plan['required_tools'] = ['python_unittest', 'javascript_check']
        with self.assertRaises(PlanToolsUnavailable) as raised:
            self.parse(plan, available_tools=('javascript_check',), delivery_mode='files')
        self.assertEqual(raised.exception.blocked_tools, ('python_unittest',))
        self.assertIn('隔离运行 Python', str(raised.exception))
        with self.assertRaises(PlanToolsUnavailable):
            self.parse(plan, available_tools=('python_unittest', 'javascript_check'), delivery_mode='text')
        parsed = self.parse(plan, available_tools=('python_unittest', 'javascript_check'), delivery_mode='files')
        self.assertEqual(parsed['required_tools'], plan['required_tools'])

    def test_unimplemented_tools_cannot_be_enabled_by_caller_or_omitted_silently(self):
        plan = sample_plan()
        plan['required_tools'] = ['web_search', 'image_generate']
        with self.assertRaises(PlanToolsUnavailable) as raised:
            self.parse(plan, delivery_mode='files')
        self.assertEqual(raised.exception.blocked_tools, ('web_search', 'image_generate'))
        with self.assertRaisesMessage(PlanError, '尚未实现'):
            self.parse(plan, delivery_mode='files', available_tools=('web_search',))
        plan['required_tools'] = ['shell']
        with self.assertRaisesMessage(PlanError, '未知工具'):
            self.parse(plan)
        plan['required_tools'] = ['python_unittest', 'python_unittest']
        with self.assertRaisesMessage(PlanError, '重复声明工具'):
            self.parse(plan, available_tools=('python_unittest',), delivery_mode='files')

    def test_model_cannot_supply_routing_budget_commands_or_tool_availability(self):
        for key, value in [('model', 'arbitrary-model'), ('max_calls', 999),
                           ('available_tools', ['web_search']), ('command', 'unsafe command')]:
            with self.subTest(key=key):
                plan = sample_plan()
                plan[key] = value
                with self.assertRaises(PlanError):
                    self.parse(plan)
                plan = sample_plan()
                plan['work_items'][0][key] = value
                with self.assertRaises(PlanError):
                    self.parse(plan)

    def test_malformed_json_duplicates_nonfinite_and_unicode_fail_closed(self):
        valid = json.dumps(sample_plan(), ensure_ascii=False)
        invalid = [
            valid.replace('"required_tools": []', '"required_tools": [], "required_tools": []'),
            valid.replace('"depends_on": []', '"depends_on": [], "depends_on": []', 1),
            valid.replace('"required_tools": []', '"required_tools": NaN'),
            valid.replace('"required_tools": []', '"required_tools": Infinity'),
            valid.replace('"required_tools": []', '"required_tools": -Infinity'),
            '```json\n' + valid + '\n```', valid + '\nextra', '[]', 'null', '', '\ud800',
            '[' * 1500 + ']' * 1500, valid + ' ' * MAX_PLAN_CHARS,
        ]
        for raw in invalid:
            with self.subTest(raw_prefix=repr(raw[:30])):
                with self.assertRaises(PlanError):
                    parse_plan(raw)

    def test_missing_or_vague_acceptance_criteria_rejected(self):
        for criteria in [[], ['完成任务'], ['全部通过'], ['   '], [True], ['例子\x00测试'],
                         ['交付可运行的求和文件'] * 2, ['a' * 501], ['检查输入输出的预期结果'] * 11]:
            with self.subTest(criteria=criteria):
                plan = sample_plan()
                plan['criteria'] = criteria
                with self.assertRaises(PlanError):
                    self.parse(plan)

    def test_worker_limits_specialties_identifiers_and_types_rejected(self):
        for update in [{'id': 'worker_5'}, {'id': '../worker_1'}, {'id': True},
                       {'id': 'worker_1\n'}, {'specialty': 'shell'}, {'specialty': []},
                       {'instruction': ''}, {'depends_on': 'worker_2'},
                       {'depends_on': [True]}, {'instruction': 'a' * 1201}]:
            with self.subTest(update=update):
                plan = sample_plan()
                plan['work_items'][0].update(update)
                with self.assertRaises(PlanError):
                    self.parse(plan)
        for count in [0, 1, 5]:
            plan = sample_plan()
            plan['work_items'] = [copy.deepcopy(plan['work_items'][0]) for _ in range(count)]
            with self.assertRaises(PlanError):
                self.parse(plan)
        plan = sample_plan()
        plan['work_items'][1]['id'] = 'worker_1'
        with self.assertRaisesMessage(PlanError, '重复'):
            self.parse(plan)

    def test_prompt_uses_real_availability_and_preserves_missing_capability_instruction(self):
        prompt = plan_prompt(available_tools=('python_unittest',), delivery_mode='files')
        state = json.loads(prompt.split('\n')[-1])
        available = {tool['id'] for tool in state['tools'] if tool['available']}
        self.assertEqual(available, {'python_unittest'})
        self.assertIn('不得偷偷删去工具', prompt)
        text_state = json.loads(plan_prompt(available_tools=('python_unittest',), delivery_mode='text').split('\n')[-1])
        self.assertFalse(any(tool['available'] for tool in text_state['tools']))
        with self.assertRaises(PlanError):
            plan_prompt(delivery_mode='unsupported')
        with self.assertRaises(PlanError):
            validate_plan(sample_plan(), available_tools='python_unittest')

    def test_direct_validation_rechecks_mutated_plan_before_scheduling(self):
        plan = self.parse(sample_plan())
        plan['work_items'][1]['depends_on'] = ['worker_1']
        with self.assertRaises(PlanError):
            schedule(plan)
        with self.assertRaises(PlanError):
            routing_role({'specialty': 'analysis'})
