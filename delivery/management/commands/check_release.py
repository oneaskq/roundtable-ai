import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from delivery.catalog import ALL_CAPABILITIES, IMPLEMENTED
from operations.readiness import build_release_report


class Command(BaseCommand):
    help = '只读检查正式开放前的配置与待验收项；不调用模型、不发邮件、不连接数据库。'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', dest='as_json', help='输出不含密钥的机器可读报告。')
        parser.add_argument('--strict', action='store_true', help='存在失败或待验收项时返回非零退出码。')

    def handle(self, *args, **options):
        report = build_release_report(settings, os.environ,
                                      implemented_capabilities=IMPLEMENTED, required_capabilities=ALL_CAPABILITIES)
        if options['as_json']:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            for item in report['checks']:
                self.stdout.write(f"[{item['status']}] {item['name']}：{item['detail']}")
            self.stdout.write(report['notice'])
        if options['strict'] and not report['ready']:
            raise CommandError('正式开放条件尚未满足。', returncode=2)
