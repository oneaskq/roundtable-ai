"""Opt-in, bounded execution of text bundles inside local Linux Docker containers.

No generated source is imported or executed by the web server. No host directory,
credential, socket, port, or network is exposed to a container. Inputs travel via
stdin and exist only in a size-limited container tmpfs. Images must already exist
locally and use operator-configured, immutable official-repository digest refs.

This is an execution adapter, not a claim that Docker is a complete hostile-code
security boundary or that generated tests independently prove user requirements.
Production deployment still needs a reviewed, dedicated execution environment.

Docker option references (checked 2026-10-26):
https://docs.docker.com/reference/cli/docker/container/create/
https://docs.docker.com/reference/cli/docker/container/start/
https://docs.docker.com/reference/cli/docker/container/run/
"""
from dataclasses import dataclass
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

from django.conf import settings

from .bundles import BundleError, validate_bundle


TOOLS = ('python_unittest', 'javascript_check', 'python_stdio')
TOOL_LABELS = {'python_unittest': 'Python 隔离测试', 'javascript_check': 'JavaScript 隔离语法检查',
               'python_stdio': 'Python 输入输出验收'}
POLICY_VERSION = 'docker-local-v3'
TIMEOUT_SECONDS = 15
CONTROL_TIMEOUT_SECONDS = 5
OUTPUT_LIMIT = 32768
CONTROL_OUTPUT_LIMIT = 65536
SCOPE = ('仅在已配置的本机 Linux Docker 中运行明确列出的检查。Python 使用成果附带的 unittest，'
         '其输出和测试数量是不可信自报，不能据此通过独立功能验收；不安装依赖。JavaScript 仅做语法检查，'
         '不执行脚本、不渲染页面。python_stdio 由宿主比对调用方独立提供的输入输出用例，'
         '只证明这些用例的结果。没有网络、主机目录或服务密钥访问权限。')
_IMAGE_PATTERNS = {
    'python_unittest': re.compile(r'python@sha256:[a-f0-9]{64}\Z'),
    'javascript_check': re.compile(r'node@sha256:[a-f0-9]{64}\Z'),
    'python_stdio': re.compile(r'python@sha256:[a-f0-9]{64}\Z'),
}

# These launchers are trusted application source. They only run inside Docker.
# The nonce disambiguates accidental output; it is not a security attestation.
_PYTHON_RUNNER = r'''
import json, os, pathlib, sys, unittest
payload = json.loads(sys.stdin.buffer.read(700001))
root = pathlib.Path('/workspace')
for item in payload['files']:
    name = item['path']
    parts = name.split('/')
    if not parts or any(p in ('', '.', '..') or p.startswith('.') for p in parts) or '\\' in name or ':' in name:
        raise ValueError('invalid path')
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root):
        raise ValueError('invalid destination')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as stream:
        stream.write(item['content'])
os.chdir(root)
sys.path.insert(0, str(root))
suite = unittest.defaultTestLoader.discover(str(root), pattern='test_*.py')
count = suite.countTestCases()
if count == 0:
    result = {'passed': False, 'tests_run': 0, 'skipped': 0, 'detail': 'no_tests'}
else:
    checked = unittest.TextTestRunner(verbosity=1).run(suite)
    result = {'passed': checked.wasSuccessful() and checked.testsRun > len(checked.skipped),
              'tests_run': checked.testsRun, 'skipped': len(checked.skipped), 'detail': 'unittest'}
print('\x1eROUNDTABLE_RESULT:' + payload['nonce'] + ':' + json.dumps(result), flush=True)
sys.exit(0 if result['passed'] else 1)
'''

