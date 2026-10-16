# GitHub 发布准备

GitHub 仓库地址为 [AndyVict/paper-inbox](https://github.com/AndyVict/paper-inbox)。首次发布为 `v0.1.0` Pre-release；实际验证状态见 [RELEASE_STATUS.md](RELEASE_STATUS.md)。没有向 PyPI 发布，PyPI 名称可用性未核实。

## 本地发布检查

1. 阅读 [发布状态](RELEASE_STATUS.md)，明确真实验证与模拟验证的区别。
2. 运行离线测试、演示与 `scripts/check_release.py`；检查待发布文件清单和暂存差异。
3. 构建并检查分发包：`python -m build`、`python scripts/check_distribution.py`。
4. 在干净虚拟环境安装生成的 wheel，从源码目录之外运行 `paper-inbox demo`，确认资源随包分发。
5. 审阅 MIT 许可证、贡献者署名、项目名称和描述。

## 发布维护

- 仅从本仓库的公开文件提交和构建，不上传私人运行目录或其他项目历史。
- 仓库 About 可使用：`Turn X-MOL / Stork literature alerts into deduplicated reading lists, with optional translation and phone notifications.`
- Topics 可考虑 `research-tools`、`literature`、`email-automation`、`python`、`agent-skills`、`x-mol`、`stork`，以实际功能为准。
- 推送后检查 Windows / Linux 的 Python 3.11–3.14 CI 和安装包检查；尚未运行时不要展示虚构的绿色状态。
- 可启用 GitHub 私密漏洞报告，并据实际入口调整 SECURITY.md。
- 第一版优先作为 pre-release 供外部试装；记录安装问题及重复运行结果，再确定正式版本。

## 首次发布文案草稿

> 我把自己整理文献订阅邮件的流程做成了 Paper Inbox：读取 X-MOL / Stork 邮件，按 DOI、PMID 和标题年份去重，生成阅读清单；公开摘要、中文翻译和 WxPusher App 推送都可以按需启用。
>
> 下载后直接运行合成演示，无需邮箱或 API 配置。欢迎使用这些订阅服务的科研用户试用并反馈安装、解析和阅读体验；当前真实验证以 Windows / 163 为主。

发布时附仓库实际地址和合成演示截图，不使用个人邮件或未获授权的期刊数据作为展示材料。
