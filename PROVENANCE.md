# 来源与分发范围

本发布候选版从一个本地维护的 Python 文献订阅工具中按文件清单提取通用实现，并增加公开版 CLI、可配置阶段和离线演示。没有继承原项目 Git 历史、本机运行记录、真实邮件、数据库、服务凭据、回执或期刊数据快照。

- `src/` 与 `scripts/`：项目实现，运行时依赖 Python 标准库；构建时使用 setuptools。
- `tests/fixtures/`：合成的 HTML / 纯文本结构，不是用户邮件；包含专门用于验证清理行为的 `SYNTHETIC_*` 标记。
- `src/mail_literature/resources/demo.json`：从头制作的虚构论文、摘要及固定译文，服务于离线演示。
- `examples/demo-report.*` 和 `docs/assets/demo.png`：由上述合成演示生成，不是实际 API 结果或真实手机回执。
- `examples/jcr/`：合成字段模板与示例值，不是真实期刊指标数据库，不能用于正式匹配。
- `skills/paper-inbox/`：调用项目 CLI 的说明，不分发其他本机 Skill 或其内容。

MIT 适用于本项目代码、文档和合成示例。X-MOL、Stork、WxPusher、小牛、Clarivate 及公开书目服务是各自运营方的产品/服务；本项目未宣称与其存在官方合作或背书。

真实运行中由用户取得的邮件、摘要及期刊数据不因软件的开源许可而获得新的分发权利，也不应直接作为公共测试附件。
