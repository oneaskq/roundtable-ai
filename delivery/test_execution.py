"""Execution boundaries tested with synthetic Docker metadata, never model calls."""
import io
import json
import os
import sys
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from .bundles import BundleError
from .execution import (CommandResult, OUTPUT_LIMIT, _PYTHON_RUNNER, _create_arguments,
                        _environment, _invoke, policy_fingerprint, readiness, validate_execution)


IMAGES = {'python_unittest': 'python@sha256:' + 'a' * 64,
          'javascript_check': 'node@sha256:' + 'b' * 64,
          'python_stdio': 'python@sha256:' + 'a' * 64}
INFO = {'OSType': 'linux', 'MemoryLimit': True, 'SwapLimit': True,
        'CpuCfsQuota': True, 'PidsLimit': True, 'SecurityOptions': ['name=seccomp,profile=builtin']}
IMAGE = {'Os': 'linux', 'Id': 'sha256:' + 'c' * 64, 'Config': {}}
EXITED = {'Status': 'exited', 'Running': False, 'OOMKilled': False, 'ExitCode': 0, 'Error': ''}


def bundle(*files):
    return {'summary': '隔离验证测试样例',
            'files': [{'path': name, 'content': content} for name, content in files]}


def json_result(value):
    return CommandResult(0, json.dumps(value).encode())


