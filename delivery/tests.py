import json
import uuid
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from workspace.models import Artifact, Conversation, Message, Task, TaskEvent
from .catalog import IMPLEMENTED, capability_catalog, connection_revision
from .connections import ProviderConnection, get_connection
from .models import CapabilityEvidence, RequestBudget
from .provider import ProviderError, generate_reply


class DeliveryTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(email='one@example.test', password='ExamplePass123!')
        self.other = get_user_model().objects.create_user(email='two@example.test', password='ExamplePass123!')
        self.client.force_login(self.owner)
        self.conversation = Conversation.objects.create(owner=self.owner, title='需求')
        Message.objects.create(conversation=self.conversation, role='user', content='整理一份学习计划')
        self.task = Task.objects.create(owner=self.owner, conversation=self.conversation, goal='学习计划', status='waiting')

    def post(self, path, data=None):
        if data and data.get('confirm_cost') is True and 'expected_connection' not in data:
            data = dict(data, expected_connection=connection_revision(get_connection()))
        return self.client.post('/api/' + path, json.dumps(data or {}), content_type='application/json')

    def approve_chat(self):
        connection = get_connection()
        for key in IMPLEMENTED:
            CapabilityEvidence.objects.create(capability=key, provider=connection.provider,
                connection_fingerprint=connection.fingerprint, model='test/model', service_region='test',
                evidence='Offline test fixture only', verified_at=timezone.now(),
                expires_at=timezone.now() + timezone.timedelta(days=1), region_approved=True,
                compliance_approved=True, enabled=True)

    def test_all_eight_categories_start_pending(self):
        response = self.client.get('/api/capabilities')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['capabilities']), 8)
        self.assertTrue(all(item['status'] == 'pending' for item in response.json()['capabilities']))
        self.assertFalse(response.json()['release']['public_ready'])

    @override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='not-a-real-key', SERVICE_REGION='test')
    def test_evidence_required_and_subcapabilities_independent(self):
        self.approve_chat()
        self.assertEqual(capability_catalog()[0]['status'], 'available')
        CapabilityEvidence.objects.filter(capability='chat.context').update(compliance_approved=False)
        self.assertEqual(capability_catalog()[0]['status'], 'partial')
        CapabilityEvidence.objects.filter(capability='chat.context').update(compliance_approved=True, model='other/model')
        self.assertEqual(capability_catalog()[0]['status'], 'partial')
        CapabilityEvidence.objects.filter(capability='chat.context').update(model='test/model', expires_at=timezone.now())
        self.assertEqual(capability_catalog()[0]['status'], 'partial')
        self.assertEqual(capability_catalog()[7]['status'], 'pending')

    @patch('delivery.views.generate_reply')
    def test_pending_never_calls_provider_or_fakes_reply(self, generate):
        response = self.post(f'conversations/{self.conversation.id}/respond', {'confirm_cost': True})
        self.assertEqual(response.status_code, 503)
        generate.assert_not_called()
        self.assertFalse(Message.objects.filter(role='assistant').exists())

    def test_pause_resume_is_persisted_and_invalid_transition_rejected(self):
        self.assertEqual(self.post(f'tasks/{self.task.id}/transition', {'action': 'pause'}).status_code, 200)
        self.assertEqual(self.post(f'tasks/{self.task.id}/transition', {'action': 'pause'}).status_code, 409)
        self.assertEqual(self.post(f'tasks/{self.task.id}/transition', {'action': 'resume'}).json()['task']['status'], 'waiting')
        self.assertEqual(TaskEvent.objects.filter(task=self.task).count(), 2)

    def test_requirement_export_is_real_and_labelled(self):
        response = self.post(f'tasks/{self.task.id}/brief')
        self.assertEqual(response.status_code, 200)
        artifact = response.json()['artifact']
        content = self.client.get(artifact['url']).content.decode()
        self.assertIn('整理一份学习计划', content)
        self.assertIn('不是 AI 分析', content)
        self.post(f'tasks/{self.task.id}/brief')
        self.assertEqual(Artifact.objects.filter(task=self.task).count(), 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'waiting')

    def test_other_users_cannot_read_or_change_tasks_and_artifacts(self):
        artifact = Artifact.objects.create(owner=self.owner, task=self.task, name='secret.md', content='owner-only')
        self.client.force_login(self.other)
        self.assertEqual(self.post(f'tasks/{self.task.id}/transition', {'action': 'pause'}).status_code, 404)
        self.assertEqual(self.post(f'tasks/{self.task.id}/brief').status_code, 404)
        self.assertEqual(self.client.get(f'/api/tasks/{self.task.id}/artifacts').status_code, 404)
        self.assertEqual(self.client.get(f'/api/artifacts/{artifact.id}/download').status_code, 404)
        self.assertEqual(self.post(f'conversations/{self.conversation.id}/respond').status_code, 404)

    @override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='fake', SERVICE_REGION='test')
    @patch('delivery.views.generate_reply', return_value='真实适配器的离线测试响应')
    def test_verified_adapter_flow_persists_result_once(self, generate):
        self.approve_chat()
        url = f'conversations/{self.conversation.id}/respond'
        self.assertEqual(self.post(url).status_code, 400)
        response = self.post(url, {'confirm_cost': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['task']['status'], 'completed')
        self.assertEqual(Artifact.objects.get(task=self.task).content, '真实适配器的离线测试响应')
        self.assertEqual(self.post(url, {'confirm_cost': True}).status_code, 409)
        generate.assert_called_once_with([{'role': 'user', 'content': '整理一份学习计划'}], connection=get_connection())

    @override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='fake', SERVICE_REGION='test')
    def test_late_response_cannot_overwrite_a_new_execution(self):
        self.approve_chat()
        newer_id = uuid.uuid4()
        def late_response(messages, **kwargs):
            Task.objects.filter(id=self.task.id).update(execution_id=newer_id)
            return '迟到响应'
        with patch('delivery.views.generate_reply', side_effect=late_response):
            response = self.post(f'conversations/{self.conversation.id}/respond', {'confirm_cost': True})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(Message.objects.filter(role='assistant').exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.execution_id, newer_id)

    @override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='fake', SERVICE_REGION='test')
    @patch('delivery.views.generate_reply', side_effect=ProviderError('模型服务余额或预算不足。'))
    def test_provider_failure_records_real_status(self, generate):
        self.approve_chat()
        response = self.post(f'conversations/{self.conversation.id}/respond', {'confirm_cost': True})
        self.assertEqual(response.status_code, 502)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'failed')
        self.assertFalse(Artifact.objects.exists())
        self.assertFalse(Message.objects.filter(role='assistant').exists())

    def test_anonymous_and_invalid_body(self):
        self.assertEqual(self.client.post(f'/api/tasks/{self.task.id}/transition', '[]', content_type='application/json').status_code, 400)
        self.client.logout()
        self.assertEqual(self.client.get('/api/capabilities').status_code, 401)

    @override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='fake', SERVICE_REGION='test')
    @patch('delivery.views.generate_reply')
    def test_request_budget_blocks_without_model_call(self, generate):
        self.approve_chat()
        RequestBudget.objects.create(user=self.owner, attempts=20)
        response = self.post(f'conversations/{self.conversation.id}/respond', {'confirm_cost': True})
        self.assertEqual(response.status_code, 429)
        generate.assert_not_called()
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'waiting')

    @override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='fake', SERVICE_REGION='test')
    def test_key_or_provider_rotation_requires_new_evidence(self):
        self.approve_chat()
        original = get_connection()
        self.assertEqual(capability_catalog(original)[0]['status'], 'available')
        rotated = ProviderConnection(provider=original.provider, model=original.model,
                                     api_key='different-key', service_region=original.service_region)
        self.assertEqual(capability_catalog(rotated)[0]['status'], 'pending')
        changed_provider = ProviderConnection(provider='deepseek', model=original.model,
                                             api_key=original.api_key, service_region=original.service_region)
        self.assertEqual(capability_catalog(changed_provider)[0]['status'], 'pending')

    @override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='fake', SERVICE_REGION='test')
    @patch('delivery.views.generate_reply')
    def test_outdated_connection_consent_never_sends_user_content(self, generate):
        self.approve_chat()
        response = self.post(f'conversations/{self.conversation.id}/respond',
                             {'confirm_cost': True, 'expected_connection': 'previous-provider-version'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'connection_changed')
        generate.assert_not_called()
        self.assertFalse(RequestBudget.objects.exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'waiting')

    @override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='private-test-key', SERVICE_REGION='test')
    def test_public_connection_metadata_never_exposes_credentials(self):
        self.approve_chat()
        response = self.client.get('/api/capabilities')
        self.assertEqual(response.json()['connection']['readiness'], 'available')
        self.assertNotIn('private-test-key', response.content.decode())
        self.assertNotIn(get_connection().fingerprint, response.content.decode())

    @override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='fake', SERVICE_REGION='test')
    def test_revoked_evidence_while_request_runs_discards_result(self):
        self.approve_chat()
        def revoke(messages, **kwargs):
            CapabilityEvidence.objects.update(enabled=False)
            return '不得保存的过期结果'
        with patch('delivery.views.generate_reply', side_effect=revoke):
            response = self.post(f'conversations/{self.conversation.id}/respond', {'confirm_cost': True})
        self.assertEqual(response.status_code, 409)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'failed')
        self.assertFalse(Message.objects.filter(role='assistant').exists())
        self.assertFalse(Artifact.objects.exists())

    @override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='fake', SERVICE_REGION='test')
    def test_private_provider_continuation_is_saved_but_not_exposed(self):
        self.approve_chat()
        class Reply(str):
            continuation = 'opaque-encrypted-provider-context'
        with patch('delivery.views.generate_reply', return_value=Reply('可见回答')):
            response = self.post(f'conversations/{self.conversation.id}/respond', {'confirm_cost': True})
        self.assertEqual(response.status_code, 200)
        message = Message.objects.get(role='assistant')
        self.assertEqual(message.provider_context, Reply.continuation)
        for path in (f'/api/conversations/{self.conversation.id}', '/api/data/export'):
            exported = self.client.get(path)
            self.assertEqual(exported.status_code, 200)
            self.assertNotIn(Reply.continuation, exported.content.decode())
            self.assertNotIn('provider_context', exported.content.decode())
        self.post(f'conversations/{self.conversation.id}', {'content': '继续'})
        with patch('delivery.views.generate_reply', return_value='后续回答') as generate:
            self.assertEqual(self.post(f'conversations/{self.conversation.id}/respond', {'confirm_cost': True}).status_code, 200)
        assistant = next(item for item in generate.call_args.args[0] if item['role'] == 'assistant')
        self.assertEqual(assistant['_provider_context'], Reply.continuation)


