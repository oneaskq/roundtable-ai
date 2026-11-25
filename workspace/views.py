"""Owner-scoped APIs. Never accept an owner identifier from an HTTP client."""
import json
import uuid
from functools import wraps

from django.contrib.auth import get_user_model, logout as auth_logout
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from delivery.acceptance_cases import case_revision, normalize_cases
from delivery.execution import readiness
from delivery.provider import ProviderError

from .deletion import account_fingerprint, identity_key_id, record_deletion
from .personal_memory import enabled as memory_enabled, learn_turn
from .models import AccountDeletionReceipt, Artifact, ComposerDraft, Conversation, DataPolicy, DeletionRequest, Memory, Message, Project, RecordDeletionReceipt, SourceMaterial, Task, TaskEvent


class APIError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


def api_required(*methods):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                response = JsonResponse({"error": "此操作不支持当前请求方式。"}, status=405)
                response["Allow"] = ", ".join(methods)
                return response
            if not request.user.is_authenticated:
                return JsonResponse({"error": "请先登录。"}, status=401)
            try:
                return view(request, *args, **kwargs)
            except APIError as exc:
                return JsonResponse({"error": exc.message}, status=exc.status)
            except Http404:
                return JsonResponse({"error": "内容不存在或无权访问。"}, status=404)
            except ValidationError:
                return JsonResponse({"error": "提交的内容格式不正确。"}, status=400)
            except (IntegrityError, OperationalError):
                return JsonResponse({"error": "状态已发生变化，请刷新后重试。"}, status=409)
        return wrapped
    return decorate


def payload(request):
    if request.content_type != "application/json":
        raise APIError("请提交 JSON 格式的数据。", 415)
    try:
        value = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise APIError("请求内容不是有效的 JSON。")
    if not isinstance(value, dict):
        raise APIError("请求内容必须是 JSON 对象。")
    return value


def string_value(data, key, max_length, *, default=None, allow_blank=False):
    value = data.get(key, default)
    if not isinstance(value, str):
        raise APIError(f"{key} 必须是文字。")
    value = value.strip()
    if not value and not allow_blank:
        raise APIError("内容不能为空。")
    if len(value) > max_length:
        raise APIError(f"内容超过 {max_length} 字限制。")
    return value


def parse_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise APIError("记录编号格式不正确。")


def timestamp(value):
    return value.isoformat() if value else None


def serialize_project(project):
    return {"id": str(project.id), "title": project.title, "description": project.description,
            "created_at": timestamp(project.created_at), "updated_at": timestamp(project.updated_at)}


def serialize_conversation(conversation):
    from .sources import sources_revision
    sources = list(conversation.sources.filter(owner=conversation.owner))
    return {"id": str(conversation.id), "title": conversation.title,
            "sources_revision": sources_revision(sources), "sources_count": len(sources),
            "included_sources_count": sum(source.included for source in sources),
            "acceptance_cases": conversation.acceptance_cases,
            "acceptance_revision": case_revision(conversation.acceptance_cases),
            "project_id": str(conversation.project_id) if conversation.project_id else None,
            "created_at": timestamp(conversation.created_at), "updated_at": timestamp(conversation.updated_at)}


def serialize_message(message):
    return {"id": str(message.id), "role": message.role, "content": message.content,
            "created_at": timestamp(message.created_at),
            "edited_from": str(message.edited_from_id) if message.edited_from_id else None,
            "superseded_by": str(message.superseded_by_id) if message.superseded_by_id else None}


def serialize_memory(memory):
    return {"id": str(memory.id), "content": memory.content,
            "source": "conversation" if memory.source_turn else "manual",
            "created_at": timestamp(memory.created_at), "updated_at": timestamp(memory.updated_at)}


def serialize_event(event):
    return {"id": str(event.id), "kind": event.kind, "content": event.content,
            "created_at": timestamp(event.created_at)}


def serialize_artifact(artifact, include_content=False):
    value = {"id": str(artifact.id), "task_id": str(artifact.task_id), "name": artifact.name,
             "created_at": timestamp(artifact.created_at), "kind": artifact.kind,
             "revision": artifact.revision, "review_status": artifact.review_status,
             "file_count": len(artifact.files) if isinstance(artifact.files, list) else 0}
    if include_content:
        value["content"] = artifact.content
        value["files"] = artifact.files
        value["validation"] = artifact.validation
    return value


