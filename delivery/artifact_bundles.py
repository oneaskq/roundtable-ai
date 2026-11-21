"""Persist bounded source-file bundles without writing or executing their contents."""
from django.db import transaction
from django.db.models import F, Max

from workspace.models import Artifact, Conversation, Task
from .bundles import check_bundle, validate_bundle
from .provider import ProviderError


def _case_evidence_matches(execution, run, cases):
    """Check trusted receipt facts, never a candidate's claimed test result."""
    from .acceptance_cases import case_revision
    from .execution import policy_fingerprint

    if (not isinstance(execution, dict) or execution.get('passed') is not True
            or execution.get('run_id') != str(run.id)
            or execution.get('policy_fingerprint') != policy_fingerprint()):
        return False
    checks = execution.get('checks')
    if not isinstance(checks, list):
        return False
    stdio = [item for item in checks if isinstance(item, dict) and item.get('tool') == 'python_stdio']
    if len(stdio) != 1:
        return False
    check = stdio[0]
    outcomes = check.get('cases')
    return (check.get('status') == 'passed' and check.get('independent_validation') is True
            and check.get('case_fingerprint') == case_revision(cases)
            and type(check.get('requested_cases')) is int and check['requested_cases'] == len(cases)
            and isinstance(outcomes, list) and len(outcomes) == len(cases)
            and all(isinstance(item, dict) and item.get('status') == 'passed'
                    and all(item.get(key) is True for key in
                            ('matched_stdout', 'command_completed', 'cleanup_complete'))
                    for item in outcomes))


