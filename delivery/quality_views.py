"""Personal, offline comparison of manually supplied model replies."""
import hashlib
import re

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from workspace.views import APIError, api_required, payload
from .models import QualityRound, QualitySample
from .quality_cases import available_cases, case_snapshot, evaluate


MAX_ROUNDS = 50
MAX_SAMPLES = 3
MAX_OUTPUT = 12000
RATING_KEYS = {'accuracy', 'coverage', 'usefulness'}


def round_summary(round_):
    return {'id': str(round_.id), 'status': round_.status, 'case_id': round_.case_id,
            'case_title': round_.case_snapshot.get('title', ''), 'case_role': round_.case_snapshot.get('role', ''),
            'case_revision': round_.case_revision, 'sample_count': round_.samples.count(),
            'created_at': round_.created_at.isoformat(), 'updated_at': round_.updated_at.isoformat()}


def round_detail(round_, *, export=False):
    reveal = export or round_.status == 'completed'
    samples = []
    for item in round_.samples.all():
        data = {'id': str(item.id), 'alias': f'样本 {chr(64 + item.position)}',
                'output': item.output, 'output_sha256': item.output_sha256,
                'checks': item.checks, 'review': item.review,
                'created_at': item.created_at.isoformat(),
                'reviewed_at': item.reviewed_at.isoformat() if item.reviewed_at else None}
        if reveal:
            data['model_label'] = item.model_label
        samples.append(data)
    return {**round_summary(round_), 'case': round_.case_snapshot, 'samples': samples,
            'source': 'manual_unverified',
            'notice': '回复和模型名称由用户手动填写，来源未经验证；检查与评分不能自动启用模型或决定最佳模型。'}


def _locked_round(user, round_id):
    round_ = get_object_or_404(QualityRound.objects.select_for_update(), pk=round_id, owner=user)
    # SQLite has no row-level SELECT FOR UPDATE: a no-op update takes the write
    # lock before checking status, counts, and positions.
    QualityRound.objects.filter(pk=round_.pk, owner=user).update(updated_at=F('updated_at'))
    round_.refresh_from_db()
    return round_


def _bounded_text(value, max_length, label, *, required=True):
    if not isinstance(value, str) or len(value) > max_length or (required and not value.strip()):
        raise APIError(f'{label}不能为空或超过{max_length}字符。')
    if any(ord(char) < 32 and char not in '\n\r\t' for char in value) or '\x7f' in value:
        raise APIError(f'{label}包含不支持的控制字符。')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise APIError(f'{label}包含无效文字。') from None
    return value


@api_required('GET')
def cases(request):
    return JsonResponse({'cases': available_cases(),
        'notice': '所有用例使用虚构材料。这里不会连接模型，也不会使用账户额度。'})


@api_required('GET', 'POST')
def rounds(request):
    if request.method == 'GET':
        rows = QualityRound.objects.filter(owner=request.user).prefetch_related('samples')
        return JsonResponse({'rounds': [round_summary(row) for row in rows]})
    data = payload(request)
    if set(data) != {'case_id'} or not isinstance(data['case_id'], str):
        raise APIError('请选择一个评估任务。')
    selected = case_snapshot(data['case_id'])
    if selected is None:
        raise APIError('评估任务不存在，请刷新后选择。')
    with transaction.atomic():
        # Serialize round creation per account on SQLite and row-locking DBs.
        user_model = get_user_model()
        user_model.objects.select_for_update().get(pk=request.user.pk)
        user_model.objects.filter(pk=request.user.pk).update(is_active=F('is_active'))
        if QualityRound.objects.filter(owner=request.user).count() >= MAX_ROUNDS:
            raise APIError('最多保存50组评估，请先删除不需要的记录。', 409)
        item = QualityRound.objects.create(owner=request.user, case_id=selected['id'],
                                           case_revision=selected['revision'], case_snapshot=selected)
    return JsonResponse({'round': round_detail(item)}, status=201)


