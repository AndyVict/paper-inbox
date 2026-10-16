# Paper Inbox

**把 X-MOL / Stork 文献订阅邮件，整理成去重的阅读清单。**

公开摘要、中文翻译、用户自备期刊指标、手机推送，按需启用。Python 标准库运行，核心流程无需 AI Agent。

[English](README.en.md) · [离线演示页面](examples/demo-report.html) · [配置指南](docs/SETUP.md) · [配套 Skill](skills/paper-inbox/SKILL.md)

![Paper Inbox 合成演示：两封订阅邮件的四条候选合并为三篇文献](docs/assets/demo.png)

## 先体验，无需账号

使用 Python 3.11 或以上版本，克隆仓库后直接运行：

```powershell
git clone https://github.com/AndyVict/paper-inbox.git
cd paper-inbox
python -X utf8 scripts/demo.py
```

也可以从 GitHub 下载源码 ZIP，解压后在项目目录运行同一条演示命令。

用浏览器打开 `data/demo/report.html`，即可看到 **2 封邮件 → 4 条候选 → 3 篇文献**。演示复用真实解析器和去重逻辑，摘要与译文为固定合成内容；不读取 `.env`、不联网、不推送、不写入活动数据库。

想先看结果，可以阅读 [Markdown 示例](examples/demo-report.md)。全部示例论文、标识和指标模板都是合成数据。

## 它能做什么

| 功能 | 行为与边界 |
| --- | --- |
| 解析订阅 | X-MOL / Stork 的 HTML 或纯文本邮件；其他格式可能需要适配 |
| 合并重复 | DOI → PMID → 标准化标题与年份，保留来源和稳定 ID |
| 接续运行 | SQLite 保存游标、缓存和处理状态，重复运行复用已有结果 |
| 查询摘要 | PubMed / Europe PMC / Crossref / OpenAlex；未取得可靠摘要时保留缺项 |
| 中文翻译 | 可选小牛 `cloud` / `neu` 接口；译文缓存和长度分段 |
| 期刊指标 | 可选导入用户自备数据，以 ISSN/eISSN 精确匹配；不附带 JCR 数据库 |
| 手机推送 | 可选 WxPusher App，记录平台接收与人工确认，结果不明不自动重发 |
| 定时运行 | Windows 任务脚本；其他系统可自行调用 CLI，调度适配待验证 |

## 安装与真实使用

以下命令在项目根目录执行。Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\paper-inbox.exe --help
if (Test-Path -LiteralPath .env) { throw 'config_already_exists' }
Copy-Item -LiteralPath .env.example -Destination .env
```

复制配置是首次安装操作，已有 `.env` 时直接编辑自己的文件。macOS / Linux 的虚拟环境解释器和命令分别位于 `.venv/bin/python`、`.venv/bin/paper-inbox`。

编辑 `.env` 的 `IMAP_*` 配置，再先检查格式、只读收信：

```powershell
.\.venv\Scripts\paper-inbox.exe sync --check-config
.\.venv\Scripts\paper-inbox.exe sync --fetch --max-mails 20
```

查看 `data/outbox/` 中的 Markdown。收信使用只读连接和 `BODY.PEEK`，不标记已读、不移动或删除邮件。

再按需要增加功能：

```powershell
# 收信 + 查询公开摘要 + 英文发送预览；不需要翻译或 JCR 配置，不发送
.\.venv\Scripts\paper-inbox.exe weekly --fetch

# 配好小牛后加入翻译；仍只生成中文预览
.\.venv\Scripts\paper-inbox.exe weekly --fetch --stages sync,abstracts,translation,delivery --language zh

# 查询发送记录，不加载推送配置、不联网
.\.venv\Scripts\paper-inbox.exe send --status
```

`weekly` 默认阶段为 `sync,abstracts,delivery`、默认英文；单独 `send` 默认中文，可通过 `--language en` 改为原文。阶段按固定依赖顺序执行，未选阶段不调用其配置或服务。中文真实发送会暂缓尚未完成翻译的文献。

真实推送须显式 `--send`；首次单条测试、实际接收确认及每周任务见 [配置指南](docs/SETUP.md)。只有 `--fetch` 才启用对应阶段的联网查询。纯 `weekly` 默认只使用本地数据和缓存。

## 支持状态

- Python 声明范围为 **3.11+**；各版本和平台的实际验证记录见 [发布状态](docs/RELEASE_STATUS.md)。CI 计划覆盖 Windows / Linux、Python 3.11–3.14；远程运行结果以 Actions 为准。
- IMAP 支持可配置的 TLS 主机、端口和 ASCII 文件夹名，使用密码或授权码登录。**163 已做过真实连接验证**；其他服务仅有模拟验证，OAuth2 登录尚未实现。
- 主要接收端为 **WxPusher App**。微信渠道依赖平台绑定及激活条件，不承诺个人微信无人值守投递。
- 当前为 `0.1.0` 发布候选版。手动端到端流程已使用；首次实际周任务验收仍待完成，不宣称已长期无人值守运行。

## 配套 Agent Skill

[skills/paper-inbox/](skills/paper-inbox/SKILL.md) 提供环境检查、配置引导、预览、状态查询和故障定位。把该目录安装到支持 Agent Skills 的客户端，并另外安装本工具；仅安装 Skill 不会安装 Python 核心。

可提出：“用 paper-inbox 先跑离线演示，再帮我检查本地收信配置。” 客户端与安装渠道的兼容性见 [Skill 说明](docs/SKILL.md)。

## 参与贡献

欢迎提交合成邮件样例、新来源解析器、服务适配和文档改进。请先阅读 [贡献指南](CONTRIBUTING.md)；报告问题时只提供错误码和合成复现材料。

```powershell
python -X utf8 -m unittest discover -s tests -q
python -X utf8 scripts/check_release.py
```

原始邮件、授权码、数据库、日志和用户自备期刊数据均留在本地；详见 [安全与数据流](SECURITY.md) 和 [数据来源](PROVENANCE.md)。

代码与合成示例采用 [MIT License](LICENSE)。外部服务和用户自备数据受各自的使用条件约束。
