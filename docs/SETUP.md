# 配置与逐步启用

所有命令在项目根目录执行，使用虚拟环境内的 `paper-inbox` 命令。Windows 完整路径为 `.\.venv\Scripts\paper-inbox.exe`；macOS / Linux 为 `.venv/bin/paper-inbox`。

## 1. 先离线体验

`python -X utf8 scripts/demo.py` 不需要安装第三方包或配置账号。结果在 `data/demo/`，与活动库 `data/state.sqlite3` 隔离；演示数据不会自动进入真实待发队列。

已有真实 `.eml` 时，放入本机 `data/samples/`，运行 `paper-inbox preview`，生成 `data/outbox/` 离线索引。之后 `paper-inbox sync` 可以将旧离线索引接入 SQLite。原始 `.eml` 不提交到 Git。

## 2. 只读邮箱

首次安装时从 `.env.example` 复制 `.env`，已有配置不要覆盖。只填写需要使用的服务字段。

| 字段 | 说明 |
| --- | --- |
| `IMAP_HOST` / `IMAP_PORT` | TLS IMAP 主机和端口，默认 `imap.163.com:993`；不接受 URL |
| `IMAP_USER` | 邮箱账号 |
| `IMAP_APP_PASSWORD` | 服务商允许的 IMAP 密码或客户端授权码 |
| `IMAP_MAILBOX` | 默认 `INBOX`；允许含空格的 ASCII 文件夹名，非 ASCII 名须使用服务端的 modified UTF-7 表示 |
| `IMAP_TIMEOUT` | 1–60 秒 |
| `IMAP_MAX_MESSAGE_BYTES` | 单封限制，默认 10 MiB，可设 1 KiB–50 MiB |

```console
paper-inbox sync --check-config
paper-inbox sync --fetch --max-mails 20
```

第一条只检查格式，不能证明凭据有效。第二条才连接配置的服务。使用 TLS 证书验证、只读 `EXAMINE` 和 `BODY.PEEK`；不执行 `STORE`、`MOVE`、`EXPUNGE` 等修改邮件的命令。

163 已经过真实连接验证。其他主机、端口和文件夹目前有合成协议测试，仍需用户在自己的服务上验证。OAuth2 尚未实现；仅允许 OAuth2 的账号不能直接使用本登录方式。修改账号、主机或文件夹会形成独立扫描命名空间，文献去重继续保留。

## 3. 摘要与流水线

```console
paper-inbox abstracts --fetch --max-articles 20
paper-inbox weekly --fetch
```

摘要查询依次使用支持的公开书目服务，可能提交 DOI、PMID 或文献查询字段；不上传原始邮件。未取得可靠原文摘要时保留缺项，不能用生成概述补作原文摘要。

`weekly` 默认 `sync,abstracts,delivery`，只生成英文发送预览。`--stages` 从 `sync,abstracts,translation,jcr,delivery` 选择，实际按这一顺序执行，拒绝重复或未知阶段。没有 `--fetch` 时，阶段只使用本地数据与缓存；没有 `--send` 时不投递。

## 4. 可选中文翻译

填写小牛密钥与 `NIUTRANS_API_VARIANT=cloud` 或 `neu`，以自己的接口协议为准。也兼容旧字段 `TRANSLATION_API_KEY`，专用字段优先。

```console
paper-inbox translate --check-config
paper-inbox translate --fetch --max-articles 10 --max-characters 20000
paper-inbox weekly --fetch --stages sync,abstracts,translation,delivery --language zh
```

翻译会将待译标题与摘要发送给配置对应的小牛服务，可能消耗账号额度。已有有效译文和分段缓存可复用；`neu` 使用较小的保守输入预算。仅选择英文模式时不需要翻译配置。

## 5. 可选期刊指标

代码支持本地 CSV / XLSX 导入及 ISSN 精确匹配；不提供现成 JCR 数据库、下载账号或许可。字段模板和接入步骤见 [JCR.md](JCR.md)。