@api_required('GET', 'DELETE')
def round_detail_view(request, round_id):
    item = get_object_or_404(QualityRound.objects.filter(owner=request.user), pk=round_id)
    if request.method == 'GET':
        return JsonResponse({'round': round_detail(item)})
    with transaction.atomic():
        from workspace.deletion import record_deletion
        item = _locked_round(request.user, round_id)
        record_deletion('quality_round', item.id)
        item.delete()
    return JsonResponse({'deleted': True})


@api_required('POST')
def add_sample(request, round_id):
    data = payload(request)
    if set(data) != {'model_label', 'output'}:
        raise APIError('请填写模型名称和这道任务的完整回复。')
    label = _bounded_text(data['model_label'], 80, '模型名称').strip()
    if ('\n' in label or '\r' in label or re.search(r'(?i)(?:\bBearer\b|\bsk-[A-Za-z0-9])', label)):
        raise APIError('模型名称应为简短名称，请勿填写密钥。')
    output = _bounded_text(data['output'], MAX_OUTPUT, '模型回复')
    with transaction.atomic():
        round_ = _locked_round(request.user, round_id)
        if round_.status != 'collecting':
            raise APIError('评审已开始，不能再向此组添加回复。', 409)
        current_case = case_snapshot(round_.case_id)
        if current_case is None or not constant_time_compare(current_case['revision'], round_.case_revision):
            raise APIError('评估题目或检查规则已更新，请新建一组，旧记录仍可查看。', 409)
        existing = list(round_.samples.all())
        if len(existing) >= MAX_SAMPLES:
            raise APIError('每组最多比较3份回复。', 409)
        if any(sample.model_label.casefold() == label.casefold() for sample in existing):
            raise APIError('该名称已存在，请用不同名称区分版本。', 409)
        try:
            checks = evaluate(round_.case_id, output)
        except ValueError:
            raise APIError('评估任务版本无效，请新建一组。', 409) from None
        QualitySample.objects.create(round=round_, position=len(existing) + 1, model_label=label, output=output,
            output_sha256=hashlib.sha256(output.encode('utf-8')).hexdigest(), checks=checks)
        round_.updated_at = timezone.now()
        round_.save(update_fields=['updated_at'])
    return JsonResponse({'round': round_detail(round_)}, status=201)


@api_required('POST')
def start_review(request, round_id):
    data = payload(request)
    if data:
        raise APIError('开始评审不需要额外参数。')
    with transaction.atomic():
        round_ = _locked_round(request.user, round_id)
        if round_.status != 'collecting':
            raise APIError('此组评审已经开始。', 409)
        if round_.samples.count() < 2:
            raise APIError('请先添加至少两份回复，再开始比较。', 409)
        round_.status = 'reviewing'
        round_.save(update_fields=['status', 'updated_at'])
    return JsonResponse({'round': round_detail(round_)})


@api_required('POST')
def review_sample(request, sample_id):
    data = payload(request)
    if set(data) != {'ratings', 'note'} or not isinstance(data['ratings'], dict) or set(data['ratings']) != RATING_KEYS:
        raise APIError('请分别填写三项评分。')
    ratings = data['ratings']
    if any(type(score) is not int or score not in (0, 1, 2) for score in ratings.values()):
        raise APIError('每项评分请选择0、1或2。')
    note = _bounded_text(data['note'], 1000, '评审备注', required=False).strip()
    with transaction.atomic():
        initial = get_object_or_404(QualitySample, pk=sample_id, round__owner=request.user)
        round_ = _locked_round(request.user, initial.round_id)
        sample = get_object_or_404(QualitySample, pk=sample_id, round=round_)
        if round_.status != 'reviewing' or sample.reviewed_at:
            raise APIError('该回复已评分或此组已结束；如需重评请新建一组。', 409)
        sample.review = {'ratings': ratings, 'note': note}
        sample.reviewed_at = timezone.now()
        sample.save(update_fields=['review', 'reviewed_at'])
        if not round_.samples.filter(reviewed_at__isnull=True).exists():
            round_.status = 'completed'
        round_.save(update_fields=['status', 'updated_at'])
    return JsonResponse({'round': round_detail(round_)})