def serialize_task(task, detail=False):
    value = {"id": str(task.id), "conversation_id": str(task.conversation_id), "goal": task.goal,
             "status": task.status, "created_at": timestamp(task.created_at), "updated_at": timestamp(task.updated_at),
             "superseded_by": str(task.superseded_by_message_id) if task.superseded_by_message_id else None}
    if detail:
        from delivery.team_views import serialize_run
        value["runs"] = [serialize_run(run) for run in task.runs.prefetch_related('steps')]
        value["events"] = [serialize_event(e) for e in task.events.all()]
        value["artifacts"] = [serialize_artifact(a) for a in task.artifacts.filter(owner=task.owner)]
    return value


def serialize_policy(policy=None):
    automatic = bool(policy and policy.auto_cleanup and policy.retention_days)
    return {"retention_days": policy.retention_days if policy else 0, "choices": [0, 30, 90, 365],
            "scope": "会话及其草稿、消息、任务和成果；新对话草稿、记忆与项目需单独管理。",
            "auto_cleanup": automatic, "execution": "automatic" if automatic else "manual",
            "notice": ("已选择自动清理；后台服务运行时会检查超期对话，进行中的任务会保留。历史备份和外部副本需另行处理。"
                       if automatic else "当前为手动清理；保留期限不会自动删除数据。")}


def serialize_deletion_request(item):
    return {"id": str(item.id), "status": item.status, "requested_at": timestamp(item.requested_at),
            "processed_at": timestamp(item.processed_at)}


def revise_task(task, content):
    """Do not overwrite a model request that claimed this task after our read."""
    target_status = Task.Status.PAUSED if task.status == Task.Status.PAUSED else Task.Status.WAITING
    # updated_at is also a consent revision. Two edits in one clock tick must
    # not reuse that revision, and a stale reader must not overwrite a newer one.
    revised_at = max(timezone.now(), task.updated_at + timezone.timedelta(microseconds=1))
    changed = Task.objects.filter(
        id=task.id, owner_id=task.owner_id, status=task.status, execution_id=task.execution_id,
        updated_at=task.updated_at,
    ).update(goal=content, status=target_status, execution_id=None, updated_at=revised_at)
    if not changed:
        raise APIError("任务状态刚刚发生变化，请刷新后再补充。", 409)
    task.refresh_from_db()


def lock_conversation(owner, conversation_id):
    """Lock before reading mutable consent, including on SQLite.

    Call inside transaction.atomic(). A no-op write acquires SQLite's write
    lock; select_for_update also serializes this row on server databases.
    """
    if not Conversation.objects.filter(id=conversation_id, owner=owner).update(title=F('title')):
        raise Http404
    return Conversation.objects.select_for_update().get(id=conversation_id, owner=owner)


def acceptance_execution_state():
    available = 'python_stdio' in readiness()['available_tools']
    return {'execution_available': available,
            'notice': ('输入输出执行环境已就绪；保存不会运行代码或调用模型，验收只覆盖已保存用例。'
                       if available else
                       '输入输出执行环境尚未就绪；用例可保存，准备运行环境后才能验收。保存不会运行代码或调用模型。')}


@api_required("GET", "PUT")
def acceptance_cases(request, conversation_id):
    conversation = get_object_or_404(Conversation, id=conversation_id, owner=request.user)
    if request.method == "GET":
        return JsonResponse({"cases": conversation.acceptance_cases,
                             "revision": case_revision(conversation.acceptance_cases),
                             **acceptance_execution_state()})
    data = payload(request)
    try:
        cases = normalize_cases(data.get('cases'))
    except ProviderError as error:
        raise APIError(str(error)) from None
    expected = data.get('expected_revision')
    with transaction.atomic():
        conversation = lock_conversation(request.user, conversation_id)
        revision = case_revision(conversation.acceptance_cases)
        if not isinstance(expected, str) or not constant_time_compare(expected, revision):
            raise APIError('验收用例已改变或尚未确认当前版本，请刷新后重试。', 409)
        if conversation.tasks.filter(owner=request.user, status=Task.Status.RUNNING).exists():
            raise APIError('任务正在处理，验收用例不能修改；请先暂停或等待本轮结束。', 409)
        if cases != conversation.acceptance_cases:
            from delivery.decision_lifecycle import invalidate_waiting_decisions
            invalidate_waiting_decisions(conversation, request.user, '验收条件已改变，请重新整理问题。')
        conversation.acceptance_cases = cases
        conversation.save(update_fields=['acceptance_cases', 'updated_at'])
    return JsonResponse({"cases": cases, "revision": case_revision(cases),
                         "conversation": serialize_conversation(conversation),
                         **acceptance_execution_state()})


