"""Resolve an explicit or conversational reference to a decision report on the active branch."""
import re

from workspace.models import Message
from .models import TeamRun


RECEIPTS = ('好', '好的', '好啊', '好嘞', '可以', '可以的', '行', '没问题', '收到', '收到了', '明白', '明白了',
            '了解了', '知道了', '谢谢', '谢谢你', '谢谢你们', '好的，谢谢', '收到，谢谢', 'ok', 'okay', 'thanks', 'got it', '👍')
_INVITATION = re.compile(r'[？?]|要不要|需不需要|需要我|要我帮|我(?:们)?(?:还|也)?可以(?:帮|替|为|给)'
                         r'|(?:你|您)(?:能否|是否|愿意|方便|能不能)|(?:请|可以)告诉我|如果你愿意'
                         r'|\b(?:shall I|would you|do you want|I can help)\b', re.I)


def receipt_word(text):
    return text.strip().rstrip('。.!！~～').strip().casefold()


def report_receipts(state):
    advice = (state.get('summary') or {}).get('recommendation')
    return RECEIPTS if isinstance(advice, str) and advice.strip() and not _INVITATION.search(advice) else ()


def acknowledged_task(conversation, text):
    """Bind a standalone acknowledgement to the latest finished report on this branch."""
    word = receipt_word(text)
    if word not in RECEIPTS:
        return None
    task = conversation.tasks.filter(owner_id=conversation.owner_id, superseded_by_message__isnull=True).first()
    if not task or task.status != 'completed':
        return None
    run = task.runs.first()
    if not run or run.workflow != 'decision_v1' or run.status != 'completed' or word not in report_receipts(run.decision_state):
        return None
    from workspace.sources import sources_revision
    sources = conversation.sources.filter(owner_id=conversation.owner_id)
    if (run.source_revision and run.source_revision != sources_revision(sources)) or (not run.source_revision and sources.exists()):
        return None
    last = conversation.messages.filter(role='assistant', superseded_by__isnull=True).last()
    return task if last and str(last.pk) == run.decision_state.get('result_message_id') else None


def prior_question(context):
    if (len(context) < 2 or receipt_word(context[-1]['content']) not in RECEIPTS
            or context[-1].get('role') != 'user' or context[-2].get('role') != 'assistant'):
        return ''
    text = context[-2]['content']
    if text.startswith('## 当前建议与条件\n'):
        text = text.partition('\n')[2].partition('\n\n')[0]
    return text[-2000:] if _INVITATION.search(text) else ''


def preceding_public_reply(context):
    """Return the previous visible answer, excluding attached inputs and unpublished drafts."""
    seen_user = False
    for turn in reversed(context):
        if turn['role'] == 'user':
            seen_user = True
        elif seen_user and turn['role'] == 'assistant':
            text = turn['content'].strip()
            if not text or text.startswith(('【此前团队待复核材料，', '【用户选定的参考资料，', '【此前文件成果第')):
                continue
            # Every short group reply fits. A long report has its own reference flow.
            return text if len(text) <= 8000 else text[:4000] + '\n[…]\n' + text[-4000:]
    return ''


_QUOTE = re.compile(r'^引用第 ([1-9]\d{0,5}) 份圆桌报告[：:]\n((?:>[^\n]*(?:\n|$))+)(?:\n)?([\s\S]*)$')
_FOLLOWUP = re.compile(
    r'(?:刚才|前面|上面|上次|之前|上一轮|上一份|上文|上述).{0,10}(?:建议|说的|提到|回答|结论|报告|依据|方案|第.{1,4}[条点项个])'
    r'|(?:你|你们|圆桌)(?:为什么|为何|怎么|刚才|之前|前面|给我).{0,5}(?:建议|推荐|判断|得出)'
    r'|(?:这个|那个|这条|那条|那份|这份|这项|这一|这一条|这一步)(?:结论|判断|建议|报告|依据|推荐)'
    r'|(?:为什么|为何|怎么)(?:会)?(?:这么|这样|如此)(?:说|建议|判断|推荐|认为)'
    r'|^(?:那|那么)?第[一二三四五六七八九十\d]+[条点项个].{0,8}(?:为什么|为何|怎么|如何|依据|解释|展开|呢|[?？]$)'
    r'|(?:解释|展开|细说).{0,4}第[一二三四五六七八九十\d]+[条点项]'
    r'|^(?:那|请|可以|能不能|能否)?(?:再|进一步)(?:展开|说说|解释|具体讲|细说)(?:一下|一点|讲讲|吗|呢|么|[。？?！!\s])*$'
    r'|^(?:那如果|那要是|如果(?:我的)?(?:预算|期限|时间|偏好|目标)(?:换成|改为|变成|降到|提高到|只有))'
    r'|^(?:那|所以)?(?:为什么|为何)(?:建议|推荐|选)(?:第|方案|[A-Z][？?\s]|这个|那个)'
    r'|(?:your|the previous|the last) (?:recommendation|report|conclusion|second point)', re.I)


def split_report_quote(text):
    # Forwarded interjections retain their user/target marker across rounds.
    tag = re.match(r'^(【(?:此前)?协作过程中的用户发言(?:，指定 [^】\n]+)?】\n)', text)
    prefix = tag[0] if tag else ''
    match = _QUOTE.match(text[len(prefix):])
    if not match:
        return None, '', text
    excerpt = '\n'.join(line[1:].lstrip(' ') for line in match[2].splitlines()).strip()
    return int(match[1]), excerpt, prefix + (match[3].strip() or '请解释引用的这一段。')


