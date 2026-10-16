# 来源与许可

项目代码和原创文档采用 MIT。真实论文、译文及第三方期刊数据按各自来源处理，不因进入此仓库而改用 MIT。

## 完整消息样例

`src/mail_literature/resources/showcase.json` 保存两篇已有本地消息的公开展示字段：论文标题、DOI/PMID、期刊、原文摘要、原有中文机器译文，以及消息中的少量期刊指标。没有复制邮件头、收件人、追踪参数、邮件 UID、私人回执、活动数据库或完整 JCR 快照。

原文及译文采用 **CC BY 4.0**，译文属于对原文的改编。2026-10-16 核对了以下文章页的版权声明和许可链接：

1. © 2026 Han, Zhao, Song, Zhang, Lu and Yuan. *UAV-based estimation of sunflower leaf flavonol content across multiple flight heights using RGB–multispectral canopy features and machine learning*. Frontiers in Plant Science 17, 1931472. [原文](https://www.frontiersin.org/journals/plant-science/articles/10.3389/fpls.2026.1931472/full) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)。
2. © 2026 Zhao, Li, Hu, Zhang, Zhang, Xu and Xiu. *Coupling critical nitrogen dilution curve with hyperspectral feature optimization and machine learning for inversion of nitrogen nutrition index in greenhouse cucumber*. Frontiers in Plant Science 17, 1909263. [原文](https://www.frontiersin.org/journals/plant-science/articles/10.3389/fpls.2026.1909263/full) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)。

中文标题和摘要直接沿用已有机器译文，可能存在术语或表达问题，阅读时以原文为准。文档、HTML 与截图中的该部分内容同样保留以上署名和许可。

展示中的 JIF/JCI 分区沿用生成消息时使用的 2025 年本地快照，仅供展示字段与阅读效果；来源和版本尚未完成官方核验，不声明为最新官方指标。展示值与随包期刊数据库均不适用项目 MIT 许可。

## 合成演示与测试

- `src/mail_literature/resources/demo.json`：虚构邮件、论文、摘要和译文，用于离线解析及去重演示。
- `tests/fixtures/`：合成邮件结构，不是用户邮件；包含用于验证清理行为的 `SYNTHETIC_*` 标记。
- `examples/parser-demo.*`：合成邮件演示的输出。
- `examples/jcr/`：合成字段模板，不能用于真实期刊匹配。

合成资源采用项目 MIT 许可。`examples/demo-report.*` 和 `docs/assets/demo*.png` 是上述完整消息展示的输出，使用正式消息模板生成，不是手机接收回执。

## 随包期刊快照

`src/mail_literature/resources/jcr/bundled-jcr-2025/` 提供整理后的 22,643 刊、32,215 条分类记录。2026-10-16 提供者确认可公开分发，并说明由其逐一下载整理；依此确认纳入公开版，未将其记录为独立核验的官方授权。数据适用 `LicenseRef-Journal-Data`，具体依据见 [第三方内容说明](THIRD_PARTY_NOTICES.md)。

导出保留全部期刊/分类行及已映射的核心和扩展指标，移除本机获取信息及重复工作簿列。CSV 内容 SHA-256 记录在 `coverage_report.json`，源数据摘要保存在 manifest 以供追溯；原本机文件保持不变。

指标年度 2025、发布年 2026、冻结日期 2026-10-12，未宣称包含后续官方 reload。原缺失值及 3 个异常标识符保留，`full_complete=false`；查询数据来源和版本仍未独立核验。详细说明见 [JCR.md](docs/JCR.md)。

## 其他来源

通用实现从原本地工具按文件清单提取，没有继承私有 Git 历史。运行凭据、原始邮件、业务数据库、原始期刊工作簿和私人回执不随项目分发。

X-MOL、Stork、WxPusher、小牛、Clarivate 及公开书目服务属于各自运营方，本项目不代表官方合作或背书。新增展示素材应逐项记录原始来源、署名、许可及改动，不能直接上传真实邮件或整份业务库。
