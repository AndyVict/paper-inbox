# 安装与使用配套 Skill

公开仓库：[AndyVict/paper-inbox](https://github.com/AndyVict/paper-inbox)。Skill 位于 [skills/paper-inbox/](../skills/paper-inbox/SKILL.md)，包含 `SKILL.md` 和 `references/operations.md`。

## 安装

1. 克隆本仓库，或从 GitHub 下载源码 ZIP 后解压。
2. 按 [README](../README.md) 安装 Python 工具。Skill 通过工具的 CLI 工作，单独复制 Skill 不会安装 Python 核心。
3. 将 `skills/paper-inbox/` 整个文件夹安装到客户端指定的 Skill 目录，保留其中的 `references/`。
4. 按客户端方式重新发现 Skill，并告诉 Agent 代码仓库路径和可用的虚拟环境解释器。

Skill 遵循 [Agent Skills 规范](https://agentskills.io/specification)。不同客户端的安装目录、重新发现方式和外部命令权限不同；目前已验证结构和离线命令行为，客户端安装与自动发现需要分别实测。

## 可以这样使用

- “先生成 Paper Inbox 的完整消息展示，打开结果给我看。”
- “运行合成邮件演示，解释两个来源里的重复论文如何被合并。”
- “帮我检查邮箱配置格式，先不要连接邮箱。”
- “按推荐流程生成中文预览，我有本地期刊快照。”
- “查看发送状态，帮我定位哪些记录需要人工检查。”

Agent 使用现有 CLI，不修改投递历史或绕过去重。在用户已授权的任务范围内继续执行；配置检查、联网查询、翻译、推送和系统任务各自遵循已有授权范围。