def save_bundle_artifact(task, owner, bundle, validation, review_status='ready', *, run=None):
    """Save a new immutable conversation-level revision after independent checks.

    The supplied report is useful at the call site but is never authority to mark
    unsafe or malformed content ready. Recheck exactly the bytes being stored.
    """
    normalized = validate_bundle(bundle)
    checked = check_bundle(normalized)
    if isinstance(validation, dict) and validation.get('execution'):
        from .execution_evidence import verified_execution
        execution = verified_execution(normalized, validation['execution'])
        if (not isinstance(execution, dict) or not isinstance(execution.get('checks'), list)
                or any(not isinstance(item, dict) for item in execution['checks'])
                or not isinstance(execution.get('scope'), str)):
            raise ProviderError('运行记录内容无效，不能用于交付验收。')
        checked['execution'] = execution
        checked['checks'] = [*checked['checks'], *execution['checks']]
        checked['scope'] += ' 隔离检查范围：' + execution['scope']
        checked['passed'] = checked['passed'] and execution.get('passed') is True
    if review_status not in (Artifact.ReviewStatus.READY, Artifact.ReviewStatus.DRAFT):
        raise ProviderError('文件成果审核状态无效。')
    if review_status == Artifact.ReviewStatus.READY and checked.get('passed') is not True:
        raise ProviderError('文件成果尚未通过本轮必需检查，不能标为可交付。')
    with transaction.atomic():
        # Take a write lock before reading the next revision. This works on local
        # SQLite as well as row-locking production databases and changes no data.
        stored_task = Task.objects.select_for_update().filter(
            pk=task.pk, owner=owner, conversation__owner=owner).first()
        conversation_id = stored_task.conversation_id if stored_task else None
        if not conversation_id or not Conversation.objects.filter(pk=conversation_id, owner=owner).update(
                updated_at=F('updated_at')):
            raise ProviderError('任务不存在或无权保存文件成果。')
        from .models import TeamRun
        from .acceptance_cases import case_revision, normalize_cases

        # A passed receipt is authority only for the explicitly identified run.
        # Re-read its snapshot from storage so an in-memory mutation cannot alter
        # the user-provided acceptance standard. An execution claim identifies
        # the active batch; creation time alone can tie on Windows clocks.
        latest_run = TeamRun.objects.filter(task=task).first()
        selected_run = None
        if run is not None:
            selected_run = TeamRun.objects.filter(pk=run.pk, task=stored_task,
                                                 task__owner=owner, task__conversation__owner=owner).first()
            if selected_run is None:
                raise ProviderError('验收记录不属于当前任务或用户。')
        current_cases = normalize_cases(Conversation.objects.get(pk=conversation_id).acceptance_cases)
        run_cases = normalize_cases(selected_run.acceptance_cases) if selected_run else []
        latest_cases = normalize_cases(latest_run.acceptance_cases) if latest_run else []
        cases_required = bool(current_cases or run_cases or (selected_run is None and latest_cases))
        if cases_required:
            current_batch = False
            if selected_run is not None:
                if stored_task.status == Task.Status.RUNNING:
                    current_batch = stored_task.execution_id == selected_run.execution_id
                elif stored_task.status in (Task.Status.COMPLETED, Task.Status.FAILED) and stored_task.execution_id is None:
                    # Existing callers clear the claim in the enclosing finish
                    # transaction. Reject ambiguous/newer batches rather than
                    # using UUID ordering as evidence of which one executed last.
                    current_batch = not TeamRun.objects.filter(task=stored_task,
                        created_at__gte=selected_run.created_at).exclude(pk=selected_run.pk).exists()
            same_standard = bool(run_cases) and case_revision(run_cases) == case_revision(current_cases)
            accepted = (current_batch and same_standard
                        and _case_evidence_matches(checked.get('execution'), selected_run, run_cases))
            source_cases = run_cases if selected_run else current_cases or latest_cases
            checked['acceptance'] = {'run_id': str(selected_run.pk) if selected_run else None,
                                     'revision': case_revision(source_cases), 'source': 'user',
                                     'case_count': len(source_cases), 'passed': bool(accepted)}
            if not accepted:
                checked['passed'] = False
                checked['checks'].append({'path': '', 'tool': 'python_stdio', 'check': '用户输入输出验收',
                    'status': 'not_run', 'reason_code': 'acceptance_evidence_missing',
                    'detail': '缺少与当前运行、用户用例版本及文件内容一致的完整验收证据。'})
                if review_status == Artifact.ReviewStatus.READY:
                    raise ProviderError('用户输入输出验收尚未通过，或记录属于其他运行、用例或配置版本，不能标为可交付。')
        required_run = selected_run or latest_run
        required = required_run.plan.get('required_tools', []) if required_run and required_run.workflow == 'adaptive' else []
        if review_status == Artifact.ReviewStatus.READY and required:
            execution = checked.get('execution', {})
            completed = {item.get('tool') for item in execution.get('checks', []) if item.get('status') == 'passed'}
            if execution.get('passed') is not True or not set(required).issubset(completed):
                raise ProviderError('缺少本轮计划要求的真实执行证据，不能将草稿标为可交付。')
        latest = Artifact.objects.filter(owner=owner, task__owner=owner,
                                         task__conversation_id=conversation_id, kind=Artifact.Kind.BUNDLE).aggregate(
                                             highest=Max('revision'))['highest'] or 0
        revision = latest + 1
        prefix = '圆桌文件成果' if review_status == Artifact.ReviewStatus.READY else '待复核文件草稿'
        return Artifact.objects.create(
            task=task, owner=owner, kind=Artifact.Kind.BUNDLE,
            name=f'{prefix}-v{revision}.zip', content=normalized['summary'],
            files=normalized['files'], validation=checked, review_status=review_status, revision=revision,
        )


def artifact_bundle(artifact):
    """Validate saved data again before exposing it as downloadable files."""
    return validate_bundle({'summary': artifact.content, 'files': artifact.files})


def artifact_metadata(artifact):
    return {
        'id': str(artifact.id), 'task_id': str(artifact.task_id), 'name': artifact.name,
        'kind': artifact.kind, 'revision': artifact.revision, 'review_status': artifact.review_status,
        'file_count': len(artifact.files) if isinstance(artifact.files, list) else 0,
        'created_at': artifact.created_at.isoformat(),
        'url': f'/api/artifacts/{artifact.id}/download',
        'detail_url': f'/api/artifacts/{artifact.id}/detail',
    }
