# 命令与状态

在用户的仓库/工作目录运行，确保它包含自己的 `.env` 和 `data/`。优先使用该项目虚拟环境的命令：Windows 为 `.venv\Scripts\paper-inbox.exe`，其他系统为 `.venv/bin/paper-inbox`。已正确配置 PATH 时可直接使用 `paper-inbox`。

## 最小收信配置

引导用户在自己的工作目录本地创建 `.env`，已有文件则只编辑所需字段，不覆盖配置。以下是使用 163 时的占位模板；其他 TLS IMAP 服务按用户自己的服务参数填写。不要要求用户把实际值粘贴到对话中。

```dotenv
IMAP_HOST=imap.163.com
IMAP_PORT=993
IMAP_USER=your_email@example.invalid
IMAP_APP_PASSWORD=replace_with_app_password
IMAP_MAILBOX=INBOX
```

账号和授权码需由用户在邮箱服务中取得并在本地填写。英文流程只需要 IMAP 配置；公开摘要查询不需要小牛或推送配置。`sync --check-config` 只验证格式，不能证明账号可以登录。

## 操作选择

| 意图 | 命令 | 外部影响 |
| --- | --- | --- |
| 合成演示 | `paper-inbox demo` | 只生成 `data/demo/` |
| 解析已有邮件文件 | `paper-inbox preview --input-dir data/samples` | 本地预览 |
| 收信配置格式 | `paper-inbox sync --check-config` | 读取本地 IMAP 配置，不联网 |
| 只读收信 | `paper-inbox sync --fetch --max-mails 20` | 登录配置的邮箱，读取邮件 |
| 本地接续 | `paper-inbox sync` | 本地 SQLite 与预览 |
| 公开摘要 | `paper-inbox abstracts --fetch --max-articles 20` | 公开书目 API |
| 翻译配置格式 | `paper-inbox translate --check-config` | 读取本地翻译配置，不联网 |
| 翻译 | `paper-inbox translate --fetch --max-articles 10 --max-characters 20000` | 小牛接口，消耗用户额度 |
| 英文流程预览 | `paper-inbox weekly --fetch` | 收信与摘要查询，不投递 |
| 中文流程预览 | `paper-inbox weekly --fetch --stages sync,abstracts,translation,delivery --language zh` | 增加翻译，不投递 |
| 推送配置格式 | `paper-inbox send --check-config` | 读取配置，不联网 |
| 本地投递状态 | `paper-inbox send --status` | 不读取凭据、不联网 |
| 一篇英文试发 | `paper-inbox send --send --language en --max-messages 1` | 真实 App 推送，需对应用户授权 |
| 记录实际收到 | `paper-inbox send --confirm-received 1` | 仅凭用户反馈，将示例编号换成真实对应尝试编号 |
| 每周真实流程 | `paper-inbox weekly --fetch --send` | 需要当前接收者已有人工接收确认 |

单独 `send` 默认中文；每周流程默认英文。阶段名仅允许 `sync,abstracts,translation,jcr,delivery`，实际顺序固定。`jcr` 需要用户自己的有效快照，使用 `--snapshot <name>`；合成模板会被拒绝。

退出码通常为：0 本轮完成；2 参数/配置不满足；3 部分完成或有待处理项；4 状态/执行失败；5 收信失败或投递结果不明（依具体命令）。结合安全错误码解释，不能仅凭非零码重放真实发送。

`unknown` / `inflight` 需要人工核对；`accepted` 不能自动升级为 `confirmed`。更改模板、补全摘要或换语言不触发已投递文献重发。

IMAP 为 TLS 加密码/授权码，不支持 OAuth2。163 有真实连接记录，其他提供商仅有模拟协议测试。文件夹需要 ASCII 或服务端 modified UTF-7。遇到锁冲突，先确认其他进程状态，不批量清理文件或重建数据库。