_JAVASCRIPT_RUNNER = r'''
const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const raw = fs.readFileSync(0);
if (raw.length > 700000) throw new Error('input too large');
const payload = JSON.parse(raw.toString('utf8'));
const root = '/workspace';
for (const item of payload.files) {
  const parts = item.path.split('/');
  if (parts.some(p => !p || p === '.' || p === '..' || p.startsWith('.')) || item.path.includes('\\') || item.path.includes(':')) throw new Error('invalid path');
  const target = path.join(root, ...parts);
  if (!target.startsWith(root + '/')) throw new Error('invalid destination');
  fs.mkdirSync(path.dirname(target), {recursive: true});
  fs.writeFileSync(target, item.content, {encoding: 'utf8', flag: 'wx'});
}
const files = payload.files.filter(item => item.path.toLowerCase().endsWith('.js'));
let passed = files.length > 0;
let checksRun = 0;
for (const item of files) {
  const result = cp.spawnSync(process.execPath, ['--check', path.join(root, item.path)],
    {timeout: 5000, maxBuffer: 8192, encoding: 'utf8', env: {PATH: '/usr/local/bin:/usr/bin:/bin'}});
  checksRun += 1;
  if (result.stdout) process.stdout.write(result.stdout.slice(0, 8192));
  if (result.stderr) process.stderr.write(result.stderr.slice(0, 8192));
  if (result.error || result.status !== 0 || result.signal) passed = false;
}
console.log('\x1eROUNDTABLE_RESULT:' + payload.nonce + ':' + JSON.stringify({passed, checks_run: checksRun}));
process.exit(passed ? 0 : 1);
'''

# Expected outputs NEVER enter this launcher, its arguments, environment or
# stdin. A child process gets the actual input through an ordinary stdin pipe.
_PYTHON_STDIO_RUNNER = r'''
import json, pathlib, subprocess, sys
payload = json.loads(sys.stdin.buffer.read(700001))
root = pathlib.Path('/workspace')
for item in payload['files']:
    name = item['path']
    parts = name.split('/')
    if not parts or any(p in ('', '.', '..') or p.startswith('.') for p in parts) or '\\' in name or ':' in name:
        raise ValueError('invalid path')
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root):
        raise ValueError('invalid destination')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as stream:
        stream.write(item['content'])
result = subprocess.run([sys.executable, '-I', '-u', '/workspace/main.py'],
    input=payload['stdin'].encode('utf-8'), cwd='/workspace',
    env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}, timeout=12)
sys.exit(result.returncode)
'''


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: bytes = b''
    timed_out: bool = False
    output_limited: bool = False
    stderr: bytes = b''


def execution_policy():
    """Return a stable, public policy description without probing the daemon."""
    configured = getattr(settings, 'EXECUTION_IMAGES', {})
    images = {tool: (configured.get(tool, '') if isinstance(configured, dict) else None)
              for tool in TOOLS}
    # Invalid configuration cannot accidentally become enabled or serializable
    # arbitrary objects. Its invalidity remains represented in the fingerprint.
    images = {tool: value if isinstance(value, str) else '<invalid>' for tool, value in images.items()}
    enabled = getattr(settings, 'EXECUTION_ENABLED', False)
    return {'version': POLICY_VERSION, 'enabled': enabled is True,
            'configuration_valid': isinstance(enabled, bool) and isinstance(configured, dict),
            'images': images, 'timeout_seconds': TIMEOUT_SECONDS, 'memory_mib': 128,
            'cpus': '0.5', 'pids_limit': 32, 'output_bytes': OUTPUT_LIMIT,
            'network': 'none', 'host_mounts': False, 'root_read_only': True,
            'local_daemon_only': True, 'image_pull': 'never',
            'stdio_case_budget_seconds': 30, 'stdio_max_cases': 5}


