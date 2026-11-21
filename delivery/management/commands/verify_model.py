"""Explicit local acceptance workflow. Merely displaying help never reads credentials."""
from django.core.management.base import BaseCommand, CommandError

from delivery.acceptance import (
    AcceptanceError, MANUAL_NOTICE, MAX_OUTPUT_TOKENS, approve_report, discover_models, recheck_report, run_acceptance,
)
from delivery.connections import ConnectionError, ENDPOINTS, get_connection, load_named_connection


class Command(BaseCommand):
    help = '预览、运行合成模型测试并由本机操作者审核；默认不读取密钥、不外发请求。'

    def add_arguments(self, parser):
        action = parser.add_mutually_exclusive_group()
        action.add_argument('--run', action='store_true', help='显式允许支持的官方名单查询和最多5次合成生成请求')
        action.add_argument('--approve', action='store_true', help='审核签名报告并启用已通过的四项通用能力')
        action.add_argument('--interactive', action='store_true', help='逐步确认的本机验收向导')
        action.add_argument('--recheck', action='store_true', help='仅对已有签名报告做离线规则复核')
        parser.add_argument('--report', help='审批使用的原始JSON报告路径')
        parser.add_argument('--confirm-reviewed', action='store_true', help='确认已人工阅读样例输入输出并接受质量')
        parser.add_argument('--region-reviewed', action='store_true', help='确认服务地区符合使用要求')
        parser.add_argument('--terms-reviewed', action='store_true', help='确认服务条款允许当前使用')
        parser.add_argument('--reviewer-email', default='', help='可选；需为已启用MFA的超级管理员，否则按本机操作者审核')
        parser.add_argument('--provider', choices=tuple(ENDPOINTS), help='验收指定的已保存连接，不切换当前主连接')

    def handle(self, *args, **options):
        try:
            if options['interactive']:
                self.interactive(options)
            elif options['run']:
                if not (options['region_reviewed'] and options['terms_reviewed']):
                    raise AcceptanceError('运行前请加 --region-reviewed --terms-reviewed，确认允许联系该服务。')
                path, report = run_acceptance(connection=self.selected_connection(options))
                self.show_report(path, report)
                if not report['passed']:
                    raise AcceptanceError('部分样例未通过，报告已保留，能力尚未启用。')
                self.stdout.write('检查通过，仍待人工审核。阅读报告后再使用 --approve --report 路径及三项确认参数。')
            elif options['recheck']:
                if not options['report']:
                    raise AcceptanceError('离线复核必须提供原始JSON报告路径。')
                path, report = recheck_report(options['report'])
                self.show_report(path, report)
                self.stdout.write('已离线复核，原始报告未修改、未发送请求，仍须人工审核。')
            elif options['approve']:
                if not options['report']:
                    raise AcceptanceError('审批必须提供 --report 原始报告路径。')
                approve_report(options['report'], confirm_reviewed=options['confirm_reviewed'],
                               region_reviewed=options['region_reviewed'], terms_reviewed=options['terms_reviewed'],
                               reviewer_email=options['reviewer_email'], connection=self.selected_connection(options))
                self.stdout.write(self.style.SUCCESS('人工审核已记录：四项通用对话能力已启用，有效期从测试时间起30天。'))
            else:
                self.stdout.write('验收预览：问答、中文写作、事实总结、两轮上下文。只使用虚构材料。')
                self.stdout.write(f'实际运行对支持的服务查询官方模型名单并最多生成5次，每次输出上限 {MAX_OUTPUT_TOKENS} tokens（包括思考），可能产生费用。')
                self.stdout.write('千问兼容接口不提供模型名单接口；模型名称须在对应平台及区域的官方目录确认。')
                self.stdout.write(MANUAL_NOTICE)
                self.stdout.write('当前未读取连接密钥、未发送请求、未启用能力。使用 --interactive 逐步操作，或 --run --region-reviewed --terms-reviewed 生成报告。')
        except (AcceptanceError, ConnectionError) as error:
            raise CommandError(str(error)) from None
        except (KeyboardInterrupt, EOFError):
            self.stdout.write('操作已取消，未执行后续审批。')

    def show_report(self, path, report):
        self.stdout.write(f'报告：{path}')
        if report['provider'] == 'qwen':
            self.stdout.write('千问兼容接口未进行模型名单认证。以下结果只来自此次合成生成验收。')
        self.stdout.write(MANUAL_NOTICE)
        for result in report['results']:
            self.stdout.write(f"\n{result['capability']}：{'机械检查通过，待人工审核' if result['passed'] else '未通过'}")
            if result.get('response_format'):
                self.stdout.write('本项使用结构化传输；回复在本机完成字数校验后再展示，不是逐字流式展示。')
            if result.get('system_instruction'):
                self.stdout.write(f"系统指令：{result['system_instruction']}")
            for index, prompt in enumerate(result['prompts']):
                self.stdout.write(f'输入 {index + 1}：{prompt}')
                if index < len(result['outputs']):
                    self.stdout.write(f"输出 {index + 1}：{result['outputs'][index]}")
                provider_outputs = result.get('provider_outputs', [])
                if index < len(provider_outputs) and provider_outputs[index] is not None:
                    self.stdout.write(f'模型原始结构化响应 {index + 1}：{provider_outputs[index]}')
            for check in result['checks']:
                self.stdout.write(f"  {'通过' if check['passed'] else '未通过'}：{check['name']}")
            if result['error']:
                self.stdout.write(result['error'])
                if result.get('diagnostic_code'):
                    self.stdout.write(f"诊断类别：{result['diagnostic_code']}")

    def selected_connection(self, options):
        provider = options.get('provider')
        connection = load_named_connection(provider) if provider else get_connection()
        if connection is None:
            raise ConnectionError('指定的模型连接尚未保存，请先完成该服务的连接配置。')
        return connection

    def interactive(self, options):
        connection = self.selected_connection(options)
        if not connection.configured:
            raise AcceptanceError('请先完成模型连接设置。')
        self.stdout.write(f'服务：{connection.provider_label}；模型：{connection.model}；服务地区：{connection.service_region}')
        catalog = None
        if connection.provider == 'qwen':
            self.stdout.write('千问兼容接口不提供模型名单；请先在对应平台和区域的官方目录确认模型名称。')
            if input('已确认模型名称、密钥创建区域和服务条款？输入“确认配置”继续：').strip() != '确认配置':
                self.stdout.write('已取消，未联系模型服务。')
                return
        else:
            self.stdout.write('第一步只联系官方服务获取模型名单，不发送聊天内容。')
            if input('已确认服务地区和条款允许使用？输入“确认并获取名单”继续：').strip() != '确认并获取名单':
                self.stdout.write('已取消，未联系模型服务。')
                return
            catalog = discover_models(connection)
            self.stdout.write('官方返回的模型：' + '、'.join(catalog))
        self.stdout.write(f'接下来最多5次生成，每次输出上限 {MAX_OUTPUT_TOKENS} tokens（包括思考），可能产生费用；只发送合成材料。')
        if input('输入“开始验收”继续：').strip() != '开始验收':
            self.stdout.write('已取消，未发送生成请求。')
            return
        path, report = run_acceptance(connection=connection, catalog=catalog)
        self.show_report(path, report)
        if not report['passed']:
            self.stdout.write('存在未通过项目，报告已保存，能力尚未启用。')
            return
        self.stdout.write('请逐项阅读实际输入输出，确认语言、事实与上下文质量符合预期，并确认服务地区和条款。')
        if input('完成上述人工审核后，输入“确认启用”继续：').strip() != '确认启用':
            self.stdout.write('报告已保留，能力仍待审核。')
            return
        approve_report(path, confirm_reviewed=True, region_reviewed=True, terms_reviewed=True,
                       reviewer_email=options['reviewer_email'], connection=self.selected_connection(options))
        self.stdout.write(self.style.SUCCESS('四项通用对话能力已启用，有效期从测试时间起30天。'))
