# 用户自备期刊数据

JCR 功能为可选项。代码与合成字段模板公开，真实期刊数据由使用者依据自己的来源与许可在本地准备。

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
