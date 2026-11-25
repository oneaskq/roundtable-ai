import json
import os
import uuid
from functools import wraps
from urllib.parse import quote

from django.db import IntegrityError, OperationalError, transaction
from django.conf import settings
from django.db.models import F
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from workspace.models import Artifact, Conversation, Message, Task, TaskEvent
from workspace.views import lock_conversation
from .catalog import capability_catalog, chat_available, connection_revision, connection_summary, current_connection
from .provider import ProviderError, generate_reply
from .models import AgentStep, RequestBudget, TeamRun


def api(method):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return JsonResponse({'error': '请先登录。'}, status=401)
            if request.method != method:
                return JsonResponse({'error': '请求方式不支持。'}, status=405)
            try:
                request.payload = json.loads(request.body or b'{}') if method == 'POST' else {}
                if not isinstance(request.payload, dict):
                    raise ValueError
                return view(request, *args, **kwargs)
            except (ValueError, UnicodeDecodeError):
                return JsonResponse({'error': '请求内容无效。'}, status=400)
            except IntegrityError:
                return JsonResponse({'error': '任务状态已改变，请刷新后再操作。'}, status=409)
            except OperationalError:
                return JsonResponse({'error': '记录暂时无法更新，请刷新状态；不会自动重试模型调用。'}, status=503)
        return wrapped
    return decorate


@api('GET')
def capabilities(request):
    from .team_config import team_summary
    connection = current_connection()
    catalog = capability_catalog(connection)
    return JsonResponse({'capabilities': catalog, 'connection': connection_summary(connection, catalog), 'team': team_summary(),
        'hourly_budget': RequestBudget.snapshot(request.user),
        'material_tools': [
            {'id': 'document_text', 'name': '文档正文提取', 'status': 'available',
             'description': '在本机读取文本、DOCX 和 PDF 正文，扫描页和 PNG/JPG 截图可使用本机文字识别。'},
            {'id': 'public_page', 'name': '公开网页读取', 'status': 'available',
             'description': '按你提供的 HTTPS 链接读取公开文字正文并保留出处；不进行通用搜索或自动事实核验。'}],
        'release': {
        'stage': 'internal', 'label': '内部测试', 'public_ready': False,
        'blockers': (['通用对话真实任务验收待完成'] if catalog[0]['status'] != 'available' else []) + ['团队执行与多模态能力待交付',
                     '生产邮件、安全审查、备份恢复与服务区域验收待完成'],
        'policy': '内部测试 → 小范围公测 → 正式开放。已正式上线的能力向所有普通用户开放。',
    }})


@api('GET')
def release_readiness(request):
    """Admin-only read-only configuration check, not a release approval."""
    if not request.user.is_superuser:
        return JsonResponse({'error': '只有管理员可以查看上线准备。'}, status=403)
    from operations.readiness import build_release_report
    from .catalog import ALL_CAPABILITIES, IMPLEMENTED
    report = build_release_report(settings, os.environ,
                                  implemented_capabilities=IMPLEMENTED, required_capabilities=ALL_CAPABILITIES)
    return JsonResponse({'report': report})


def task_data(task):
    return {'id': str(task.id), 'goal': task.goal, 'status': task.status,
            'updated_at': task.updated_at.isoformat()}