```console
paper-inbox jcr --snapshot local-jcr
paper-inbox weekly --fetch --stages sync,abstracts,jcr,delivery --snapshot local-jcr
```

先准备自己的有效快照，再执行这些命令。合成模板不会被允许用作真实匹配来源。分区缺失显示缺失，不回填 Q4；不同年度或修订的指标不混写。

## 6. 可选 WxPusher App 推送

在 `.env` 填写 `WXPUSHER_APP_TOKEN` 与逗号分隔的 `WXPUSHER_UIDS`。本工具使用 WxPusher App 为主要接收端，服务账号及额度由使用者自己管理。

```console
paper-inbox send --check-config
paper-inbox send --language en
paper-inbox send --send --language en --max-messages 1
```

前两条不联网。第三条才发送一篇文献；首次应先用单条确认实际接收，再继续批量或定时运行。终端给出的是本地尝试编号；实际在 App 看到后，将下面的 `1` 替换成对应编号：

```console
paper-inbox send --confirm-received 1
paper-inbox weekly --fetch --send
```

每周发送要求当前全部接收者已有人工确认。`accepted` 仅表示平台接收；`confirmed` 表示用户已明确确认。`unknown` / `inflight` 不自动重发，已有发送记录也不因文案、译文、模式或摘要更新而重发。

单独 `send` 默认中文，中文模式会暂缓缺有效译文的文献；英文模式使用 `--language en`。缺少可靠原文摘要的文献仍可发送标题和缺摘要提示，不以生成内容替代。手动默认每批 20、每周每批 100，每接收者滚动 24 小时本地上限 200，可通过参数降低。

## 7. Windows 每周任务

先用 Windows PowerShell 运行导出计划：

```powershell
.\scripts\install_weekly_task.ps1 -Day Monday -At 10:00 -Fetch
```

只有增加 `-Register` 才注册系统任务；只有增加 `-EnablePush` 才把真实发送写入计划，且事先检查 App 接收确认。`-Stages`、`-Language`、`-Snapshot` 分别对应流水线选择、语言和快照。时间使用当前 Windows 本地时区。

任务名为 `PaperInbox-Weekly`，存在同名任务时拒绝覆盖。任务以当前登录用户运行，电脑须开机并保持该用户登录。调度器不自动重放失败投递；先检查具体日志和状态再决定恢复步骤。

任务调用仓库中的 `scripts/run_weekly.py` 和该仓库的 `.venv` 解释器，以固定代码、解释器和工作目录，避免依赖系统 PATH 中的同名命令。该包装入口与已安装的 `paper-inbox weekly` 复用同一个 Python 实现；任务安装脚本供源码安装用户使用。

首次实际定时运行仍须单独观察，不能以脚本导出、手动运行或 CI 通过代替。其他系统可以自行定时调用 CLI，但本版没有验证其系统调度流程。

## 排错

| 错误或现象 | 处理方向 |
| --- | --- |
| `missing_imap_credentials` | 在本机检查账号和授权码；不要发送实际值 |
| `imap_login_failed` | 核对是否开启 IMAP、授权码和账号支持的认证方式 |
| `invalid_imap_mailbox` | 检查文件夹名及 modified UTF-7 表示 |
| `jcr_real_snapshot_required` | 当前是合成或空模板；真实使用需自备数据 |
| `missing_or_invalid_niutrans_api_key` | 本机检查翻译配置与接口类型 |
| `wxpusher_receipt_not_confirmed` | 单条测试后先在 App 实际查看，再记录对应尝试编号 |
| `sync_locked` / `outbox_locked` | 先确认另一进程是否仍在运行；不要批量删除锁或数据 |
| 翻译或摘要缺项 | 查看安全状态码；保留已有成功结果，按预算接续 |

提问时提供 Python/系统版本、安全错误码和合成复现，避免上传 `.env`、真实邮件、数据库或完整运行目录。