@api_required("GET", "POST")
def projects(request):
    if request.method == "GET":
        return JsonResponse({"projects": [serialize_project(p) for p in Project.objects.filter(owner=request.user)]})
    data = payload(request)
    project = Project.objects.create(owner=request.user, title=string_value(data, "title", 120),
                                     description=string_value(data, "description", 4000, default="", allow_blank=True))
    return JsonResponse({"project": serialize_project(project)}, status=201)


@api_required("GET", "PATCH", "DELETE")
def project_detail(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    if request.method == "GET":
        return JsonResponse({"project": serialize_project(project), "conversations": [
            serialize_conversation(c) for c in project.conversations.filter(owner=request.user)]})
    data = payload(request)
    if request.method == "DELETE":
        if data.get("confirm_title") != project.title or not isinstance(data.get("password"), str) or not request.user.check_password(data["password"]):
            raise APIError("请填写当前密码和完整项目名称以确认删除。")
        # Removing a container retains its conversations as ungrouped work.
        with transaction.atomic():
            project = get_object_or_404(Project.objects.select_for_update(), id=project_id, owner=request.user)
            record_deletion('project', project.id)
            project.delete()
        return HttpResponse(status=204)
    if "title" in data:
        project.title = string_value(data, "title", 120)
    if "description" in data:
        project.description = string_value(data, "description", 4000, allow_blank=True)
    project.save()
    return JsonResponse({"project": serialize_project(project)})


@api_required("GET", "POST")
def composer_drafts(request):
    if request.method == 'GET':
        drafts = ComposerDraft.objects.filter(owner=request.user).exclude(content='').select_related('conversation')
        return JsonResponse({'drafts': [{'conversation_id': str(d.conversation_id) if d.conversation_id else None,
                                         'project_id': str(d.project_id) if d.project_id else None,
                                         'content': d.content, 'updated_at': timestamp(d.updated_at)}
                                        for d in drafts if not d.conversation_id or d.conversation.owner_id == request.user.id]})
    data = payload(request)
    if set(data) != {'conversation_id', 'project_id', 'content', 'writer_id', 'sequence'}:
        raise APIError('草稿内容格式不正确。')
    content = data['content']
    if not isinstance(content, str) or len(content) > 12000:
        raise APIError('草稿内容超过允许长度。')
    try:
        content.encode('utf-8')
        writer_id = uuid.UUID(data['writer_id'])
    except (UnicodeError, ValueError, TypeError, AttributeError):
        raise APIError('草稿内容格式不正确。') from None
    sequence = data['sequence']
    if type(sequence) is not int or not 1 <= sequence <= 9007199254740991:
        raise APIError('草稿保存编号无效。')
    for field in ('conversation_id', 'project_id'):
        if data[field] is not None and (not isinstance(data[field], str) or not data[field]):
            raise APIError('草稿关联编号无效。')
    conversation_id = parse_uuid(data['conversation_id']) if data['conversation_id'] else None
    project_id = parse_uuid(data['project_id']) if data['project_id'] else None
    with transaction.atomic():
        User = get_user_model()
        if not User.objects.filter(pk=request.user.pk, is_active=True).update(is_active=F('is_active')):
            raise APIError('账号不可用。', 401)
        conversation = lock_conversation(request.user, conversation_id) if conversation_id else None
        if conversation and project_id:
            raise APIError('已存在的对话不支持更改草稿项目。')
        project = get_object_or_404(Project, id=project_id, owner=request.user) if project_id else None
        draft, _ = ComposerDraft.objects.select_for_update().get_or_create(
            owner=request.user, conversation=conversation)
        if draft.writer_id == writer_id and sequence <= draft.sequence:
            return JsonResponse({'saved': False, 'stale': True, 'sequence': draft.sequence})
        draft.content, draft.project, draft.writer_id, draft.sequence = content, project, writer_id, sequence
        draft.save(update_fields=['content', 'project', 'writer_id', 'sequence', 'updated_at'])
        if conversation and content:
            # Editing an old conversation makes it active for retention purposes.
            conversation.save(update_fields=['updated_at'])
    return JsonResponse({'saved': True})


@api_required("GET", "POST")
def conversations(request):
    if request.method == "GET":
        queryset = Conversation.objects.filter(owner=request.user)
        if request.GET.get("project_id"):
            project = get_object_or_404(Project, id=parse_uuid(request.GET["project_id"]), owner=request.user)
            queryset = queryset.filter(project=project)
        query = request.GET.get("q", "").strip()
        if query:
            if len(query) > 200:
                raise APIError("搜索词最多 200 字。")
            messages = Message.objects.filter(conversation_id=OuterRef("pk"), superseded_by__isnull=True,
                                              content__icontains=query)
            matches = queryset.filter(Q(title__icontains=query) | Exists(messages))
            return JsonResponse({"conversations": list(matches.values("id", "title")[:50])})
        return JsonResponse({"conversations": [serialize_conversation(c) for c in queryset]})
    data = payload(request)
    project = None
    if data.get("project_id"):
        project = get_object_or_404(Project, id=parse_uuid(data["project_id"]), owner=request.user)
    if 'content' in data:
        content = string_value(data, 'content', 30000)
        message_id = parse_uuid(data.get('client_message_id'))
        with transaction.atomic():
            # The first send creates the conversation and question together.
            # A lost HTTP acknowledgement can be retried with the same UUID.
            conversation, created = Conversation.objects.get_or_create(pk=message_id, owner=request.user,
                                                                        defaults={'project': project})
            if (conversation.project_id != (project.pk if project else None)
                    or (not created and not conversation.messages.filter(pk=message_id).exists())):
                raise APIError("提交内容已改变，请重新发送。", 409)
            return _save_question(request.user, conversation, content, message_id)
    conversation = Conversation.objects.create(owner=request.user, project=project,
                                               title=string_value(data, "title", 120, default="新的任务"))
    return JsonResponse({"conversation": serialize_conversation(conversation)}, status=201)


@api_required("GET", "POST", "DELETE")
def conversation_detail(request, conversation_id):
    conversation = get_object_or_404(Conversation, id=conversation_id, owner=request.user)
    if request.method == "GET":
        # Progress remains truthful even if the separate queue worker is down.
        from delivery.background import expire_queued_runs
        expire_queued_runs(conversation_id=conversation.id)
        return JsonResponse({"conversation": serialize_conversation(conversation),
                             "messages": [serialize_message(m) for m in conversation.messages.all()],
                             "tasks": [serialize_task(t, detail=True) for t in conversation.tasks.filter(owner=request.user)]})
    if request.method == "DELETE":
        from accounts.models import LoginAttempt
        from accounts.views import attempt_key, blocked, count_attempt
        from delivery.models import TeamRun
        data = payload(request)
        if set(data) != {"password", "confirmation"} or data.get("confirmation") != "删除此对话":
            raise APIError("请输入完整的确认文字。")
        password = data.get("password")
        if not isinstance(password, str) or not password or len(password) > 1024:
            raise APIError("请填写当前密码。")
        key = attempt_key("account-deletion", str(request.user.pk))
        if blocked(key):
            raise APIError("尝试次数过多，请 15 分钟后再试。", 429)
        User = get_user_model()
        with transaction.atomic():
            if not User.objects.filter(pk=request.user.pk, is_active=True).update(is_active=F("is_active")):
                raise APIError("账号不可用。", 403)
            user = User.objects.select_for_update().get(pk=request.user.pk, is_active=True)
            if not user.check_password(password):
                count_attempt(key)
                return JsonResponse({"error": "当前密码不正确。"}, status=400)
            conversation = lock_conversation(user, conversation_id)
            if conversation.tasks.filter(owner=user, status=Task.Status.RUNNING).exists() or \
                    TeamRun.objects.filter(task__conversation=conversation, task__owner=user,
                                           status__in=["queued", "running"]).exists():
                raise APIError("此对话仍有任务排队或处理中，请先等待结束或暂停。", 409)
            LoginAttempt.objects.filter(key=key).delete()
            record_deletion('conversation', conversation.id)
            conversation.delete()
        return HttpResponse(status=204)
    data = payload(request)
    content = string_value(data, "content", 30000)
    message_id = parse_uuid(data['client_message_id']) if 'client_message_id' in data else None
    return _save_question(request.user, conversation, content, message_id)


def _save_question(owner, conversation, content, message_id):
    with transaction.atomic():
        conversation = lock_conversation(owner, conversation.id)
        if message_id:
            previous = Message.objects.filter(pk=message_id, conversation=conversation).first()
            if previous:
                latest = conversation.messages.filter(role=Message.Role.USER, superseded_by__isnull=True).order_by('-created_at', '-id').first()
                task = conversation.tasks.filter(owner=owner, superseded_by_message__isnull=True).first()
                if (not latest or latest.id != previous.id or previous.role != Message.Role.USER
                        or previous.content != content or not task):
                    raise APIError("此消息已改变，请刷新对话后继续。", 409)
                return JsonResponse({"conversation": serialize_conversation(conversation), "message": serialize_message(previous),
                                     "task": serialize_task(task, detail=True)})
        task = conversation.tasks.filter(owner=owner, status__in=["draft", "waiting", "running", "paused"]).first()
        if task and task.status == Task.Status.RUNNING:
            raise APIError("当前任务正在处理，请等待本次处理结束后补充。", 409)
        from delivery.report_followups import acknowledged_task
        acknowledged = acknowledged_task(conversation, content) if not task else None
        message = Message.objects.create(**({'id': message_id} if message_id else {}),
                                         conversation=conversation, role=Message.Role.USER, content=content)
        learn_turn(conversation, message)
        if acknowledged:
            conversation.save(update_fields=['updated_at'])
            return JsonResponse({"conversation": serialize_conversation(conversation), "message": serialize_message(message),
                                 "task": serialize_task(acknowledged, detail=True)}, status=201)
        from delivery.decision_lifecycle import invalidate_waiting_decisions
        invalidate_waiting_decisions(conversation, owner, '需求已补充，请重新整理问题。')
        if task:
            revise_task(task, content)
        else:
            task = Task.objects.create(owner=owner, conversation=conversation, goal=content, status=Task.Status.WAITING)
        event_content = ("补充已保存，任务保持暂停；恢复后再处理。" if task.status == Task.Status.PAUSED
                         else "需求已保存，等待执行。保存消息不会自动调用模型。")
        TaskEvent.objects.create(task=task, kind="saved", content=event_content)
        if conversation.title == "新的任务":
            conversation.title = content[:60]
        conversation.save(update_fields=["title", "updated_at"])
    return JsonResponse({"conversation": serialize_conversation(conversation), "message": serialize_message(message),
                         "task": serialize_task(task, detail=True)}, status=201)


@api_required("POST")
def edit_message(request, message_id):
    """Branch from a user message, keeping the prior exchange in history."""
    data = payload(request)
    if set(data) != {'content', 'expected_content', 'client_edit_id'}:
        raise APIError('编辑内容不完整，请刷新后重试。')
    content = string_value(data, 'content', 12000)
    expected = string_value(data, 'expected_content', 30000)
    edit_id = parse_uuid(data['client_edit_id'])
    original = get_object_or_404(Message, pk=message_id, conversation__owner=request.user, role=Message.Role.USER)
    with transaction.atomic():
        conversation = lock_conversation(request.user, original.conversation_id)
        original = get_object_or_404(Message, pk=message_id, conversation=conversation, role=Message.Role.USER)
        existing = Message.objects.filter(edit_request_id=edit_id).first()
        if existing:
            if (existing.conversation_id != conversation.id or existing.edited_from_id != original.id
                    or existing.content != content or original.content != expected):
                raise APIError('编辑编号已使用，请刷新后重试。', 409)
            task = Task.objects.filter(edit_message=existing, owner=request.user).first()
            if not task:
                raise APIError('编辑记录已变化，请刷新对话。', 409)
            return JsonResponse({'replayed': True, 'conversation': serialize_conversation(conversation),
                                 'message': serialize_message(existing), 'task': serialize_task(task, detail=True)})
        if original.superseded_by_id or original.content != expected:
            raise APIError('这条消息已被修改，请刷新后重新编辑。', 409)
        if content == original.content:
            raise APIError('消息没有变化。')
        if conversation.tasks.filter(owner=request.user, status=Task.Status.RUNNING).exists():
            raise APIError('团队正在处理，请先停止本轮再编辑消息。', 409)
        from delivery.decision_lifecycle import invalidate_waiting_decisions
        invalidate_waiting_decisions(conversation, request.user, '用户编辑了消息，请重新整理问题。')
        new_message = Message.objects.create(conversation=conversation, role=Message.Role.USER,
            content=content, edited_from=original, edit_request_id=edit_id)
        later = Q(created_at__gt=original.created_at) | Q(created_at=original.created_at, id__gte=original.id)
        Message.objects.filter(conversation=conversation, superseded_by__isnull=True).filter(later).exclude(
            pk=new_message.pk).update(superseded_by=new_message)
        affected = Task.objects.filter(conversation=conversation, owner=request.user, superseded_by_message__isnull=True).filter(
            Q(updated_at__gte=original.created_at) | Q(status__in=['draft', 'waiting', 'paused']))
        affected.filter(status__in=['draft', 'waiting', 'paused']).update(status=Task.Status.CANCELLED, execution_id=None,
                                                                             updated_at=timezone.now())
        affected.update(superseded_by_message=new_message)
        from delivery.models import TeamInterjection
        superseded_ids = Message.objects.filter(superseded_by=new_message).values('id')
        note_ids = TeamInterjection.objects.filter(run__task__superseded_by_message=new_message).values('id')
        Memory.objects.filter(owner=request.user, source_conversation=conversation).filter(
            Q(source_turn__in=superseded_ids) | Q(source_turn__in=note_ids)).delete()
        learn_turn(conversation, new_message)
        task = Task.objects.create(owner=request.user, conversation=conversation, goal=content,
                                   status=Task.Status.WAITING, edit_message=new_message)
        TaskEvent.objects.create(task=task, kind='edited', content='消息已修改；编辑前的对话和结果保留在历史中。')
        if conversation.title == original.content[:60]:
            conversation.title = content[:60]
            conversation.save(update_fields=['title', 'updated_at'])
        else:
            conversation.save(update_fields=['updated_at'])
        return JsonResponse({'conversation': serialize_conversation(conversation), 'message': serialize_message(new_message),
                             'task': serialize_task(task, detail=True)}, status=201)


@api_required("GET", "POST", "PATCH")
def memories(request):
    if request.method == "GET":
        return JsonResponse({"enabled": memory_enabled(request.user.pk),
                             "memories": [serialize_memory(m) for m in Memory.objects.filter(owner=request.user)]})
    if request.method == "PATCH":
        data = payload(request)
        if set(data) != {'enabled'} or type(data['enabled']) is not bool:
            raise APIError("请选择是否开启跨对话记忆。")
        DataPolicy.objects.update_or_create(user=request.user, defaults={'cross_chat_memory': data['enabled']})
        return JsonResponse({'enabled': data['enabled']})
    content = string_value(payload(request), "content", 6000)
    memory = Memory.objects.create(owner=request.user, content=content)
    return JsonResponse({"memory": serialize_memory(memory)}, status=201)


@api_required("PATCH", "DELETE")
def memory_detail(request, memory_id):
    memory = get_object_or_404(Memory, id=memory_id, owner=request.user)
    if request.method == "DELETE":
        with transaction.atomic():
            memory = get_object_or_404(Memory.objects.select_for_update(), id=memory_id, owner=request.user)
            record_deletion('memory', memory.id)
            memory.delete()
        return HttpResponse(status=204)
    memory.content = string_value(payload(request), "content", 6000)
    memory.source_turn = None
    memory.source_conversation = None
    memory.save(update_fields=["content", "source_turn", "source_conversation", "updated_at"])
    return JsonResponse({"memory": serialize_memory(memory)})


@api_required("GET")
def overview(request):
    from delivery.models import QualityRound
    user = request.user
    return JsonResponse({
        "account": {"email": user.email, "display_name": getattr(user, "display_name", "")},
        "counts": {"projects": Project.objects.filter(owner=user).count(),
                   "conversations": Conversation.objects.filter(owner=user).count(),
                   "messages": Message.objects.filter(conversation__owner=user).count(),
                   "tasks": Task.objects.filter(owner=user, conversation__owner=user).count(),
                   "artifacts": Artifact.objects.filter(owner=user, task__owner=user, task__conversation__owner=user).count(),
                   "memories": Memory.objects.filter(owner=user).count(),
                   "quality_rounds": QualityRound.objects.filter(owner=user).count()},
        "policy": serialize_policy(DataPolicy.objects.filter(user=user).first()),
        "deletion_requests": [serialize_deletion_request(d) for d in DeletionRequest.objects.filter(user=user)],
        "account_deletion": {"available": not user.is_superuser or get_user_model().objects.filter(
            is_superuser=True, is_active=True).exclude(pk=user.pk).exists(),
            "backup_status": "manual_pending",
            "backup_notice": "注销后会保留不含账号内容的核对记录。历史备份、已导出文件和外部模型已收到的数据不会自动删除；恢复旧备份前需处理已注销账号。"},
        "notice": "这里只显示你的个人数据。注销申请当前由维护人员处理，提交申请不会立即删除账号。",
    })


def export_data(user, project=None):
    from .sources import serialize_source
    from delivery.models import TeamRun
    from delivery.team_views import serialize_run
    conversations_query = Conversation.objects.filter(owner=user)
    projects_query = Project.objects.filter(owner=user)
    if project is not None:
        conversations_query = conversations_query.filter(project=project)
        projects_query = projects_query.filter(id=project.id)
    tasks_query = Task.objects.filter(owner=user, conversation__in=conversations_query)
    messages = [{**serialize_message(m), "conversation_id": str(m.conversation_id)}
                for m in Message.objects.filter(conversation__in=conversations_query)]
    events = [{**serialize_event(e), "task_id": str(e.task_id)} for e in TaskEvent.objects.filter(task__in=tasks_query)]
    result = {
        "schema_version": 1, "exported_at": timestamp(timezone.now()), "scope": "project" if project else "personal",
        "account": {"email": user.email, "display_name": getattr(user, "display_name", "")},
        "projects": [serialize_project(p) for p in projects_query],
        "conversations": [serialize_conversation(c) for c in conversations_query],
        "messages": messages, "tasks": [serialize_task(t) for t in tasks_query], "task_events": events,
        "team_runs": [serialize_run(run) for run in TeamRun.objects.filter(task__in=tasks_query).prefetch_related('steps')],
        "artifacts": [serialize_artifact(a, include_content=True) for a in Artifact.objects.filter(owner=user, task__in=tasks_query)],
        "sources": [dict(serialize_source(source, include_content=True), conversation_id=str(source.conversation_id))
                    for source in SourceMaterial.objects.filter(owner=user, conversation__in=conversations_query)],
        "drafts": [{'conversation_id': str(d.conversation_id) if d.conversation_id else None,
                    'project_id': str(d.project_id) if d.project_id else None, 'content': d.content,
                    'updated_at': timestamp(d.updated_at)}
                   for d in ComposerDraft.objects.filter(owner=user).exclude(content='').filter(
                       Q(conversation__in=conversations_query) | Q(conversation__isnull=True, project__in=projects_query)
                       if project else Q(conversation__in=conversations_query) | Q(conversation__isnull=True))],
    }
    if project is None:
        from delivery.models import QualityRound
        from delivery.quality_views import round_detail as quality_round_detail
        result["memories"] = [serialize_memory(m) for m in Memory.objects.filter(owner=user)]
        result["quality_rounds"] = [quality_round_detail(item, export=True)
                                    for item in QualityRound.objects.filter(owner=user).prefetch_related('samples')]
        result["data_policy"] = serialize_policy(DataPolicy.objects.filter(user=user).first())
        result["deletion_requests"] = [serialize_deletion_request(d) for d in DeletionRequest.objects.filter(user=user)]
    return result


def export_response(data, prefix):
    response = JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})
    response["Content-Disposition"] = f'attachment; filename="{prefix}-{timezone.localdate().isoformat()}.json"'
    response["Cache-Control"] = "no-store"
    return response


