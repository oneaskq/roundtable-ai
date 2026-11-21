# 圆桌 Roundtable

圆桌是一个在本机运行的多 AI 决策助手。你提出问题，团队会在必要时追问、从不同角度分析、互相审查，再把建议写成对话里的最后一条回复。你可以中途补充条件。遇到证据不足或成员意见不一致时，报告会保留分歧，说明哪些新信息可能改变判断。

例如，试着问「我有两个实习机会，A 有导师但通勤远，B 离家近但工作内容还没谈清。我该先向哪家追问什么？」讨论中再告诉团队「我每周有两天必须回校」，观察最后的建议如何调整。

## 在 Windows 上打开

1. 下载并解压源码，或把仓库克隆到一个可写的文件夹。
2. 安装带 Windows `py` 启动器的 Python 3.14。此版本的启动脚本会检查 Python 至少为 3.14.10，且所带 OpenSSL 为 3.5.9 或更高的 3.5.x。可用下方命令查看版本。
3. 双击 **打开圆桌.cmd**。首次会创建虚拟环境、安装锁定依赖并初始化本机数据库。启动完成后，在浏览器打开 [本机圆桌](http://127.0.0.1:8910/)，注册自己的账号。保留启动窗口即可继续使用。

```powershell
py -3.14 -c "import sys, ssl; print(sys.version.split()[0], ssl.OPENSSL_VERSION)"
```

没有模型密钥时也可以创建工作区、保存问题和添加参考资料。需要看到真实 AI 回复时，再连接自己的模型服务。首次安装需要联网下载 Python 依赖。

## 连接模型

1. 双击 **配置模型.cmd**，选择 DeepSeek、Kimi 或千问，按提示输入自己的 API 密钥、区域和模型。密钥输入时隐藏，保存在本机的加密连接文件中。
2. DeepSeek 或 Kimi 配置后可双击 **验收模型.cmd**，根据提示测试当前主连接。向导会在你确认后发送合成样例，最多生成五次，按服务商价格计费。审阅结果并启用后，回到网页查看团队状态。
3. 在对话设置里确认团队与模型，输入问题开始讨论。真实讨论会把任务上下文和选中的参考资料发送给参与的模型服务商。

若要组合多个服务商，分别运行配置向导，按名称测试要用的每个连接，再为各角色选择模型。千问始终保存为命名连接。以下命令在项目根目录的 PowerShell 中运行，示例假设三种连接均已配置。

```powershell
.\.venv314\Scripts\python.exe manage.py verify_model --provider deepseek --interactive
.\.venv314\Scripts\python.exe manage.py verify_model --provider kimi --interactive
.\.venv314\Scripts\python.exe manage.py verify_model --provider qwen --interactive
.\.venv314\Scripts\python.exe manage.py configure_team --proposer deepseek --challenger kimi --reviewer qwen --synthesizer deepseek --verifier kimi
```

## 资料、隐私和运行范围

- 参考资料支持粘贴文字、上传文件，以及读取你提供的公开 HTTPS 网页。截图和扫描 PDF 的文字识别需要本机的 OCR 语言支持。阅读网页可提供引用材料，资料中的事实仍需核对。
- 本机账号、聊天、资料和连接配置保存在 `data/`。请将该目录和导出的聊天记录作为私人文件保管。提交问题反馈时可使用虚构输入复现。
- 当前发布的是本机运行的源码预览。服务默认仅监听回环地址。真实调用使用你自己的服务商账户，产生的费用由该账户承担。

## 开发与测试

后端使用 Django 和 SQLite，前端使用原生 JavaScript。Windows 启动脚本调用 `launch.py`，它负责迁移数据库、启动网页和后台任务进程。命令行用户也可以在自己的虚拟环境中安装 `requirements.lock`，再用该环境的 Python 运行 `launch.py`。

在项目根目录运行测试。第一条命令会加载隔离的 Django 测试配置；第二条需要 Node.js 22 或更高版本，使用内置测试运行器。

```powershell
.\.venv314\Scripts\python.exe manage.py test --noinput
node --test "tests/*.test.cjs"
```

## 许可

项目源码使用 [MIT 许可证](LICENSE)。Python、依赖包及其随附组件各自遵循上游许可。服务商名称用于标识支持的接口。
