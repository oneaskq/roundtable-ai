"""Run only jobs that were explicitly authorized through the ordinary product API."""
import time

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, close_old_connections

from delivery.background import process_next_run
from operations.local_lock import LocalProcessLock
from operations.local_supervisor import OFFLINE_GRACE_SECONDS, POLL_SECONDS, server_is_healthy


class Command(BaseCommand):
    help = 'Process persistent authorized team jobs; never replay a started or interrupted run.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Process at most one queued job, then exit.')
        parser.add_argument('--poll-seconds', type=float,
                            help='Idle queue polling interval (1 second when locally supervised, otherwise 2).')
        parser.add_argument('--local-supervised', action='store_true',
                            help='Stop accepting work while the local web service is offline.')

    def handle(self, *args, **options):
        poll_seconds = options['poll_seconds']
        if poll_seconds is None:
            poll_seconds = 1.0 if options['local_supervised'] else 2.0
        if not 0.25 <= poll_seconds <= 60:
            raise CommandError('poll-seconds must be between 0.25 and 60.')
        local = getattr(settings, 'ENVIRONMENT', '') == 'development'
        if options['local_supervised'] and not local:
            raise CommandError('Local supervision is only available in local development.')
        lock = LocalProcessLock(settings.LOCAL_WORKER_LOCK_PATH) if local else None
        if lock is not None and not lock.acquire():
            raise CommandError('Another local background worker is already running.')
        try:
            return self.run_worker(poll_seconds, options)
        finally:
            if lock is not None:
                lock.release()

    def run_worker(self, poll_seconds, options):
        self.stdout.write('Background team worker ready. Only explicitly authorized queued jobs are eligible.')
        next_recovery = 0.0
        next_retention = 0.0
        next_web_check = 0.0
        last_web_ok = time.monotonic()
        web_ok = True
        try:
            while True:
                if options['local_supervised']:
                    now = time.monotonic()
                    if now >= next_web_check:
                        web_ok = server_is_healthy()
                        next_web_check = now + POLL_SECONDS
                        if web_ok:
                            last_web_ok = now
                    if not web_ok:
                        if now - last_web_ok >= OFFLINE_GRACE_SECONDS:
                            self.stdout.write('Local web service is offline; background worker stopped.')
                            return
                        time.sleep(min(poll_seconds, POLL_SECONDS))
                        continue
                close_old_connections()
                try:
                    if time.monotonic() >= next_recovery:
                        call_command('recover_tasks', stdout=self.stdout)
                        next_recovery = time.monotonic() + 30
                    if time.monotonic() >= next_retention:
                        # Only an account's explicit opt-in allows automatic deletion.
                        call_command('purge_expired', apply=True, automatic_only=True, stdout=self.stdout)
                        next_retention = time.monotonic() + 3600
                    run_id = process_next_run()
                    if run_id:
                        self.stdout.write(f'Finished processing queued run {run_id}.')
                except OperationalError:
                    # Never print database payloads or rerun a claimed job after a write failure.
                    self.stderr.write('Database temporarily unavailable; claimed jobs will not be replayed.')
                    run_id = None
                except Exception:
                    # A failed job must not kill queue supervision or expose private context.
                    self.stderr.write('Worker operation interrupted; started jobs remain recorded and will not be replayed.')
                    run_id = None
                if options['once']:
                    return
                if run_id is None:
                    time.sleep(poll_seconds)
        except KeyboardInterrupt:
            self.stdout.write('Worker stopped. Interrupted requests will not be automatically retried.')
