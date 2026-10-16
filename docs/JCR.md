# 随包期刊库与自备数据

项目内置 `bundled-jcr-2025`，包含 **22,643 刊 / 32,215 条学科分类 / 254 个学科**。源码和安装包均携带相同快照，查询时不连接邮箱或外部服务，也不需要账号与密钥。

```powershell
# 无需安装，直接从源码查询
python -X utf8 scripts/journals.py --issn 1664-462X
python -X utf8 scripts/journals.py "Nature" --limit 5
# 安装后的统一入口
.\.venv\Scripts\paper-inbox.exe journals --info
.\.venv\Scripts\paper-inbox.exe journals --issn 1664-462X --json
```

按名称查询会查找期刊英文名称及缩写；按 ISSN/eISSN 查询为精确匹配。结果保留全部学科的 JIF/JCI 分区。默认最多显示 10 刊，`--limit` 范围为 1–100。

## 数据来源与状态

随包文件位于 `src/mail_literature/resources/jcr/bundled-jcr-2025/`。提供者在 2026-10-16 确认可公开分发，并说明记录由其逐一下载整理；许可依据为提供者确认，原始下载与授权文件未独立核验。见 [第三方内容说明](../THIRD_PARTY_NOTICES.md)。

- 指标年度 2025，发布年 2026，来自 2026-10-12 冻结的本地数据；不宣称已纳入后续官方 reload。
- 原有期刊和分类行全部保留；JIF/JCI、排名及扩展指标按原样保存，没有将缺失值补为 0 或 Q4。
- 保留 3 个源标识符异常。名称查询仍可显示对应记录；无效 ISSN 不用于精确匹配。
- `full_complete=false`、`source_verified=false`、`version_confirmed=false`。文件全部导出不等于官方全集或来源已独立核验。
- 仅分发整理后的数据表及非敏感来源说明，不分发原工作簿、本机获取路径、邮件或业务数据库。

## 文献自动匹配

`paper-inbox jcr` 和流水线中的 `jcr` 阶段默认使用随包库。已有核对过的 ISSN 时可直接离线匹配；新增论文若缺少期刊 ISSN，需要显式 `--fetch` 从公开书目服务补查，这与期刊库本身的离线查询不同。

```console
paper-inbox jcr
paper-inbox weekly --fetch --stages sync,abstracts,translation,jcr,delivery --language zh
```

## 导入自己的快照

已有其他年度、更新修订或机构数据时，可以按自己的来源与许可导入，并通过 `--snapshot YOUR_SNAPSHOT` 显式选择。随包快照保持冻结。

[examples/jcr/](../examples/jcr/README.md) 的 CSV 和 manifest 仅演示字段、全部学科以及缺失值表示，不能作为正式期刊指标使用。匹配器会拒绝 `synthetic=true` 的快照。

如果已有与本工具支持结构一致的工作簿，先把 `examples/jcr/snapshot.json` 复制为本机 `config.local.jcr-manifest.json`，根据自己文件的实际情况填写：

- `snapshot_id` 与下面命令一致，例如 `local-jcr`。
- `metric_year`、`release_year` 和 `revision` 来自自己的数据，不能由文件名猜测。
- `synthetic` 只有在输入确为真实数据时才设为 `false`。
- `source_kind`、来源和 `permission` 如实记录。缺少证据不填写“已核验”，`version_confirmed` 不默认为真。

```console
python scripts/jcr_data.py init --snapshot local-jcr --manifest config.local.jcr-manifest.json
python scripts/jcr_data.py import-xlsx --snapshot local-jcr --file data/private/your-journal-workbook.xlsx
python scripts/jcr_data.py validate --snapshot local-jcr
paper-inbox jcr --snapshot local-jcr
```

工作簿导入器只支持已实现的列和工作表结构，不保证兼容任意服务商或任意年份的文件。遇到格式错误应先检查导入报告，不能把缺失值填成 0、Q4 或其他推测结果。CSV 自定义列映射可通过 `python scripts/jcr_data.py import-csv --help` 查看参数。

实际快照位于 `data/jcr/<snapshot_id>/`，保存 `snapshot.json`、`coverage_report.json`、`journals.csv` 与 `journal_categories.csv` 等文件。只有文件身份和版本信息一致，才允许进入匹配。文件完整导入不等于已证明官方全集覆盖或具备重新分发许可。

运行 `paper-inbox jcr --snapshot local-jcr --fetch` 会联网补查公开期刊 ISSN；去掉 `--fetch` 时只使用已有标识与缓存。不读取邮箱、翻译或推送配置。全部学科分别保留 JIF / JCI，名称仅生成候选，不作为模糊自动匹配依据。
