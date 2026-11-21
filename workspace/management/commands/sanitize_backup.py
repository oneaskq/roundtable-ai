import json

from django.core.management.base import BaseCommand, CommandError

from operations.backups import BackupError
from operations.deletion_backups import sanitized_snapshot


class Command(BaseCommand):
    help = '为本地 SQLite 快照生成应用删除记录的新副本；不覆盖原备份或运行中的数据库。'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--snapshot', required=True, help='原 SQLite 文件或快照目录。')
        parser.add_argument('--output', required=True, help='必须不存在的新输出目录。')
        parser.add_argument('--ledger', help='可选：最近导出的签名删除清单。')

    def handle(self, *args, **options):
        try:
            result = sanitized_snapshot(options['snapshot'], options['output'], ledger_file=options.get('ledger'))
        except BackupError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(json.dumps(result, ensure_ascii=False))
