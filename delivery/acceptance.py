"""Synthetic, opt-in model probes followed by separate local human approval."""
import getpass
import copy
import hashlib
import json
import re
import secrets
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import MFADevice
from .connections import get_connection
from .models import CapabilityEvidence
from .provider import (ProviderError, QWEN_TEXT_MODELS, SYSTEM_MESSAGE, generate_reply,
                       list_models, qwen_thinking_enabled, safe_provider_diagnostic)
from .text_guidance import text_guidance_for


CAPABILITIES = ('chat.qa', 'chat.writing', 'chat.summary', 'chat.context')
REPORT_VALIDITY = timedelta(days=1)
EVIDENCE_VALIDITY = timedelta(days=30)
MAX_OUTPUT_TOKENS = 4096
CHECK_VERSION = 4
SIGNING_SALT = 'roundtable.model-acceptance.v1'
MANUAL_NOTICE = ('机械检查仅验证这些合成样例的结构和已知事实；写作主题是否覆盖须人工判断。'
                 '必须人工阅读实际输出后确认质量，不代表完整能力评估。')
QWEN_CATALOG_STATUS = 'unsupported_official_api'
WRITING_PROMPT = ('为第一次去自习室的学生写三条中文建议，主题分别是计划、安静、休息。'
                  '只输出三行，分别以1.、2.、3.开头，总计120至180个汉字，不加标题。')


class AcceptanceError(Exception):
    """Safe operator-facing failure; never includes upstream bodies or credentials."""


def report_directory():
    intended = Path(settings.BASE_DIR).resolve() / 'work' / 'model-acceptance'
    if intended.resolve() != intended:
        raise AcceptanceError('验收报告目录不可链接到项目之外。')
    return intended


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _redact(text, connection):
    text = str(text)
    if connection.api_key:
        text = text.replace(connection.api_key, '[已隐藏密钥]')
    text = re.sub(r'(?i)Bearer\s+[^\s"\'<>]+', 'Bearer [已隐藏]', text)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)


def _check(name, passed):
    return {'name': name, 'passed': bool(passed)}


def _checks(capability, outputs, token=''):
    value = outputs[-1].strip()
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if capability == 'chat.qa':
        return [_check('已知算术答案正确', value == '323'),
                _check('只输出要求的整数', bool(re.fullmatch(r'\d+', value)))]
    if capability == 'chat.writing':
        chinese = len(re.findall(r'[\u4e00-\u9fff]', value))
        return [_check('恰好三条编号建议', len(lines) == 3 and all(
                    re.match(rf'^{index}[.、]\s*\S', line) for index, line in enumerate(lines, 1))),
                _check('中文字数在120至180之间', 120 <= chinese <= 180),
                _check('以中文为主', chinese >= len(value) * .65)]
    if capability == 'chat.summary':
        expected = ('- 项目名：松林', '- 启动日期：2026年11月3日', '- 参与人数：8人')
        normalized = [line.replace(':', '：').replace(' ', '').rstrip('。') for line in lines]
        expected_normalized = [line.replace(' ', '') for line in expected]
        correct = len(normalized) == 3 and normalized[0] in (expected_normalized[0], '-项目名：松林项目') \
            and normalized[1:] == expected_normalized[1:]
        return [_check('恰好三条摘要', len(lines) == 3 and all(line.startswith('- ') for line in lines)),
                _check('三个已知事实完整且没有增加事实', correct)]
    return [_check('第一轮正确确认指令', outputs[0].strip() == '已记住'),
            _check('第二轮准确回忆随机合成短语', value == token)]


def _probe_instruction(capability, prompts):
    return (SYSTEM_MESSAGE['content'] + '\n' + text_guidance_for(prompts[0])
            if capability == 'chat.writing' else None)


