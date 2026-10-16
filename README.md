# Paper Inbox

**每周整理 X-MOL / Stork 订阅邮件，把值得读的论文送到手机。**

合并重复文献，补齐中文标题、摘要和期刊指标，再通过 WxPusher App 逐篇推送。打开一条消息，就能看到论文讲什么、发表在哪本期刊，以及原文链接。

[English](README.en.md) · [完整消息样例](examples/demo-report.md) · [配置与推送教程](docs/SETUP.md) · [Agent Skill](docs/SKILL.md)

[![CI](https://github.com/AndyVict/paper-inbox/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/AndyVict/paper-inbox/actions/workflows/ci.yml)

## 省下哪些整理工作

- **不用逐封翻邮件。** 从邮箱读取 X-MOL / Stork 订阅，按周整理。
- **同一篇只处理一次。** 合并两个来源和不同批次中的重复文献，保留已有翻译与发送记录。
- **先用中文判断是否值得读。** 一篇一条，包含中英文标题、期刊、JIF/JCI 分区、中文摘要和原文链接。
- **中断后继续。** SQLite 保存进度和缓存，已有结果可复用；结果不明的投递保留待查。

期刊库随项目提供，含 **22,643 刊、32,215 条分类、254 个学科**，无需另接数据源即可离线查询。中文翻译使用小牛；收信、翻译、期刊匹配和推送可以分别启用。

## 消息预览

![已有消息的完整预览：中英文标题、期刊及分区、中文摘要和论文链接](docs/assets/demo.png)

*图为本地消息预览，使用与正式推送相同的模板。论文来自公开的 CC BY 4.0 文章，中文内容沿用已有机器译文；出处见[完整样例](examples/demo-report.md)。*

## 先看效果

直接打开[完整消息样例](examples/demo-report.md)，无需安装。也可以在本地生成相同的 HTML 展示：

```powershell
git clone https://github.com/AndyVict/paper-inbox.git
cd paper-inbox
python -X utf8 scripts/showcase.py
```

用浏览器打开 `data/showcase/report.html`。两条消息来自已有预览，保留完整摘要和七字段；不需要账号，不联网、不推送。

想查看邮件如何被解析、合并，另运行 `python -X utf8 scripts/demo.py`，打开 `data/demo/report.html`。这条演示使用合成邮件，展示 **2 封邮件 → 4 条候选 → 3 篇文献**，与真实待发队列分开。

## 离线查询期刊

克隆仓库后即可按期刊名或 ISSN 查询，无需邮箱、API 密钥或其他数据源：

```powershell
python -X utf8 scripts/journals.py --issn 1664-462X
python -X utf8 scripts/journals.py "Nature" --limit 5
```

安装后对应命令为 `paper-inbox journals`，可增加 `--json` 输出结构化结果。随包数据为 2025 年指标、2026 发布年，来源为维护者逐一下载整理并确认可公开分发的快照；版本与官方全集尚未独立核验，详见[期刊库说明](docs/JCR.md)。

## 开始使用

需要 Python **3.11+**。运行时只使用 Python 标准库。以下命令在项目根目录执行，适用于 Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

在本机编辑 `.env`，填写邮箱的 `IMAP_*` 配置。163 邮箱使用客户端授权码。先检查配置，再只读收信：

```powershell
.\.venv\Scripts\paper-inbox.exe sync --check-config
.\.venv\Scripts\paper-inbox.exe sync --fetch --max-mails 20
```

结果在 `data/outbox/`。收信不会标记已读、移动或删除邮件。

填写小牛配置后，可以继续生成中文消息预览：

```powershell
.\.venv\Scripts\paper-inbox.exe weekly --fetch --stages sync,abstracts,translation,jcr,delivery --language zh
```

查看 `data/send-preview/`。这条命令尚未推送；单条试发、确认收到及每周任务的完整步骤见[推荐流程](docs/SETUP.md#推荐流程从中文预览到每周推送)。macOS / Linux 使用 `.venv/bin/python` 和 `.venv/bin/paper-inbox`。

| 想使用的功能 | 需要准备 |
| --- | --- |
| 收取订阅邮件 | 开启 IMAP 的邮箱与授权码；163 已做真实验证 |
| 查询公开摘要 | 可访问 PubMed / Europe PMC / Crossref / OpenAlex，无需小牛或推送账号 |
| 中文标题与摘要 | 小牛 `cloud` 或 `neu` 接口账号及可用额度 |
| JIF / JCI 和分区 | 默认使用随包期刊库，也可[导入自己的快照](docs/JCR.md) |
| 手机接收 | WxPusher App、应用 Token 和接收者 UID |

## 使用前了解

主要接收端为 **WxPusher App**；微信渠道取决于平台绑定和激活条件。已有手动端到端使用记录，首次实际周任务观察仍待完成。Windows 提供任务计划脚本；其他系统可自行调度 CLI。

IMAP 支持可配置的 TLS 主机和文件夹，其他服务目前仅有模拟测试，不支持 OAuth2。缺少可靠摘要时保留缺项。期刊库为冻结快照，保留源数据的缺失值和 3 个异常标识符，不表示已纳入后续官方 reload。

`weekly` 默认生成英文预览；中文需选择 `translation` 和 `--language zh`。联网查询需要 `--fetch`，真实推送需要 `--send`。首次试发确认实际收到后，再启用批量或定时推送。具体支持范围见[发布与验证记录](docs/RELEASE_STATUS.md)。

## 用 Agent 帮忙配置

仓库附带 [paper-inbox Skill](skills/paper-inbox/SKILL.md)，可引导安装、配置、预览和排错。Python 工具和 Skill 分别安装，步骤见[安装说明](docs/SKILL.md)。

例如：“先生成 Paper Inbox 的完整消息展示，再帮我检查本地邮箱配置。”

## 反馈与贡献

欢迎反馈邮件格式兼容性和阅读体验，也欢迎提供合成样本或新解析器。开发检查：

```powershell
python -X utf8 -m unittest discover -s tests -q
python -X utf8 scripts/check_release.py
```

提交问题前请阅读[贡献指南](CONTRIBUTING.md)和[安全说明](SECURITY.md)，避免上传真实邮件、配置或数据库。项目代码和原创文档采用 [MIT](LICENSE)；展示论文及译文保留各自许可，详见[来源说明](PROVENANCE.md)。
