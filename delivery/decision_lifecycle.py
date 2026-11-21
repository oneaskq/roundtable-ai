"""Retire a clarification when its frozen inputs can no longer be used."""
from django.utils import timezone

from .models import AgentStep, TeamRun


def invalidate_waiting_decisions(conversation, owner, reason, *, status='failed'):
    # Caller holds the conversation transaction lock. No model request is made.
    runs = TeamRun.objects.filter(task__conversation=conversation, task__owner=owner,
                                  workflow='decision_v1', status='awaiting_input')
    for run in runs:
        from .clarification_mentions import cancel_mentions
        cancel_mentions(run)
        state = dict(run.decision_state)
        state.pop('sources', None)
        state['phase'] = status
        state['stop_reason'] = reason
        state.setdefault('unresolved', []).append('问题说明未确认，独立分析尚未开始。')
        now = timezone.now()
        AgentStep.objects.filter(run=run, status='pending').update(status='skipped', updated_at=now)
        TeamRun.objects.filter(pk=run.pk, status='awaiting_input').update(
            status=status, error=reason, decision_state=state, input_context=[], finished_at=now, updated_at=now)
