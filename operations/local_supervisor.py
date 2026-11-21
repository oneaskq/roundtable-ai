"""Supervise the local queue worker without replaying any started work."""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .local_lock import LocalProcessLock, lock_is_held


ROOT = Path(__file__).resolve().parent.parent
HEALTH_URL = 'http://127.0.0.1:8910/health/'
SUPERVISOR_LOCK_PATH = ROOT / 'data' / 'local-supervisor.lock'
WORKER_LOCK_PATH = ROOT / 'data' / 'local-worker.lock'
POLL_SECONDS = 3.0
OFFLINE_GRACE_SECONDS = 30.0


def server_is_healthy(url=HEALTH_URL):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=1) as response:
            payload = response.read(2049)
            if response.status != 200 or len(payload) > 2048:
                return False
        body = json.loads(payload)
        return isinstance(body, dict) and body.get('application') == 'roundtable-internal'
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, AttributeError):
        return False


def detached_options():
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
               'stderr': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    else:
        options['start_new_session'] = True
    return options


def supervisor_logger(root):
    logger = logging.getLogger('roundtable.local-supervisor')
    if not logger.handlers:
        path = Path(root) / 'data' / 'local-supervisor.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=200_000, backupCount=2, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class LocalWorkerSupervisor:
    def __init__(self, *, root=ROOT, supervisor_lock=SUPERVISOR_LOCK_PATH,
                 worker_lock=WORKER_LOCK_PATH, health_url=HEALTH_URL,
                 worker_command=None, poll_seconds=POLL_SECONDS,
                 offline_grace=OFFLINE_GRACE_SECONDS, logger=None):
        self.root = Path(root)
        self.supervisor_lock = Path(supervisor_lock)
        self.worker_lock = Path(worker_lock)
        self.health_url = health_url
        self.worker_command = worker_command or [
            sys.executable, '-u', 'manage.py', 'run_team_worker', '--local-supervised']
        self.poll_seconds = poll_seconds
        self.offline_grace = offline_grace
        self.logger = logger

    def run(self):
        lock = LocalProcessLock(self.supervisor_lock)
        if not lock.acquire():
            return False
        try:
            log = self.logger or supervisor_logger(self.root)
            last_healthy = time.monotonic()
            child = None
            started_at = 0.0
            failures = 0
            retry_at = 0.0
            log.info('Local background supervision started.')
            while True:
                now = time.monotonic()
                if not server_is_healthy(self.health_url):
                    if now - last_healthy >= self.offline_grace:
                        log.info('Local web service is offline; supervision stopped.')
                        return True
                    time.sleep(self.poll_seconds)
                    continue
                last_healthy = now

                if child is not None and child.poll() is not None:
                    failures = failures + 1 if now - started_at < 30 else 1
                    retry_at = now + min(60, 2 ** min(failures, 5))
                    log.warning('Background worker exited; a replacement will be attempted without replaying started work.')
                    child = None

                if child is None and now >= retry_at:
                    try:
                        occupied = lock_is_held(self.worker_lock)
                        if not occupied:
                            child = subprocess.Popen(self.worker_command, cwd=self.root, **detached_options())
                            started_at = now
                            log.info('Background worker started.')
                    except OSError:
                        failures += 1
                        retry_at = now + min(60, 2 ** min(failures, 5))
                        log.warning('Background worker could not be started; a later attempt is scheduled.')
                time.sleep(self.poll_seconds)
        finally:
            lock.release()


if __name__ == '__main__':
    LocalWorkerSupervisor().run()
