"""Threaded provider simulation: no network, no ORM access from workers."""
import json
import threading
import time
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TransactionTestCase

from .decision_team_engine import _parallel_analyses, make_state, run_decision
from .background import process_next_run
from .models import AgentStep, TeamRun
from .provider import ProviderError
from .test_decision_team import connections, fixture
from workspace.models import Conversation, Message, SourceMaterial, Task
from workspace.sources import sources_revision


class ParallelAnalysisTests(SimpleTestCase):
    def setUp(self):
        self.team = connections()
        self.state = make_state(self.team, [])
        self.state.update(phase='confirmed', brief={'goal': '比较方案', 'unknowns': '', 'assumptions': ''})
        self.main_thread = threading.get_ident()

    def test_three_requests_overlap_and_share_frozen_input(self):
        barrier = threading.Barrier(3, timeout=2)
        packets, workers, authorized = [], [], []

        def before(stage, label, connection):
            self.assertEqual(threading.get_ident(), self.main_thread)
            authorized.append(stage)

        def generate(messages, **kwargs):
            packets.append(messages[0]['content'])
            workers.append(threading.get_ident())
            barrier.wait()
            time.sleep(0.12)
            return '{}'

        started = time.monotonic()
        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            result = _parallel_analyses(self.state['participants'], self.team, {'goal': '相同输入'}, '分析',
                                        before, lambda: None, lambda: 3)
        elapsed = time.monotonic() - started
        self.assertEqual(len(result), 3)
        self.assertEqual(len(set(packets)), 1)
        self.assertEqual(len(set(workers)), 3)
        self.assertNotIn(self.main_thread, workers)
        self.assertEqual(authorized, ['analysis_p1', 'analysis_p2', 'analysis_p3'])
        self.assertLess(elapsed, 0.34)

    def test_parallel_writes_are_ordered_and_evidence_ids_stable(self):
        stage = ''
        stages, committed, packets = [], [], []
        barrier = threading.Barrier(3, timeout=2)
        fast_committed = threading.Event()
        mapping = {'deepseek': 'p1', 'kimi': 'p2', 'qwen': 'p3'}

        def before(current, label, connection):
            nonlocal stage
            self.assertEqual(threading.get_ident(), self.main_thread)
            stage = current
            stages.append(current)

        def after(current, output):
            self.assertEqual(threading.get_ident(), self.main_thread)
            committed.append(current)
            if current == 'analysis_p3':
                fast_committed.set()

        def generate(messages, **kwargs):
            if '只依据同一份问题说明独立分析' in kwargs['system_instruction']:
                packets.append(messages[0]['content'])
                part = mapping[kwargs['connection'].provider]
                barrier.wait()
                if part == 'p1' and not fast_committed.wait(2):
                    raise AssertionError('fast member was not published before slow member finished')
                value = fixture('analysis_' + part)
                quote = {'p1': '报价100元', 'p2': '预算200元', 'p3': '限期30天'}[part]
                value['claims'][0]['evidence'] = [{'source': 'U1', 'quote': quote, 'scope': '用户提供条件'}]
            else:
                value = fixture(stage)
            return json.dumps(value, ensure_ascii=False)

        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            run_decision([{'role': 'user', 'content': '报价100元，预算200元，限期30天'}], self.team, self.state,
                         before, after, lambda _: None, parallel_analysis=True)
        self.assertEqual(len(set(packets)), 1)
        self.assertLess(committed.index('analysis_p3'), committed.index('analysis_p1'))
        self.assertEqual([a['participant'] for a in self.state['analyses']], ['p1', 'p2', 'p3'])
        self.assertEqual([a['claims'][0]['evidence_ids'] for a in self.state['analyses']], [['E1'], ['E2'], ['E3']])

    def test_failure_does_not_commit_late_outputs_or_replay(self):
        barrier = threading.Barrier(3, timeout=2)
        release = threading.Event()
        calls = []

        def generate(messages, **kwargs):
            calls.append(kwargs['connection'].provider)
            barrier.wait()
            if kwargs['connection'].provider == 'kimi':
                raise ProviderError('模拟失败')
            release.wait(2)
            return '{}'

        try:
            with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
                with self.assertRaises(ProviderError):
                    _parallel_analyses(self.state['participants'], self.team, {}, '分析',
                                       lambda *_: None, lambda: None, lambda: 3)
        finally:
            release.set()
        self.assertCountEqual(calls, ['deepseek', 'kimi', 'qwen'])
        self.assertEqual(self.state['analyses'], [])

    def test_cancellation_rejects_returned_batch_before_any_state_write(self):
        barrier = threading.Barrier(3, timeout=2)
        committed = []

        def generate(messages, **kwargs):
            barrier.wait()
            return json.dumps(fixture('analysis_p1'), ensure_ascii=False)

        def after(stage, output):
            raise ProviderError('本轮已暂停')

        with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            with self.assertRaises(ProviderError):
                run_decision([{'role': 'user', 'content': '比较方案'}], self.team, self.state,
                             lambda *_: None, after, lambda value: committed.append(value['phase']),
                             parallel_analysis=True)
        self.assertEqual(self.state['analyses'], [])
        self.assertEqual(committed, ['analysis'])

    def test_cancel_after_fast_result_does_not_accept_slow_late_results(self):
        barrier = threading.Barrier(3, timeout=2)
        release = threading.Event()
        finished = threading.Event()
        guard = threading.Lock()
        late = []
        attempts = []

        def generate(messages, **kwargs):
            provider = kwargs['connection'].provider
            barrier.wait()
            if provider != 'deepseek':
                release.wait(2)
                with guard:
                    late.append(provider)
                    if len(late) == 2:
                        finished.set()
            return json.dumps(fixture('analysis_p1'), ensure_ascii=False)

        def after(stage, output):
            attempts.append(stage)
            raise ProviderError('本轮已暂停')

        try:
            with patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
                with self.assertRaises(ProviderError):
                    run_decision([{'role': 'user', 'content': '比较方案'}], self.team, self.state,
                                 lambda *_: None, after, lambda _: None, parallel_analysis=True)
        finally:
            release.set()
            self.assertTrue(finished.wait(3))
        self.assertEqual(attempts, ['analysis_p1'])
        self.assertEqual(self.state['analyses'], [])


