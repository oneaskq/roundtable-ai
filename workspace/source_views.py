"""Owner-scoped reference material APIs; extraction never invokes a model."""
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404

from .models import Conversation, Project, SourceMaterial, Task
from .source_parsers import SourceParseError, parse_uploaded_source
from .sources import MAX_SOURCES, MAX_TOTAL_SOURCE_CHARS, serialize_source, source_limits, sources_revision
from .views import APIError, api_required, lock_conversation, parse_uuid, payload, serialize_conversation


def owned_sources(conversation, owner):
    return SourceMaterial.objects.filter(conversation=conversation, conversation__owner=owner, owner=owner)


def _editable(conversation, owner):
    if conversation.tasks.filter(owner=owner, status=Task.Status.RUNNING).exists():
        raise APIError('任务正在处理，资料不能修改；请先暂停或等待本轮结束。', 409)


def _result(conversation, owner):
    items = list(owned_sources(conversation, owner))
    return {'sources': [serialize_source(item) for item in items],
            'revision': sources_revision(items), 'limits': source_limits()}


@api_required('GET', 'POST')
def conversation_sources(request, conversation_id, draft=False):
    project = None
    if draft:
        if request.method != 'POST':
            raise APIError('请选择要添加的材料。', 405)
        conversation = Conversation.objects.filter(pk=conversation_id).first()
        if conversation and conversation.owner_id != request.user.id:
            raise APIError('对话不存在。', 404)
        if request.GET.get('project'):
            project = get_object_or_404(Project, pk=parse_uuid(request.GET['project']), owner=request.user)
    else:
        conversation = get_object_or_404(Conversation, pk=conversation_id, owner=request.user)
    if request.method == 'GET':
        return JsonResponse(_result(conversation, request.user))
    if conversation:
        _editable(conversation, request.user)
    if conversation and owned_sources(conversation, request.user).count() >= MAX_SOURCES:
        raise APIError('每个对话最多保存8份资料；请在新对话中继续。', 409)
    # Ownership and editable state are checked before parsing or contacting any
    # website. Slow extraction/fetching runs outside the database transaction.
    if request.content_type == 'multipart/form-data':
        if getattr(request, 'source_upload_too_large', False):
            raise APIError('上传文件总大小超过2MiB，请拆分后上传。', 413)
        files = request.FILES.getlist('file')
        if getattr(request, 'source_upload_too_large', False):
            raise APIError('上传文件总大小超过2MiB，请拆分后上传。', 413)
        if len(files) != 1 or set(request.FILES) != {'file'}:
            raise APIError('每次请选择一份文件。')
        try:
            extracted = parse_uploaded_source(files[0])
        except SourceParseError as error:
            raise APIError(str(error)) from None
        kind = 'file'
    elif request.content_type == 'application/json':
        data = payload(request)
        if set(data) != {'url'} or not isinstance(data['url'], str):
            raise APIError('请填写一条公开网页的HTTPS地址。')
        from .web_sources import SourceFetchError, fetch_public_source
        try:
            extracted = fetch_public_source(data['url'])
        except SourceFetchError as error:
            raise APIError(str(error)) from None
        kind = 'web'
    else:
        raise APIError('请上传支持的文件，或提交网页地址。', 415)
    with transaction.atomic():
        if draft:
            # The draft and its first valid source commit together. Retrying a
            # lost response reuses the owner's draft without creating another.
            conversation, _ = Conversation.objects.get_or_create(pk=conversation_id,
                defaults={'owner': request.user, 'project': project})
        conversation = lock_conversation(request.user, conversation_id)
        _editable(conversation, request.user)
        items = list(owned_sources(conversation, request.user))
        if draft:
            if conversation.project_id != (project.pk if project else None) or conversation.messages.exists():
                raise APIError('此草稿已改变，请重新打开对话。', 409)
            previous = next((item for item in items if item.kind == kind and item.sha256 == extracted['sha256']
                             and item.name == extracted['name'] and item.source_url == extracted['source_url']), None)
            if previous:
                return JsonResponse({**_result(conversation, request.user), 'source': serialize_source(previous),
                                     'conversation': serialize_conversation(conversation)})
        if len(items) >= MAX_SOURCES:
            raise APIError('每个对话最多保存8份资料；请在新对话中继续。', 409)
        if sum(len(item.content) for item in items if item.included) + len(extracted['content']) > MAX_TOTAL_SOURCE_CHARS:
            raise APIError('已选资料正文合计超过16000字符，请先取消部分资料的本轮引用。', 409)
        source = SourceMaterial.objects.create(conversation=conversation, owner=request.user, kind=kind, **extracted)
        from delivery.decision_lifecycle import invalidate_waiting_decisions
        invalidate_waiting_decisions(conversation, request.user, '参考资料已改变，请重新整理问题。')
        conversation.save(update_fields=['updated_at'])
        result = _result(conversation, request.user)
        result['source'] = serialize_source(source)
        if draft:
            result['conversation'] = serialize_conversation(conversation)
    return JsonResponse(result, status=201)


@api_required('GET', 'POST', 'DELETE')
def source_detail(request, source_id):
    source = get_object_or_404(SourceMaterial, pk=source_id, owner=request.user,
                               conversation__owner=request.user)
    if request.method == 'GET':
        return JsonResponse({'source': serialize_source(source, include_content=True)})
    if request.method == 'DELETE':
        with transaction.atomic():
            from .deletion import record_deletion
            conversation = lock_conversation(request.user, source.conversation_id)
            source = get_object_or_404(SourceMaterial, pk=source_id, owner=request.user, conversation=conversation)
            _editable(conversation, request.user)
            record_deletion('source', source.id)
            source.delete()
            from delivery.decision_lifecycle import invalidate_waiting_decisions
            invalidate_waiting_decisions(conversation, request.user, '参考资料已改变，请重新整理问题。')
            conversation.save(update_fields=['updated_at'])
            result = _result(conversation, request.user)
        return JsonResponse(result)
    data = payload(request)
    if set(data) != {'included'} or type(data['included']) is not bool:
        raise APIError('请选择是否在本轮引用这份资料。')
    with transaction.atomic():
        conversation = lock_conversation(request.user, source.conversation_id)
        source = get_object_or_404(SourceMaterial, pk=source_id, owner=request.user, conversation=conversation)
        _editable(conversation, request.user)
        if data['included']:
            others = owned_sources(conversation, request.user).filter(included=True).exclude(pk=source.pk)
            if sum(len(item.content) for item in others) + len(source.content) > MAX_TOTAL_SOURCE_CHARS:
                raise APIError('已选资料正文合计超过16000字符，请先取消其他资料的本轮引用。', 409)
        if source.included != data['included']:
            source.included = data['included']
            source.save(update_fields=['included', 'updated_at'])
            from delivery.decision_lifecycle import invalidate_waiting_decisions
            invalidate_waiting_decisions(conversation, request.user, '参考资料已改变，请重新整理问题。')
            conversation.save(update_fields=['updated_at'])
        result = _result(conversation, request.user)
        result['source'] = serialize_source(source)
    return JsonResponse(result)
