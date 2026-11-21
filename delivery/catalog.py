from django.utils import timezone
from django.utils.crypto import salted_hmac

from .connections import ConnectionError, ProviderConnection, get_connection
from .models import CapabilityEvidence


CATALOG = [
    ('conversation', '通用对话', '能正常完成问答、写作、总结，并按预期管理上下文。', [
        ('chat.qa', '问答'), ('chat.writing', '写作'), ('chat.summary', '总结'), ('chat.context', '上下文管理')]),
    ('reasoning', '深度推理', '完成代表性复杂分析、规划和多步骤任务测试。', [
        ('reasoning.analysis', '复杂分析'), ('reasoning.planning', '规划与多步骤任务')]),
    ('coding', '代码开发', '编写、修改、运行和测试代码，展示真实测试结果。', [
        ('coding.edit', '编写与修改'), ('coding.execute', '隔离运行'), ('coding.test', '测试与结果展示')]),
    ('understanding', '图片与文档理解', '按支持格式接收文件，正确提取与理解内容。', [
        ('understanding.image', '图片理解'), ('understanding.document', '文档提取与理解')]),
    ('research', '联网搜索与研究', '执行实际搜索，提供可核验来源并标明无法核实的信息。', [
        ('research.search', '真实搜索'), ('research.sources', '研究与来源核验')]),
    ('images', '图片生成与编辑', '实际生成或编辑图片，输出符合接口能力与格式要求。', [
        ('images.generate', '图片生成'), ('images.edit', '图片编辑')]),
    ('audio', '语音与音频', '各项已承诺能力分别进行端到端测试。', [
        ('audio.input', '音频输入'), ('audio.transcribe', '语音转写'), ('audio.conversation', '语音对话'), ('audio.generate', '音频生成')]),
    ('video', '视频理解与生成', '视频分析与视频生成分别验收。', [
        ('video.understand', '视频理解'), ('video.generate', '视频生成')]),
]

IMPLEMENTED = {'chat.qa', 'chat.writing', 'chat.summary', 'chat.context'}
ALL_CAPABILITIES = {key for _, _, _, children in CATALOG for key, _ in children}


def current_connection():
    try:
        return get_connection()
    except ConnectionError:
        return ProviderConnection(provider='', model='', api_key='')


def subcapability_status(key, name, records, connection):
    record = records.get(key)
    reason = '待接入与验收'
    available = False
    if key == 'understanding.document':
        reason = '已接通文字与 Word 正文提取；PDF、扫描件与模型理解效果仍待接入或验收'
    elif key == 'research.sources':
        reason = '已接通指定公开网页正文读取；通用搜索与来源事实核验仍待接入或验收'
    if key in IMPLEMENTED:
        if not connection.configured:
            reason = '请在本机完成模型连接设置'
        elif not record or not record.enabled:
            reason = '待真实任务验收'
        elif (record.provider != connection.provider or record.model != connection.model
              or record.service_region != connection.service_region
              or record.connection_fingerprint != connection.fingerprint):
            reason = '当前连接、模型或服务区域已变更，需要重新验收'
        elif record.expires_at <= timezone.now() or record.verified_at > timezone.now():
            reason = '验收记录已过期或日期无效'
        elif not record.region_approved or not record.compliance_approved or not record.evidence.strip():
            reason = '待服务区域与合规条件确认'
        else:
            reason = '已接入并通过当前模型与服务区域验收'
            available = True
    return {'id': key, 'name': name, 'status': 'available' if available else 'pending', 'reason': reason}


def capability_catalog(connection=None):
    connection = connection if connection is not None else current_connection()
    records = {item.capability: item for item in CapabilityEvidence.objects.filter(
        connection_fingerprint=connection.fingerprint)}
    result = []
    for key, name, criterion, children in CATALOG:
        statuses = [subcapability_status(child, label, records, connection) for child, label in children]
        count = sum(child['status'] == 'available' for child in statuses)
        status = 'available' if count == len(statuses) else 'partial' if count else 'pending'
        result.append({'id': key, 'name': name, 'category': name, 'criterion': criterion,
                       'status': status, 'reason': '全部子能力通过验收' if status == 'available' else '按子能力分别交付和验收',
                       'subcapabilities': statuses})
    return result


def chat_available(connection=None):
    return capability_catalog(connection)[0]['status'] == 'available'


def connection_revision(connection):
    return salted_hmac('roundtable.public-connection-version', connection.fingerprint,
                       algorithm='sha256').hexdigest() if connection.configured else ''


def connection_summary(connection, catalog):
    ready = catalog[0]['status'] == 'available'
    return {
        'configured': connection.configured,
        'provider': connection.provider,
        'provider_label': connection.provider_label,
        'model': connection.model,
        'revision': connection_revision(connection),
        'readiness': 'available' if ready else 'pending' if connection.configured else 'unconfigured',
        'notice': ('通用对话已通过当前连接验收。每位用户每小时最多尝试 20 次。' if ready
                   else '连接已保存；请在本机运行“验收模型.cmd”，完成真实对话验收。' if connection.configured
                   else '请在项目文件夹双击“配置模型.cmd”，在本机填写服务密钥并选择模型。'),
    }

