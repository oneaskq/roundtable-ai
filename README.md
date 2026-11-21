# 圆桌 Roundtable

圆桌是一个在本机运行的多 AI 决策助手。你提出问题，团队会在必要时追问、从不同角度分析、互相审查，再把建议写成对话里的最后一条回复。你可以中途补充条件。遇到证据不足或成员意见不一致时，报告会保留分歧，说明哪些新信息可能改变判断。

## 先看一段讨论

这是一段手写的虚构示例，可以直接在 GitHub 展开阅读。下载源码后也可以双击 [实习选择示例](static/examples/internship.html) 离线回放，无需安装依赖、注册账号或配置密钥。启动圆桌后，新对话首页也有同一入口。

**提问**　第一次找实习，希望有人带。A 有固定导师、每周复盘，单程通勤 70 分钟；B 步行 15 分钟，但还没说清工作内容。应该先和哪家谈？

**初步建议**　优先和 A 的导师谈首月指导安排，同时请 B 说明任务和反馈人。A 每个到岗日往返多花 110 分钟，两家的到岗天数会影响选择。

<details>
<summary>提问者补充「我每周有两天必须返校」后，建议怎样变了？</summary>

**调整后的建议**　先向两家确认能否每周到岗三天。若 A 接受，且导师能在到岗日指导，仍优先谈 A；若 A 要求四天以上而 B 接受三天，转向 B 核实工作内容与指导人。返校是硬约束，排班可行性要先于导师与通勤的权衡。

</details>

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

- 讨论需要外部事实时，圆桌会建议一组公开检索关键词。你可以修改、同意或跳过。只有确认的关键词会发送给界面标明的检索服务。聊天记录和附件不会作为检索请求发送。返回的公开资料可供参与本轮讨论的模型引用。
- 默认从维基百科读取背景资料。需要搜索一般网页时，可在启动圆桌前为服务器进程设置 `BRAVE_SEARCH_API_KEY`，使用自己的 Brave Search API 额度。系统最多读取两个来源；检索失败、没有结果或网页正文不可读时，会在报告中说明。点开依据可查看原文。报告中的读取日期只表示获取时间，招聘条件、政策和价格等仍需核对发布方及生效日期。
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
