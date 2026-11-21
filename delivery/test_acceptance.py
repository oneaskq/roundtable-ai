import json
from dataclasses import replace
from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from accounts.models import MFADevice
from .acceptance import (
    AcceptanceError, CAPABILITIES, CHECK_VERSION, MANUAL_NOTICE, SIGNING_SALT,
    _canonical, _checks, approve_report, recheck_report, run_acceptance,
)
from .connections import ProviderConnection
from .models import CapabilityEvidence
from .provider import ProviderError, ProviderReply
from .text_guidance import TEXT_CONSTRAINT_GUIDANCE


WRITING = '\n'.join([
    '1. 出门前先制定学习计划，把今天要完成的阅读和练习写清楚，按难度分配时间，带齐文具和资料，结束时核对进度。',
    '2. 进入自习室后保持安静，把手机调成静音，轻拿轻放个人物品，需要讨论时先离开座位，也不要随意占用其他人的座位。',
    '3. 学习一段时间后安排短暂休息，起身喝水并看看远处，回来后继续下一项任务，根据自己的状态调整节奏，避免过度疲劳。',
])


class AcceptanceTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.override = self.settings(BASE_DIR=Path(self.directory.name))
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.connection = ProviderConnection('deepseek', 'test-model', 'synthetic-test-key', 'test-region')
        self.get_connection = patch('delivery.acceptance.get_connection', return_value=self.connection).start()
        self.catalog = patch('delivery.acceptance.list_models', return_value=['test-model']).start()
        self.generate = patch('delivery.acceptance.generate_reply', side_effect=self.reply).start()
        self.addCleanup(patch.stopall)

    def reply(self, messages, **kwargs):
        prompt = messages[-1]['content']
        if '17乘19' in prompt:
            return ProviderReply('323')
        if '第一次去自习室' in prompt:
            return ProviderReply(WRITING)
        if '以下是虚构项目资料' in prompt:
            return ProviderReply('- 项目名：松林\n- 启动日期：2026年11月3日\n- 参与人数：8人')
        if '这是合成记忆测试' in prompt:
            return ProviderReply('已记住', continuation='opaque-test-continuation')
        self.assertEqual(messages[-2]['_provider_context'], 'opaque-test-continuation')
        phrase = messages[0]['content'].split('短语：')[1].split('。')[0]
        return ProviderReply(phrase)

    def run_report(self):
        path, report = run_acceptance()
        self.assertTrue(report['passed'], report)
        return path, report

    def approve(self, path, **kwargs):
        return approve_report(path, confirm_reviewed=True, region_reviewed=True,
                              terms_reviewed=True, **kwargs)

    def resign(self, path, mutator):
        document = json.loads(path.read_text(encoding='utf-8'))
        mutator(document['report'])
        document['signature'] = signing.Signer(salt=SIGNING_SALT).signature(_canonical(document['report']))
        path.write_text(json.dumps(document), encoding='utf-8')

    @patch('delivery.management.commands.verify_model.get_connection')
    def test_preview_never_reads_connection_or_sends_requests(self, command_connection):
        output = StringIO()
        call_command('verify_model', stdout=output)
        command_connection.assert_not_called()
        self.get_connection.assert_not_called()
        self.catalog.assert_not_called()
        self.generate.assert_not_called()
        self.assertFalse(CapabilityEvidence.objects.exists())

    def test_run_requires_region_and_terms_consent_before_reading_connection(self):
        with self.assertRaises(CommandError):
            call_command('verify_model', run=True, stdout=StringIO())
        self.get_connection.assert_not_called()
        self.catalog.assert_not_called()

    def test_model_absent_from_actual_catalog_never_generates(self):
        self.catalog.return_value = ['different-model']
        with self.assertRaises(AcceptanceError):
            run_acceptance()
        self.generate.assert_not_called()
        self.assertFalse(CapabilityEvidence.objects.exists())

    def test_catalog_failure_does_not_generate_or_enable(self):
        self.catalog.side_effect = ProviderError('synthetic-test-key must not surface')
        with self.assertRaisesRegex(AcceptanceError, '获取官方模型名单失败'):
            run_acceptance()
        self.generate.assert_not_called()

    def test_run_makes_five_synthetic_calls_and_does_not_enable(self):
        path, report = self.run_report()
        self.assertEqual(self.generate.call_count, 5)
        self.assertFalse(CapabilityEvidence.objects.exists())
        self.assertNotIn(self.connection.api_key, path.read_text(encoding='utf-8'))
        self.assertNotIn('opaque-test-continuation', path.read_text(encoding='utf-8'))
        for call in self.generate.call_args_list:
            self.assertEqual(call.kwargs['max_output_tokens'], 4096)
            self.assertEqual(call.kwargs['connection'], self.connection)
        self.assertEqual([result['capability'] for result in report['results']], list(CAPABILITIES))

    def test_writing_guidance_is_recorded_without_changing_the_user_probe(self):
        path, report = self.run_report()
        writing = next(item for item in report['results'] if item['capability'] == 'chat.writing')
        self.assertEqual(writing['prompts'], [
            '为第一次去自习室的学生写三条中文建议，主题分别是计划、安静、休息。'
            '只输出三行，分别以1.、2.、3.开头，总计120至180个汉字，不加标题。'])
        self.assertIn(TEXT_CONSTRAINT_GUIDANCE, writing['system_instruction'])
        self.assertIn(TEXT_CONSTRAINT_GUIDANCE, self.generate.call_args_list[1].kwargs['system_instruction'])
        self.assertIn('分成3项时每项平均约50个汉字', writing['system_instruction'])
        self.assertTrue(all('system_instruction' not in call.kwargs for index, call in
                            enumerate(self.generate.call_args_list) if index != 1))
        self.assertEqual(report['manual_notice'], MANUAL_NOTICE)
        self.assertTrue(writing['manual_review_required'])

    def test_budget_hint_does_not_make_an_overlength_model_reply_pass(self):
        def reply(messages, **kwargs):
            if '第一次去自习室' in messages[-1]['content']:
                return ProviderReply('\n'.join(f'{index}. ' + '字' * 77 for index in range(1, 4)))
            return self.reply(messages, **kwargs)

        self.generate.side_effect = reply
        path, report = run_acceptance()
        writing = next(item for item in report['results'] if item['capability'] == 'chat.writing')
        self.assertFalse(writing['passed'])
        self.assertFalse(report['passed'])
        self.assertEqual(self.generate.call_count, 5)
        self.assertIn('分成3项时每项平均约50个汉字', writing['system_instruction'])
        with self.assertRaises(AcceptanceError):
            self.approve(path)
        self.assertFalse(CapabilityEvidence.objects.exists())

    def test_explicit_stop_on_check_failure_does_not_send_later_probes(self):
        def reply(messages, **kwargs):
            if '第一次去自习室' in messages[-1]['content']:
                return ProviderReply('\n'.join(f'{index}. ' + '字' * 77 for index in range(1, 4)))
            return self.reply(messages, **kwargs)

        self.generate.side_effect = reply
        path, report = run_acceptance(stop_on_check_failure=True)
        self.assertFalse(report['passed'])
        self.assertEqual(self.generate.call_count, 2)
        self.assertTrue(report['results'][0]['passed'])
        self.assertFalse(report['results'][1]['passed'])
        self.assertTrue(all(item['diagnostic_code'] == 'not_run' and
                            item['error'] == '前一项验收检查未通过，本项未执行。'
                            for item in report['results'][2:]))
        with self.assertRaises(AcceptanceError):
            self.approve(path)

    def test_explicit_stop_on_check_failure_stops_before_context_second_turn(self):
        def reply(messages, **kwargs):
            if '这是合成记忆测试' in messages[-1]['content']:
                return ProviderReply('没有按照要求确认')
            return self.reply(messages, **kwargs)

        self.generate.side_effect = reply
        _path, report = run_acceptance(stop_on_check_failure=True)
        self.assertFalse(report['passed'])
        self.assertEqual(self.generate.call_count, 4)
        context = report['results'][-1]
        self.assertEqual(context['diagnostic_code'], 'check_failed')
        self.assertEqual(context['outputs'], ['没有按照要求确认'])
        self.assertIn('第二轮未执行', context['error'])

    def test_structured_provider_output_and_instruction_are_kept_for_review(self):
        raw = json.dumps({'answer': WRITING}, ensure_ascii=False)
        def reply(messages, **kwargs):
            if '第一次去自习室' in messages[-1]['content']:
                return ProviderReply(WRITING, raw_content=raw,
                                     effective_system_instruction='实际发送的结构化系统指令',
                                     response_format={'type': 'json_schema'})
            return self.reply(messages, **kwargs)

        self.generate.side_effect = reply
        path, report = self.run_report()
        writing = next(item for item in report['results'] if item['capability'] == 'chat.writing')
        self.assertEqual(writing['outputs'], [WRITING])
        self.assertEqual(writing['provider_outputs'], [raw])
        self.assertEqual(writing['system_instruction'], '实际发送的结构化系统指令')
        self.assertEqual(writing['response_format'], {'type': 'json_schema'})
        self.assertEqual(writing['stream_callback_mode'], 'buffered_then_validated')
        self.assertFalse(CapabilityEvidence.objects.exists())
        with self.assertRaisesMessage(AcceptanceError, '结构化长度检查仍处于关闭状态'):
            self.approve(path)

    def test_failed_structured_output_is_recorded_without_leaking_secrets(self):
        def reply(messages, **kwargs):
            if '第一次去自习室' in messages[-1]['content']:
                failure = ProviderError('synthetic failure')
                failure.raw_content = '{"answer":"synthetic-test-key"}'
                failure.response_format = {'type': 'json_schema'}
                failure.effective_system_instruction = '实际结构化指令'
                raise failure
            return self.reply(messages, **kwargs)

        self.generate.side_effect = reply
        path, report = run_acceptance()
        writing = report['results'][1]
        self.assertFalse(writing['passed'])
        self.assertEqual(writing['outputs'], [])
        self.assertEqual(writing['response_format'], {'type': 'json_schema'})
        self.assertEqual(writing['system_instruction'], '实际结构化指令')
        self.assertIn('[已隐藏密钥]', writing['provider_outputs'][0])
        self.assertNotIn('synthetic-test-key', path.read_text(encoding='utf-8'))
        self.assertEqual(self.generate.call_count, 2)

    def test_writing_checks_length_and_leave_topic_meaning_to_human_review(self):
        paraphrased = WRITING.replace('计划', '安排').replace('安静', '清静').replace('休息', '小憩')
        checks = _checks('chat.writing', [paraphrased])
        self.assertTrue(all(item['passed'] for item in checks))
        self.assertNotIn('覆盖计划、安静与休息', [item['name'] for item in checks])
        for count, expected in ((119, False), (120, True), (180, True), (181, False)):
            value = '1. ' + '字' * (count - 2) + '\n2. 字\n3. 字'
            length = next(check for check in _checks('chat.writing', [value])
                          if check['name'] == '中文字数在120至180之间')
            self.assertEqual(length['passed'], expected, count)
        self.assertIn('写作主题是否覆盖须人工判断', MANUAL_NOTICE)

    def test_approval_requires_all_human_confirmations(self):
        path, _ = self.run_report()
        for omitted in ('confirm_reviewed', 'region_reviewed', 'terms_reviewed'):
            flags = dict(confirm_reviewed=True, region_reviewed=True, terms_reviewed=True)
            flags[omitted] = False
            with self.assertRaises(AcceptanceError):
                approve_report(path, **flags)
        self.assertFalse(CapabilityEvidence.objects.exists())

    def test_local_approval_binds_evidence_without_more_provider_calls(self):
        path, report = self.run_report()
        self.generate.reset_mock()
        self.catalog.reset_mock()
        self.approve(path)
        self.assertEqual(CapabilityEvidence.objects.count(), 4)
        for evidence in CapabilityEvidence.objects.all():
            self.assertTrue(evidence.enabled)
            self.assertEqual(evidence.provider, self.connection.provider)
            self.assertEqual(evidence.connection_fingerprint, self.connection.fingerprint)
            self.assertEqual(evidence.expires_at - evidence.verified_at, timedelta(days=30))
            self.assertIsNone(evidence.reviewer)
            self.assertTrue(json.loads(evidence.evidence)['human_reviewed'])
        expiry = list(CapabilityEvidence.objects.values_list('expires_at', flat=True))
        self.approve(path)
        self.assertEqual(list(CapabilityEvidence.objects.values_list('expires_at', flat=True)), expiry)
        self.generate.assert_not_called()
        self.catalog.assert_not_called()

    def test_tampered_or_external_report_rejected(self):
        path, _ = self.run_report()
        document = json.loads(path.read_text(encoding='utf-8'))
        document['report']['results'][0]['outputs'] = ['wrong']
        path.write_text(json.dumps(document), encoding='utf-8')
        with self.assertRaises(AcceptanceError):
            self.approve(path)
        external = Path(self.directory.name) / 'other.json'
        external.write_text(json.dumps(document), encoding='utf-8')
        with self.assertRaises(AcceptanceError):
            self.approve(external)
        self.assertFalse(CapabilityEvidence.objects.exists())

    def test_key_rotation_or_region_change_rejects_report(self):
        path, _ = self.run_report()
        for connection in (
            ProviderConnection('deepseek', 'test-model', 'rotated-test-key', 'test-region'),
            ProviderConnection('deepseek', 'test-model', 'synthetic-test-key', 'other-region'),
        ):
            with self.assertRaises(AcceptanceError):
                self.approve(path, connection=connection)

    def test_stale_or_future_report_rejected(self):
        for delta in (timedelta(days=-2), timedelta(hours=1)):
            path, _ = self.run_report()
            self.resign(path, lambda report: report.update(completed_at=(timezone.now() + delta).isoformat()))
            with self.assertRaises(AcceptanceError):
                self.approve(path)

    def test_fluent_but_wrong_answers_do_not_pass(self):
        self.generate.side_effect = None
        self.generate.return_value = ProviderReply('这是一段流畅但完全不符合输入要求的回复。')
        path, report = run_acceptance()
        self.assertFalse(report['passed'])
        with self.assertRaises(AcceptanceError):
            self.approve(path)
        self.assertFalse(CapabilityEvidence.objects.exists())

    def test_summary_accepts_source_name_with_or_without_project_suffix(self):
        for name in ('松林', '松林项目'):
            checks = _checks('chat.summary', [f'- 项目名：{name}\n- 启动日期：2026年11月3日\n- 参与人数：8人'])
            self.assertTrue(all(item['passed'] for item in checks))
        for name, date in (('松林项目新增预算', '2026年11月3日'), ('松林项目', '2026年11月4日')):
            checks = _checks('chat.summary', [f'- 项目名：{name}\n- 启动日期：{date}\n- 参与人数：8人'])
            self.assertFalse(all(item['passed'] for item in checks))

    def test_recheck_preserves_signed_original_and_does_not_call_or_approve(self):
        path, original = self.run_report()
        self.resign(path, lambda report: report.update(check_version=1, passed=False))
        before = path.read_bytes()
        self.generate.reset_mock()
        self.catalog.reset_mock()
        revised_path, revised = recheck_report(path)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotEqual(path, revised_path)
        self.assertEqual(revised['rechecked_from'], original['report_id'])
        self.assertEqual(revised['completed_at'], original['completed_at'])
        self.assertEqual(revised['outputs_hash'], original['outputs_hash'])
        self.assertTrue(revised['passed'])
        self.assertEqual(revised['check_version'], CHECK_VERSION)
        self.assertEqual(revised['manual_notice'], MANUAL_NOTICE)
        self.generate.assert_not_called()
        self.catalog.assert_not_called()
        self.assertFalse(CapabilityEvidence.objects.exists())
        with self.assertRaises(AcceptanceError):
            approve_report(revised_path)
        path.write_text('tampered', encoding='utf-8')
        with self.assertRaises(AcceptanceError):
            recheck_report(path)

    def test_older_report_without_streaming_cannot_enable_the_updated_transport(self):
        path, _ = self.run_report()
        self.resign(path, lambda report: report.pop('streaming_tested'))
        with self.assertRaises(AcceptanceError):
            self.approve(path)
        self.assertFalse(CapabilityEvidence.objects.exists())

    def test_migration_disables_only_evidence_without_streaming_validation(self):
        from importlib import import_module
        from django.apps import apps
        migration = import_module('delivery.migrations.0011_require_streaming_evidence')
        now = timezone.now()
        common = {'provider': 'deepseek', 'model': 'synthetic', 'service_region': 'test',
                  'verified_at': now, 'expires_at': now + timedelta(days=1), 'enabled': True}
        old = CapabilityEvidence.objects.create(capability='chat.qa', connection_fingerprint='old', evidence='{}', **common)
        new = CapabilityEvidence.objects.create(capability='chat.qa', connection_fingerprint='new',
                                                 evidence='{"streaming_tested": true}', **common)
        migration.disable_legacy_evidence(apps, None)
        old.refresh_from_db(); new.refresh_from_db()
        self.assertFalse(old.enabled)
        self.assertTrue(new.enabled)

    def test_provider_failure_persists_failed_report_without_secret(self):
        self.generate.side_effect = ProviderError('synthetic-test-key')
        path, report = run_acceptance()
        self.assertFalse(report['passed'])
        self.assertEqual(self.generate.call_count, 1)
        self.assertTrue(all(item['error'] == '前一项请求失败，本项未执行。'
                            for item in report['results'][1:]))
        self.assertNotIn(self.connection.api_key, path.read_text(encoding='utf-8'))
        with self.assertRaises(AcceptanceError):
            self.approve(path)

    def test_provider_failure_after_success_stops_all_later_probes(self):
        def fail_on_writing(messages, **kwargs):
            if '第一次去自习室' in messages[-1]['content']:
                raise ProviderError('simulated provider failure')
            return self.reply(messages, **kwargs)

        self.generate.side_effect = fail_on_writing
        path, report = run_acceptance()
        self.assertEqual(self.generate.call_count, 2)
        self.assertTrue(report['results'][0]['passed'])
        self.assertFalse(report['results'][1]['passed'])
        self.assertEqual([item['outputs'] for item in report['results'][2:]], [[], []])
        self.assertFalse(report['passed'])
        with self.assertRaises(AcceptanceError):
            self.approve(path)

    def test_report_keeps_only_safe_provider_error_categories(self):
        self.generate.side_effect = ProviderError('synthetic-test-key', diagnostic_code='http_400')
        path, report = run_acceptance()
        self.assertEqual(report['results'][0]['diagnostic_code'], 'http_400')
        self.assertEqual(report['results'][1]['diagnostic_code'], 'not_run')
        self.assertNotIn('synthetic-test-key', path.read_text(encoding='utf-8'))
        self.generate.side_effect = ProviderError('synthetic-test-key', diagnostic_code='secret-value')
        _path, report = run_acceptance()
        self.assertEqual(report['results'][0]['diagnostic_code'], '')

    def test_named_reviewer_requires_active_superuser_and_mfa(self):
        path, _ = self.run_report()
        user = get_user_model().objects.create_superuser(email='reviewer@example.test', password='TestPass123!')
        with self.assertRaises(AcceptanceError):
            self.approve(path, reviewer_email=user.email)
        MFADevice.objects.create(user=user, enabled=True)
        self.approve(path, reviewer_email=user.email)
        self.assertTrue(all(row.reviewer == user for row in CapabilityEvidence.objects.all()))

    @patch('delivery.management.commands.verify_model.get_connection')
    @patch('builtins.input', return_value='取消')
    def test_interactive_cancel_never_queries_provider(self, question, command_connection):
        command_connection.return_value = self.connection
        call_command('verify_model', interactive=True, stdout=StringIO())
        self.catalog.assert_not_called()
        self.generate.assert_not_called()

    @patch('delivery.management.commands.verify_model.get_connection')
    @patch('builtins.input', side_effect=['确认并获取名单', '开始验收', '取消'])
    def test_interactive_no_final_approval_keeps_pending(self, question, command_connection):
        command_connection.return_value = self.connection
        call_command('verify_model', interactive=True, stdout=StringIO())
        self.assertEqual(self.generate.call_count, 5)
        self.assertFalse(CapabilityEvidence.objects.exists())

    @patch('delivery.management.commands.verify_model.get_connection')
    @patch('builtins.input', side_effect=['确认并获取名单', '开始验收', '确认启用'])
    def test_interactive_full_review_enables_only_four_capabilities(self, question, command_connection):
        command_connection.return_value = self.connection
        call_command('verify_model', interactive=True, stdout=StringIO())
        self.assertEqual(self.catalog.call_count, 1)
        self.assertEqual(set(CapabilityEvidence.objects.values_list('capability', flat=True)), set(CAPABILITIES))

    def test_approving_second_connection_preserves_first_evidence(self):
        path, _ = self.run_report()
        self.approve(path)
        other = ProviderConnection('kimi', 'test-model', 'synthetic-other-key', 'test-region')
        other_path, report = run_acceptance(connection=other)
        self.assertTrue(report['passed'])
        self.approve(other_path, connection=other)
        self.assertEqual(CapabilityEvidence.objects.count(), 8)
        for connection in (self.connection, other):
            self.assertEqual(CapabilityEvidence.objects.filter(
                connection_fingerprint=connection.fingerprint, enabled=True).count(), 4)
        self.approve(other_path, connection=other)
        self.assertEqual(CapabilityEvidence.objects.count(), 8)

    @patch('delivery.management.commands.verify_model.load_named_connection')
    @patch('delivery.management.commands.verify_model.get_connection')
    def test_named_provider_preview_does_not_read_either_connection(self, active, named):
        call_command('verify_model', provider='kimi', stdout=StringIO())
        active.assert_not_called()
        named.assert_not_called()
        self.catalog.assert_not_called()
        self.generate.assert_not_called()

    @patch('delivery.management.commands.verify_model.load_named_connection')
    @patch('delivery.management.commands.verify_model.get_connection')
    def test_named_provider_runs_and_approves_without_active_connection(self, active, named):
        other = ProviderConnection('kimi', 'test-model', 'synthetic-other-key', 'test-region')
        named.return_value = other
        with patch('delivery.management.commands.verify_model.run_acceptance', wraps=run_acceptance) as run:
            call_command('verify_model', provider='kimi', run=True,
                         region_reviewed=True, terms_reviewed=True, stdout=StringIO())
        self.assertEqual(run.call_args.kwargs['connection'], other)
        for call in self.generate.call_args_list:
            self.assertEqual(call.kwargs['connection'], other)
        path = next((Path(self.directory.name) / 'work' / 'model-acceptance').glob('*.json'))
        call_command('verify_model', provider='kimi', approve=True, report=str(path),
                     confirm_reviewed=True, region_reviewed=True, terms_reviewed=True, stdout=StringIO())
        active.assert_not_called()
        self.assertEqual(CapabilityEvidence.objects.filter(connection_fingerprint=other.fingerprint).count(), 4)

    @patch('delivery.management.commands.verify_model.load_named_connection', return_value=None)
    @patch('delivery.management.commands.verify_model.get_connection')
    def test_missing_named_provider_does_not_fall_back_or_generate(self, active, named):
        with self.assertRaises(CommandError):
            call_command('verify_model', provider='kimi', run=True,
                         region_reviewed=True, terms_reviewed=True, stdout=StringIO())
        active.assert_not_called()
        self.catalog.assert_not_called()
        self.generate.assert_not_called()

    def test_qwen_acceptance_records_missing_catalog_api_without_bypassing_human_review(self):
        qwen = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing')
        path, report = run_acceptance(connection=qwen)
        self.catalog.assert_not_called()
        self.assertEqual(self.generate.call_count, 5)
        self.assertTrue(report['passed'])
        self.assertIs(report['catalog_model_verified'], False)
        self.assertEqual(report['catalog_verification'], 'unsupported_official_api')
        self.assertFalse(CapabilityEvidence.objects.exists())
        with self.assertRaises(AcceptanceError):
            approve_report(path, connection=qwen)
        self.approve(path, connection=qwen)
        self.assertEqual(CapabilityEvidence.objects.filter(connection_fingerprint=qwen.fingerprint).count(), 4)
        for evidence in CapabilityEvidence.objects.all():
            self.assertIs(json.loads(evidence.evidence)['catalog_model_verified'], False)
        with self.assertRaises(AcceptanceError):
            self.approve(path, connection=replace(qwen, service_region='ap-southeast-1'))

    def test_qwen_thinking_writing_must_observe_thinking_and_binds_the_connection(self):
        qwen = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing', 'low', 'bailian')

        def reply(messages, **kwargs):
            result = self.reply(messages, **kwargs)
            if '第一次去自习室' in messages[-1]['content']:
                return ProviderReply(result, thinking_observed=True)
            return result

        self.generate.side_effect = reply
        path, report = run_acceptance(connection=qwen, stop_on_check_failure=True)
        self.assertTrue(report['passed'])
        self.assertEqual(self.generate.call_count, 5)
        self.assertEqual(report['reasoning_effort'], 'low')
        self.assertTrue(report['results'][1]['thinking_observed'])
        self.assertTrue(next(check['passed'] for check in report['results'][1]['checks']
                             if check['name'] == '写作请求观察到思考模式'))
        with self.assertRaises(AcceptanceError):
            self.approve(path, connection=replace(qwen, reasoning_effort='none'))
        self.assertFalse(CapabilityEvidence.objects.exists())
        self.approve(path, connection=qwen)
        self.assertEqual(CapabilityEvidence.objects.count(), 4)

    def test_qwen_thinking_missing_signal_stops_the_remaining_acceptance(self):
        qwen = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing', 'low', 'bailian')
        path, report = run_acceptance(connection=qwen, stop_on_check_failure=True)
        self.assertEqual(self.generate.call_count, 2)
        self.assertFalse(report['passed'])
        self.assertFalse(report['results'][1]['thinking_observed'])
        self.assertTrue(all(item['diagnostic_code'] == 'not_run' for item in report['results'][2:]))
        with self.assertRaises(AcceptanceError):
            self.approve(path, connection=qwen)
        self.assertFalse(CapabilityEvidence.objects.exists())

    def test_bailian_report_is_bound_to_its_platform_and_cannot_enable_qwencloud(self):
        bailian = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing', '', 'bailian')
        path, report = run_acceptance(connection=bailian)
        self.assertEqual(report['service_platform'], 'bailian')
        self.assertTrue(report['passed'])
        with self.assertRaises(AcceptanceError):
            self.approve(path, connection=replace(bailian, service_platform='qwencloud'))

    def test_qwen_report_rejects_missing_or_falsely_claimed_catalog_status(self):
        qwen = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing')
        for change in ({'catalog_model_verified': True}, {'catalog_verification': ''}):
            path, _ = run_acceptance(connection=qwen)
            self.resign(path, lambda report: report.update(change))
            with self.assertRaises(AcceptanceError):
                self.approve(path, connection=qwen)
        self.assertFalse(CapabilityEvidence.objects.exists())

    def test_qwen_failure_and_unsupported_model_never_enable(self):
        qwen = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing')
        with self.assertRaises(AcceptanceError):
            run_acceptance(connection=replace(qwen, model='qwq-plus'))
        self.generate.assert_not_called()
        self.generate.side_effect = ProviderError('synthetic-qwen-key')
        path, report = run_acceptance(connection=qwen)
        self.assertFalse(report['passed'])
        self.assertNotIn(qwen.api_key, path.read_text(encoding='utf-8'))
        with self.assertRaises(AcceptanceError):
            self.approve(path, connection=qwen)
        self.catalog.assert_not_called()
        self.assertFalse(CapabilityEvidence.objects.exists())

    @patch('delivery.management.commands.verify_model.load_named_connection')
    @patch('delivery.management.commands.verify_model.get_connection')
    def test_qwen_cli_preview_and_cancel_do_not_call_network(self, active, named):
        named.return_value = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing')
        call_command('verify_model', provider='qwen', stdout=StringIO())
        named.assert_not_called()
        with patch('builtins.input', side_effect=['确认配置', '取消']):
            call_command('verify_model', provider='qwen', interactive=True, stdout=StringIO())
        active.assert_not_called()
        self.catalog.assert_not_called()
        self.generate.assert_not_called()

    @patch('delivery.management.commands.verify_model.load_named_connection')
    @patch('delivery.management.commands.verify_model.get_connection')
    @patch('builtins.input', side_effect=['确认配置', '开始验收', '确认启用'])
    def test_qwen_cli_uses_named_connection_and_retains_full_approval_flow(self, question, active, named):
        qwen = ProviderConnection('qwen', 'qwen3.7-plus', 'synthetic-qwen-key', 'cn-beijing')
        named.return_value = qwen
        output = StringIO()
        call_command('verify_model', provider='qwen', interactive=True, stdout=output)
        active.assert_not_called()
        self.catalog.assert_not_called()
        self.assertEqual(self.generate.call_count, 5)
        self.assertEqual(CapabilityEvidence.objects.filter(connection_fingerprint=qwen.fingerprint).count(), 4)
        self.assertIn('未进行模型名单认证', output.getvalue())
        self.assertNotIn(qwen.api_key, output.getvalue())
