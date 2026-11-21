"""Versioned fictional cases and bounded mechanical checks. Never calls a model."""
import hashlib
import json
import re


RUBRIC = [
    {'id': 'accuracy', 'name': '事实与约束', 'description': '事实、计算与限制是否正确。'},
    {'id': 'coverage', 'name': '任务覆盖', 'description': '是否回应了任务中的全部关键要求。'},
    {'id': 'usefulness', 'name': '清晰与可用', 'description': '回答是否清楚，下一步或理由是否可使用。'},
]

CASES = [
    {'id': 'arithmetic', 'title': '准确问答', 'role': '分析与推理', 'version': 1,
     'prompt': '计算 17 乘以 19。只输出十进制整数，不加解释或标点。',
     'review_focus': '除计算正确外，检查是否严格遵守只输出整数的约束。'},
    {'id': 'materials', 'title': '识别材料冲突', 'role': '材料研究', 'version': 1,
     'prompt': '资料 A：活动预算 12000 元，地点未确定。\n资料 B：活动预算 14000 元，地点未确定。\n'
               '两份资料均为草稿且没有更高优先级。不要猜测最终预算。只输出 JSON 对象，'
               '字段为 conflict（布尔值）、budget_values（两个整数的数组）、final_budget（整数或 null）、location（文字）。',
     'review_focus': '检查是否保留两个冲突数字，并避免把未经确认的预算当成定论。'},
    {'id': 'planning', 'title': '独立分工与依赖', 'role': '任务规划', 'version': 1,
     'prompt': '为一次 30 人读书会制定简短计划，预算上限 1500 元。先安排两项可以互不依赖的工作，'
               '再安排一项依赖这两项的汇总工作，并写出至少一条可观察的验收条件。只输出 JSON，格式为：'
               '{"independent_tasks":[{"id":"a","purpose":"工作一"},{"id":"b","purpose":"工作二"}],'
               '"follow_up_task":{"id":"c","depends_on":["a","b"],"purpose":"汇总"},'
               '"acceptance":["具体条件"]}。',
     'review_focus': '结构检查不判断工作是否真的独立、预算是否可行，需人工逐项阅读。'},
    {'id': 'critique', 'title': '质疑未经验证的结论', 'role': '交叉审查', 'version': 1,
     'prompt': '候选方案写道：“程序测试已通过，可以交付。”已知唯一依据是编写者自己声称测试通过；'
               '没有独立运行记录。请判断证据是否充分并给出下一步。只输出 JSON 对象，字段为 '
               'independent_execution_evidence（布尔值）、self_report_sufficient（布尔值）、'
               'status（“已验证”或“未验证”）和 next_step（文字）。',
     'review_focus': '检查是否明确区分自报与独立证据，下一步是否可执行。'},
    {'id': 'synthesis', 'title': '比较约束与综合方案', 'role': '综合交付', 'version': 1,
     'prompt': '会议方案 A 场地 900 元、交通 600 元；方案 B 场地 1300 元、交通 100 元。'
               '预算上限 1500 元，均满足其他要求；选总成本较低者。只输出 JSON 对象，字段为 '
               'recommendation（“A”或“B”）、total（所选方案总费用，整数）和 other_total（另一方案总费用，整数）。',
     'review_focus': '核对两种总费用、预算限制与最终选择，不能仅凭模型身份选择。'},
    {'id': 'verification', 'title': '核验交付证据', 'role': '最终核验', 'version': 1,
     'prompt': '用户要求程序通过两组独立输入输出用例。现有记录显示用例执行数为 0，'
               '只有模型声称“都通过了”。能否批准交付？只输出 JSON 对象，字段为 '
               'approved（布尔值）、executed_cases（整数）和 reason（文字）。',
     'review_focus': '不能将未运行或模型自报视为通过；理由需要说明缺失的执行证据。'},
]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def case_snapshot(case_id):
    found = next((item for item in CASES if item['id'] == case_id), None)
    if found is None:
        return None
    snapshot = {**found, 'rubric': [dict(item) for item in RUBRIC], 'scoring_version': 2}
    return {**snapshot, 'revision': hashlib.sha256(canonical(snapshot).encode('utf-8')).hexdigest()}