@api_required("GET")
def personal_export(request):
    return export_response(export_data(request.user), "roundtable-personal-data")


@api_required("GET")
def project_export(request, project_id):
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    return export_response(export_data(request.user, project), "roundtable-project")


@api_required("GET", "POST")
def policy(request):
    if request.method == "GET":
        return JsonResponse({"policy": serialize_policy(DataPolicy.objects.filter(user=request.user).first())})
    data = payload(request)
    days = data.get("retention_days")
    if type(days) is not int or days not in {0, 30, 90, 365}:
        raise APIError("保留期限仅支持 0、30、90 或 365 天。")
    automatic = data.get("auto_cleanup", False)
    if type(automatic) is not bool or (automatic and (days == 0 or data.get("confirm_auto_cleanup") is not True)):
        raise APIError("开启自动清理前，请选择期限并确认删除范围。")
    User = get_user_model()
    with transaction.atomic():
        if not User.objects.filter(pk=request.user.pk, is_active=True).update(is_active=F("is_active")):
            raise APIError("账号不可用。", 403)
        item, _ = DataPolicy.objects.update_or_create(user=request.user, defaults={"retention_days": days, "auto_cleanup": automatic})
    return JsonResponse({"policy": serialize_policy(item)})


@api_required("GET", "POST")
def deletion_requests(request):
    if request.method == "GET":
        return JsonResponse({"deletion_requests": [serialize_deletion_request(d) for d in DeletionRequest.objects.filter(user=request.user)]})
    from accounts.models import LoginAttempt
    from accounts.views import attempt_key, blocked, count_attempt
    data = payload(request)
    password = data.get("password")
    if data.get("confirm") is not True or not isinstance(password, str) or not password or len(password) > 1024:
        raise APIError("请明确确认注销申请并填写正确的当前密码。")
    key = attempt_key("account-deletion", str(request.user.pk))
    if blocked(key):
        raise APIError("尝试次数过多，请 15 分钟后再试。", 429)
    if not request.user.check_password(password):
        count_attempt(key)
        raise APIError("请明确确认注销申请并填写正确的当前密码。")
    LoginAttempt.objects.filter(key=key).delete()
    with transaction.atomic():
        item, created = DeletionRequest.objects.get_or_create(user=request.user, status=DeletionRequest.Status.PENDING)
    return JsonResponse({"deletion_request": serialize_deletion_request(item),
                         "notice": "已记录申请，待人工处理；尚未删除账号和数据。"}, status=201 if created else 200)


