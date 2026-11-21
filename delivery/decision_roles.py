"""Question-specific, complementary assignments for independent analyses."""
import re


# The clarifier selects a lens. Only these server-authored responsibilities
# enter the system instruction; free text from a model never does.
LENSES = {
    'criteria': ('目标与取舍', '梳理用户的判断标准，比较不同优先级会怎样改变建议。'),
    'feasibility': ('落地条件', '判断时间、资源和必要条件是否允许实施，说明可行的下一步。'),
    'evidence': ('事实与依据', '区分有依据的事实与待验证的假设，指出哪些证据会改变判断。'),
    'concepts': ('概念辨析', '厘清核心概念、前提和适用边界，考虑不同解释分别意味着什么。'),
    'people': ('各方影响', '考虑相关各方的处境，说明谁会受益、承担代价，以及哪些偏好需要本人决定。'),
    'alternatives': ('备选路径', '探索其他可行选择、折中办法及其成立条件。'),
    'growth': ('成长机会', '比较学习、反馈和长期发展机会，说明判断取决于哪些具体条件。'),
    'resources': ('成本与投入', '比较直接成本、时间与机会成本，区分已知投入和估计。'),
    'dependencies': ('技术依赖', '检查技术选项的依赖、集成条件和维护工作，说明如何验证关键假设。'),
}

_TOPICS = (
    (r'实习|岗位|求职|职业|跳槽|录用|\b(?:internship|career|job|offer)\b', ('growth', 'criteria', 'feasibility')),
    (r'哲学|概念|价值观|本质|如何理解|公平|道德|\b(?:concept|philosophy|ethics)\b', ('concepts', 'people', 'evidence')),
    (r'代码|架构|软件|数据库|部署|开发|编程|\b(?:software|database|architecture|code)\b', ('dependencies', 'feasibility', 'alternatives')),
    (r'预算|价格|费用|报价|采购|成本|\b(?:budget|price|cost)\b', ('resources', 'criteria', 'evidence')),
)


def assignment_prompt():
    choices = '、'.join(f'{key}（{label}）' for key, (label, _) in LENSES.items())
    return (' discussion 时同时为 member_ids 中每个成员分配不同且与问题相关的分析角度，不指定立场。'
            '增加 assignments 数组，每项格式为 {"member":"成员ID","angle":"角度代码","focus":"用户问题中的一小段原文"}。'
            'focus 是可选的分析主题，最多32字，不写答案或建议。可选角度为' + choices + '。')


def assign_responsibilities(members, question, proposals=None):
    by_member = {}
    used = set()
    ids = {member['id'] for member in members}
    if isinstance(proposals, list) and len(proposals) == len(members):
        for item in proposals:
            if not isinstance(item, dict):
                break
            member, angle = item.get('member'), item.get('angle')
            if not (isinstance(member, str) and member in ids and member not in by_member
                    and isinstance(angle, str) and angle in LENSES and angle not in used):
                break
            focus = item.get('focus', '')
            if not (isinstance(focus, str) and 0 < len(focus) <= 32 and focus in question and focus.isprintable()):
                focus = ''
            by_member[member] = (angle, focus.strip())
            used.add(angle)
    if len(by_member) != len(members):
        fallback = next((angles for pattern, angles in _TOPICS if re.search(pattern, question, re.I)),
                        ('criteria', 'evidence', 'alternatives'))
        by_member = {member['id']: (angle, '') for member, angle in zip(members, fallback)}

    for member in members:
        angle, focus = by_member[member['id']]
        label, task = LENSES[angle]
        member['assignment'] = {'angle': angle, 'label': label, 'task': task, 'focus': focus}


def analysis_input(member, packet, instruction):
    assignment = member.get('assignment')
    if not assignment:
        return packet, instruction
    label, task = LENSES[assignment['angle']]
    instruction += (f'\n你的本轮职责是「{label}」。{task} '
                    '职责只限定观察角度，不指定结论；仍须考虑完整问题，可以与其他人得出相同判断。')
    if assignment['focus']:
        packet = {**packet, 'assigned_focus_from_user': assignment['focus']}
    return packet, instruction
