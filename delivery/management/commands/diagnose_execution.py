"""Inspect execution prerequisites without running generated source or pulling images."""
import json

from django.core.management.base import BaseCommand

from delivery.execution import readiness


class Command(BaseCommand):
    help = '检查本机隔离执行环境；不运行成果、不下载镜像、不调用模型。'

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(readiness(), ensure_ascii=False, indent=2))
