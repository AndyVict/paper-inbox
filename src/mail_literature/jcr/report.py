"""独立全集/分类集合核对，以及禁止虚报全量的验收条件。"""
from collections import Counter
import json
from .storage import (CATEGORY_FIELDS, CATEGORY_METRICS, JOURNAL_FIELDS, JOURNAL_METRICS,
                      now, valid_issn, write_csv, write_json, atomic_write, read_json)

PLACEHOLDERS = {'', 'N/A', 'NA', 'N/R', 'Not Ranked', 'Not ranked', 'Forthcoming', 'n/a', '-', '—'}


def ckey(row):
    # ID 不存在时以官方学科名作为辅助维度；索引/版本始终在唯一键中。
    return (row['journal_id'], str(row['metric_year']),
            row.get('category_id') or row.get('category_name', ''), row.get('index_or_edition', ''))


def reasons(row):
    value = row.get('field_missing_reasons', {})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def completeness(rows, fields):
    result = {}
    for field in fields:
        present = sum(str(r.get(field, '')) not in PLACEHOLDERS for r in rows)
        explained = sum(str(r.get(field, '')) in PLACEHOLDERS and
                        str(reasons(r).get(field, '')).startswith('official:') for r in rows)
        result[field] = {'records': len(rows), 'present': present, 'official_missing_reason': explained,
                         'unresolved': len(rows) - present - explained,
                         'presence_percent': round(100 * present / len(rows), 4) if rows else None,
                         'resolved_percent': round(100 * (present+explained) / len(rows), 4) if rows else None}
    return result


