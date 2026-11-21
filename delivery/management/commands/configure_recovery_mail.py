"""Interactive Gmail setup. Never places an app password in argv or chat."""
import getpass
import re
import smtplib
import ssl
import sys

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email

from accounts.local_mail import MailConfigError, save_local_mail


class Command(BaseCommand):
    help = '在本机交互配置 Gmail 找回邮件；只验证连接，不发送邮件。'

    def handle(self, *args, **options):
        if not settings.DEBUG or not settings.LOCAL_MAIL_CONFIG_ENABLED:
            raise CommandError('此向导仅适用于没有外部邮件环境变量覆盖的本机内测。')
        if not sys.stdin.isatty():
            raise CommandError('请双击“配置找回邮件.cmd”在本机输入；不要把授权密码发到聊天或写入命令。')
        self.stdout.write('圆桌 · Gmail 找回邮件配置')
        self.stdout.write('需要 Google 账号的应用专用密码，不能使用 Google 登录密码。输入时不会显示。')
        self.stdout.write('本机版恢复链接只能在安装圆桌的这台电脑上打开；验证连接不会发送邮件。')
        try:
            email = input('发信 Gmail 地址：').strip().casefold()
            try:
                validate_email(email)
            except ValidationError:
                raise CommandError('请输入有效的 Gmail 地址；设置未更改。') from None
            if not email.endswith(('@gmail.com', '@googlemail.com')):
                raise CommandError('请填写 Gmail 地址；设置未更改。')
            secret = re.sub(r'\s+', '', getpass.getpass('Google 应用专用密码（输入隐藏）：'))
            if not re.fullmatch(r'[A-Za-z]{16}', secret):
                raise CommandError('应用专用密码格式不正确；设置未更改。')
            if input('将连接 Gmail 验证授权并加密保存，是否继续？输入 Y 确认：').strip().casefold() != 'y':
                self.stdout.write('已取消，邮件设置未更改。')
                return
            try:
                with smtplib.SMTP('smtp.gmail.com', 587, timeout=15) as smtp:
                    smtp.ehlo()
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                    smtp.login(email, secret)
            except (smtplib.SMTPException, OSError):
                raise CommandError('Gmail 连接或授权验证失败；请检查网络和应用专用密码，设置未更改。') from None
            record = {'version': 1, 'host': 'smtp.gmail.com', 'port': 587, 'username': email,
                      'password': secret, 'sender': email, 'tls': True, 'ssl': False}
            try:
                save_local_mail(settings.LOCAL_EMAIL_PATH, settings.MFA_ENCRYPTION_KEY, record)
            except MailConfigError:
                raise CommandError('无法加密保存邮件设置；原有设置未更改。') from None
        except (EOFError, KeyboardInterrupt):
            self.stdout.write('\n已取消，邮件设置未更改。')
            return
        self.stdout.write(self.style.SUCCESS('Gmail 授权已验证并加密保存。请刷新圆桌登录页后使用“忘记密码”。'))
        self.stdout.write('向导没有发送邮件；第一次使用时请确认邮件能收到，并在这台电脑打开链接。')