@api_required("POST")
def delete_account(request):
    """Delete only the authenticated account in the live DB, never backup files."""
    from accounts.models import DeviceSession, LoginAttempt
    from accounts.views import attempt_key, blocked, count_attempt
    from delivery.models import TeamRun

    data = payload(request)
    if set(data) != {"password", "confirmation", "acknowledge_backup"} or \
            data.get("confirmation") != "删除我的账号" or data.get("acknowledge_backup") is not True:
        raise APIError("请确认删除范围，并输入完整确认文字。")
    password = data.get("password")
    if not isinstance(password, str) or not password or len(password) > 1024:
        raise APIError("请填写当前密码。")
    key = attempt_key("account-deletion", str(request.user.pk))
    if blocked(key):
        raise APIError("尝试次数过多，请 15 分钟后再试。", 429)
    User = get_user_model()
    with transaction.atomic():
        # The collaborator claims the same user lock before queueing work.
        if not User.objects.filter(pk=request.user.pk, is_active=True).update(is_active=F("is_active")):
            raise APIError("账号不可用。", 403)
        user = User.objects.select_for_update().get(pk=request.user.pk, is_active=True)
        if not user.check_password(password):
            # Return instead of raising inside atomic so the rate limit is saved.
            count_attempt(key)
            return JsonResponse({"error": "当前密码不正确。"}, status=400)
        if user.is_superuser:
            administrators = list(User.objects.select_for_update().filter(is_superuser=True, is_active=True)
                                  .order_by("pk").values_list("pk", flat=True))
            if len(administrators) < 2:
                raise APIError("这是最后一个管理员账号，请先设置另一名管理员。", 409)
        if Task.objects.filter(owner=user, status=Task.Status.RUNNING).exists() or \
                TeamRun.objects.filter(task__owner=user, status__in=["queued", "running"]).exists():
            raise APIError("仍有任务排队或处理中，请先等待结束或暂停，再删除账号。", 409)
        receipt, _ = AccountDeletionReceipt.objects.get_or_create(
            identity_digest=account_fingerprint(user.pk, user.date_joined), defaults={"identity_key_id": identity_key_id()})
        if receipt.identity_key_id != identity_key_id():
            raise APIError("注销核对记录的密钥不一致，请先处理备份。", 409)
        sessions = DeviceSession.objects.filter(user=user)
        Session.objects.filter(session_key__in=sessions.values_list("session_key", flat=True)).delete()
        keys = [attempt_key(scope, str(user.pk)) for scope in (
            "account-deletion", "password-change", "mfa", "mfa-setup", "mfa-enable", "mfa-recovery-rotate")]
        keys.append(attempt_key("login-email", user.email))
        LoginAttempt.objects.filter(key__in=keys).delete()
        user.delete()
    auth_logout(request)
    return JsonResponse({"ok": True, "receipt": str(receipt.id), "backup_status": "pending",
        "notice": "当前数据库中的账号及关联记录已删除；历史备份、导出文件及外部服务仍需分别处理。"})


