import json

from django.core.management.base import BaseCommand, CommandError

from operations.backups import BackupError, create_snapshot


class Command(BaseCommand):
    help = '对明确指定的 SQLite 文件创建新目录快照；不覆盖源库或已有快照，不使用默认生产库。'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--source', required=True, help='明确指定已有 SQLite 文件。')
        parser.add_argument('--output', required=True, help='明确指定尚不存在的快照目录。')

    def handle(self, *args, **options):
        try:
            result = create_snapshot(options['source'], options['output'])
        except BackupError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
