# 发布候选版状态

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

仅分发通用源码、合成测试、公开使用文档、合成演示与 Skill。不含真实邮箱数据、私人运行记录、JCR 快照或原项目历史。

公开仓库为 [AndyVict/paper-inbox](https://github.com/AndyVict/paper-inbox)，首轮远程 CI 已通过。后续发布维护见 [PUBLISHING.md](PUBLISHING.md)。
