"""文件范围完整性：主表 JSON、分类表、汇总表三方独立对照。"""
from collections import Counter


def audit_workbook(tables, manifest, journals, categories, expected, fields):
    expected_rows = Counter(tuple(str(row.get(key, '')) for key in fields) for row in expected)
    actual_rows = Counter(tuple(str(row.get(key, '')) for key in fields) for row in categories)
    missing_rows, extra_rows = expected_rows-actual_rows, actual_rows-expected_rows
    summary = tables['Category_Summary']
    actual_counts = Counter(row['category_name'] for row in categories)
    declared_counts, failures = {}, []
    for item in summary['records']:
        values = item['values']
        try:
            name, count = values['Category'], int(values['Journal-category records'])
            if count < 0 or name in declared_counts:
                raise ValueError
        except (KeyError, ValueError):
            failures.append({'code': 'invalid_workbook_category_summary', 'source_row': item['row_number']})
            continue
        declared_counts[name] = count
    summary_mismatches = [{'category_name': name, 'declared': declared_counts.get(name), 'observed': actual_counts.get(name)}
                          for name in sorted(set(declared_counts) | set(actual_counts))
                          if declared_counts.get(name) != actual_counts.get(name)]
    if missing_rows or extra_rows:
        failures.append({'code': 'workbook_category_json_sheet_mismatch', 'missing': sum(missing_rows.values()), 'extra': sum(extra_rows.values())})
    if summary_mismatches:
        failures.append({'code': 'workbook_category_summary_mismatch', 'categories': len(summary_mismatches)})
    ranks = [j['source_rank'] for j in journals]
    sequential = Counter(ranks) == Counter(str(i) for i in range(1, len(journals)+1))
    if not sequential:
        failures.append({'code': 'workbook_rank_sequence_incomplete'})
    expected_j = len(tables['Journals']['records'])
    expected_c = len(tables['Category_Quartiles']['records'])
    result = {'scope': 'user_provided_file_only_not_independent_official_universe',
              'journal_source_rows': expected_j, 'journal_output_rows': len(journals),
              'category_source_rows': expected_c, 'category_output_rows': len(categories),
              'category_names': len(actual_counts), 'summary_category_names': len(declared_counts),
              'main_json_category_records': sum(expected_rows.values()), 'actual_category_records': sum(actual_rows.values()),
              'json_rows_missing_from_category_sheet': [list(key)+[n] for key, n in missing_rows.items()],
              'category_rows_extra_to_main_json': [list(key)+[n] for key, n in extra_rows.items()],
              'summary_mismatches': summary_mismatches, 'source_rank_sequence_complete': sequential,
              'file_journal_row_coverage_percent': 100*len(journals)/expected_j if expected_j else None,
              'file_category_row_coverage_percent': 100*len(categories)/expected_c if expected_c else None,
              'headline_count_equal': len(journals) == manifest.get('headline_release_journal_count'),
              'file_ingestion_complete': not failures and len(journals) == expected_j and len(categories) == expected_c,
              'official_universe_verified': False,
              'note': '三张表属于同一来源；一致性与发布计数相等不证明官方全集、合法来源或确切重载版本。'}
    return result, failures
