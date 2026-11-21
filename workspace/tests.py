import json
import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase
from django.utils import timezone

from .models import Artifact, Conversation, DataPolicy, DeletionRequest, Memory, Message, Project, RecordDeletionReceipt, Task, TaskEvent
from .views import APIError, revise_task


class WorkspaceIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "A-local-test-password-72!"
        User = get_user_model()
        cls.alice = User.objects.create_user(email="alice@example.test", password=cls.password, display_name="Alice")
        cls.bob = User.objects.create_user(email="bob@example.test", password=cls.password)
        cls.project = Project.objects.create(owner=cls.alice, title="Alice 的项目", description="个人资料")
        cls.other_project = Project.objects.create(owner=cls.bob, title="BOB_SECRET_PROJECT")
        cls.conversation = Conversation.objects.create(owner=cls.alice, project=cls.project, title="Alice 的任务")
        cls.other_conversation = Conversation.objects.create(owner=cls.bob, project=cls.other_project, title="BOB_SECRET_CHAT")
        cls.task = Task.objects.create(owner=cls.alice, conversation=cls.conversation, goal="整理文件", status="completed")
        cls.other_task = Task.objects.create(owner=cls.bob, conversation=cls.other_conversation, goal="BOB_SECRET_TASK", status="completed")
        cls.memory = Memory.objects.create(owner=cls.alice, content="请使用简体中文")
        cls.other_memory = Memory.objects.create(owner=cls.bob, content="BOB_SECRET_MEMORY")
        Message.objects.create(conversation=cls.conversation, role="user", content="我的需求")
        Message.objects.create(conversation=cls.other_conversation, role="user", content="BOB_SECRET_MESSAGE")
        TaskEvent.objects.create(task=cls.task, kind="review", content="已检查我的文件")
        TaskEvent.objects.create(task=cls.other_task, kind="review", content="BOB_SECRET_EVENT")
        Artifact.objects.create(owner=cls.alice, task=cls.task, name="note.txt", content="我的成果内容")
        Artifact.objects.create(owner=cls.bob, task=cls.other_task, name="other.txt", content="BOB_SECRET_ARTIFACT")

    def setUp(self):
        self.client.force_login(self.alice)

    def post(self, url, data, method="post"):
        return getattr(self.client, method)(url, json.dumps(data), content_type="application/json")

    def test_anonymous_cannot_list_or_mutate(self):
        self.client.logout()
        for endpoint in ["projects", "conversations", "memories", "data/overview", "data/export", "data/policy"]:
            self.assertEqual(self.client.get(f"/api/{endpoint}").status_code, 401)
        self.assertEqual(self.post("/api/projects", {"title": "未授权"}).status_code, 401)

    def test_lists_and_overview_are_owner_scoped(self):
        for endpoint in ["projects", "conversations", "memories", "data/overview"]:
            response = self.client.get(f"/api/{endpoint}")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("BOB_SECRET", response.content.decode())
        counts = self.client.get("/api/data/overview").json()["counts"]
        self.assertEqual(counts, {"projects": 1, "conversations": 1, "messages": 1, "tasks": 1, "artifacts": 1, "memories": 1, "quality_rounds": 0})

    def test_foreign_project_access_and_mutation_are_hidden(self):
        url = f"/api/projects/{self.other_project.id}"
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.post(url, {"title": "修改"}, "patch").status_code, 404)
        self.assertEqual(self.post(url, {"confirm_title": self.other_project.title, "password": self.password}, "delete").status_code, 404)
        self.assertEqual(self.client.get(url + "/export").status_code, 404)
        self.other_project.refresh_from_db()
        self.assertEqual(self.other_project.title, "BOB_SECRET_PROJECT")

    def test_foreign_conversation_and_parent_are_hidden(self):
        url = f"/api/conversations/{self.other_conversation.id}"
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.post(url, {"content": "侵入"}).status_code, 404)
        self.assertEqual(self.post("/api/conversations", {"title": "嵌入", "project_id": str(self.other_project.id)}).status_code, 404)
        self.assertEqual(self.client.get("/api/conversations", {"project_id": str(self.other_project.id)}).status_code, 404)

    def test_owner_in_payload_never_changes_ownership(self):
        response = self.post("/api/projects", {"title": "新的", "owner": self.bob.pk, "owner_id": self.bob.pk})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Project.objects.get(id=response.json()["project"]["id"]).owner_id, self.alice.pk)
        response = self.post("/api/memories", {"content": "新的记忆", "owner_id": self.bob.pk})
        self.assertEqual(Memory.objects.get(id=response.json()["memory"]["id"]).owner_id, self.alice.pk)

    def test_project_validation_and_patch(self):
        for bad in ["", " ", "a" * 121, 42, None]:
            self.assertEqual(self.post("/api/projects", {"title": bad}).status_code, 400)
        response = self.post(f"/api/projects/{self.project.id}", {"description": "更新"}, "patch")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["project"]["description"], "更新")

    def test_project_delete_requires_confirmation_and_retains_work(self):
        url = f"/api/projects/{self.project.id}"
        self.assertEqual(self.post(url, {}, "delete").status_code, 400)
        response = self.post(url, {"confirm_title": self.project.title, "password": self.password}, "delete")
        self.assertEqual(response.status_code, 204)
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.project_id)
        self.assertTrue(Task.objects.filter(id=self.task.id).exists())

    def test_new_conversation_uses_current_user_and_project(self):
        response = self.post("/api/conversations/", {"project_id": str(self.project.id)})
        self.assertEqual(response.status_code, 201)
        item = Conversation.objects.get(id=response.json()["conversation"]["id"])
        self.assertEqual(item.owner, self.alice)
        self.assertEqual(item.project, self.project)
        self.assertEqual(item.title, "新的任务")

    def test_save_message_creates_waiting_task_without_fake_reply(self):
        response = self.post(f"/api/conversations/{self.conversation.id}", {"content": "帮我制作一个文件"})
        self.assertEqual(response.status_code, 201)
        value = response.json()
        self.assertEqual(value["message"]["role"], "user")
        self.assertEqual(value["task"]["status"], "waiting")
        self.assertEqual(self.conversation.messages.filter(role="assistant").count(), 0)
        self.assertTrue(value["task"]["events"])
        self.assertEqual(Task.objects.get(id=value["task"]["id"]).owner, self.alice)

    def test_retry_after_a_lost_first_send_or_followup_ack_reuses_exact_message(self):
        first = {'content':'比较两种方案', 'client_message_id':str(uuid.uuid4())}
        result = self.post('/api/conversations', first)
        self.assertEqual(result.status_code, 201)
        self.assertEqual(self.post('/api/conversations', first).status_code, 200)
        conv = Conversation.objects.get(pk=result.json()['conversation']['id'])
        self.assertEqual((conv.tasks.count(), conv.messages.count()), (1, 1))
        followup = {'content':'补充预算', 'client_message_id':str(uuid.uuid4())}
        url = f'/api/conversations/{conv.id}'
        self.assertEqual(self.post(url, followup).status_code, 201)
        self.assertEqual(self.post(url, followup).status_code, 200)
        self.assertEqual(self.post(url, {**followup,'content':'其他问题'}).status_code, 409)
        self.assertEqual(self.post('/api/conversations', first).status_code, 409)
        self.assertEqual((conv.tasks.count(), conv.messages.count(), conv.tasks.first().events.count()), (1, 2, 2))

    def test_supplement_reuses_active_task_and_keeps_history(self):
        url = f"/api/conversations/{self.conversation.id}"
        first = self.post(url, {"content": "第一条需求"}).json()
        second = self.post(url, {"content": "补充第二条需求"}).json()
        self.assertEqual(first["task"]["id"], second["task"]["id"])
        task = Task.objects.get(id=second["task"]["id"])
        self.assertEqual(task.goal, "补充第二条需求")
        self.assertEqual(task.events.count(), 2)
        self.assertEqual(self.conversation.messages.filter(role="user").count(), 3)

    def test_running_task_rejects_supplement_atomically(self):
        Task.objects.create(owner=self.alice, conversation=self.conversation, goal="正在执行", status="running")
        before = self.conversation.messages.count()
        response = self.post(f"/api/conversations/{self.conversation.id}", {"content": "补充"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.conversation.messages.count(), before)

    def test_supplement_to_paused_task_does_not_resume_execution(self):
        task = Task.objects.create(owner=self.alice, conversation=self.conversation, goal="暂停前", status="paused")
        response = self.post(f"/api/conversations/{self.conversation.id}", {"content": "暂停时补充"})
        self.assertEqual(response.status_code, 201)
        task.refresh_from_db()
        self.assertEqual(task.status, "paused")
        self.assertEqual(task.goal, "暂停时补充")

    def test_stale_task_cannot_overwrite_new_running_execution(self):
        import uuid
        task = Task.objects.create(owner=self.alice, conversation=self.conversation, goal="旧需求", status="waiting")
        execution_id = uuid.uuid4()
        Task.objects.filter(id=task.id).update(status="running", execution_id=execution_id)
        with self.assertRaises(APIError) as error:
            revise_task(task, "新需求")
        self.assertEqual(error.exception.status, 409)
        task.refresh_from_db()
        self.assertEqual(task.status, "running")
        self.assertEqual(task.execution_id, execution_id)
        self.assertEqual(task.goal, "旧需求")

    def test_concurrent_task_change_rolls_back_the_saved_message(self):
        Task.objects.create(owner=self.alice, conversation=self.conversation, goal="旧需求", status="waiting")
        before = self.conversation.messages.count()
        with patch("workspace.views.revise_task", side_effect=APIError("状态已变化", 409)):
            response = self.post(f"/api/conversations/{self.conversation.id}", {"content": "冲突中的补充"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.conversation.messages.count(), before)

    def test_conversation_detail_includes_task_events_and_artifact_metadata(self):
        response = self.client.get(f"/api/conversations/{self.conversation.id}")
        value = response.json()
        self.assertEqual(value["tasks"][0]["events"][0]["content"], "已检查我的文件")
        self.assertEqual(value["tasks"][0]["artifacts"][0]["name"], "note.txt")
        self.assertNotIn("content", value["tasks"][0]["artifacts"][0])

    def test_foreign_memories_cannot_be_edited_or_deleted(self):
        url = f"/api/memories/{self.other_memory.id}"
        self.assertEqual(self.post(url, {"content": "更改"}, "patch").status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertTrue(Memory.objects.filter(id=self.other_memory.id).exists())

    def test_own_memory_update_and_delete_are_real(self):
        url = f"/api/memories/{self.memory.id}"
        self.assertEqual(self.post(url, {"content": "新记忆"}, "patch").status_code, 200)
        self.memory.refresh_from_db()
        self.assertEqual(self.memory.content, "新记忆")
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertFalse(Memory.objects.filter(id=self.memory.id).exists())

    def test_personal_export_is_complete_and_excludes_other_user_and_secrets(self):
        response = self.client.get("/api/data/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        content = response.content.decode()
        self.assertNotIn("BOB_SECRET", content)
        self.assertNotIn(self.alice.password, content)
        self.assertNotIn("password", content)
        self.assertNotIn("session_key", content)
        data = response.json()
        self.assertEqual(data["artifacts"][0]["content"], "我的成果内容")
        self.assertEqual(len(data["task_events"]), 1)
        self.assertEqual(len(data["memories"]), 1)

    def test_project_export_excludes_unrelated_personal_work_and_memory(self):
        extra = Conversation.objects.create(owner=self.alice, title="无项目的聊天")
        Message.objects.create(conversation=extra, role="user", content="不属于此项目")
        response = self.client.get(f"/api/projects/{self.project.id}/export")
        data = response.json()
        self.assertEqual(data["scope"], "project")
        self.assertEqual(len(data["conversations"]), 1)
        self.assertNotIn("memories", data)
        self.assertNotIn("不属于此项目", response.content.decode())

    def test_retention_policy_rejects_bool_string_and_unsupported_values(self):
        for bad in [True, False, "30", 1, -1, 31, None, []]:
            response = self.post("/api/data/policy", {"retention_days": bad})
            self.assertEqual(response.status_code, 400)
        self.assertFalse(DataPolicy.objects.filter(user=self.alice).exists())
        response = self.post("/api/data/policy", {"retention_days": 30, "user": self.bob.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DataPolicy.objects.get(user=self.alice).retention_days, 30)
        self.assertFalse(DataPolicy.objects.filter(user=self.bob).exists())
        self.assertEqual(response.json()["policy"]["execution"], "manual")

    def test_automatic_retention_requires_explicit_opt_in(self):
        url = '/api/data/policy'
        self.assertEqual(self.post(url, {'retention_days': 30, 'auto_cleanup': True}).status_code, 400)
        self.assertEqual(self.post(url, {'retention_days': 0, 'auto_cleanup': True, 'confirm_auto_cleanup': True}).status_code, 400)
        response = self.post(url, {'retention_days': 30, 'auto_cleanup': True, 'confirm_auto_cleanup': True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['policy']['auto_cleanup'])
        self.assertEqual(response.json()['policy']['execution'], 'automatic')
        self.assertEqual(self.post(url, {'retention_days': 30}).json()['policy']['execution'], 'manual')

    def test_deletion_request_needs_password_and_is_trackable_and_idempotent(self):
        url = "/api/data/deletion-requests"
        for data in [{}, {"confirm": True, "password": "wrong"}, {"password": self.password}]:
            self.assertEqual(self.post(url, data).status_code, 400)
        one = self.post(url, {"confirm": True, "password": self.password})
        two = self.post(url, {"confirm": True, "password": self.password})
        self.assertEqual(one.status_code, 201)
        self.assertEqual(two.status_code, 200)
        self.assertEqual(one.json()["deletion_request"]["id"], two.json()["deletion_request"]["id"])
        self.assertEqual(self.client.get(url).json()["deletion_requests"][0]["status"], "pending")
        self.assertTrue(get_user_model().objects.get(pk=self.alice.pk).is_active)
        self.assertTrue(Conversation.objects.filter(id=self.conversation.id).exists())

    def test_deletion_request_cancel_is_owner_scoped_and_transitions_once(self):
        own = DeletionRequest.objects.create(user=self.alice)
        other = DeletionRequest.objects.create(user=self.bob)
        self.assertEqual(self.post(f"/api/data/deletion-requests/{other.id}/cancel", {}).status_code, 404)
        url = f"/api/data/deletion-requests/{own.id}/cancel"
        self.assertEqual(self.post(url, {}).status_code, 200)
        self.assertEqual(self.post(url, {}).status_code, 409)
        own.refresh_from_db()
        self.assertEqual(own.status, "cancelled")
        self.assertIsNotNone(own.processed_at)
        self.assertEqual(DeletionRequest.objects.get(pk=other.pk).status, "pending")

    def test_bad_json_and_uuid_return_client_errors(self):
        self.assertEqual(self.client.post("/api/projects", "{", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post("/api/projects", "[]", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post("/api/projects", {"title": "form"}).status_code, 415)
        self.assertEqual(self.post("/api/conversations", {"project_id": "bad-id"}).status_code, 400)

    def test_csrf_is_required_for_browser_mutations(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.alice)
        response = browser.post("/api/projects", json.dumps({"title": "missing csrf"}), content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_retention_dry_run_does_not_delete_and_apply_is_scoped(self):
        DataPolicy.objects.create(user=self.alice, retention_days=30)
        DataPolicy.objects.create(user=self.bob, retention_days=30)
        old = timezone.now() - timedelta(days=60)
        Conversation.objects.filter(id__in=[self.conversation.id, self.other_conversation.id]).update(updated_at=old)
        output = StringIO()
        call_command("purge_expired", user_email=self.alice.email, stdout=output)
        self.assertIn("1", output.getvalue())
        self.assertTrue(Conversation.objects.filter(id=self.conversation.id).exists())
        call_command("purge_expired", apply=True, user_email=self.alice.email, stdout=StringIO())
        self.assertFalse(Conversation.objects.filter(id=self.conversation.id).exists())
        self.assertFalse(Task.objects.filter(id=self.task.id).exists())
        self.assertTrue(Conversation.objects.filter(id=self.other_conversation.id).exists())
        self.assertTrue(Project.objects.filter(id=self.project.id).exists())
        self.assertTrue(Memory.objects.filter(id=self.memory.id).exists())

    def test_retention_preserves_active_tasks_and_manual_policy(self):
        DataPolicy.objects.create(user=self.alice, retention_days=30)
        old = timezone.now() - timedelta(days=60)
        Conversation.objects.filter(id__in=[self.conversation.id, self.other_conversation.id]).update(updated_at=old)
        Task.objects.create(owner=self.alice, conversation=self.conversation, goal="未完成", status="paused")
        call_command("purge_expired", apply=True, stdout=StringIO())
        self.assertTrue(Conversation.objects.filter(id=self.conversation.id).exists())
        self.assertTrue(Conversation.objects.filter(id=self.other_conversation.id).exists())

    def test_automatic_cleanup_only_removes_opted_in_expired_conversations(self):
        from .deletion import record_fingerprint
        DataPolicy.objects.create(user=self.alice, retention_days=30, auto_cleanup=True)
        DataPolicy.objects.create(user=self.bob, retention_days=30)
        old = timezone.now() - timedelta(days=60)
        Conversation.objects.filter(id__in=[self.conversation.id, self.other_conversation.id]).update(updated_at=old)
        call_command('purge_expired', apply=True, automatic_only=True, stdout=StringIO())
        self.assertFalse(Conversation.objects.filter(id=self.conversation.id).exists())
        self.assertTrue(Conversation.objects.filter(id=self.other_conversation.id).exists())
        self.assertTrue(RecordDeletionReceipt.objects.filter(kind='conversation',
            object_digest=record_fingerprint('conversation', self.conversation.id)).exists())
