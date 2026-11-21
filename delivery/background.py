"""Persistent, at-most-once claiming. A started run is never automatically replayed."""
import threading
from contextlib import contextmanager

from django.db import OperationalError, close_old_connections, transaction
from django.http import Http404
from django.utils import timezone

from workspace.models import Task, TaskEvent
from workspace.views import lock_conversation
from .models import AgentStep, TeamRun


QUEUE_MAX_AGE = timezone.timedelta(minutes=15)
HEARTBEAT_SECONDS = 20


def _resuming_decision(run):
    return (run.workflow == 'decision_v1' and run.calls_used == (
                run.decision_state.get('clarification_calls', 1) + run.decision_state.get('clarification_member_calls', 0))
            and run.decision_state.get('phase') == 'confirmed'
            and bool(run.decision_state.get('brief')))


def _queue_started_at(run):
    # A confirmed clarification has a fresh queue window even if the owner
    # took a long time to answer the questions.
    return run.updated_at if _resuming_decision(run) else run.created_at


@contextmanager
def execution_heartbeat(run):
    """Keep a live run distinguishable from a crashed worker while a call waits."""
    stopped = threading.Event()
    run_id, execution_id = run.pk, run.execution_id
    owner_id, conversation_id = run.task.owner_id, run.task.conversation_id

    def beat():
        try:
            while not stopped.wait(HEARTBEAT_SECONDS):
                close_old_connections()
                try:
                    with transaction.atomic():
                        # Share the recovery lock so a fresh heartbeat and a
                        # stale-run decision cannot pass one another.
                        lock_conversation(owner_id, conversation_id)
                        if stopped.is_set() or not TeamRun.objects.filter(
                                pk=run_id, execution_id=execution_id, status='running').update(updated_at=timezone.now()):
                            return
                except Http404:
                    return
                except OperationalError:
                    # A transient database outage is retried; never call a provider here.
                    continue
        finally:
            close_old_connections()

    thread = threading.Thread(target=beat, name='roundtable-run-heartbeat', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=2)


def stop_queued(run, reason):
    """Caller holds the conversation transaction lock."""
    now = timezone.now()
    changed = TeamRun.objects.filter(pk=run.pk, status='queued').update(
        status='failed', error=reason, input_context=[], finished_at=now, updated_at=now)
    if changed:
        if run.workflow == 'decision_v1':
            state = dict(run.decision_state)
            state.pop('sources', None)
            state['phase'] = 'failed'
            state['stop_reason'] = reason
            state.setdefault('unresolved', []).append('后续讨论未完成。')
            TeamRun.objects.filter(pk=run.pk).update(decision_state=state)
        Task.objects.filter(pk=run.task_id, status='running', execution_id=run.execution_id).update(
            status='failed', execution_id=None, updated_at=now)
        AgentStep.objects.filter(run=run, status='pending').update(status='skipped', updated_at=now)
        TaskEvent.objects.create(task=run.task, kind='error', content=reason)
    return bool(changed)


def expire_queued_runs(*, conversation_id=None):
    count = 0
    queued = TeamRun.objects.filter(background=True, status='queued', created_at__lt=timezone.now() - QUEUE_MAX_AGE)
    if conversation_id is not None:
        queued = queued.filter(task__conversation_id=conversation_id)
    ids = list(queued.values_list('id', flat=True))
    for run_id in ids:
        with transaction.atomic():
            run = TeamRun.objects.select_related('task__owner', 'task__conversation').filter(pk=run_id).first()
            if not run:
                continue
            try:
                lock_conversation(run.task.owner, run.task.conversation_id)
            except Http404:
                continue
            run.refresh_from_db()
            if run.status == 'queued' and _queue_started_at(run) < timezone.now() - QUEUE_MAX_AGE:
                message = ('确认后排队超过15分钟，后续步骤未调用；此前问题整理可能已计费。'
                           if _resuming_decision(run) else '排队超过15分钟，本轮已停止且未调用模型；请重新确认后开始。')
                count += stop_queued(run, message)
    return count


def claim_next_run():
    """Claim an unstarted run using the same lock as pause, edits and submission."""
    ids = list(TeamRun.objects.filter(background=True, status='queued').order_by('created_at', 'id')
               .values_list('id', flat=True)[:100])
    for run_id in ids:
        with transaction.atomic():
            run = TeamRun.objects.select_related('task__owner', 'task__conversation').filter(pk=run_id).first()
            if not run:
                continue
            try:
                lock_conversation(run.task.owner, run.task.conversation_id)
            except Http404:
                stop_queued(run, '会话归属已改变，后台任务已停止且未调用模型。')
                continue
            run.refresh_from_db()
            if run.status != 'queued':
                continue
            resumed = _resuming_decision(run)
            if _queue_started_at(run) < timezone.now() - QUEUE_MAX_AGE:
                stop_queued(run, '确认后排队超过15分钟，后续步骤未调用；此前问题整理可能已计费。'
                            if resumed else '排队超过15分钟，本轮已停止且未调用模型；请重新确认后开始。')
                continue
            if not run.task.owner.is_active or not Task.objects.filter(
                    pk=run.task_id, owner=run.task.owner, conversation__owner=run.task.owner,
                    status='running', execution_id=run.execution_id).exists():
                stop_queued(run, '账号或任务状态已改变，后台任务已停止且未调用模型。')
                continue
            now = timezone.now()
            if resumed and not AgentStep.objects.filter(run=run, role='clarifier', status='completed').exists():
                stop_queued(run, '问题整理记录不完整，后续步骤未自动开始。')
                continue
            if not TeamRun.objects.filter(pk=run.pk, status='queued', started_at__isnull=True,
                                          calls_used=run.calls_used if resumed else 0).update(status='running', started_at=now, updated_at=now):
                stop_queued(run, '任务已有执行痕迹，未自动重试；请检查工作记录。')
                continue
            Task.objects.filter(pk=run.task_id, execution_id=run.execution_id).update(updated_at=now)
            TaskEvent.objects.create(task=run.task, kind='team_started',
                content='后台团队开始处理。可以离开页面，回来后查看进度与成果。')
            run.refresh_from_db()
            return run
    return None


def process_next_run():
    """No retries here: a process crash leaves its started run for explicit recovery."""
    from .team_views import execute_team_run
    run = claim_next_run()
    if run is None:
        from .clarification_mentions import process_waiting_mention
        return process_waiting_mention()
    execute_team_run(run)
    return run.id
