"""Local process supervision with fake workers and isolated lock files."""
import logging
from pathlib import Path
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

import launch
from operations.local_lock import LocalProcessLock, lock_is_held
from operations.local_supervisor import LocalWorkerSupervisor


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FAKE_WORKER = '''
import sys
import time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from operations.local_lock import LocalProcessLock
lock = LocalProcessLock(sys.argv[2])
if not lock.acquire():
    raise SystemExit(3)
try:
    with Path(sys.argv[3]).open('a', encoding='utf-8') as stream:
        stream.write('started\\n')
    stop = Path(sys.argv[4])
    while not stop.exists():
        time.sleep(0.05)
    stop.unlink(missing_ok=True)
finally:
    lock.release()
'''


class LocalLauncherTests(SimpleTestCase):
    @patch('launch.webbrowser.open')
    @patch('launch.ensure_local_supervisor')
    @patch('launch.running_server_version', return_value=launch.APP_VERSION)
    @patch('launch.server_is_healthy', return_value=True)
    @patch('launch.subprocess.run')
    def test_reopening_existing_web_service_restores_supervision(self, _migrate, _healthy, _version, ensure, browser):
        self.assertEqual(launch.main(), 0)
        ensure.assert_called_once_with()
        browser.assert_called_once_with(launch.URL)

    @patch('launch.ensure_local_supervisor')
    @patch('launch.running_server_version', return_value='0.13.1-internal')
    @patch('launch.server_is_healthy', return_value=True)
    @patch('launch.subprocess.run')
    def test_old_running_web_service_is_reported_instead_of_mistaken_for_the_update(
            self, _migrate, _healthy, _version, ensure):
        with self.assertRaisesMessage(RuntimeError, '仍在使用旧版服务'):
            launch.main()
        ensure.assert_not_called()

    @patch('launch.webbrowser.open')
    @patch('launch.ensure_local_supervisor')
    @patch('launch.wait_for_server', return_value=True)
    @patch('launch.server_is_healthy', return_value=False)
    @patch('launch.subprocess.Popen')
    @patch('launch.subprocess.run')
    def test_fresh_start_brings_up_supervision_after_web_service(
            self, _migrate, popen, _healthy, wait, ensure, browser):
        server = popen.return_value
        server.wait.return_value = 0
        server.poll.return_value = 0
        self.assertEqual(launch.main(), 0)
        wait.assert_called_once_with(server)
        ensure.assert_called_once_with()
        browser.assert_called_once_with(launch.URL)

    def test_second_local_worker_is_rejected(self):
        lock = LocalProcessLock(settings.LOCAL_WORKER_LOCK_PATH)
        self.assertTrue(lock.acquire())
        try:
            with self.assertRaisesMessage(CommandError, 'Another local background worker'):
                call_command('run_team_worker', once=True)
        finally:
            lock.release()

    @patch('delivery.management.commands.run_team_worker.process_next_run')
    @patch('delivery.management.commands.run_team_worker.server_is_healthy', return_value=False)
    @patch('delivery.management.commands.run_team_worker.time')
    def test_supervised_worker_takes_no_queued_work_while_web_is_offline(self, clock, _health, process):
        clock.monotonic.side_effect = [0, 0, 31]
        call_command('run_team_worker', once=True, local_supervised=True)
        process.assert_not_called()


class LocalSupervisorProcessTests(SimpleTestCase):
    def test_worker_exit_is_replaced_once_and_duplicate_supervisor_is_rejected(self):
        state = {'healthy': True}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if state['healthy'] else 503)
                self.end_headers()
                self.wfile.write(b'{"application":"roundtable-internal"}')

            def log_message(self, *_args):
                pass

        web = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        web_thread = threading.Thread(target=web.serve_forever, daemon=True)
        web_thread.start()
        self.addCleanup(web.server_close)
        self.addCleanup(web.shutdown)

        with tempfile.TemporaryDirectory(prefix='local-supervisor-') as folder:
            folder = Path(folder)
            worker_lock = folder / 'worker.lock'
            supervisor_lock = folder / 'supervisor.lock'
            starts = folder / 'starts.txt'
            stop = folder / 'stop.txt'
            fake = folder / 'fake_worker.py'
            fake.write_text(FAKE_WORKER, encoding='utf-8')
            logger = Mock(spec=logging.Logger)
            kwargs = dict(root=PROJECT_ROOT, supervisor_lock=supervisor_lock, worker_lock=worker_lock,
                          health_url=f'http://127.0.0.1:{web.server_port}/health/',
                          worker_command=[sys._base_executable, '-u', str(fake), str(PROJECT_ROOT),
                                          str(worker_lock), str(starts), str(stop)],
                          poll_seconds=0.1, offline_grace=0.5, logger=logger)
            supervisor = LocalWorkerSupervisor(**kwargs)
            thread = threading.Thread(target=supervisor.run, daemon=True)

            def wait_until(predicate, timeout=8):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if predicate():
                        return True
                    time.sleep(0.05)
                return False

            def count_starts():
                return len(starts.read_text(encoding='utf-8').splitlines()) if starts.exists() else 0

            thread.start()
            try:
                self.assertTrue(wait_until(lambda: count_starts() == 1 and lock_is_held(worker_lock)))
                self.assertFalse(LocalWorkerSupervisor(**kwargs).run())
                time.sleep(0.4)
                self.assertEqual(count_starts(), 1)
                stop.write_text('stop', encoding='utf-8')
                self.assertTrue(wait_until(lambda: count_starts() == 2 and lock_is_held(worker_lock)))
                time.sleep(0.4)
                self.assertEqual(count_starts(), 2)
            finally:
                state['healthy'] = False
                thread.join(timeout=4)
                stop.write_text('stop', encoding='utf-8')
                stopped = wait_until(lambda: not lock_is_held(worker_lock))
            self.assertFalse(thread.is_alive())
            self.assertTrue(stopped)