def _probe(connection, capability, prompts, token='', *, stop_on_check_failure=False):
    messages, outputs, provider_outputs, checks = [], [], [], []
    error = ''
    diagnostic_code = ''
    response_format = None
    thinking_observed = False
    # Writing is a fresh, single-turn exchange. Apply the same general length
    # guidance as the team while preserving the original user test prompt.
    # Context probes continue to use the default system message and lineage.
    system_instruction = _probe_instruction(capability, prompts)
    for prompt in prompts:
        messages.append({'role': 'user', 'content': prompt})
        try:
            # The same paid probe also verifies the streaming transport used by
            # the text team; no additional request or automatic retry is made.
            response = generate_reply(messages, connection=connection, max_output_tokens=MAX_OUTPUT_TOKENS,
                                      on_text=lambda _fragment: None,
                                      **({'system_instruction': system_instruction} if system_instruction else {}))
        except ProviderError as failure:
            diagnostic_code = safe_provider_diagnostic(failure)
            if getattr(failure, 'response_format', None) is not None:
                response_format = failure.response_format
                system_instruction = failure.effective_system_instruction or system_instruction
                raw = getattr(failure, 'raw_content', None)
                if raw is not None:
                    provider_outputs.append(_redact(raw, connection))
            error = '模型请求或输出校验失败，本项未通过；没有自动重试。'
            break
        if len(response) > 16000:
            error = '模型输出超过验收报告上限，本项未通过。'
            break
        outputs.append(_redact(response, connection))
        thinking_observed = thinking_observed or getattr(response, 'thinking_observed', None) is True
        raw = getattr(response, 'raw_content', None)
        provider_outputs.append(_redact(raw, connection) if raw is not None else None)
        if getattr(response, 'response_format', None) is not None:
            response_format = response.response_format
            system_instruction = response.effective_system_instruction or system_instruction
        if (stop_on_check_failure and capability == 'chat.context' and len(outputs) == 1 and
                outputs[0].strip() != '已记住'):
            error = '上下文首轮验收检查未通过，第二轮未执行。'
            diagnostic_code = 'check_failed'
            break
        assistant = {'role': 'assistant', 'content': str(response)}
        continuation = getattr(response, 'continuation', '')
        if continuation:
            assistant['_provider_context'] = continuation
        messages.append(assistant)
    if not error:
        checks = _checks(capability, outputs, token)
        if capability == 'chat.writing' and qwen_thinking_enabled(connection):
            checks.append(_check('写作请求观察到思考模式', thinking_observed))
    return {'capability': capability, 'prompts': prompts, 'outputs': outputs,
            'provider_outputs': provider_outputs, 'response_format': response_format,
            'stream_callback_mode': 'buffered_then_validated' if response_format else 'incremental',
            'system_instruction': system_instruction or SYSTEM_MESSAGE['content'],
            'checks': checks, 'passed': not error and all(check['passed'] for check in checks),
            'error': error, 'diagnostic_code': diagnostic_code,
            'thinking_observed': thinking_observed if qwen_thinking_enabled(connection) else None,
            'manual_review_required': True, 'output_hash': _hash(outputs)}


def discover_models(connection):
    if not connection.configured:
        raise AcceptanceError('请先配置服务商、模型、服务地区和密钥。')
    try:
        catalog = list_models(connection)
    except ProviderError:
        raise AcceptanceError('获取官方模型名单失败；未开始生成验收。') from None
    if connection.model not in catalog:
        raise AcceptanceError('当前模型不在官方接口返回的名单中；未开始生成验收。')
    return catalog


