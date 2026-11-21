import json

from django.core.management.base import BaseCommand, CommandError

from operations.backups import BackupError
from operations.deletion_backups import export_ledger


class Command(BaseCommand):
    help = 'Export a separately signed pseudonymous deletion ledger without overwriting files.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True)

    def handle(self, *args, **options):
        try:
            result = export_ledger(options['output'])
        except BackupError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(json.dumps(result, ensure_ascii=False))
