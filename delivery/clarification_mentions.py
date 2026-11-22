"""One explicitly addressed reply while the decision itself stays paused for input."""
from django.db import transaction
from django.db.models import F, Max, Q
from django.http import Http404
from django.utils import timezone

from workspace.models import SourceMaterial
from workspace.sources import sources_revision
from workspace.views import lock_conversation
from .acceptance_cases import case_revision
from .connections import ConnectionError
from .decision_team_engine import _call, _decision_conversation, _text
from .models import AgentStep, RequestBudget, TeamInterjection, TeamRun
from .provider import ProviderError
from .team_engine import MAX_PACKET_CHARS, _json


def _role(item):
    return 'mention_' + item.id.hex[:22]


def room_for_mention(run, *, include_queued=False):
    members = run.decision_state.get('participants', [])
    # Keep all independent analyses, reviews, responses and the final synthesis.
    reserved = 3 * len(members) + 1
    queued = run.interjections.filter(status='mention_queued').count() if include_queued else 0
    return run.calls_used + queued + 1 + reserved <= run.max_calls


def check_mention(run, target, connections):
    from .member_address import member_connection
    from .team_views import team_summary
    state, task = run.decision_state, run.task
    if (not task.owner.is_active or task.owner_id != task.conversation.owner_id or task.superseded_by_message_id
            or task.status != 'waiting' or run.status != 'awaiting_input' or state.get('phase') != 'awaiting_input'
            or task.updated_at.isoformat() != state.get('waiting_task_updated_at')
            or not state.get('participants')):
        raise ProviderError('这轮讨论已变化，无法继续本次点名。')
    summary = team_summary(connections)
    if summary['readiness'] != 'available' or summary['revision'] != run.team_revision:
        raise ProviderError('团队配置已变化，请刷新后重新开始。')
    if (case_revision(task.conversation.acceptance_cases) != case_revision(run.acceptance_cases)
            or (run.source_revision and run.source_revision != sources_revision(
                SourceMaterial.objects.filter(owner=task.owner, conversation=task.conversation)))):
        raise ProviderError('本轮资料或验收条件已改变，无法继续本次点名。')
    return member_connection(connections, target, state['participants'])


def cancel_mentions(run):
    """Called under the conversation lock when an explicit answer or pause wins."""
    active = run.interjections.filter(status__in=['mention_queued', 'mention_running'])
    roles = [_role(item) for item in active]
    if roles:
        run.steps.filter(role__in=roles, status='running').update(status='skipped', updated_at=timezone.now())
        active.update(status='mention_cancelled')


def _locked_item(item_id, owner, conversation_id):
    lock_conversation(owner, conversation_id)
    return TeamInterjection.objects.select_related('run__task__owner', 'run__task__conversation').filter(
        pk=item_id, run__task__owner=owner, run__task__conversation_id=conversation_id).first()


def _fail(item, error):
    item.status = 'mention_failed'
    item.save(update_fields=['status'])
    item.run.steps.filter(role=_role(item), status='running').update(
        status='failed', error=error, updated_at=timezone.now())


def _claim(item):
    from .team_views import run_connections
    run = item.run
    try:
        connection = check_mention(run, item.target_provider, run_connections(run))
        packet = _reply_packet(item)
        if len(_json(packet)) > MAX_PACKET_CHARS:
            raise ProviderError('本轮讨论资料超过单次回应的长度上限。')
    except (ConnectionError, ProviderError) as error:
        _fail(item, str(error))
        return
    if (item.created_at < timezone.now() - timezone.timedelta(minutes=15) or not room_for_mention(run)):
        _fail(item, '点名已过期或可用调用次数不足。')
        return
    budget, _ = RequestBudget.objects.get_or_create(user=run.task.owner)
    now = timezone.now()
    RequestBudget.objects.filter(pk=budget.pk, period_started_at__lte=now - timezone.timedelta(hours=1)).update(
        attempts=0, period_started_at=now)
    if not RequestBudget.objects.filter(pk=budget.pk, attempts__lt=RequestBudget.HOURLY_LIMIT).update(attempts=F('attempts') + 1):
        _fail(item, '已达到每小时模型调用上限。')
        return
    position = run.steps.aggregate(last=Max('position'))['last'] or 0
    AgentStep.objects.create(run=run, position=position + 1, role=_role(item), label='回应点名',
        provider=connection.provider, model=connection.model, status='running')
    state = run.decision_state
    state['clarification_member_calls'] = state.get('clarification_member_calls', 0) + 1
    TeamRun.objects.filter(pk=run.pk).update(decision_state=state, calls_used=F('calls_used') + 1, updated_at=now)
    item.status = 'mention_running'
    item.save(update_fields=['status'])
    return connection, packet