class ParallelDatabaseTests(TransactionTestCase):
    def test_background_run_uses_parallel_providers_and_sequential_database_updates(self):
        user = get_user_model().objects.create_user(email='parallel@example.test', password='ExamplePass123!')
        self.client.force_login(user)
        conversation = Conversation.objects.create(owner=user, title='讨论')
        Message.objects.create(conversation=conversation, role='user', content='比较方案')
        task = Task.objects.create(owner=user, conversation=conversation, goal='比较方案', status='waiting')
        team = connections()
        barrier = threading.Barrier(3, timeout=3)
        worker_threads = []

        def generate(messages, **kwargs):
            if '只依据同一份问题说明独立分析' in kwargs['system_instruction']:
                worker_threads.append(threading.get_ident())
                barrier.wait()
                part = {'deepseek': 'p1', 'kimi': 'p2', 'qwen': 'p3'}[kwargs['connection'].provider]
                value = fixture('analysis_' + part)
            else:
                value = fixture(AgentStep.objects.get(status='running').role)
            return json.dumps(value, ensure_ascii=False)

        body = {'background': True, 'client_request_id': str(uuid.uuid4()), 'confirm_cost': True,
                'expected_team': 'a' * 64, 'workflow': 'decision_v1', 'delivery_mode': 'text',
                'expected_max_calls': 14, 'expected_task': str(task.id),
                'expected_task_updated_at': task.updated_at.isoformat(),
                'expected_sources': sources_revision(SourceMaterial.objects.filter(conversation=conversation))}
        with patch('delivery.team_views.resolve_team', return_value=team), \
             patch('delivery.team_views.team_summary', return_value={'readiness': 'available', 'revision': 'a' * 64}), \
             patch('delivery.decision_team_engine.generate_reply', side_effect=generate):
            response = self.client.post(f'/api/conversations/{conversation.id}/collaborate',
                                        json.dumps(body), content_type='application/json')
            self.assertEqual(response.status_code, 202)
            process_next_run()
            run = TeamRun.objects.get()
            response = self.client.post(f'/api/conversations/{conversation.id}/confirm-decision',
                json.dumps({'run_id': str(run.id), 'answer': '优先速度', 'confirm_continue': True,
                            'expected_max_calls': 14}), content_type='application/json')
            self.assertEqual(response.status_code, 202)
            process_next_run()
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used), ('completed', 12))
        self.assertEqual(len(set(worker_threads)), 3)
        self.assertEqual(run.steps.filter(role__startswith='analysis_', status='completed').count(), 3)