_REPORT_SUBJECT = (r'(?:(?:你们?|圆桌)?(?:刚才|上面|前面|报告里|上一轮)(?:的|提到的|说的)?)?'
                   r'(?:(?:这|那|该)(?:一)?(?:条|点|段|个|项|份)?|第[一二三四五六七八九十\d]+(?:条|点|项|段|个))?'
                   r'(?:建议|结论|依据|判断|方案|观点|报告|推荐|话)?')
_EXPLANATION = re.compile(
    r'^(?:请问|请|那|那么)?(?:' + _REPORT_SUBJECT + r'(?:的)?(?:'
    r'(?:对应|出自|源自|来自|引用了?|依据|参照|参考的是|根据的是|见于|是根据)?'
    r'哪(?:一)?(?:条|份|个|篇|段|页|里|儿)(?:资料|通知|原文|文件|来源|材料|规定|条款|文档)?'
    r'|(?:来源|出处|依据|原文)(?:是什么|在哪(?:里|儿|一页)?|在哪里可以看到|呢)?'
    r'|(?:为什么|为何)(?:成立|这样说|这么说|这样建议|这么建议|这样判断|这么判断)?'
    r'|是什么意思|怎么理解)'
    r'|(?:再|能否|可以)?(?:解释|说明|展开|细说)(?:一下)?(?:引用的)?' + _REPORT_SUBJECT +
    r'|有什么(?:依据|证据))'
    r'(?:吗|呢|么)?[?？。！!]*$')


def is_report_explanation(question):
    return bool(_EXPLANATION.fullmatch(question.strip()))


def same_selected_sources(previous, current):
    keys = ('reference', 'id', 'sha256', 'name', 'source_url', 'kind')
    if current is None or len(previous) != len(current):
        return False
    return all(isinstance(old, dict) and old.get('id') and old.get('sha256') and
               all(old.get(key) == new.get(key) for key in keys) for old, new in zip(previous, current))


def resolve_report(conversation, question, *, selected_sources=None):
    ordinal, excerpt, prompt = split_report_quote(question)
    if not ordinal and (re.match(r'^(?:换个话题|另外问|新话题|另一个问题|换个问题|先不谈刚才)', prompt)
                        or not (_FOLLOWUP.search(prompt) or is_report_explanation(prompt))):
        return None
    messages = Message.objects.filter(conversation=conversation, role='assistant', superseded_by__isnull=True)
    runs = TeamRun.objects.filter(task__conversation=conversation, task__owner_id=conversation.owner_id,
                                  task__superseded_by_message__isnull=True, workflow='decision_v1', status='completed')
    if ordinal or re.search(r'(?:报告|上一轮的?结论)', prompt):
        ids = [mid for mid, kind in runs.values_list('decision_state__result_message_id', 'decision_state__response_kind')
               if mid and kind not in ('quick', 'quick_group', 'direct_member')]
        messages = messages.filter(id__in=ids).order_by('created_at', 'id')
        message = messages[ordinal - 1:ordinal].first() if ordinal else messages.last()
    else:
        message = messages.order_by('-created_at', '-id').first()
    saved = (runs.filter(decision_state__result_message_id=str(message.id))
             .values('decision_state', 'source_snapshot').first() if message else None)
    state = saved['decision_state'] if saved else {}
    if state.get('response_kind') in ('quick', 'quick_group', 'direct_member') and state.get('followup_source_message_id'):
        message = messages.filter(pk=state['followup_source_message_id']).first()
        saved = (runs.filter(decision_state__result_message_id=str(message.id))
                 .values('decision_state', 'source_snapshot').first() if message else None)
        state = saved['decision_state'] if saved else {}
    if not state or state.get('response_kind') in ('quick', 'quick_group', 'direct_member'):
        return ({'user_selected_text': excerpt[:2000], 'notice': '用户引述的历史文字，无法定位对应报告，未经核实。'}
                if ordinal else None)
    # Prior IDs live only in this quote. The current round verifies its own sources and evidence.
    start = message.content.find(excerpt) if excerpt else -1
    # Rendering can remove Markdown emphasis and wrap lines. An edited quote which
    # no longer occurs in the saved report must not qualify as a simple lookup.
    selection_in_report = not excerpt or start >= 0
    if not selection_in_report:
        plain = re.compile(r'[\s*_~`]+')
        selected = plain.sub('', excerpt)
        selection_in_report = bool(selected) and selected in plain.sub('', message.content)
    report = message.content[:12000]
    if start >= 0 and start + len(excerpt) > len(report):
        report = message.content[:8000] + '\n[…]\n' + message.content[max(0, start - 600):start + len(excerpt) + 800]
    included_ids = set(re.findall(r'\bE\d+\b', excerpt + '\n' + report))
    evidence = [{key: str(item[key])[:600] for key in
                 ('id', 'type', 'status', 'name', 'excerpt', 'scope', 'expression', 'result') if key in item}
                for item in state.get('evidence', []) if item.get('id') in included_ids][:12]
    return {'message_id': str(message.id), 'report': report, 'truncated': len(message.content) > 12000,
            'same_selected_sources': same_selected_sources(saved['source_snapshot'], selected_sources),
            'selection_in_report': selection_in_report,
            'user_selected_text': excerpt[:2000], 'prior_evidence': evidence}