def _reply_packet(item):
    from .team_views import quoted_member_turn
    run = item.run
    packet = {'conversation': _decision_conversation(run.input_context), 'brief': run.decision_state.get('brief_draft', {}),
              'question_to_you': item.content, 'sources': run.decision_state.get('sources', [])}
    if quoted := quoted_member_turn(item):
        packet['reply_to'] = quoted
    earlier = list(run.interjections.filter(public_order__lt=item.public_order).order_by('public_order')
                   .values_list('content', flat=True))
    if earlier:
        packet['earlier_user_interjections'] = earlier
    return packet


def _reply(item, connection, packet):
    instruction = ('用户在等待澄清时单独点名你。请用一两句话回应 question_to_you，可以说明你的看法或补问。'
                   '回复“千问呢”等点名时，要接住本次讨论的话题。不要替用户确认草稿、代替其他成员发言或宣布结论。'
                   '只输出 {"reply":"你的简短回应"}。')
    result = _call(_role(item), '回应点名', connection, packet, instruction, lambda *_: None, lambda *_: None)
    return _text(result.get('reply', ''), '点名回应', 2000, required=True)


def process_waiting_mention():
    from .team_views import run_connections
    # Visits running rows only to retire an abandoned claim; never replays it.
    stale = timezone.now() - timezone.timedelta(minutes=10)
    candidates = TeamInterjection.objects.filter(
        Q(status='mention_queued') | Q(status='mention_running', created_at__lt=stale)).order_by(
        'created_at', 'id').values_list('id', 'run_id', 'run__task__owner_id', 'run__task__conversation_id')
    for item_id, run_id, owner, conversation_id in list(candidates):
        try:
            with transaction.atomic():
                item = _locked_item(item_id, owner, conversation_id)
                if not item or item.status not in ('mention_running', 'mention_queued'):
                    continue
                if item.status == 'mention_running':
                    if not item.run.steps.filter(role=_role(item), status='running', updated_at__gt=stale).exists():
                        _fail(item, '点名回应已中断，已发送的请求不会自动重试。')
                    continue
                if item.run.interjections.filter(status='mention_running').exists():
                    continue
                claim = _claim(item)
            if claim is None:
                return run_id
            try:
                reply, failure = _reply(item, *claim), ''
            except ProviderError as error:
                reply, failure = '', str(error)
            with transaction.atomic():
                item = _locked_item(item_id, owner, conversation_id)
                if not item or item.status != 'mention_running':
                    return run_id
                try:
                    check_mention(item.run, item.target_provider, run_connections(item.run))
                except (ConnectionError, ProviderError) as error:
                    failure = str(error)
                if failure:
                    _fail(item, failure)
                else:
                    order = item.run.discussion_counter + 1
                    TeamRun.objects.filter(pk=run_id).update(discussion_counter=order, updated_at=timezone.now())
                    item.run.steps.filter(role=_role(item), status='running').update(
                        status='completed', output=reply, public_order=order, updated_at=timezone.now())
                    item.status = 'answered'
                    item.save(update_fields=['status'])
            return run_id
        except Http404:
            continue
