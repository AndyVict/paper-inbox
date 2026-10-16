# 发布与验证记录

## 2026-10-16：完整消息展示与随包期刊库（main）

本轮改进位于仓库 main，`v0.1.0` Release 附件仍对应首次发布，没有覆盖既有发布包。

- README 首先展示完整消息和实际用途，中英文同步；新增 `showcase` 离线生成两条已有消息，复用正式七字段模板，保留完整译文和 CC BY 4.0 署名。
- 新增 `journals` 离线名称/ISSN 查询；源码和安装包携带 22,643 刊、32,215 条分类、254 学科。提供者已确认可公开分发；来源、版本和官方全集未独立核验，保留 `full_complete=false` 和 3 个源标识符异常。
- `jcr` 阶段默认使用随包快照，自备快照仍可显式选择。Windows 任务脚本的默认快照同步调整，未注册或变更任何现有系统任务。
- Windows Python 3.12.4 完整 **282 项**离线回归通过，ResourceWarning 检查通过；wheel/sdist 内容检查通过。wheel 在独立虚拟环境安装后，已从源码目录之外实际运行 `showcase` 和 `journals`，成功读取随包资源，无需账号或联网。最新远端矩阵见仓库 Actions。
- 桌面与 390 像素手机宽度均检查了七字段和长摘要，未发现横向溢出。截图：[桌面](assets/demo.png)、[手机](assets/demo-mobile.png)。

以下保留首发版本的验证历史；其中 269 项测试及“仅合成演示”的描述对应首发范围。

记录日期：2026-10-16。版本：`0.1.0` Pre-release。本文件区分已执行的本地/CI 验证和仍需外部发生的验证。

## 已有验证

- Windows 下 Python **3.11.16 / 3.12.4 / 3.13.15 / 3.14.7** 各通过 **269 项**离线回归；启用 ResourceWarning 检查后，最终各版本资源告警为 0。聚合结果见 [validation.json](validation.json)。
- 合成演示通过实际解析及去重生成 2 封邮件 / 4 条候选 / 3 篇文献，并经过浏览器截图检查。
- Windows 任务脚本仅做语法检查，没有为公开版注册或启用系统任务。
- 已构建 wheel / sdist 并检查分发内容；wheel 在独立虚拟环境安装，从源码目录之外执行演示，确认随包资源可用。Windows CLI 显式 UTF-8 输出。
- 配套 Skill 通过结构验证和独立离线前向试用；未使用邮箱授权码、翻译服务或消息推送完成试用。
- 独立静态首审未发现 Critical，提出 2 项文档一致性修正；修正后复核 Critical / Required 均为 0。静态审查不替代实际运行验证。
- 发布检查覆盖 UTF-8、Python 3.11 语法、文档本地链接、凭据特征和被误跟踪的私人文件；不把模式扫描当作绝对安全保证。
- 核心项目已有 163、小牛和 WxPusher App 的手动真实使用记录；本次公开版准备没有重新连接私人邮箱、调用翻译或发送消息。
- 2026-10-16 首轮公开提交 `afe889a` 的 [GitHub Actions](https://github.com/AndyVict/paper-inbox/actions/runs/38868525709) 已实际通过：Windows / Linux × Python 3.11–3.14 共 8 组，每组 269 项测试；分发包任务也通过，合计 9 个任务成功。文档后续提交的最新结果参见仓库 Actions。

## 尚未验证

- Windows 系统任务目前仅做语法检查；CI 中的 Linux CLI 测试不等于已验证 Linux 系统调度。
- 其他邮箱服务目前只有模拟协议验证，没有真实账号兼容性结论；OAuth2 未实现。
- 首次实际每周定时运行仍待后续观察，不把手动运行或预检当成长期运行证明。
- Skill 的规范结构与离线命令可验证；不同客户端的安装、自动发现和权限交互需要分别实测。

## 分发范围

首发版本仅分发通用源码、合成测试、公开使用文档、合成演示与 Skill，不含真实邮箱数据、私人运行记录、JCR 快照或原项目历史。本轮 main 新增明确署名的真实论文展示，以及经提供者确认公开分发的期刊快照；许可与来源见 [PROVENANCE.md](../PROVENANCE.md) 和 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

公开仓库为 [AndyVict/paper-inbox](https://github.com/AndyVict/paper-inbox)，首轮远程 CI 已通过。后续发布维护见 [PUBLISHING.md](PUBLISHING.md)。
