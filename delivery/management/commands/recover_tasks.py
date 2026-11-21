from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from workspace.models import Task, TaskEvent
from workspace.views import lock_conversation
from delivery.background import expire_queued_runs
from delivery.models import AgentStep, TeamRun


class Command(BaseCommand):
    help = 'Mark old interrupted model requests failed, without retrying paid requests.'

    def handle(self, *args, **options):
        cutoff = timezone.now() - timezone.timedelta(minutes=5)
        # Queued work has not sent a request and has a separate 15-minute consent deadline.
        queued = TeamRun.objects.filter(background=True, status='queued').values_list('execution_id', flat=True)
        tasks = Task.objects.filter(status='running', updated_at__lt=cutoff).exclude(execution_id__in=queued)
        count = 0
        for task in tasks:
            with transaction.atomic():
                lock_conversation(task.owner, task.conversation_id)
                # A live worker refreshes the run without changing the task's
                # consent revision. The shared lock makes this check atomic.
                if TeamRun.objects.filter(task=task, execution_id=task.execution_id,
                                          status='running', updated_at__gte=cutoff).exists():
                    continue
                if Task.objects.filter(id=task.id, status='running', execution_id=task.execution_id,
                                       updated_at__lt=cutoff).update(status='failed', execution_id=None, updated_at=timezone.now()):
                    TaskEvent.objects.create(task=task, kind='interrupted', content='上次处理被中断；未自动重试，原请求可能已计费。')
                    runs = TeamRun.objects.filter(task=task, execution_id=task.execution_id, status='running')
                    for run in runs.filter(workflow='decision_v1'):
                        state = dict(run.decision_state)
                        state.pop('sources', None)
                        state['phase'] = 'failed'
                        state['stop_reason'] = '处理被中断，未自动重试；原请求可能已计费。'
                        state.setdefault('unresolved', []).append('本轮没有完成全部核验与回应。')
                        TeamRun.objects.filter(pk=run.pk).update(decision_state=state)
                    AgentStep.objects.filter(run__in=runs, status='running').update(
                        status='failed', error='处理被中断，未自动重试。', updated_at=timezone.now())
                    AgentStep.objects.filter(run__in=runs, status='pending').update(status='skipped', updated_at=timezone.now())
                    runs.update(status='failed', error='处理被中断，未自动重试；原请求可能已计费。',
                                input_context=[], finished_at=timezone.now(), updated_at=timezone.now())
                    count += 1
        self.stdout.write(f'Recovered {count} interrupted tasks without sending model requests.')
        expired = expire_queued_runs()
        if expired:
            self.stdout.write(f'Expired {expired} queued tasks without sending model requests.')
