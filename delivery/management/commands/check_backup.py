import json

from django.core.management.base import BaseCommand, CommandError

from operations.backups import BackupError, check_snapshot


class Command(BaseCommand):
    help = '验证 SQLite 快照并在新临时文件中恢复演练；没有写回当前库的选项。'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--snapshot', required=True, help='已有 snapshot.sqlite3 与 manifest.json 的目录。')

    def handle(self, *args, **options):
        try:
            result = check_snapshot(options['snapshot'])
        except BackupError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