@api_required("GET")
def deletion_backup_status(request):
    if not request.user.is_superuser:
        raise APIError("只有管理员可以查看注销备份处理情况。", 403)
    pending = AccountDeletionReceipt.objects.filter(backup_status="pending").order_by("-deleted_at")
    return JsonResponse({"pending_count": pending.count(),
        "record_receipts_count": RecordDeletionReceipt.objects.count(),
        "receipts": [{"id": str(item.id), "deleted_at": timestamp(item.deleted_at)} for item in pending[:100]],
        "notice": "清单包含账号及新版记录的单项删除；旧备份、之前的单项删除、其他副本和外部服务仍需分别处理。"})


@api_required("GET")
def deletion_ledger_download(request):
    if not request.user.is_superuser:
        raise APIError("只有管理员可以导出注销核对清单。", 403)
    from operations.backups import BackupError
    from operations.deletion_backups import serialize_ledger
    try:
        token, _ = serialize_ledger()
    except BackupError as error:
        raise APIError(str(error), 503) from None
    response = HttpResponse(token, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="roundtable-deletion-ledger.txt"'
    response["Cache-Control"] = "no-store"
    return response


@api_required("POST")
def cancel_deletion(request, request_id):
    with transaction.atomic():
        item = get_object_or_404(DeletionRequest.objects.select_for_update(), id=request_id, user=request.user)
        if item.status != DeletionRequest.Status.PENDING:
            raise APIError("这条申请已结束，无法再次取消。", 409)
        item.status = DeletionRequest.Status.CANCELLED
        item.processed_at = timezone.now()
        item.save(update_fields=["status", "processed_at"])
    return JsonResponse({"deletion_request": serialize_deletion_request(item)})