@api('POST')
def transition(request, task_id):
    action = request.payload.get('action')
    targets = {'pause': ('paused', ['draft', 'waiting', 'running']), 'resume': ('waiting', ['paused'])}
    if action not in targets:
        return JsonResponse({'error': '不支持的任务操作。'}, status=400)
    status, sources = targets[action]
    with transaction.atomic():
        task = Task.objects.filter(id=task_id, owner=request.user, conversation__owner=request.user).first()
        if not task:
            return JsonResponse({'error': '任务不存在。'}, status=404)
        lock_conversation(request.user, task.conversation_id)
        updated = Task.objects.filter(id=task.id, status__in=sources).update(status=status, execution_id=None, updated_at=timezone.now())
        if not updated:
            return JsonResponse({'error': '当前任务状态不支持此操作，请刷新。'}, status=409)
        if action == 'pause':
            from .decision_lifecycle import invalidate_waiting_decisions
            invalidate_waiting_decisions(task.conversation, request.user, '任务已暂停，问题说明未继续。', status='paused')
            runs = TeamRun.objects.filter(task=task, status__in=['queued', 'running', 'awaiting_input'])
            for run in runs.filter(workflow='decision_v1'):
                decision = dict(run.decision_state)
                decision.pop('sources', None)
                decision['phase'] = 'paused'
                decision['stop_reason'] = '任务已暂停；已发出请求可能仍计费。'
                decision.setdefault('unresolved', []).append('本轮没有完成全部核验与回应。')
                TeamRun.objects.filter(pk=run.pk).update(decision_state=decision)
            AgentStep.objects.filter(run__in=runs, status='running').update(
                status='failed', error='任务已暂停，此步骤的迟到结果不会写入。', updated_at=timezone.now())
            AgentStep.objects.filter(run__in=runs, status='pending').update(status='skipped', updated_at=timezone.now())
            runs.update(status='paused', input_context=[], finished_at=timezone.now(), updated_at=timezone.now())
        TaskEvent.objects.create(task=task, kind=action, content=(
            '任务已暂停；已发送的模型请求可能仍在服务端处理，其结果不会自动写入。' if action == 'pause'
            else '已恢复为等待处理；不会自动发起模型调用。'))
        task.refresh_from_db()
    return JsonResponse({'task': task_data(task)})


@api('GET')
def artifacts(request, task_id):
    from .artifact_bundles import artifact_metadata
    task = Task.objects.filter(id=task_id, owner=request.user, conversation__owner=request.user).first()
    if not task:
        return JsonResponse({'error': '任务不存在。'}, status=404)
    return JsonResponse({'artifacts': [artifact_metadata(item)
        for item in Artifact.objects.filter(task=task, owner=request.user).order_by(
            '-updated_at', '-created_at', '-revision', '-id')]})


@api('GET')
def conversation_artifacts(request, conversation_id):
    from .artifact_bundles import artifact_metadata
    conversation = Conversation.objects.filter(id=conversation_id, owner=request.user).first()
    if not conversation:
        return JsonResponse({'error': '会话不存在。'}, status=404)
    items = Artifact.objects.filter(owner=request.user, task__owner=request.user,
                                     task__conversation=conversation).order_by(
                                         '-updated_at', '-created_at', '-revision', '-id')
    return JsonResponse({'artifacts': [artifact_metadata(item) for item in items]})


def owned_artifact(user, artifact_id):
    return Artifact.objects.filter(id=artifact_id, owner=user, task__owner=user,
                                   task__conversation__owner=user).first()