def run_acceptance(connection=None, catalog=None, *, stop_on_check_failure=False):
    """No database changes. Caller must obtain explicit outbound-call consent."""
    connection = connection or get_connection()
    if connection.provider == 'qwen':
        # QwenCloud officially has no GET /models. A selected ID is not a
        # verified catalog result: actual generation and human review remain
        # mandatory, and the signed report records this distinction.
        if (not connection.configured or connection.model not in QWEN_TEXT_MODELS or
                (connection.reasoning_effort not in ('', 'none') and not qwen_thinking_enabled(connection))):
            raise AcceptanceError('千问连接未完整配置为当前支持的文字模型及思考模式。')
        catalog_verified = False
    else:
        catalog = discover_models(connection) if catalog is None else catalog
        if not connection.configured or connection.model not in catalog:
            raise AcceptanceError('当前模型未通过官方名单核对。')
        catalog_verified = True
    phrase = '松林-' + secrets.token_hex(6)
    probes = [
        ('chat.qa', ['计算17乘19。只输出十进制整数，不解释。']),
        ('chat.writing', [WRITING_PROMPT]),
        ('chat.summary', ['以下是虚构项目资料：松林项目将在2026年11月3日启动，参与人数为8人。'
                         '预算和负责人都尚未确定。请只概括三个已确定事实，不做推测。严格输出三行：'
                         '“- 项目名：…”、“- 启动日期：…”、“- 参与人数：…”，不用引号。']),
        ('chat.context', [f'这是合成记忆测试，请记住短语：{phrase}。现在只回复“已记住”，不带引号。',
                          '我上一条消息让你记住的短语是什么？只输出该短语，不解释。']),
    ]
    results = []
    stopped = False
    stopped_reason = ''
    for capability, prompts in probes:
        if stopped:
            # Do not send another potentially billable request after a provider
            # failure. Keep the incomplete signed report explicit and unapprovable.
            results.append({'capability': capability, 'prompts': prompts, 'outputs': [],
                            'provider_outputs': [], 'response_format': None, 'stream_callback_mode': 'not_run',
                            'system_instruction': _probe_instruction(capability, prompts) or SYSTEM_MESSAGE['content'],
                            'checks': [], 'passed': False, 'error': stopped_reason,
                            'diagnostic_code': 'not_run',
                            'thinking_observed': None,
                            'manual_review_required': True, 'output_hash': _hash([])})
            continue
        result = _probe(connection, capability, prompts, phrase,
                        stop_on_check_failure=stop_on_check_failure)
        results.append(result)
        stopped = bool(result['error']) or (stop_on_check_failure and not result['passed'])
        if stopped:
            stopped_reason = ('前一项请求失败，本项未执行。' if result['error'] else
                              '前一项验收检查未通过，本项未执行。')
    payload = {
        'schema_version': 1, 'report_id': uuid.uuid4().hex,
        'completed_at': timezone.now().isoformat(),
        'provider': connection.provider, 'model': connection.model,
        'service_platform': connection.service_platform or ('qwencloud' if connection.provider == 'qwen' else 'official'),
        'service_region': connection.service_region, 'connection_fingerprint': connection.fingerprint,
        'reasoning_effort': connection.reasoning_effort,
        'catalog_model_verified': catalog_verified,
        'catalog_verification': 'official_model_list' if catalog_verified else QWEN_CATALOG_STATUS,
        'max_output_tokens': MAX_OUTPUT_TOKENS,
        'streaming_tested': True,
        'check_version': CHECK_VERSION,
        'synthetic_only': True, 'manual_notice': MANUAL_NOTICE, 'results': results,
        'passed': all(result['passed'] for result in results),
    }
    payload['outputs_hash'] = _hash([result['outputs'] for result in results])
    document = {'report': payload, 'signature': signing.Signer(salt=SIGNING_SALT).signature(_canonical(payload))}
    directory = report_directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{payload['report_id']}.json"
    # Exclusive creation avoids overwriting any previous audit report.
    with path.open('x', encoding='utf-8') as output:
        json.dump(document, output, ensure_ascii=False, indent=2)
    return path, payload


def read_report(path):
    path = Path(path).resolve()
    directory = report_directory().resolve()
    if path.parent != directory or path.suffix != '.json':
        raise AcceptanceError('只能审核本项目 work/model-acceptance 目录内的原始验收报告。')
    try:
        if path.stat().st_size > 1024 * 1024:
            raise AcceptanceError('验收报告大小异常。')
        document = json.loads(path.read_text(encoding='utf-8'))
        payload = document['report']
        signing.Signer(salt=SIGNING_SALT).unsign(_canonical(payload) + ':' + document['signature'])
        if payload['schema_version'] != 1 or path.stem != payload['report_id']:
            raise ValueError()
        if payload['outputs_hash'] != _hash([result['outputs'] for result in payload['results']]):
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError, signing.BadSignature):
        raise AcceptanceError('验收报告不可读取、已被修改或签名无效，请重新运行验收。') from None
    return payload


def recheck_report(path):
    """Re-evaluate signed outputs offline without changing the original or its timestamp."""
    original = read_report(path)
    results = original.get('results')
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results) or \
            [item.get('capability') for item in results] != list(CAPABILITIES):
        raise AcceptanceError('原报告项目不完整，无法离线复核。')
    revised = copy.deepcopy(original)
    for item in revised['results']:
        outputs, prompts = item.get('outputs'), item.get('prompts')
        if not isinstance(outputs, list) or not isinstance(prompts, list) or not all(isinstance(x, str) for x in outputs + prompts):
            raise AcceptanceError('原报告内容无效，无法离线复核。')
        if item.get('error'):
            item['checks'], item['passed'] = [], False
            continue
        expected_count = 2 if item['capability'] == 'chat.context' else 1
        if len(outputs) != expected_count or len(prompts) != expected_count:
            raise AcceptanceError('原报告输出不完整，无法离线复核。')
        token = ''
        if item['capability'] == 'chat.context':
            match = re.search(r'短语：(松林-[0-9a-f]{12})。', prompts[0])
            if not match:
                raise AcceptanceError('原报告的上下文样例无效，无法离线复核。')
            token = match.group(1)
        item['checks'] = _checks(item['capability'], outputs, token)
        if (revised.get('provider') == 'qwen' and revised.get('reasoning_effort') == 'low' and
                item['capability'] == 'chat.writing'):
            item['checks'].append(_check('写作请求观察到思考模式', item.get('thinking_observed') is True))
        item['passed'] = bool(item['checks']) and all(check['passed'] for check in item['checks'])
        item['output_hash'] = _hash(outputs)
    revised['report_id'] = uuid.uuid4().hex
    revised['rechecked_from'] = original['report_id']
    revised['rechecked_at'] = timezone.now().isoformat()
    revised['check_version'] = CHECK_VERSION
    revised['manual_notice'] = MANUAL_NOTICE
    revised['passed'] = all(item['passed'] for item in revised['results'])
    revised['outputs_hash'] = _hash([item['outputs'] for item in revised['results']])
    document = {'report': revised, 'signature': signing.Signer(salt=SIGNING_SALT).signature(_canonical(revised))}
    destination = report_directory() / f"{revised['report_id']}.json"
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
    return destination, revised