def make_report(path, manifest, journals, categories, failures=None, reference=None, samples=None):
    failures = failures or []
    reference = reference or {}
    samples = samples or []
    ids = {j['journal_id'] for j in journals}
    duplicate_j = [list(k) for k, n in Counter((j['journal_id'], str(j['metric_year'])) for j in journals).items() if n > 1]
    duplicate_c = [list(k) for k, n in Counter(ckey(c) for c in categories).items() if n > 1]
    orphan_c = sorted({c['journal_id'] for c in categories} - ids)
    invalid_issns = [{'journal_id': j['journal_id'], 'field': f, 'code': 'invalid_issn'}
                     for j in journals for f in ('issn', 'eissn') if j.get(f, '') not in PLACEHOLDERS and not valid_issn(j[f])]
    missing_issns = [{'journal_id': j['journal_id'], 'field': f, 'source_value': j.get(f, '')}
                     for j in journals for f in ('issn', 'eissn') if j.get(f, '') in PLACEHOLDERS]
    invalid_quartiles = [{'journal_id': c['journal_id'], 'field': f, 'code': 'invalid_quartile'}
                        for c in categories for f in ('jif_quartile', 'jci_quartile')
                        if c.get(f, '') not in PLACEHOLDERS | {'Q1', 'Q2', 'Q3', 'Q4'}]
    metric_sources = {'jif': 'impactMetrics__jif', 'five_year_jif': 'impactMetrics__jif5Years',
                      'jci': 'impactMetrics__jci', 'total_citations': 'impactMetrics__totalCites',
                      'eigenfactor': 'influenceMetrics__eigenFactor__score',
                      'article_influence_score': 'influenceMetrics__articleInfluence'}
    value_mismatches = [{'journal_id': j['journal_id'], 'field': field, 'code': 'normalized_source_value_mismatch'}
                        for j in journals for field, source_path in metric_sources.items()
                        if 'official_report__metrics__' + source_path in j and
                        str(j.get(field, '')) != str(j['official_report__metrics__' + source_path])]
    scope_fields = ('snapshot_id', 'metric_year', 'release_year', 'revision')
    reference_ok = (all(reference.get(k) == manifest.get(k) for k in scope_fields) and
                    reference.get('official') is True and reference.get('complete') is True and
                    bool(reference.get('source_url')) and bool(reference.get('evidence')) and
                    bool(reference.get('journal_ids')) and
                    set(reference.get('indices', [])) >= {'SCIE', 'SSCI', 'AHCI', 'ESCI'})
    expected_ids = set(reference.get('journal_ids', [])) if reference_ok else set()
    missing = sorted(expected_ids - ids)
    extras = sorted(ids - expected_ids) if reference_ok else []
    ref_duplicates = len(reference.get('journal_ids', [])) != len(set(reference.get('journal_ids', [])))
    expected_categories = {tuple(map(str, k)) for k in reference.get('category_keys', [])} if reference_ok else set()
    actual_categories = {tuple(map(str, ckey(c))) for c in categories}
    missing_categories = sorted(expected_categories - actual_categories)
    extra_categories = sorted(actual_categories - expected_categories) if expected_categories else []
    sample_ids, sample_categories, sample_coverage = set(), set(), set()
    for sample in samples:
        if (sample.get('journal_id') in ids and sample.get('passed') is True and
                sample.get('independent_evidence') and
                all(sample.get(k) == manifest.get(k) for k in scope_fields) and
                all(sample.get(k) is True for k in ('identity_checked', 'jif_checked', 'all_categories_checked', 'ranks_checked'))):
            sample_ids.add(sample['journal_id'])
            sample_categories.update(sample.get('categories', []))
            sample_coverage.update(sample.get('cases', []))
    sample_ok = (len(sample_ids) >= 10 and len(sample_categories) >= 10 and
                 sample_coverage >= {'multi_category', 'multiple_issn', 'unranked_or_missing'})
    jm = completeness(journals, JOURNAL_METRICS)
    cm = completeness(categories, CATEGORY_METRICS)
    extra_metrics = sorted({k for j in journals for k in j if k.startswith('official_report__metrics__')} |
                           set(manifest.get('additional_journal_metric_fields', [])))
    extra_completeness = completeness(journals, extra_metrics)
    extra_category_completeness = completeness(categories, manifest.get('additional_category_metric_fields', []))
    blockers = []
    if any(any(str(row.get(k, '')) != str(manifest[k]) for k in ('snapshot_id', 'metric_year', 'release_year'))
           for row in journals + categories):
        blockers.append('mixed_row_year_or_snapshot')
    if manifest.get('synthetic'):
        blockers.append('synthetic_not_real_coverage')
    if not manifest.get('permission', {}).get('full_access_confirmed') or not manifest.get('permission', {}).get('local_retention_confirmed') or not manifest.get('permission', {}).get('evidence'):
        blockers.append('full_access_or_local_retention_unverified')
    if not manifest.get('version_confirmed'):
        blockers.append('exact_revision_or_reload_unverified')
    if not reference_ok or ref_duplicates:
        blockers.append('same_snapshot_official_universe_unverified')
    if not expected_categories:
        blockers.append('same_snapshot_category_universe_unverified')
    if missing or extras:
        blockers.append('journal_set_mismatch')
    if missing_categories or extra_categories:
        blockers.append('category_set_mismatch')
    if duplicate_j or duplicate_c or orphan_c:
        blockers.append('duplicate_or_orphan_records')
    if invalid_issns or invalid_quartiles:
        blockers.append('invalid_identifier_or_quartile')
    if value_mismatches:
        blockers.append('normalized_source_value_mismatch')
    if any(v['unresolved'] for v in list(jm.values()) + list(cm.values())):
        blockers.append('metrics_or_ranks_unresolved')
    if any(v['unresolved'] for v in extra_completeness.values()):
        blockers.append('additional_source_metrics_unresolved')
    if any(v['unresolved'] for v in extra_category_completeness.values()):
        blockers.append('additional_category_metrics_unresolved')
    if ids - {c['journal_id'] for c in categories}:
        blockers.append('journals_without_category_rows')
    if any(j.get('identity_status') != 'official_api_id' and j.get('identity_status') != 'official_export_id' for j in journals):
        blockers.append('official_identity_unverified')
    if failures:
        blockers.append('unresolved_failures')
    if not sample_ok:
        blockers.append('independent_sampling_incomplete')
    if not journals:
        blockers.append('no_journal_data')
    report = {'generated_at': now(), **{k: manifest[k] for k in scope_fields},
              'status': 'full_complete' if not blockers else ('synthetic_only' if manifest.get('synthetic') else 'partial_or_access_pending'),
              'full_complete': not blockers, 'blockers': blockers,
              'real_unique_journals': 0 if manifest.get('synthetic') else len(ids),
              'dataset_unique_journals': len(ids), 'journal_rows': len(journals), 'category_rows': len(categories),
              'official_universe_verified': reference_ok, 'official_universe_size': len(expected_ids) if reference_ok else None,
              'journal_coverage_percent': round(100*len(ids & expected_ids)/len(expected_ids), 4) if expected_ids and not manifest.get('synthetic') else None,
              'category_coverage_percent': round(100*len(actual_categories & expected_categories)/len(expected_categories), 4) if expected_categories and not manifest.get('synthetic') else None,
              'headline_release_journal_count': manifest.get('headline_release_journal_count'),
              'headline_note': '发布时计数仅作背景；不能代替同快照全集或覆盖率分母。',
              'indices_observed': sorted({v for j in journals for v in j.get('citation_indices', '').split(';') if v}),
              'missing_journals': missing, 'extra_journals': extras, 'missing_categories': missing_categories,
              'extra_categories': extra_categories, 'duplicate_journal_keys': duplicate_j,
              'duplicate_category_keys': duplicate_c, 'orphan_category_journals': orphan_c,
              'invalid_issns': invalid_issns, 'invalid_quartiles': invalid_quartiles,
              'missing_issn_markers': missing_issns,
              'source_value_mismatches': value_mismatches,
              'journal_metric_completeness': jm, 'category_metric_completeness': cm,
              'additional_source_metric_completeness': extra_completeness,
              'additional_category_metric_completeness': extra_category_completeness,
              'all_journal_field_presence': completeness(journals, JOURNAL_FIELDS),
              'all_category_field_presence': completeness(categories, CATEGORY_FIELDS),
              'unresolved_failures': failures, 'accepted_sample_journals': len(sample_ids),
              'accepted_sample_categories': len(sample_categories), 'sample_cases': sorted(sample_coverage)}
    workbook_report = path / 'workbook_consistency_report.json'
    if workbook_report.exists():
        file_scope = read_json(workbook_report)
        report['file_ingestion_complete'] = file_scope['file_ingestion_complete'] and not failures
        for field in ('file_journal_row_coverage_percent', 'file_category_row_coverage_percent'):
            report[field] = file_scope[field]
        report['file_scope_note'] = file_scope['note']
        if report['file_ingestion_complete'] and not report['full_complete'] and not manifest.get('synthetic'):
            report['status'] = 'file_complete_official_verification_pending'
    public_checks = path / 'publisher_sample_checks_report.json'
    if public_checks.exists():
        sample_report = read_json(public_checks)
        report['public_publisher_sample_checks'] = {k: sample_report[k] for k in
            ('sampled_journals','sampled_workbook_categories','all_available_public_fields_passed','formal_full_acceptance_samples')}
    write_json(path / 'coverage_report.json', report)
    write_json(path / 'duplicates.json', {'journals': duplicate_j, 'categories': duplicate_c})
    write_json(path / 'missing_records.json', {'journals': missing, 'categories': missing_categories,
                                             'note': '无同快照全集时空清单不代表零遗漏。'})
    write_json(path / 'validation_issues.json', {'invalid_issns': invalid_issns, 'invalid_quartiles': invalid_quartiles,
                                              'orphans': orphan_c, 'failures': failures})
    lines = ['# JCR 覆盖报告', '', f'- 状态：{report["status"]}；全量完成：{report["full_complete"]}',
             f'- 发布/指标年度：{manifest["release_year"]}/{manifest["metric_year"]}；修订：{manifest["revision"]}',
             f'- 唯一期刊：{len(ids)}；期刊行：{len(journals)}；分类行：{len(categories)}',
             f'- 真实数据计数：{report["real_unique_journals"]}；合成：{manifest.get("synthetic", False)}',
             f'- 同快照官方全集已核验：{reference_ok}；覆盖率：{report["journal_coverage_percent"]}',
             '- 发布公告计数不可替代同快照官方 ID 全集；未提供全集时覆盖率为 null，遗漏数未知。',
             f'- 独立抽样：{len(sample_ids)} 种期刊、{len(sample_categories)} 个学科', '',
             '## 未满足条件', '', *['- ' + b for b in blockers], '', '## 字段完整率', '',
             '| 表 | 字段 | 有值 | 官方缺失理由 | 未解决 | 有值率 |', '|---|---|---:|---:|---:|---:|']
    if 'file_ingestion_complete' in report:
        lines[8:8] = [f'- 用户文件完整入库：{report["file_ingestion_complete"]}；文件期刊行覆盖率：{report["file_journal_row_coverage_percent"]}%；文件分类行覆盖率：{report["file_category_row_coverage_percent"]}%',
                      '- 文件范围入库完整不等于同快照官方全集已验证；来源/版本未知时仍不标为官方全量完成。']
    if 'public_publisher_sample_checks' in report:
        checks = report['public_publisher_sample_checks']
        lines += ['', f'公开出版方抽查：{checks["sampled_journals"]} 刊、{checks["sampled_workbook_categories"]} 学科；已公开字段全部一致：{checks["all_available_public_fields_passed"]}。未覆盖的 JCI/全部排名及同修订证据仍待核验。', '']
    for label, metrics in (('journals', jm), ('journal_categories', cm)):
        for field, value in metrics.items():
            percent = '未知' if value['presence_percent'] is None else str(value['presence_percent']) + '%'
            lines.append(f'| {label} | {field} | {value["present"]} | {value["official_missing_reason"]} | {value["unresolved"]} | {percent} |')
    for label, metrics in (('journals extra', extra_completeness), ('categories extra', extra_category_completeness)):
        for field, value in metrics.items():
            percent = '未知' if value['presence_percent'] is None else str(value['presence_percent']) + '%'
            lines.append(f'| {label} | {field} | {value["present"]} | {value["official_missing_reason"]} | {value["unresolved"]} | {percent} |')
    lines += ['', '完整 JSON 同时包含全部核心字段完整率、重复键、标识符错误、遗漏/额外集合和失败清单。',
              'api_field_not_returned / export_column_not_provided 是工具观察，不能冒充官方缺失理由。', '']
    atomic_write(path / 'coverage_report.md', '\n'.join(lines).encode('utf-8'))
    return report


