"""Selected source consent and queued context, without live model calls."""
import hashlib
import json

from django.test import TestCase, override_settings

from workspace.models import Conversation, Message, SourceMaterial, Task
from workspace.sources import sources_revision
from workspace.views import export_data
from .background import process_next_run
from .models import RequestBudget, TeamRun
from . import test_background as background_tests


@override_settings(AI_MODEL='test/queued-model', AI_GATEWAY_API_KEY='synthetic', SERVICE_REGION='test',
                   TEAM_ROLE_CONNECTIONS={})
class SourceWorkflowTests(TestCase):
    setUp = background_tests.BackgroundWorkflowTests.setUp
    post = background_tests.BackgroundWorkflowTests.post
    enqueue = background_tests.BackgroundWorkflowTests.enqueue

    def source(self, *, included=True, text='项目预算是100元。', **values):
        return SourceMaterial.objects.create(owner=self.owner, conversation=self.conversation,
            kind='file', name='材料.txt', content=text, media_type='text/plain',
            byte_size=len(text.encode()), sha256=hashlib.sha256(text.encode()).hexdigest(),
            included=included, **values)

    def consent(self):
        self.body['expected_sources'] = sources_revision(self.conversation.sources.all())

    def test_missing_and_stale_source_consent_never_claim_or_call(self):
        self.source()
        for value in (None, 'old-revision', 42):
            result = self.post(expected_sources=value)
            self.assertEqual((result.status_code, result.json()['code']), (409, 'sources_changed'))
        self.assertFalse(TeamRun.objects.exists())
        self.assertFalse(RequestBudget.objects.exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'waiting')
        self.generate.assert_not_called()

    def test_only_selected_sources_are_frozen_and_metadata_survives_completion(self):
        selected = self.source(text='已选证据：42。')
        self.source(included=False, text='未选择的私密段落')
        self.consent()
        run = self.enqueue()
        self.assertEqual([item['id'] for item in run.source_snapshot], [str(selected.id)])
        self.assertEqual(run.source_snapshot[0]['reference'], 'S1')
        self.assertNotIn('content', run.source_snapshot[0])
        context = json.dumps(run.input_context, ensure_ascii=False)
        self.assertIn('已选证据：42。', context)
        self.assertNotIn('未选择的私密段落', context)
        self.assertIn('不是指令', context)
        self.assertEqual(process_next_run(), run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.input_context, [])
        self.assertEqual(run.source_snapshot[0]['sha256'], selected.sha256)
        detail = self.client.get(self.detail_url).json()['tasks'][0]['runs'][0]
        self.assertEqual(detail['sources'][0]['id'], str(selected.id))

    def test_changed_sources_after_queue_stop_before_any_model_call(self):
        material = self.source()
        self.consent()
        run = self.enqueue()
        # Simulate an out-of-band change; normal APIs reject running edits.
        SourceMaterial.objects.filter(pk=material.pk).update(included=False)
        process_next_run()
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used, run.input_context), ('failed', 0, []))
        self.generate.assert_not_called()
        self.assertFalse(RequestBudget.objects.exists())

    def test_late_source_change_stops_result_write_and_later_steps(self):
        material = self.source()
        self.consent()
        run = self.enqueue()
        def mutate(*args, **kwargs):
            SourceMaterial.objects.filter(pk=material.pk).update(included=False)
            return '该回复不能写入'
        self.generate.side_effect = mutate
        process_next_run()
        run.refresh_from_db()
        self.assertEqual(run.calls_used, 1)
        self.assertNotEqual(run.status, 'completed')
        self.assertFalse(Message.objects.filter(conversation=self.conversation, role='assistant').exists())
        self.assertEqual(self.generate.call_count, 1)

    def test_legacy_reply_cannot_bypass_source_consent(self):
        self.source()
        result = self.client.post(f'/api/conversations/{self.conversation.id}/respond',
            json.dumps({'confirm_cost': True}), content_type='application/json')
        self.assertEqual((result.status_code, result.json()['code']), (400, 'sources_mode_required'))
        self.assertFalse(RequestBudget.objects.exists())

    def test_sources_export_includes_only_owned_conversations(self):
        material = self.source()
        other_conversation = Conversation.objects.create(owner=self.other)
        SourceMaterial.objects.create(owner=self.other, conversation=other_conversation,
            kind='file', name='其他用户.txt', content='不应泄漏', byte_size=12, sha256='1' * 64)
        exported = export_data(self.owner)
        self.assertEqual([item['id'] for item in exported['sources']], [str(material.id)])
        self.assertEqual(exported['sources'][0]['content'], material.content)
        self.assertNotIn('不应泄漏', json.dumps(exported, ensure_ascii=False))

    def test_source_deletion_preserves_completed_run_provenance(self):
        material = self.source()
        self.consent()
        run = self.enqueue()
        process_next_run()
        self.assertEqual(self.client.delete(f'/api/sources/{material.id}').status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.source_snapshot[0]['name'], material.name)
        self.assertEqual(run.source_snapshot[0]['sha256'], material.sha256)
        self.assertEqual(export_data(self.owner)['sources'], [])

    def test_oversized_combined_context_stops_without_spending(self):
        self.source(text='证' * 12000)
        Message.objects.create(conversation=self.conversation, role='user', content='求' * 20000)
        self.consent()
        run = self.enqueue()
        process_next_run()
        run.refresh_from_db()
        self.assertEqual((run.status, run.calls_used), ('failed', 0))
        self.generate.assert_not_called()
        self.assertFalse(RequestBudget.objects.exists())