def policy_fingerprint():
    """Bind caller consent to configuration, image digests and fixed limits."""
    raw = json.dumps(execution_policy(), sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _environment():
    # Never forward model keys, application settings, remote Docker contexts, or
    # image-pull authentication environment into the Docker CLI.
    allowed = ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP')
    return {name: os.environ[name] for name in allowed if name in os.environ}


def _invoke(arguments, *, input_bytes=None, timeout=CONTROL_TIMEOUT_SECONDS, limit=CONTROL_OUTPUT_LIMIT,
            merge_stderr=True):
    """Drain output continuously with a memory bound; shell is never involved."""
    kwargs = {'stdin': subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
              'stdout': subprocess.PIPE, 'stderr': subprocess.STDOUT if merge_stderr else subprocess.PIPE,
              'shell': False, 'env': _environment()}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        process = subprocess.Popen(arguments, **kwargs)
    except OSError:
        return CommandResult(-1)
    chunks, error_chunks = bytearray(), bytearray()
    limited = threading.Event()
    buffer_lock = threading.Lock()

    def read_output(stream, destination):
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                with buffer_lock:
                    remaining = max(0, limit - len(chunks) - len(error_chunks))
                    destination.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    limited.set()
                    process.kill()
                    break
        except (OSError, ValueError):
            pass

    def write_input():
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=read_output, args=(process.stdout, chunks), daemon=True)
    reader.start()
    error_reader = None
    if not merge_stderr:
        error_reader = threading.Thread(target=read_output, args=(process.stderr, error_chunks), daemon=True)
        error_reader.start()
    writer = None
    if input_bytes is not None:
        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    reader.join(timeout=2)
    if error_reader:
        error_reader.join(timeout=2)
    if writer:
        writer.join(timeout=2)
    if process.stdout:
        process.stdout.close()
    if process.stderr:
        process.stderr.close()
    return CommandResult(process.returncode if process.returncode is not None else -1,
                         bytes(chunks), timed_out, limited.is_set(), bytes(error_chunks))


def _docker_prefix():
    binary = shutil.which('docker')
    if not binary:
        return None
    endpoint = 'npipe:////./pipe/docker_engine' if os.name == 'nt' else 'unix:///var/run/docker.sock'
    return [binary, '--host', endpoint]


