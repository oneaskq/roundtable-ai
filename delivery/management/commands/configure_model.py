"""Local operator wizard. Credentials never travel through a product API or argv."""
import getpass
import sys

from django.core.management.base import BaseCommand, CommandError

from delivery.connections import (ConnectionError, ProviderConnection, QWEN_REGIONS, get_connection,
                                  load_named_connection, save_connection, save_named_connection)
from delivery.provider import ProviderError, QWEN_TEXT_MODELS, list_models


class Command(BaseCommand):
    help = '在本机安全配置 DeepSeek / Kimi / 千问官方接口；不发送生成请求。'

    def add_arguments(self, parser):
        parser.add_argument('--status', action='store_true', help='仅显示服务商、模型和区域，不显示密钥。')
        parser.add_argument('--provider', choices=('deepseek', 'kimi', 'qwen'),
                            help='预选服务商；配合 --status 查看指定的命名连接。')

    def handle(self, *args, **options):
        if options['status']:
            try:
                connection = load_named_connection(options['provider']) if options['provider'] else get_connection()
            except ConnectionError:
                raise CommandError('连接设置无法读取，请重新运行配置向导。') from None
            if connection is None or not connection.configured:
                self.stdout.write('尚未配置模型连接。')
                return
            self.stdout.write(f'服务商：{connection.provider_label}\n模型：{connection.model}\n验收适用区域：{connection.service_region}')
            self.stdout.write('连接已配置；能力是否开放以真实验收结果为准。')
            return
        if not sys.stdin.isatty():
            raise CommandError('请双击“配置模型.cmd”在本机交互窗口填写，不要通过命令参数或聊天传递密钥。')
        self.stdout.write('圆桌 · 官方模型连接设置\n密钥输入时不会显示，将加密保存在本机 data 目录。')
        self.stdout.write('1. DeepSeek 官方接口\n2. Kimi 官方接口\n3. 千问按量付费接口（阿里云百炼或 QwenCloud）')
        try:
            provider = options['provider'] or {'1': 'deepseek', '2': 'kimi', '3': 'qwen'}.get(
                input('选择服务商（1 / 2 / 3）：').strip())
            if not provider:
                raise CommandError('请选择 1、2 或 3。连接未更改。')
            platform = ''
            if provider == 'qwen':
                platform = {'1': 'bailian', '2': 'qwencloud'}.get(
                    input('密钥来自哪个平台？1 阿里云百炼 / 2 QwenCloud：').strip(), '')
                if not platform:
                    raise CommandError('请选择密钥创建的平台。连接未更改。')
            api_key = getpass.getpass('粘贴该服务的 API 密钥（输入隐藏）：').strip()
            if not api_key or len(api_key) > 4096 or any(character.isspace() for character in api_key):
                raise CommandError('密钥为空或格式不正确。连接未更改。')
            if provider == 'qwen':
                self.stdout.write('密钥与区域绑定，请填写创建该密钥时的区域；不会自动尝试其他地区。')
                regions = {'cn-beijing': '北京'} if platform == 'bailian' else QWEN_REGIONS
                for region, label in regions.items():
                    self.stdout.write(f'{region}：{label}')
            service_region = input('密钥创建区域 ID：' if provider == 'qwen' else
                                   '此次验收适用的服务区域（请填写实际使用区域）：').strip()
            if not service_region or len(service_region) > 80:
                raise CommandError('请填写不超过 80 个字符的服务区域。连接未更改。')
            discovery = ProviderConnection(provider=provider, model='', api_key=api_key, service_region=service_region,
                                           service_platform=platform)
            if provider == 'qwen':
                self.stdout.write('当前兼容接口不提供 /models。请先在对应平台和区域的官方目录确认可用文字模型：')
                self.stdout.write('https://help.aliyun.com/zh/model-studio/text-generation-model' if platform == 'bailian'
                                  else 'https://docs.qwencloud.com/developer-guides/getting-started/text-generation-models')
                self.stdout.write('当前适配 Qwen3.7/3.8 Max、Qwen3.5/3.6/3.7 Plus、Qwen3.5/3.6/3.7/3.8 Flash 及已核对的无思考版本。')
                model = input('粘贴已在所选区域确认的完整模型 ID（无默认值）：').strip()
                if model not in QWEN_TEXT_MODELS:
                    raise CommandError('模型名称尚未适配当前文字接口。连接未更改。')
            else:
                self.stdout.write('正在从所选服务的官方接口获取当前账号可用模型；不会发送对话请求。')
                try:
                    models = list_models(discovery)
                except (ProviderError, ConnectionError):
                    raise CommandError('无法获取官方模型名单，请检查服务商、密钥与网络后重试。连接未更改。') from None
                if not models:
                    raise CommandError('官方接口没有返回可选模型。连接未更改。')
                for index, model in enumerate(models, 1):
                    self.stdout.write(f'{index}. {model}')
                selection = input('输入要使用的模型序号：').strip()
                if not selection.isdecimal() or not 1 <= int(selection) <= len(models):
                    raise CommandError('模型序号无效。连接未更改。')
                model = models[int(selection) - 1]
            connection = ProviderConnection(provider=provider, model=model,
                                            api_key=api_key, service_region=service_region, service_platform=platform)
            try:
                save_named_connection(connection)
                if provider != 'qwen':
                    save_connection(connection)
            except ConnectionError:
                raise CommandError('无法保存连接设置，请检查本机数据目录权限。') from None
        except ConnectionError:
            raise CommandError('连接信息格式无效，请重新运行向导检查填写内容。') from None
        except (EOFError, KeyboardInterrupt):
            self.stdout.write('\n配置已取消，连接未更改。')
            return
        self.stdout.write(self.style.SUCCESS(f'已保存 {provider} / {connection.model}。密钥没有写入命令参数或网页。'))
        if provider == 'qwen':
            self.stdout.write('千问已另存为独立连接，当前主连接不变。未发送任何请求，尚未验证密钥或模型可用性。')
            self.stdout.write('以后需要生成验收时，使用 verify_model --provider qwen --interactive；各角色仍需单独选择该连接。')
            return
        self.stdout.write('下一步：双击“验收模型.cmd”。完成真实问答、写作、总结与上下文验收后，网页才会开放 AI 回复。')
