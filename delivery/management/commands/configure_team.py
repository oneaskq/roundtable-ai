"""Select existing local connections per role without contacting a model provider."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from delivery.connections import ConnectionError
from delivery.team_config import ROLES, SOURCES, load_routes, resolve_team, save_routes


class Command(BaseCommand):
    help = '查看或设置文字团队各角色的已有连接；不读取用户对话、不发起模型调用。'

    def add_arguments(self, parser):
        for role, label in ROLES:
            parser.add_argument('--' + role, choices=SOURCES, help=f'{label}使用的已保存连接')

    def handle(self, *args, **options):
        try:
            changes = {role: options[role] for role, _ in ROLES if options[role] is not None}
            if changes and getattr(settings, 'TEAM_ROLE_CONNECTIONS', None) is not None:
                raise ConnectionError('团队角色由应用设置指定，请先调整应用设置后再使用本机配置。')
            routes = load_routes()
            routes.update(changes)
            connections = resolve_team(routes)
            if changes:
                if not all(connection.configured for connection in connections.values()):
                    raise ConnectionError('所选连接尚未配置完整，团队设置未保存。')
                save_routes(routes)
                self.stdout.write(self.style.SUCCESS('团队配置已保存；下次调用须按新的团队配置重新确认。'))
            for role, label in ROLES:
                connection = connections[role]
                self.stdout.write(f'{label}（{role}）：{routes[role]} / {connection.provider or "未配置"} / {connection.model or "未配置"}')
            self.stdout.write('此操作没有发起模型调用，也没有自动启用能力。各连接须分别完成真实验收。')
            self.stdout.write('当前角色路由是初始配置，尚未完成各角色模型优选。')
        except ConnectionError as error:
            raise CommandError(str(error)) from None