def approve_report(path, *, confirm_reviewed=False, region_reviewed=False, terms_reviewed=False,
                   reviewer_email='', connection=None):
    if not (confirm_reviewed and region_reviewed and terms_reviewed):
        raise AcceptanceError('启用前必须确认已阅读实际输出、服务地区和服务条款。')
    payload = read_report(path)
    completed_at = parse_datetime(payload.get('completed_at', ''))
    now = timezone.now()
    if (not completed_at or timezone.is_naive(completed_at) or completed_at > now or
            now - completed_at > REPORT_VALIDITY):
        raise AcceptanceError('报告已超过24小时有效期或时间异常，请重新运行验收。')
    connection = connection or get_connection()
    if not connection.configured or any(payload.get(name) != getattr(connection, attr) for name, attr in (
            ('provider', 'provider'), ('model', 'model'), ('service_region', 'service_region'),
            ('connection_fingerprint', 'fingerprint'))):
        raise AcceptanceError('连接配置已改变，原验收报告不可用于当前连接，请重新验收。')
    results = payload.get('results', [])
    if (any(isinstance(result, dict) and result.get('response_format') for result in results) and
            getattr(settings, 'QWEN_STRUCTURED_HANZI_ENABLED', False) is not True):
        raise AcceptanceError('结构化长度检查仍处于关闭状态；实验报告不能用于启用当前服务。')
    if connection.provider == 'qwen':
        catalog_ok = (payload.get('catalog_model_verified') is False
                      and payload.get('catalog_verification') == QWEN_CATALOG_STATUS
                      and connection.model in QWEN_TEXT_MODELS
                      and (connection.reasoning_effort in ('', 'none') or qwen_thinking_enabled(connection)))
        if qwen_thinking_enabled(connection):
            writing = next((item for item in results if item.get('capability') == 'chat.writing'), {})
            catalog_ok = (catalog_ok and payload.get('reasoning_effort') == 'low' and
                          writing.get('thinking_observed') is True)
    else:
        catalog_ok = payload.get('catalog_model_verified') is True
    if (not payload.get('passed') or not payload.get('synthetic_only') or payload.get('streaming_tested') is not True or
            not catalog_ok or
            [result.get('capability') for result in results] != list(CAPABILITIES) or
            any(not result.get('passed') or not result.get('checks') or
                not all(check.get('passed') for check in result['checks']) or
                result.get('output_hash') != _hash(result.get('outputs')) for result in results)):
        raise AcceptanceError('报告有未通过或不完整的项目，不可启用能力。')
    reviewer = None
    if reviewer_email:
        reviewer = get_user_model().objects.filter(email__iexact=reviewer_email.strip(),
                                                   is_active=True, is_superuser=True).first()
        if not reviewer or not MFADevice.objects.filter(user=reviewer, enabled=True).exists():
            raise AcceptanceError('指定审核人必须是已启用多因素认证的有效超级管理员。')
    with transaction.atomic():
        for result in results:
            evidence = {
                'report_id': payload['report_id'], 'output_hash': result['output_hash'],
                'outputs_hash': payload['outputs_hash'], 'human_reviewed': True,
                'reviewed_at': now.isoformat(),
                'reviewer': reviewer.email if reviewer else f'local operator: {getpass.getuser()}',
                'notice': MANUAL_NOTICE, 'result': result,
                'catalog_model_verified': payload['catalog_model_verified'],
                'catalog_verification': payload.get('catalog_verification', 'official_model_list'),
                'streaming_tested': True,
            }
            CapabilityEvidence.objects.update_or_create(
                capability=result['capability'], connection_fingerprint=connection.fingerprint, defaults={
                'provider': connection.provider, 'model': connection.model,
                'service_region': connection.service_region,
                'connection_fingerprint': connection.fingerprint,
                'evidence': json.dumps(evidence, ensure_ascii=False),
                'verified_at': completed_at, 'expires_at': completed_at + EVIDENCE_VALIDITY,
                'region_approved': True, 'compliance_approved': True, 'enabled': True, 'reviewer': reviewer,
            })
    return payload
