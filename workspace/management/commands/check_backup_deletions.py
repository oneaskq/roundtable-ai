import json

from django.core.management.base import BaseCommand, CommandError

from operations.backups import BackupError
from operations.deletion_backups import inspect_snapshot


class Command(BaseCommand):
    help = 'Read-only check for known deleted accounts in one explicit SQLite snapshot.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--snapshot', required=True)
        parser.add_argument('--ledger', help='Separately exported signed deletion ledger, if the live DB was restored.')

    def handle(self, *args, **options):
        try:
            result = inspect_snapshot(options['snapshot'], ledger_file=options.get('ledger'))
        except BackupError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(json.dumps(result, ensure_ascii=False))
