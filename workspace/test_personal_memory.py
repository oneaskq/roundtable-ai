import json
import uuid
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase

from delivery.connections import ProviderConnection
from delivery.decision_team_engine import _parallel_analyses
from delivery.provider import generate_reply
from delivery.team_views import execute_team_run
from .memory_rules import candidates
from .models import Conversation, DataPolicy, Memory, Message
from .personal_memory import active_memories, recall


class PersonalMemoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = get_user_model().objects.create_user(email='memory-one@example.test')
        cls.bob = get_user_model().objects.create_user(email='memory-two@example.test')

    def setUp(self):
        self.client.force_login(self.alice)

    def submit(self, sentence, client_id=None):
        response = self.client.post('/api/conversations', {'client_message_id': str(client_id or uuid.uuid4()),
                                    'content': sentence}, content_type='application/json')
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()

    def test_user_preference_reaches_the_next_chats_model_packet_for_its_owner_only(self):
        fact = '选实习时我更看重通勤'
        self.submit(fact)
        next_chat = self.submit('帮我比较两个岗位')['conversation']['id']
        self.client.force_login(self.bob)
        self.submit('选实习时我更看重薪资')
        self.assertEqual(recall(self.bob.pk, '帮我比较两个岗位'), ('选实习时我更看重薪资',))
        self.assertEqual(recall(self.alice.pk, '讲讲月食的成因'), ())
        context = [{'role': 'user', 'content': '帮我比较两个岗位'}]
        task = Conversation.objects.get(pk=next_chat).tasks.get()
        run = SimpleNamespace(task=task, input_context=context)
        provider = ProviderConnection('deepseek', 'synthetic-model', 'synthetic-key')
        result = {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}]}
        with patch('delivery.team_views._execute_team_run', side_effect=lambda *_args, **_kwargs:
                   generate_reply(context, connection=provider)), patch('delivery.background.execution_heartbeat'), \
                patch('delivery.provider._request', return_value=result) as transport:
            execute_team_run(run)
        packet = transport.call_args.args[2]['messages']
        self.assertEqual(json.loads(packet[-1]['content']), {'request': '帮我比较两个岗位', 'personal_memories': [fact]})
        self.assertNotIn(fact, packet[0]['content'])
        self.assertEqual(context, [{'role': 'user', 'content': '帮我比较两个岗位'}])
        self.assertEqual(active_memories.get(), ())

    def test_parallel_model_packets_keep_the_callers_memory_and_structured_fields(self):
        fact = '以后回答请先给结论'
        self.submit(fact)
        provider = ProviderConnection('deepseek', 'synthetic-model', 'synthetic-key')
        parts = [{'id': 'p1', 'role': 'proposer', 'label': '甲'}, {'id': 'p2', 'role': 'reviewer', 'label': '乙'}]
        from .personal_memory import memory_context
        with memory_context(self.alice.pk, '选一门课'), patch('delivery.provider._request', return_value={
            'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}]}) as transport:
            _parallel_analyses(parts, {p['role']: provider for p in parts}, {'goal': '选一门课'},
                               '独立分析', lambda *_: None, lambda: None, lambda: 5)
        self.assertEqual(transport.call_count, 2)
        for call in transport.call_args_list:
            self.assertEqual(json.loads(call.args[2]['messages'][-1]['content']),
                             {'goal': '选一门课', 'personal_memories': [fact]})

    def test_switch_manual_edits_deletion_and_request_replay(self):
        fact = '以后回答请简短一点'
        client_id = uuid.uuid4()
        self.submit(fact, client_id)
        self.submit(fact, client_id)
        note = self.client.get('/api/memories').json()['memories'][0]
        self.assertEqual(note['source'], 'conversation')
        url = '/api/memories/' + note['id']
        self.client.patch(url, {'content': '回答时先给结论'}, content_type='application/json')
        self.submit(fact)
        self.assertEqual(recall(self.alice.pk, '编一则故事'), ('回答时先给结论',))
        self.client.patch('/api/memories', {'enabled': False}, content_type='application/json')
        self.submit('我每周二下午有课')
        self.assertFalse(self.client.get('/api/memories').json()['enabled'])
        self.assertEqual(recall(self.alice.pk, '记得我吗'), ())
        self.client.patch('/api/memories', {'enabled': True}, content_type='application/json')
        self.assertEqual(recall(self.alice.pk, '记得我吗'), ('回答时先给结论',))
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.submit(fact, client_id)
        self.assertEqual(recall(self.alice.pk, '记得我吗'), ())
        self.assertEqual(Memory.objects.count(), 0)

    def test_corrected_chat_branch_and_repeated_declarations(self):
        first = self.submit('选实习时我更看重通勤')
        self.submit('选实习时我更看重导师')
        self.assertEqual(recall(self.alice.pk, '比较岗位'), ('选实习时我更看重导师',))
        self.submit('我喜欢喝茶')
        later = self.submit('我不喜欢喝茶')
        self.assertEqual(recall(self.alice.pk, '喝茶'), ('我不喜欢喝茶',))
        response = self.client.post('/api/messages/' + later['message']['id'] + '/edit',
                                    {'expected_content': '我不喜欢喝茶', 'content': '请给我一本书',
                                     'client_edit_id': str(uuid.uuid4())}, content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertNotIn('我不喜欢喝茶', recall(self.alice.pk, '记得我吗'))
        self.assertNotIn(first['message']['content'], recall(self.alice.pk, '比较岗位'))

    def test_migration_only_learns_owned_active_user_turns_and_preserves_manual_notes(self):
        chat = Conversation.objects.create(owner=self.alice)
        kept = Message.objects.create(conversation=chat, role='user', content='我每周三下午都有课')
        Message.objects.create(conversation=chat, role='assistant', content='我习惯步行')
        Message.objects.create(conversation=chat, role='user', content='我喜欢苹果', superseded_by=kept)
        Message.objects.create(conversation=Conversation.objects.create(owner=self.bob), role='user', content='我喜欢香蕉')
        DataPolicy.objects.create(user=self.bob, cross_chat_memory=False)
        Memory.objects.create(owner=self.alice, content='请使用简体中文')
        migrate = import_module('workspace.migrations.0011_learn_existing_preferences').learn_existing_preferences
        migrate(apps, SimpleNamespace(connection=connection))
        self.assertCountEqual(recall(self.alice.pk, '帮我安排下周日程'), ['我每周三下午都有课', '请使用简体中文'])
        self.assertEqual(Memory.objects.count(), 2)

    def test_quotes_questions_temporary_facts_and_private_data_are_skipped(self):
        for text in ['今天我喜欢这份便当', '比如我喜欢唱歌', '我喜欢喝茶吗？', '他喜欢乘地铁',
                     '> 我是一名设计师', '请复述“我偏好步行”', '\x60\x60\x60\n我习惯晚睡\n\x60\x60\x60',
                     '记住我的密码是 example', '我喜欢把验证码发邮件', '如果我是一名老师会怎样？',
                     '翻译下面的原文：\n我是一名老师。']:
            with self.subTest(text=text):
                self.assertFalse(list(candidates(text)))
        self.assertEqual([v for _, v in candidates('我平时习惯骑车，今天临时打车。')], ['我平时习惯骑车'])
        self.assertEqual([v for _, v in candidates('I prefer a short commute.')], ['I prefer a short commute'])
        self.assertEqual([v for _, v in candidates('@Kimi 我的专业是设计。')], ['我的专业是设计'])