def _json_result(result):
    if result.returncode != 0 or result.timed_out or result.output_limited:
        return None
    try:
        value = json.loads(result.output)
    except (ValueError, UnicodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def readiness():
    """Read local daemon/image metadata only; never pull or start containers."""
    policy = execution_policy()
    checks = []
    available = []

    def finish():
        return {'available_tools': available, 'checks': checks,
                'policy_fingerprint': policy_fingerprint(), 'scope': SCOPE}

    def add(tool, status, detail):
        checks.append({'path': '', 'tool': tool, 'check': TOOL_LABELS.get(tool, '隔离环境'),
                       'status': status, 'detail': detail})

    if not policy['configuration_valid'] or not policy['enabled']:
        add('runtime', 'not_run', '隔离执行尚未启用；没有运行生成代码。')
        return finish()
    configured = [tool for tool in TOOLS if _IMAGE_PATTERNS[tool].fullmatch(policy['images'][tool])]
    for tool in TOOLS:
        if tool not in configured:
            add(tool, 'not_run', '尚未配置允许的不可变镜像摘要；不会自动下载镜像。')
    if not configured:
        return finish()
    docker = _docker_prefix()
    if docker is None:
        add('runtime', 'not_run', '没有找到 Docker；没有运行生成代码。')
        return finish()
    info = _json_result(_invoke(docker + ['info', '--format', '{{json .}}']))
    if not info or info.get('OSType') != 'linux':
        add('runtime', 'not_run', '本机 Linux Docker 服务不可用；没有运行生成代码。')
        return finish()
    if not all(info.get(key) is True for key in ('MemoryLimit', 'SwapLimit', 'CpuCfsQuota', 'PidsLimit')):
        add('runtime', 'not_run', 'Docker 未确认支持所需资源限制；没有运行生成代码。')
        return finish()
    security = info.get('SecurityOptions')
    if not isinstance(security, list) or not any(item == 'name=seccomp,profile=builtin'
                                                for item in security):
        add('runtime', 'not_run', 'Docker 未确认启用 seccomp；没有运行生成代码。')
        return finish()
    for tool in configured:
        image = policy['images'][tool]
        metadata = _json_result(_invoke(docker + ['image', 'inspect', '--format', '{{json .}}', image]))
        if (not metadata or metadata.get('Os') != 'linux'
                or not isinstance(metadata.get('Id'), str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', metadata['Id'])
                or not isinstance(metadata.get('Config'), dict) or metadata['Config'].get('Volumes')):
            add(tool, 'not_run', '本地镜像不存在或不符合隔离配置；不会拉取或运行。')
            continue
        available.append(tool)
        add(tool, 'passed', '本机服务与不可变镜像已就绪；此诊断没有执行成果。')
    return finish()


def _create_arguments(docker, name, tool, image):
    entrypoint = 'python3' if tool.startswith('python_') else 'node'
    runner = _PYTHON_RUNNER if tool == 'python_unittest' else _PYTHON_STDIO_RUNNER
    program = (['-I', '-u', '-c', runner] if tool.startswith('python_')
               else ['--no-warnings', '--max-old-space-size=64', '-e', _JAVASCRIPT_RUNNER])
    return docker + ['create', '--name', name, '--pull=never', '--interactive', '--init',
                     '--network=none', '--read-only', '--user=65534:65534', '--cap-drop=ALL',
                     '--security-opt=no-new-privileges=true', '--cgroupns=private', '--ipc=none',
                     '--memory=128m', '--memory-swap=128m', '--cpus=0.5', '--pids-limit=32',
                     '--ulimit=nofile=128:128', '--ulimit=fsize=1048576:1048576',
                     '--tmpfs=/workspace:rw,noexec,nosuid,nodev,size=16m,mode=1777',
                     '--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=8m,mode=1777',
                     '--workdir=/workspace', '--env=PYTHONDONTWRITEBYTECODE=1',
                     '--env=PYTHONUNBUFFERED=1', '--env=NODE_OPTIONS=', '--env=NODE_PATH=',
                     '--no-healthcheck', '--log-driver=none',
                     '--restart=no', '--entrypoint', entrypoint, image] + program


def _safe_output(raw):
    text = raw.decode('utf-8', errors='replace')
    # Strip terminal controls; downstream UI must still use textContent.
    return ''.join(char for char in text if char in '\n\t' or ord(char) >= 32)[:OUTPUT_LIMIT]


def _execute(tool, files, policy, *, case=None, timeout=TIMEOUT_SECONDS, deadline=None):
    docker = _docker_prefix()
    if not docker:
        return {'status': 'not_run', 'reason_code': 'environment_unavailable', 'detail': 'Docker 在执行前不可用。'}
    name = 'roundtable-check-' + uuid.uuid4().hex
    nonce = uuid.uuid4().hex
    data = {'files': files, 'nonce': nonce}
    if tool == 'python_stdio':
        data['stdin'] = case['stdin']
    payload = json.dumps(data, ensure_ascii=True).encode('utf-8')
    result = {'status': 'failed', 'reason_code': 'runtime_failure', 'detail': '隔离容器未能完成检查。',
              'independent_validation': tool != 'python_unittest', 'command_completed': False}
    cleanup_ok = False

    def remaining(limit):
        return max(0.05, min(limit, deadline - time.monotonic())) if deadline is not None else limit

    try:
        created = _invoke(_create_arguments(docker, name, tool, policy['images'][tool]),
                          timeout=remaining(CONTROL_TIMEOUT_SECONDS))
        if created.returncode != 0 or created.timed_out or created.output_limited:
            result['detail'] = '无法创建符合限制的隔离容器；未启动成果。'
        else:
            ran = _invoke(docker + ['start', '--attach', '--interactive', name], input_bytes=payload,
                          timeout=remaining(timeout), limit=OUTPUT_LIMIT, merge_stderr=tool != 'python_stdio')
            result['output'] = _safe_output(ran.output)
            # Docker metadata, not source-controlled stdout, establishes whether
            # the command actually exited. This says nothing about test quality.
            state = _json_result(_invoke(docker + ['inspect', '--format', '{{json .State}}', name],
                                        timeout=remaining(CONTROL_TIMEOUT_SECONDS)))
            completed = (isinstance(state, dict) and state.get('Status') == 'exited'
                         and state.get('Running') is False and state.get('OOMKilled') is False
                         and state.get('ExitCode') == 0 and not state.get('Error'))
            result['command_completed'] = completed
            result['container_exit_code'] = state.get('ExitCode') if isinstance(state, dict) else None
            if ran.timed_out:
                result['detail'] = '运行超过本轮时间限制，已停止检查。'
            elif ran.output_limited:
                result['detail'] = '运行输出超过限制，已停止检查。'
            elif tool == 'python_stdio':
                matched = ran.output == case['expected_stdout'].encode('utf-8')
                success = completed and ran.returncode == 0 and matched
                result.update(status='passed' if success else 'failed', matched_stdout=matched,
                              stderr=_safe_output(ran.stderr),
                              detail='输入输出用例通过。' if success else '输出与独立用例不一致，或程序未正常退出。')
            else:
                marker = ('\x1eROUNDTABLE_RESULT:' + nonce + ':').encode()
                lines = ran.output.split(b'\n')
                reports = [line[len(marker):] for line in lines if line.startswith(marker)]
                report = None
                if len(reports) == 1:
                    try:
                        report = json.loads(reports[0])
                    except (ValueError, UnicodeError, RecursionError):
                        pass
                if not isinstance(report, dict):
                    result['detail'] = '没有收到完整检查报告，不能认定运行通过。'
                else:
                    count_key = 'tests_run' if tool == 'python_unittest' else 'checks_run'
                    count, skipped = report.get(count_key), report.get('skipped', 0)
                    valid_counts = (type(count) is int and 0 < count <= 10000
                                    and type(skipped) is int and 0 <= skipped < count)
                    success = completed and ran.returncode == 0 and report.get('passed') is True and valid_counts
                    result['status'] = 'passed' if success else 'failed'
                    result['count'] = count if type(count) is int else 0
                    result['skipped'] = skipped if type(skipped) is int else 0
                    result['detail'] = ('隔离检查通过。' if success else '检查未通过，或没有实际完成任何检查。')
                    if tool == 'python_unittest':
                        # Generated test modules can patch unittest, print any
                        # nonce/report and os._exit(0). Never seal those claims as
                        # an independent system acceptance result. Docker's exit
                        # metadata is the only trusted execution evidence here.
                        result['untrusted_test_report'] = {
                            'claimed_passed': report.get('passed') is True,
                            'claimed_tests_run': result.pop('count'),
                            'claimed_skipped': result.pop('skipped'),
                        }
                        if success:
                            result['status'] = 'not_run'
                            result['reason_code'] = 'independent_acceptance_missing'
                            result['detail'] = ('容器命令已退出；生成测试自报通过。测试数量与结论不可独立信任，'
                                                '尚需可信输入输出用例验证，不能标为功能验收通过。')
    finally:
        # Use only the locally generated name, including after partial create.
        removed = _invoke(docker + ['rm', '--force', '--volumes', name])
        cleanup_ok = removed.returncode == 0 and not removed.timed_out and not removed.output_limited
    result['cleanup_complete'] = cleanup_ok
    if not cleanup_ok:
        result['status'] = 'failed'
        result['reason_code'] = 'cleanup_unconfirmed'
        result['detail'] = '未确认隔离容器已清理；需维护人员检查，成果不能标记通过。'
    return result


def validate_execution(bundle, required_tools=(), *, cases=None):
    """Execute explicitly required tools; missing prerequisites cannot pass.

    `cases` is only for python_stdio and must come from a trusted caller (user or
    independently reviewed acceptance spec), not from the candidate's claims.
    Expected outputs remain on the host; only stdin and source go to containers.
    """
    policy = execution_policy()
    fingerprint = policy_fingerprint()
    checks = []

    def finish():
        return {'passed': all(item['status'] == 'passed' for item in checks), 'checks': checks,
                'scope': SCOPE, 'policy_fingerprint': fingerprint}

    if (not isinstance(required_tools, (list, tuple)) or len(required_tools) > len(TOOLS)
            or any(not isinstance(tool, str) or tool not in TOOLS for tool in required_tools)
            or len(set(required_tools)) != len(required_tools)):
        checks.append({'path': '', 'check': '隔离检查计划', 'status': 'failed', 'detail': '检查工具无效或重复。'})
        return finish()
    validated = validate_bundle(bundle)
    if not required_tools:
        return finish()
    valid_cases = (isinstance(cases, list) and 1 <= len(cases) <= 5
                   and all(isinstance(case, dict) and set(case) == {'stdin', 'expected_stdout'}
                           and all(isinstance(value, str) and len(value) <= 2000 for value in case.values())
                           for case in cases))
    if valid_cases:
        try:
            for case in cases:
                case['stdin'].encode('utf-8')
                case['expected_stdout'].encode('utf-8')
        except UnicodeError:
            valid_cases = False
    state = readiness()
    for tool in required_tools:
        item = {'path': '', 'tool': tool, 'check': TOOL_LABELS[tool]}
        if tool == 'python_stdio' and valid_cases:
            # Preserve the requested standard even when no container can start.
            # Neither expected text nor raw inputs belong in execution receipts.
            item.update(requested_cases=len(cases), independent_validation=False,
                        case_fingerprint=hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest())
        if tool == 'python_stdio' and not valid_cases:
            item.update(status='not_run', reason_code='required_inputs', independent_validation=False,
                        detail='需要调用方独立提供 1 至 5 个 stdin/expected_stdout 用例，每项最多 2000 字符。')
        elif tool not in state['available_tools']:
            reasons = [entry['detail'] for entry in state['checks'] if entry.get('tool') in {tool, 'runtime'}]
            item.update(status='not_run', reason_code='environment_unavailable',
                        detail=' '.join(reasons) or '所需隔离检查尚不可用。')
        elif fingerprint != policy_fingerprint() or fingerprint != state['policy_fingerprint']:
            item.update(status='not_run', reason_code='policy_changed', detail='隔离执行配置已改变，需要重新确认本轮任务。')
        elif tool == 'python_unittest' and not any(
                file['path'].split('/')[-1].startswith('test_') and file['path'].endswith('.py')
                for file in validated['files']):
            item.update(status='failed', reason_code='missing_tests', detail='成果没有 test_*.py 测试文件，不能认定功能测试通过。')
        elif tool == 'javascript_check' and not any(file['path'].lower().endswith('.js') for file in validated['files']):
            item.update(status='failed', reason_code='missing_source', detail='成果没有 JavaScript 文件，未执行语法检查。')
        elif tool == 'python_stdio' and not any(file['path'] == 'main.py' for file in validated['files']):
            item.update(status='failed', reason_code='missing_source', detail='输入输出验收需要固定入口 main.py。')
        elif tool == 'python_stdio':
            outcomes = []
            deadline = time.monotonic() + 30
            for case in cases:
                remaining = deadline - time.monotonic()
                policy_changed = fingerprint != policy_fingerprint()
                if policy_changed or remaining < 1:
                    outcomes.append({'status': 'not_run',
                                     'reason_code': 'policy_changed' if policy_changed else 'time_limit',
                                     'command_completed': False,
                                     'detail': '配置发生变化或本轮时间已用完，本用例未运行。'})
                    break
                outcome = _execute(tool, validated['files'], policy, case=case,
                                   timeout=min(TIMEOUT_SECONDS, remaining), deadline=deadline)
                outcomes.append(outcome)
                if outcome['status'] != 'passed':
                    break
            success = len(outcomes) == len(cases) and all(case['status'] == 'passed' for case in outcomes)
            stopped = None if success else outcomes[-1]
            status = 'passed' if success else 'not_run' if stopped['status'] == 'not_run' else 'failed'
            if stopped is not None:
                reason = stopped.get('reason_code', 'runtime_failure')
                item['reason_code'] = reason
                # A failed program is repairable. Unattempted following cases do
                # not turn that failure into an unavailable-environment result.
                pending_reason = reason if status == 'not_run' else 'previous_case_failed'
                outcomes.extend({'status': 'not_run', 'reason_code': pending_reason,
                                 'command_completed': False, 'detail': '前一用例未通过或执行中断，本用例未运行。'}
                                for _ in range(len(cases) - len(outcomes)))
            item.update(status=status, independent_validation=True, cases=outcomes,
                        detail=f'已通过 {sum(case["status"] == "passed" for case in outcomes)} / {len(cases)} 个独立输入输出用例。')
        else:
            item.update(_execute(tool, validated['files'], policy))
        if tool == 'python_stdio' and valid_cases and 'cases' not in item:
            item['cases'] = [{'status': 'not_run', 'reason_code': item.get('reason_code', 'not_started'),
                              'command_completed': False, 'detail': '前置条件不满足，本用例未运行。'}
                             for _ in cases]
        checks.append(item)
    return finish()
