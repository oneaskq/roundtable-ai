"""Local launcher. A human runs this through 打开圆桌.cmd."""
import os
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

from operations.local_lock import lock_is_held
from operations.local_supervisor import (
    SUPERVISOR_LOCK_PATH, WORKER_LOCK_PATH, detached_options, server_is_healthy,
)
from operations.version import APP_VERSION

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8910/'


def running_server_version():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL + 'health/', timeout=1) as response:
            data = json.loads(response.read(2049))
        return data.get('version') if isinstance(data, dict) and data.get('application') == 'roundtable-internal' else None
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return None


def ensure_local_supervisor(timeout=10):
    """Start one supervisor and wait until a local worker has acquired its lock."""
    process = None
    if not lock_is_held(SUPERVISOR_LOCK_PATH):
        process = subprocess.Popen([sys.executable, '-u', '-m', 'operations.local_supervisor'],
                                   cwd=ROOT, **detached_options())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        supervisor_active = lock_is_held(SUPERVISOR_LOCK_PATH)
        if supervisor_active and lock_is_held(WORKER_LOCK_PATH):
            return
        if process is not None and process.poll() is not None and not supervisor_active:
            break
        time.sleep(0.2)
    raise RuntimeError('后台处理进程未能就绪，请检查 data/local-supervisor.log 后重试。')


def wait_for_server(server, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_is_healthy() and running_server_version() == APP_VERSION:
            return True
        if server.poll() is not None:
            return False
        time.sleep(0.3)
    return False


def main():
    os.chdir(ROOT)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    subprocess.run([sys.executable, 'manage.py', 'migrate', '--noinput'], check=True)
    if server_is_healthy():
        if running_server_version() != APP_VERSION:
            raise RuntimeError('圆桌仍在使用旧版服务。请先保存其他工作，重启电脑后再打开圆桌。')
        ensure_local_supervisor()
        webbrowser.open(URL)
        print('圆桌已经启动，后台处理已就绪。')
        return 0

    print('圆桌 · 本地内部测试\n地址：' + URL + '\n后台任务可在离开页面后继续；关闭此窗口会停止本地服务。')
    server = subprocess.Popen([sys.executable, '-u', 'manage.py', 'runserver',
                               '127.0.0.1:8910', '--noreload'])
    try:
        if not wait_for_server(server):
            raise RuntimeError('圆桌网页未能启动，请检查窗口中的错误信息。')
        ensure_local_supervisor()
        webbrowser.open(URL)
        return server.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
