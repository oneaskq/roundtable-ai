from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F
from django.http import Http404
from django.utils import timezone

from delivery.models import TeamRun
from workspace.deletion import record_deletion
from workspace.models import Conversation, DataPolicy, Task
from workspace.views import lock_conversation


class Command(BaseCommand):
    help = '预览或清理过期对话；自动模式只处理明确选择自动清理的账号。'

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--dry-run', action='store_true', help='只预览（默认）。')
        mode.add_argument('--apply', action='store_true', help='实际清理符合条件的对话。')
        parser.add_argument('--user-email', help='只处理这个邮箱的用户。')
        parser.add_argument('--automatic-only', action='store_true', help='只处理已明确开启自动清理的账号。')

    def handle(self, *args, **options):
        policies = DataPolicy.objects.exclude(retention_days=0).select_related('user')
        if options['automatic_only']:
            policies = policies.filter(auto_cleanup=True)
        if options['user_email']:
            policies = policies.filter(user__email__iexact=options['user_email'])
        count = 0
        for policy in policies:
            cutoff = timezone.now() - timedelta(days=policy.retention_days)
            active = Task.objects.filter(owner=policy.user, status__in=['draft', 'waiting', 'running', 'paused']).values('conversation_id')
            candidates = Conversation.objects.filter(owner=policy.user, updated_at__lt=cutoff).exclude(id__in=active)
            if not options['apply']:
                count += candidates.count()
                continue
            # Limit each pass; subsequent worker checks can safely continue.
            for conversation_id in list(candidates.values_list('id', flat=True)[:200]):
                with transaction.atomic():
                    User = get_user_model()
                    if not User.objects.filter(pk=policy.user_id, is_active=True).update(is_active=F('is_active')):
                        continue
                    current = DataPolicy.objects.filter(user_id=policy.user_id).first()
                    if not current or not current.retention_days or (options['automatic_only'] and not current.auto_cleanup):
                        continue
                    try:
                        conversation = lock_conversation(policy.user_id, conversation_id)
                    except Http404:
                        continue
                    current_cutoff = timezone.now() - timedelta(days=current.retention_days)
                    if conversation.updated_at >= current_cutoff or Task.objects.filter(
                            conversation=conversation, status__in=['draft', 'waiting', 'running', 'paused']).exists() or \
                            TeamRun.objects.filter(task__conversation=conversation, status__in=['queued', 'running']).exists():
                        continue
                    record_deletion('conversation', conversation.id)
                    conversation.delete()
                    count += 1
        mode = '已清理' if options['apply'] else '预览：符合清理条件'
        self.stdout.write(f'{mode} {count} 个对话。项目、记忆和账号不受影响。')
        if not options['apply']:
            self.stdout.write('未修改数据。历史备份、导出副本及外部服务需分别处理。')