class ProviderTests(TestCase):
    @override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='fake')
    @patch('delivery.provider.urllib.request.build_opener')
    def test_transport_contract_and_incomplete_response(self, opener):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'choices': [
            {'message': {'content': 'complete'}, 'finish_reason': 'stop'}]}).encode()
        opener.return_value.open.return_value = response
        self.assertEqual(generate_reply([{'role': 'user', 'content': 'hello'}]), 'complete')
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://ai-gateway.vercel.sh/v1/chat/completions')
        payload = json.loads(request.data)
        self.assertEqual(payload['model'], 'test/model')
        self.assertEqual(payload['messages'][-1], {'role': 'user', 'content': 'hello'})
        response.__enter__.return_value.read.return_value = b'{"choices":[{"message":{"content":"partial"},"finish_reason":"length"}]}'
        with self.assertRaises(ProviderError):
            generate_reply([{'role': 'user', 'content': 'hello'}])

    @override_settings(AI_MODEL='test/model', AI_GATEWAY_API_KEY='fake')
    @patch('delivery.provider.urllib.request.build_opener')
    def test_context_limit_does_not_silently_drop_history(self, opener):
        with self.assertRaises(ProviderError):
            generate_reply([{'role': 'user', 'content': 'x' * 60001}])
        opener.assert_not_called()