def available_cases():
    return [case_snapshot(item['id']) for item in CASES]


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('non-finite number')


def _json_object(output):
    try:
        value = json.loads(output, object_pairs_hook=_object, parse_constant=_invalid_constant)
        return value if isinstance(value, dict) else None
    except (ValueError, RecursionError):
        return None


def _check(name, value):
    return {'name': name, 'passed': bool(value)}


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 1200


def _independent_plan(value):
    if not isinstance(value, dict):
        return False
    items, following, acceptance = value.get('independent_tasks'), value.get('follow_up_task'), value.get('acceptance')
    if not isinstance(items, list) or len(items) != 2 or not isinstance(following, dict):
        return False
    if any(not isinstance(item, dict) or set(item) != {'id', 'purpose'}
           or not _text(item.get('id')) or not _text(item.get('purpose')) for item in items):
        return False
    identifiers = [item['id'] for item in items]
    dependencies = following.get('depends_on')
    return (len(set(identifiers)) == 2 and set(following) == {'id', 'depends_on', 'purpose'}
            and _text(following.get('id')) and following['id'] not in identifiers
            and _text(following.get('purpose')) and isinstance(dependencies, list)
            and all(isinstance(item, str) for item in dependencies)
            and len(dependencies) == 2 and set(dependencies) == set(identifiers)
            and isinstance(acceptance, list) and 1 <= len(acceptance) <= 5
            and all(_text(item) for item in acceptance))


def evaluate(case_id, output):
    """Mechanical checks only; user review remains necessary."""
    if not isinstance(output, str) or len(output) > 12000:
        raise ValueError('invalid output')
    if case_id == 'arithmetic':
        return [_check('只输出十进制整数', bool(re.fullmatch(r'[0-9]+', output.strip()))),
                _check('与标准答案完全一致', output.strip() == '323')]
    obj = _json_object(output)
    checks = [_check('输出为有效 JSON 对象', obj is not None)]
    obj = obj or {}
    if case_id == 'materials':
        budgets = obj.get('budget_values')
        checks.extend([
            _check('识别冲突与两个预算', obj.get('conflict') is True and isinstance(budgets, list)
                   and len(budgets) == 2 and all(type(x) is int for x in budgets) and sorted(budgets) == [12000, 14000]),
            _check('不臆定最终预算或地点', 'final_budget' in obj and obj['final_budget'] is None and obj.get('location') == '未确定'),
            _check('字段符合要求', set(obj) == {'conflict', 'budget_values', 'final_budget', 'location'}),
        ])
    elif case_id == 'planning':
        checks.extend([_check('分工、依赖与验收条件结构完整', set(obj) == {'independent_tasks', 'follow_up_task', 'acceptance'}
                              and _independent_plan(obj))])
    elif case_id == 'critique':
        checks.extend([
            _check('不将自报当作独立证据', obj.get('independent_execution_evidence') is False and obj.get('self_report_sufficient') is False),
            _check('状态与核验建议存在', obj.get('status') == '未验证' and _text(obj.get('next_step'))),
            _check('字段符合要求', set(obj) == {'independent_execution_evidence', 'self_report_sufficient', 'status', 'next_step'}),
        ])
    elif case_id == 'synthesis':
        checks.extend([
            _check('选择总费用较低的 B', obj.get('recommendation') == 'B'),
            _check('两种总费用正确', type(obj.get('total')) is int and obj['total'] == 1400
                   and type(obj.get('other_total')) is int and obj['other_total'] == 1500),
            _check('字段符合要求', set(obj) == {'recommendation', 'total', 'other_total'}),
        ])
    elif case_id == 'verification':
        checks.extend([
            _check('未执行用例时不批准', obj.get('approved') is False and type(obj.get('executed_cases')) is int and obj['executed_cases'] == 0),
            _check('给出原因', _text(obj.get('reason'))),
            _check('字段符合要求', set(obj) == {'approved', 'executed_cases', 'reason'}),
        ])
    else:
        raise ValueError('unknown case')
    return checks