def export_tables(path, manifest, journals, categories):
    jf = write_csv(path / 'journals.csv', journals, JOURNAL_FIELDS)
    cf = write_csv(path / 'journal_categories.csv', categories, CATEGORY_FIELDS)
    write_json(path / 'fields.json', {'encoding': 'UTF-8 with BOM', 'journals': jf, 'journal_categories': cf,
                                    'values': '所有源值按字符串保留；JSON 额外字段原样序列化。'})
    description = ('# 字段说明\n\n详见项目 JCR_DATA.md。所有 CSV 使用 UTF-8 with BOM；'
                   'ISSN 和指标按文本读取。缺失为空，官方占位符保留，不填 0/Q4。\n\n'
                   'journals 唯一键：journal_id + metric_year；journal_categories 唯一键：'
                   'journal_id + metric_year + category_id（无 ID 时 category_name）+ index_or_edition。\n\n'
                   'official_metadata__/official_report__/official_jif__/official_jci__ 是官方 JSON 路径；'
                   'export__ / xlsx__ 是原始 CSV/工作簿列名。嵌套数组保留 JSON；原始源文件在 raw，API 响应在 cache。\n\n'
                   f'当前快照：{manifest["snapshot_id"]}；字段列表在 fields.json。\n')
    atomic_write(path / 'field_dictionary.md', description.encode('utf-8'))
