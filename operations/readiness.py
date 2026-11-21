"""Configuration diagnostics only. No provider requests, database queries, or side effects."""
from datetime import datetime, timezone

from operations.production import public_host, valid_fernet, valid_public_origin, valid_secret


def build_release_report(config, environ, *, implemented_capabilities=(), required_capabilities=()):
    checks = []

    def add(key, label, condition, success, failure):
        checks.append({'id': key, 'name': label, 'status': 'passed' if condition else 'failed',
                       'detail': success if condition else failure, 'scope': 'configuration'})

    get = lambda name, default=None: getattr(config, name, default)
    production = get('ENVIRONMENT') == 'production' and get('DEBUG') is False
    add('environment', '生产环境开关', production, '已显式使用生产设置。', '当前仍是本地或非生产配置。')
    external_keys = (valid_secret(environ.get('DJANGO_SECRET_KEY')) and
                     environ.get('DJANGO_SECRET_KEY') == get('SECRET_KEY') and
                     valid_fernet(environ.get('MFA_ENCRYPTION_KEY')) and
                     environ.get('MFA_ENCRYPTION_KEY') == get('MFA_ENCRYPTION_KEY'))
    add('secret_source', '生产密钥来源', external_keys,
        '运行配置与显式环境密钥一致；未输出密钥。', '需要独立生产签名与加密密钥；本地文件密钥不能作为生产验证依据。')
    hosts = get('ALLOWED_HOSTS', [])
    host_ok = isinstance(hosts, (list, tuple)) and bool(hosts) and all(public_host(item) for item in hosts)
    add('public_origin', '域名与恢复链接', host_ok and valid_public_origin(get('PUBLIC_BASE_URL', ''), hosts),
        '显式域名与 HTTPS 来源匹配。', '需要明确的公网域名及匹配的 HTTPS 入口，不能使用通配符或本机地址。')
    secure = (get('SESSION_COOKIE_SECURE') is True and get('CSRF_COOKIE_SECURE') is True and
              get('SESSION_COOKIE_HTTPONLY') is True and get('SECURE_SSL_REDIRECT') is True and
              isinstance(get('SECURE_HSTS_SECONDS'), int) and get('SECURE_HSTS_SECONDS') > 0 and
              get('SECURE_CONTENT_TYPE_NOSNIFF') is True and get('X_FRAME_OPTIONS') == 'DENY')
    add('transport', 'HTTPS 与浏览器保护配置', secure,
        '安全 Cookie、HTTPS 跳转与响应头配置已启用；网络实测仍待完成。', 'HTTPS、Cookie 或安全响应头配置不完整。')
    proxy = get('SECURE_PROXY_SSL_HEADER')
    add('trusted_proxy', '反向代理信任', proxy is None or (
        proxy == ('HTTP_X_FORWARDED_PROTO', 'https') and environ.get('ROUNDTABLE_TRUST_PROXY_TLS') == '1'),
        '没有隐式信任代理头；如已启用仍需验证入口覆盖客户端头。', '代理头信任必须显式配置并核对入口行为。')
    hashers = get('PASSWORD_HASHERS', [])
    add('password_hashing', '密码保存设置', bool(hashers) and all(
        item in {'django.contrib.auth.hashers.PBKDF2PasswordHasher',
                 'django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher',
                 'django.contrib.auth.hashers.Argon2PasswordHasher',
                 'django.contrib.auth.hashers.BCryptSHA256PasswordHasher',
                 'django.contrib.auth.hashers.ScryptPasswordHasher'} for item in hashers),
        '未发现测试专用快速密码哈希。', '缺少生产密码哈希，或仍包含测试用快速哈希。')
    db = get('DATABASES', {}).get('default', {})
    db_ok = (db.get('ENGINE') == 'django.db.backends.postgresql' and
             all(db.get(key) for key in ('NAME', 'USER', 'PASSWORD', 'HOST')) and
             db.get('OPTIONS', {}).get('sslmode') == 'verify-full')
    add('database', '生产数据库配置', db_ok, 'PostgreSQL 与证书校验已配置；本命令未连接数据库。',
        '仍需配置 PostgreSQL 及验证服务证书，SQLite 不满足本项目正式开放要求。')
    mail_ok = (get('EMAIL_DELIVERY_ENABLED') is True and
               get('EMAIL_BACKEND') == 'django.core.mail.backends.smtp.EmailBackend' and
               all(get(key) for key in ('EMAIL_HOST', 'EMAIL_HOST_USER', 'EMAIL_HOST_PASSWORD')) and
               bool(get('EMAIL_USE_TLS')) != bool(get('EMAIL_USE_SSL')))
    add('mail_configuration', '恢复邮件配置', mail_ok,
        '已配置加密 SMTP；本命令未发送邮件。', '需要可用的加密 SMTP 及发送账号，内存邮件不能用于恢复。')
    implemented, required = set(implemented_capabilities), set(required_capabilities)
    complete = bool(required) and required.issubset(implemented)
    add('capability_implementation', '承诺能力接入范围', complete,
        '全部登记子能力已有实现标记；仍须独立真实验收。',
        f'已实现标记 {len(required & implemented)}/{len(required)} 项；能力入口不等于完成交付。')
    # These checks cannot be cleared by config flags, timestamps, arbitrary JSON, or model self-review.
    pending = (
        ('model_quality', '真实模型与角色优选', '需要当前连接的真实任务验收、角色效果比较和人工复核。'),
        ('team_quality', '任务规划与团队质量', '需要真实任务证明分工、依赖、质疑和最终验收可靠。'),
        ('isolated_execution', '隔离执行实测', '需要部署环境的隔离、安全限制、运行与功能测试证据。'),
        ('mail_recovery', '邮件送达与账号恢复', '需要在目标环境完成真实收信、重置与会话失效测试。'),
        ('data_lifecycle', '保留与永久删除', '自动清理、账号永久删除及备份中删除履约尚待完整交付和演练。'),
        ('backup_restore', '生产备份恢复', '需要目标 PostgreSQL 的备份、隔离恢复与回滚演练；本地 SQLite 演练不能代替。'),
        ('durable_jobs', '长任务与中断恢复', '需要生产后台执行、重启恢复和重复处理控制的验证。'),
        ('security_load', '安全与并发', '需要目标环境权限、安全、容量和异常行为测试。'),
        ('beta_exit', '公测退出条件', '需要完成内部测试和小范围公测的问题清单与验收。'),
    )
    for key, label, detail in pending:
        checks.append({'id': key, 'name': label, 'status': 'pending', 'detail': detail, 'scope': 'acceptance'})
    counts = {status: sum(item['status'] == status for item in checks) for status in ('passed', 'failed', 'pending')}
    return {'schema_version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'target': 'official_release', 'ready': counts['failed'] == counts['pending'] == 0,
            'scope': 'read_only_configuration_preflight', 'counts': counts, 'checks': checks,
            'notice': '预检查不会启用能力、发送请求、连接数据库或部署；通过配置检查不代表可正式开放。'}