def attachment_response(content, filename, content_type='text/plain; charset=utf-8'):
    response = HttpResponse(content, content_type=content_type)
    safe_name = filename.replace('\r', '').replace('\n', '').replace('/', '_').replace('\\', '_')[:120]
    response['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(safe_name)}"
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'no-store'
    return response


@api('GET')
def download(request, artifact_id):
    artifact = owned_artifact(request.user, artifact_id)
    if not artifact:
        return JsonResponse({'error': '成果不存在。'}, status=404)
    if artifact.kind == Artifact.Kind.BUNDLE:
        from .artifact_bundles import artifact_bundle
        from .bundles import BundleError, zip_bundle
        try:
            bundle = artifact_bundle(artifact)
            return attachment_response(zip_bundle(bundle), artifact.name, 'application/zip')
        except (ProviderError, BundleError):
            return JsonResponse({'error': '文件成果格式无效，无法安全下载，请重新生成。'}, status=409)
    return attachment_response(artifact.content, artifact.name)


@api('GET')
def artifact_detail(request, artifact_id):
    from .artifact_bundles import artifact_bundle, artifact_metadata
    from .bundles import BundleError
    artifact = owned_artifact(request.user, artifact_id)
    if not artifact:
        return JsonResponse({'error': '成果不存在。'}, status=404)
    detail = artifact_metadata(artifact)
    detail['summary'] = artifact.content
    detail['validation'] = artifact.validation
    detail['checks'] = artifact.validation.get('checks', []) if isinstance(artifact.validation, dict) else []
    detail['files'] = []
    if artifact.kind == Artifact.Kind.BUNDLE:
        try:
            bundle = artifact_bundle(artifact)
        except (ProviderError, BundleError):
            return JsonResponse({'error': '文件成果格式无效，无法安全预览，请重新生成。'}, status=409)
        detail['files'] = [{**item, 'index': index,
                            'url': f'/api/artifacts/{artifact.id}/files/{index}'}
                           for index, item in enumerate(bundle['files'])]
    else:
        detail['content'] = artifact.content
    return JsonResponse({'artifact': detail})


@api('GET')
def artifact_file(request, artifact_id, index):
    from .artifact_bundles import artifact_bundle
    from .bundles import BundleError
    artifact = owned_artifact(request.user, artifact_id)
    if not artifact or artifact.kind != Artifact.Kind.BUNDLE:
        return JsonResponse({'error': '成果文件不存在。'}, status=404)
    try:
        bundle = artifact_bundle(artifact)
    except (ProviderError, BundleError):
        return JsonResponse({'error': '文件成果格式无效，无法安全下载，请重新生成。'}, status=409)
    if index < 0 or index >= len(bundle['files']):
        return JsonResponse({'error': '成果文件不存在。'}, status=404)
    item = bundle['files'][index]
    # The stored path is display/archive data only. It never touches a filesystem.
    return attachment_response(item['content'], item['path'].rsplit('/', 1)[-1])


@api('POST')
def brief(request, task_id):
    with transaction.atomic():
        task = Task.objects.filter(id=task_id, owner=request.user, conversation__owner=request.user).first()
        if not task:
            return JsonResponse({'error': '任务不存在。'}, status=404)
        content = '# 圆桌 · 需求记录\n\n这是已保存的用户需求记录，不是 AI 分析或执行成果。\n\n'
        content += f'当前状态：{task.status}\n\n'
        for item in Message.objects.filter(conversation=task.conversation, role='user', superseded_by__isnull=True).order_by('created_at'):
            content += f'## {item.created_at.isoformat()}\n\n{item.content}\n\n'
        artifact, _ = Artifact.objects.update_or_create(task=task, owner=request.user, name='需求记录.md', defaults={'content': content})
    return JsonResponse({'artifact': {'id': str(artifact.id), 'name': artifact.name,
                                     'url': f'/api/artifacts/{artifact.id}/download'}})


@api('POST')
def respond(request, conversation_id):
    conversation = Conversation.objects.filter(id=conversation_id, owner=request.user).first()
    if not conversation:
        return JsonResponse({'error': '会话不存在。'}, status=404)
    with transaction.atomic():
        from django.contrib.auth import get_user_model
        User = get_user_model()
        if not User.objects.filter(pk=request.user.pk, is_active=True).update(is_active=F('is_active')):
            return JsonResponse({'error': '账号不可用，请重新登录。'}, status=401)
        # All run entry points share the acceptance editor's write lock. The
        # legacy text reply cannot bypass an owner's saved acceptance cases.
        conversation = lock_conversation(request.user, conversation_id)
        if conversation.sources.filter(owner=request.user).exists():
            return JsonResponse({'error': '本会话包含参考资料，请使用团队协作入口核对资料后开始。',
                                 'code': 'sources_mode_required'}, status=400)
        if conversation.acceptance_cases:
            return JsonResponse({'error': '本会话已有输入输出验收用例，请使用按任务分工的多文件团队协作进行验收。需求和用例已保留。',
                                 'code': 'acceptance_mode_required'}, status=400)
        connection = current_connection()
        if not chat_available(connection):
            return JsonResponse({'error': '通用对话尚未通过当前模型的全部验收。需求已保存，不会生成模拟回复。',
                                 'code': 'capability_pending'}, status=503)
        if request.payload.get('confirm_cost') is not True:
            return JsonResponse({'error': '请确认本次调用可能使用模型服务额度。'}, status=400)
        expected = request.payload.get('expected_connection')
        if not isinstance(expected, str) or not constant_time_compare(expected, connection_revision(connection)):
            return JsonResponse({'error': '模型连接已改变，请刷新连接状态并重新确认后再生成回复。',
                                 'code': 'connection_changed'}, status=409)
        budget, _ = RequestBudget.objects.get_or_create(user=request.user)
        RequestBudget.objects.filter(pk=budget.pk, period_started_at__lte=timezone.now() - timezone.timedelta(hours=1)).update(
            attempts=0, period_started_at=timezone.now())
        task = Task.objects.filter(conversation=conversation, owner=request.user,
                                   superseded_by_message__isnull=True).order_by('-created_at').first()
        if not task or task.status not in ('waiting', 'failed'):
            return JsonResponse({'error': '没有可处理的任务，或任务正在处理/已暂停。'}, status=409)
        if not RequestBudget.objects.filter(pk=budget.pk, attempts__lt=RequestBudget.HOURLY_LIMIT).update(attempts=F('attempts') + 1):
            return JsonResponse({'error': '已达到内测每小时 20 次调用尝试上限。'}, status=429)
        execution_id = uuid.uuid4()
        changed = Task.objects.filter(id=task.id, status=task.status).update(status='running', execution_id=execution_id, updated_at=timezone.now())
        if not changed:
            return JsonResponse({'error': '任务已被处理，请刷新。'}, status=409)
        TaskEvent.objects.create(task=task, kind='model_requested', content='已请求通用对话模型；本次没有代码执行或联网工具。')
        context = []
        for item in Message.objects.filter(conversation=conversation, role__in=['user', 'assistant'],
                                           superseded_by__isnull=True).order_by('created_at', 'id'):
            entry = {'role': item.role, 'content': item.content}
            if item.role == 'assistant' and item.provider_context:
                entry['_provider_context'] = item.provider_context
            context.append(entry)

    def acceptance_changed():
        # Called inside a transaction holding the conversation lock. Keep a
        # paused/replaced execution intact and never preserve an untested reply.
        reason = '验收用例已改变，旧文字回复已停止；请使用按任务分工的多文件团队协作，未写入本次结果。'
        if Task.objects.filter(id=task.id, status='running', execution_id=execution_id).update(
                status='failed', execution_id=None, updated_at=timezone.now()):
            TaskEvent.objects.create(task=task, kind='error', content=reason)
        return JsonResponse({'error': reason, 'code': 'acceptance_changed'}, status=409)

    with transaction.atomic():
        current_conversation = lock_conversation(request.user, conversation_id)
        if current_conversation.acceptance_cases:
            return acceptance_changed()
        if not Task.objects.filter(id=task.id, status='running', execution_id=execution_id).exists():
            return JsonResponse({'error': '任务已暂停或结束，未发起本次模型请求。'}, status=409)
    try:
        from workspace.personal_memory import memory_context
        with memory_context(request.user.pk, context[-1]['content']):
            content = generate_reply(context, connection=connection)
    except ProviderError as error:
        with transaction.atomic():
            if Task.objects.filter(id=task.id, status='running', execution_id=execution_id).update(status='failed', execution_id=None, updated_at=timezone.now()):
                TaskEvent.objects.create(task=task, kind='error', content=str(error))
        return JsonResponse({'error': str(error)}, status=502)
    current = current_connection()
    if current.fingerprint != connection.fingerprint or not chat_available(current):
        with transaction.atomic():
            if Task.objects.filter(id=task.id, status='running', execution_id=execution_id).update(status='failed', execution_id=None, updated_at=timezone.now()):
                TaskEvent.objects.create(task=task, kind='error', content='模型连接或验收状态已改变，本次结果未保存；服务方可能已计费。请确认连接后再试。')
        return JsonResponse({'error': '模型连接或验收状态已改变，本次结果未保存；服务方可能已计费。'}, status=409)
    with transaction.atomic():
        current_conversation = lock_conversation(request.user, conversation_id)
        if current_conversation.acceptance_cases:
            return acceptance_changed()
        # Atomic status claim ensures a pause while the provider ran wins over late results.
        changed = Task.objects.filter(id=task.id, status='running', execution_id=execution_id).update(status='completed', execution_id=None, updated_at=timezone.now())
        if not changed:
            return JsonResponse({'error': '任务已暂停或结束，本次模型结果未写入。'}, status=409)
        message = Message.objects.create(conversation=conversation, role='assistant', content=str(content),
                                         provider_context=getattr(content, 'continuation', ''))
        Artifact.objects.create(task=task, owner=request.user, name='圆桌回复.md', content=content)
        TaskEvent.objects.create(task=task, kind='completed', content='本轮文字回复已保存。尚未验收用户的整体目标，没有执行额外工具。')
        conversation.updated_at = timezone.now()
        conversation.save(update_fields=['updated_at'])
        task.refresh_from_db()
    return JsonResponse({'message': {'id': str(message.id), 'role': 'assistant', 'content': content}, 'task': task_data(task)})