@override_settings(EXECUTION_ENABLED=True, EXECUTION_IMAGES=IMAGES)
class ExecutionTests(SimpleTestCase):
    def ready(self):
        return {'available_tools': list(IMAGES), 'checks': [], 'policy_fingerprint': policy_fingerprint()}

    def synthetic_docker(self, *, count=1, skipped=0, passed=True, exit_code=0,
                         timeout=False, over_limit=False, missing_report=False, cleanup_code=0):
        def invoke(arguments, **kwargs):
            if 'create' in arguments:
                return CommandResult(0, b'container-id')
            if 'rm' in arguments:
                return CommandResult(cleanup_code)
            if 'inspect' in arguments:
                return json_result({**EXITED, 'ExitCode': exit_code})
            self.assertIn('start', arguments)
            payload = json.loads(kwargs['input_bytes'])
            report = {'passed': passed, 'tests_run': count, 'checks_run': count, 'skipped': skipped}
            raw = ('\x1eROUNDTABLE_RESULT:' + payload['nonce'] + ':' + json.dumps(report) + '\n').encode()
            return CommandResult(exit_code, b'no report' if missing_report else raw, timeout, over_limit)
        return invoke

    def run_bundle(self, required=('python_unittest',), **outcome):
        with patch('delivery.execution.readiness', return_value=self.ready()), \
                patch('delivery.execution._docker_prefix', return_value=['docker', '--host', 'local']), \
                patch('delivery.execution._invoke', side_effect=self.synthetic_docker(**outcome)) as invoked:
            report = validate_execution(bundle(('test_demo.py', 'import unittest\n'), ('main.js', 'let a = 1;')), required)
        return report, invoked

    def test_default_disabled_and_omitted_tools_never_probe_or_execute(self):
        with override_settings(EXECUTION_ENABLED=False), patch('delivery.execution._invoke') as invoked:
            report = validate_execution(bundle(('main.py', 'raise RuntimeError()')), ('python_unittest',))
            self.assertFalse(report['passed'])
            self.assertEqual(report['checks'][0]['status'], 'not_run')
            self.assertEqual(readiness()['available_tools'], [])
            invoked.assert_not_called()
        with patch('delivery.execution.readiness') as probe:
            report = validate_execution(bundle(('main.py', 'raise RuntimeError()')))
            self.assertEqual(report['checks'], [])
            probe.assert_not_called()

    def test_consent_fingerprint_has_no_process_io_and_changes_with_digest_and_enablement(self):
        with patch('delivery.execution._invoke') as invoked:
            baseline = policy_fingerprint()
            self.assertEqual(baseline, policy_fingerprint())
            with override_settings(EXECUTION_IMAGES={**IMAGES, 'python_unittest': 'python@sha256:' + 'd' * 64}):
                self.assertNotEqual(baseline, policy_fingerprint())
            with override_settings(EXECUTION_ENABLED=False):
                self.assertNotEqual(baseline, policy_fingerprint())
            invoked.assert_not_called()

    def test_mutable_images_and_nonboolean_enablement_fail_closed(self):
        for bad in ({'python_unittest': 'python:3.12-slim'}, {'python_unittest': 'evil@sha256:' + 'a' * 64},
                    {'python_unittest': None}, ['python']):
            with self.subTest(images=bad), override_settings(EXECUTION_IMAGES=bad), \
                    patch('delivery.execution._invoke') as invoked:
                self.assertEqual(readiness()['available_tools'], [])
                invoked.assert_not_called()
        with override_settings(EXECUTION_ENABLED='false'), patch('delivery.execution._invoke') as invoked:
            self.assertEqual(readiness()['available_tools'], [])
            invoked.assert_not_called()

    def test_missing_docker_is_not_run_and_does_not_fall_back_to_host_python(self):
        with patch('delivery.execution.shutil.which', return_value=None), patch('delivery.execution._invoke') as invoked:
            state = readiness()
            self.assertEqual(state['available_tools'], [])
            self.assertIn('Docker', state['checks'][0]['detail'])
            invoked.assert_not_called()

    def test_readiness_checks_local_images_without_start_or_pull(self):
        with patch('delivery.execution.shutil.which', return_value='docker'), \
                patch('delivery.execution._invoke', side_effect=[json_result(INFO)] + [json_result(IMAGE)] * 3) as invoked:
            state = readiness()
        self.assertEqual(state['available_tools'], list(IMAGES))
        self.assertEqual(len(invoked.call_args_list), 4)
        for call in invoked.call_args_list:
            args = call.args[0]
            self.assertIn('--host', args)
            self.assertNotIn('create', args)
            self.assertNotIn('start', args)
            self.assertNotIn('pull', args)

    def test_missing_resource_controls_or_seccomp_rejects_daemon(self):
        bad_info = [{**INFO, field: False} for field in ('MemoryLimit', 'SwapLimit', 'CpuCfsQuota', 'PidsLimit')]
        bad_info += [{**INFO, 'OSType': 'windows'}, {**INFO, 'SecurityOptions': []}, {**INFO, 'SecurityOptions': None},
                     {**INFO, 'SecurityOptions': ['name=seccomp,profile=unconfined']}]
        for info in bad_info:
            with self.subTest(info=info), patch('delivery.execution._docker_prefix', return_value=['docker']), \
                    patch('delivery.execution._invoke', return_value=json_result(info)) as invoked:
                self.assertEqual(readiness()['available_tools'], [])
                self.assertEqual(invoked.call_count, 1)

    def test_image_volumes_missing_image_and_invalid_metadata_are_not_run(self):
        for value in (None, {**IMAGE, 'Config': {'Volumes': {'/host': {}}}}, {**IMAGE, 'Config': None},
                      {**IMAGE, 'Os': 'windows'}, {**IMAGE, 'Id': None}):
            response = CommandResult(1) if value is None else json_result(value)
            with self.subTest(value=value), patch('delivery.execution._docker_prefix', return_value=['docker']), \
                    patch('delivery.execution._invoke', side_effect=[json_result(INFO)] + [response] * 3):
                self.assertEqual(readiness()['available_tools'], [])

    def test_invalid_paths_are_rejected_before_probing_or_running(self):
        with patch('delivery.execution.readiness') as probe, self.assertRaises(BundleError):
            validate_execution(bundle(('../host.py', 'print(1)')), ('python_unittest',))
        probe.assert_not_called()
        with patch('delivery.execution.readiness') as probe, self.assertRaises(BundleError):
            validate_execution({'summary': 'x', 'files': [{'path': 'x.py', 'content': '', 'symlink': '/host'}]}, ('python_unittest',))
        probe.assert_not_called()

    def test_unknown_duplicate_and_string_tools_cannot_supply_commands(self):
        for required in (['shell'], ['python_unittest', 'python_unittest'], 'python_unittest', ['python_unittest', None]):
            with self.subTest(required=required), patch('delivery.execution.readiness') as probe:
                report = validate_execution(bundle(('main.py', '')), required)
                self.assertFalse(report['passed'])
                probe.assert_not_called()

    def test_no_test_files_or_no_js_files_cannot_report_passed(self):
        with patch('delivery.execution.readiness', return_value=self.ready()), patch('delivery.execution._invoke') as invoked:
            for required in (('python_unittest',), ('javascript_check',)):
                report = validate_execution(bundle(('main.py', 'print(1)')), required)
                self.assertFalse(report['passed'])
                self.assertEqual(report['checks'][0]['status'], 'failed')
                self.assertIn(report['checks'][0]['reason_code'], ('missing_tests', 'missing_source'))
            invoked.assert_not_called()

    def test_unittest_self_report_is_not_independent_acceptance_and_always_cleans_container(self):
        report, invoked = self.run_bundle(count=3)
        self.assertFalse(report['passed'])
        self.assertEqual(report['checks'][0]['status'], 'not_run')
        self.assertEqual(report['checks'][0]['reason_code'], 'independent_acceptance_missing')
        self.assertFalse(report['checks'][0]['independent_validation'])
        self.assertEqual(report['checks'][0]['untrusted_test_report']['claimed_tests_run'], 3)
        self.assertTrue(report['checks'][0]['command_completed'])
        self.assertTrue(report['checks'][0]['cleanup_complete'])
        self.assertIn('create', invoked.call_args_list[0].args[0])
        self.assertIn('rm', invoked.call_args_list[-1].args[0])
        created_name = invoked.call_args_list[0].args[0][invoked.call_args_list[0].args[0].index('--name') + 1]
        self.assertEqual(invoked.call_args_list[-1].args[0][-1], created_name)

    def test_zero_tests_all_skipped_nonzero_exit_and_missing_report_cannot_pass(self):
        for outcome in ({'count': 0}, {'count': 2, 'skipped': 2}, {'exit_code': 1}, {'passed': False},
                        {'missing_report': True}, {'count': True}, {'count': 1, 'skipped': -1}):
            with self.subTest(outcome=outcome):
                report, invoked = self.run_bundle(**outcome)
                self.assertFalse(report['passed'])
                self.assertEqual(report['checks'][0]['status'], 'failed')
                self.assertIn('rm', invoked.call_args_list[-1].args[0])

    def test_generated_test_can_forge_marker_but_cannot_gain_independent_pass(self):
        # A module imported by unittest can read the harness payload and exit
        # before any test. Simulate precisely the forged successful transport;
        # no generated source is executed on the host during this regression.
        malicious = ('import sys, os, json\n'
                     'nonce = sys.modules["__main__"].payload["nonce"]\n'
                     'print("\\x1eROUNDTABLE_RESULT:" + nonce + ":" + '
                     'json.dumps({"passed": True, "tests_run": 1, "skipped": 0}), flush=True)\n'
                     'os._exit(0)\n')
        with patch('delivery.execution.readiness', return_value=self.ready()), \
                patch('delivery.execution._docker_prefix', return_value=['docker']), \
                patch('delivery.execution._invoke', side_effect=self.synthetic_docker(count=1)):
            report = validate_execution(bundle(('test_forged.py', malicious)), ('python_unittest',))
        self.assertFalse(report['passed'])
        self.assertFalse(report['checks'][0]['independent_validation'])
        self.assertEqual(report['checks'][0]['reason_code'], 'independent_acceptance_missing')

    def test_timeout_output_limit_and_cleanup_failure_are_failed(self):
        for outcome in ({'timeout': True}, {'over_limit': True}, {'cleanup_code': 1}):
            with self.subTest(outcome=outcome):
                report, invoked = self.run_bundle(**outcome)
                self.assertFalse(report['passed'])
                self.assertIn('rm', invoked.call_args_list[-1].args[0])
        report, _ = self.run_bundle(cleanup_code=1)
        self.assertFalse(report['checks'][0]['cleanup_complete'])

    def test_create_error_still_attempts_cleanup_and_does_not_start(self):
        with patch('delivery.execution.readiness', return_value=self.ready()), \
                patch('delivery.execution._docker_prefix', return_value=['docker']), \
                patch('delivery.execution._invoke', side_effect=[CommandResult(1), CommandResult(0)]) as invoked:
            report = validate_execution(bundle(('test_demo.py', '')), ('python_unittest',))
        self.assertFalse(report['passed'])
        self.assertEqual(invoked.call_count, 2)
        self.assertNotIn('start', invoked.call_args_list[-1].args[0])

    def test_changed_policy_between_probe_and_run_does_not_execute(self):
        state = self.ready()
        with patch('delivery.execution.readiness', return_value=state), \
                patch('delivery.execution.policy_fingerprint', side_effect=['new-policy', 'changed-policy']), \
                patch('delivery.execution._invoke') as invoked:
            report = validate_execution(bundle(('test_demo.py', '')), ('python_unittest',))
        self.assertFalse(report['passed'])
        self.assertIn('配置已改变', report['checks'][0]['detail'])
        invoked.assert_not_called()

    def test_javascript_is_reported_as_syntax_only(self):
        report, invoked = self.run_bundle(required=('javascript_check',))
        self.assertTrue(report['passed'])
        self.assertIn('语法', report['checks'][0]['check'])
        args = invoked.call_args_list[0].args[0]
        self.assertEqual(args[args.index('--entrypoint') + 1], 'node')
        self.assertIn("['--check'", args[-1])

    def test_container_has_no_host_mounts_network_or_shell_and_has_resource_limits(self):
        args = _create_arguments(['docker'], 'roundtable-check-owned', 'python_unittest', IMAGES['python_unittest'])
        for option in ('--pull=never', '--network=none', '--read-only', '--user=65534:65534', '--cap-drop=ALL',
                       '--security-opt=no-new-privileges=true', '--memory=128m', '--memory-swap=128m',
                       '--cpus=0.5', '--pids-limit=32', '--no-healthcheck', '--log-driver=none'):
            self.assertIn(option, args)
        for option in ('--mount', '--volume', '-v', '--privileged', '--publish', '--env-file', '--use-api-socket'):
            self.assertNotIn(option, args)
        self.assertEqual(args[-1], _PYTHON_RUNNER)
        self.assertNotIn('sh', args)
        self.assertNotIn('cmd', args)

    def test_docker_cli_receives_no_application_keys_or_remote_endpoint_environment(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret', 'DJANGO_SECRET_KEY': 'secret',
                                     'DOCKER_HOST': 'tcp://remote:2375', 'DOCKER_CONTEXT': 'remote'}):
            env = _environment()
        for key in ('DEEPSEEK_API_KEY', 'DJANGO_SECRET_KEY', 'DOCKER_HOST', 'DOCKER_CONTEXT'):
            self.assertNotIn(key, env)

    def test_diagnose_command_reports_not_run_without_running_container(self):
        output = io.StringIO()
        with override_settings(EXECUTION_ENABLED=False), patch('delivery.execution._invoke') as invoked:
            call_command('diagnose_execution', stdout=output)
            self.assertEqual(json.loads(output.getvalue())['available_tools'], [])
            invoked.assert_not_called()

    def test_host_process_wrapper_bounds_output_and_timeout_using_only_trusted_fixture(self):
        # These fixed test harness programs are authored here, never generated
        # source. This verifies the subprocess transport, not code execution.
        result = _invoke([sys.executable, '-I', '-c', 'import sys; sys.stdout.write("x" * 100000)'], limit=1024)
        self.assertTrue(result.output_limited)
        self.assertLessEqual(len(result.output), 1024)
        result = _invoke([sys.executable, '-I', '-c', 'import time; time.sleep(3)'], timeout=0.05)
        self.assertTrue(result.timed_out)

    def test_stdout_and_stderr_are_separate_for_independent_comparison(self):
        result = _invoke([sys.executable, '-I', '-c',
                          'import sys; sys.stdout.write("answer"); sys.stderr.write("diagnostic")'], merge_stderr=False)
        self.assertEqual(result.output, b'answer')
        self.assertEqual(result.stderr, b'diagnostic')

    def run_stdio(self, cases, *, output=b'answer\n', state=None, files=None):
        calls = []

        def invoke(arguments, **kwargs):
            calls.append((arguments, kwargs))
            if 'start' in arguments:
                payload = json.loads(kwargs['input_bytes'])
                self.assertEqual(set(payload), {'files', 'nonce', 'stdin'})
                self.assertNotIn('expected_stdout', payload)
                self.assertFalse(kwargs['merge_stderr'])
                return CommandResult(0, output)
            if 'inspect' in arguments:
                return json_result(EXITED if state is None else state)
            return CommandResult(0)

        with patch('delivery.execution.readiness', return_value=self.ready()), \
                patch('delivery.execution._docker_prefix', return_value=['docker']), \
                patch('delivery.execution._invoke', side_effect=invoke):
            report = validate_execution(files or bundle(('main.py', 'print(input())')), ('python_stdio',), cases=cases)
        return report, calls

    def test_stdio_compares_expected_output_only_on_host_with_actual_exit_evidence(self):
        report, calls = self.run_stdio([{'stdin': 'input', 'expected_stdout': 'answer\n'}])
        self.assertTrue(report['passed'])
        self.assertTrue(report['checks'][0]['independent_validation'])
        self.assertTrue(report['checks'][0]['cases'][0]['matched_stdout'])
        for arguments, kwargs in calls:
            self.assertNotIn('answer\n', arguments)
            if 'input_bytes' in kwargs:
                self.assertNotIn(b'answer', kwargs['input_bytes'])

    def test_stdio_ignores_forged_test_report_and_requires_exact_bytes(self):
        forged = b'\x1eROUNDTABLE_RESULT:claimed:{"passed":true,"tests_run":99}\n'
        for output in (forged, b'answer', b' answer\n', b'answer\nextra'):
            with self.subTest(output=output):
                report, _ = self.run_stdio([{'stdin': '', 'expected_stdout': 'answer\n'}], output=output)
                self.assertFalse(report['passed'])

    def test_stdio_cannot_pass_oom_nonzero_exit_missing_state_or_running_container(self):
        for state in ({**EXITED, 'OOMKilled': True}, {**EXITED, 'ExitCode': 1}, {},
                      {**EXITED, 'Running': True}, {**EXITED, 'Status': 'created'}):
            with self.subTest(state=state):
                report, _ = self.run_stdio([{'stdin': '', 'expected_stdout': 'answer\n'}], state=state)
                self.assertFalse(report['passed'])

    def test_stdio_rejects_missing_or_oversized_cases_and_requires_main_py(self):
        for cases in (None, [], [{'stdin': 'x'}], [{'stdin': '', 'expected_stdout': '', 'shell': 'cmd'}],
                      [{'stdin': 'x' * 2001, 'expected_stdout': ''}], [{'stdin': '', 'expected_stdout': '\ud800'}],
                      [{'stdin': '', 'expected_stdout': ''}] * 6):
            with self.subTest(cases=cases):
                report, calls = self.run_stdio(cases)
                self.assertFalse(report['passed'])
                self.assertEqual(report['checks'][0]['reason_code'], 'required_inputs')
                self.assertEqual(calls, [])
        report, calls = self.run_stdio([{'stdin': '', 'expected_stdout': ''}], files=bundle(('other.py', '')))
        self.assertFalse(report['passed'])
        self.assertEqual(report['checks'][0]['reason_code'], 'missing_source')
        self.assertEqual(calls, [])

    def test_stdio_uses_new_container_per_case_and_stops_on_first_failure(self):
        cases = [{'stdin': str(index), 'expected_stdout': 'answer\n'} for index in range(3)]
        report, calls = self.run_stdio(cases)
        self.assertTrue(report['passed'])
        names = [args[args.index('--name') + 1] for args, _ in calls if 'create' in args]
        self.assertEqual(len(set(names)), 3)
        report, calls = self.run_stdio(cases, output=b'wrong')
        self.assertFalse(report['passed'])
        self.assertEqual(len([args for args, _ in calls if 'create' in args]), 1)
