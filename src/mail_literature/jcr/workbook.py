"""用户提供 JCR 三表工作簿导入，原值/原始分类 JSON 全部保留。"""
import json
import re
from .normalize import base, parse_rank
from .report import PLACEHOLDERS, export_tables
from .storage import (CATEGORY_FIELDS, JOURNAL_FIELDS, JcrError, atomic_write, digest,
                      read_json, sanitize, write_json, now, text_value)
from .xlsx_reader import read_workbook

JOURNAL_MAP = {
    'journal_title': 'Journal name', 'journal_abbreviation': 'Abbreviated journal',
    'publisher': 'Publisher', 'issn': 'ISSN', 'eissn': 'eISSN', 'metric_year': 'JCR year',
    'jif': '{year} JIF', 'five_year_jif': '5-year JIF', 'jci': 'JCI', 'total_citations': 'Total citations',
    'eigenfactor': 'Eigenfactor', 'article_influence_score': 'Article influence score',
    'jif_without_self_cites': 'JIF without self cites', 'immediacy_index': 'Immediacy index',
    'normalized_eigenfactor': 'Normalized eigenfactor', 'total_articles': 'Total articles',
    'citable_items': 'Citable items', 'citable_articles_percent': '% articles in citable items',
    'gold_oa_percent': '% OA gold', 'cited_half_life': 'Cited half-life', 'citing_half_life': 'Citing half-life'}
CATEGORY_MAP = {
    'category_name': 'Category', 'index_or_edition': 'Edition',
    'jif_quartile': 'JIF quartile', 'jif_percentile': 'JIF percentile', 'jif_rank': 'JIF rank',
    'jci_quartile': 'JCI quartile', 'jci_percentile': 'JCI percentile', 'jci_rank': 'JCI rank',
    'ais_quartile': 'AIS quartile', 'ais_rank': 'AIS rank', 'five_year_jif_quartile': '5-year JIF quartile'}
JSON_MAP = {'category_name': 'category', 'index_or_edition': 'edition', 'jif_quartile': 'quartile',
            'jif_percentile': 'jifPercentile', 'jif_rank': 'jifRank', 'jci_quartile': 'jciQuartile',
            'jci_percentile': 'jciPercentile', 'jci_rank': 'jciRank', 'ais_quartile': 'aisQuartile',
            'ais_rank': 'aisRank', 'five_year_jif_quartile': 'fiveYearJifQuartile'}


def identity(row):
    return tuple(row.get(k, '') for k in ('ISSN', 'eISSN', 'Journal name'))


def local_id(row):
    return 'export:' + digest('\x1f'.join(identity(row)).encode('utf-8'))[:24]


def issues_for_missing(row, mapping, source):
    return {key: ('workbook_column_not_provided' if column not in source else
                  'workbook_missing_marker:' + (row[key] or 'blank') + ';official_reason_unverified')
            for key, column in mapping.items() if row[key] in PLACEHOLDERS}


def normalize_workbook(tables, manifest, source_url):
    for sheet in ('Journals', 'Category_Quartiles', 'Category_Summary'):
        if sheet not in tables:
            raise JcrError('required_jcr_workbook_sheet_missing')
    js, cs, failures = [], [], []
    by_rank, by_identity, expected_categories = {}, {}, []
    timestamp = now()
    common = base(manifest, source_url) | {'retrieved_at': timestamp}
    jmap = {key: value.format(year=manifest['metric_year']) for key, value in JOURNAL_MAP.items()}
    if not {'Rank', 'Journal name', 'ISSN', 'eISSN', 'JCR year', f'{manifest["metric_year"]} JIF',
            'Category quartiles JSON'} <= set(tables['Journals']['headers']):
        raise JcrError('unsupported_jcr_workbook_journal_header')
    if not {'Rank', 'Journal name', 'ISSN', 'eISSN', 'Category', 'Edition'} <= set(tables['Category_Quartiles']['headers']):
        raise JcrError('unsupported_jcr_workbook_category_header')
    for item in tables['Journals']['records']:
        source = sanitize(item['values'])
        if source['JCR year'] != str(manifest['metric_year']):
            raise JcrError('workbook_metric_year_mismatch')
        if not source['Journal name']:
            raise JcrError('workbook_journal_title_missing')
        jid, rank = local_id(source), source['Rank']
        row = {key: '' for key in JOURNAL_FIELDS} | common | {'journal_id': jid,
               'record_status': 'user_workbook_source_and_revision_unverified',
               'identity_status': 'export_id_requires_official_crosswalk', 'source_sheet': 'Journals',
               'source_row': item['row_number'], 'source_rank': rank}
        row.update({key: source.get(column, '') for key, column in jmap.items()})
        row['citation_indices'] = ';'.join(sorted({v.strip() for v in re.split('[,;]', source.get('Editions', '')) if v.strip()}))
        row['official_id_candidate'] = source.get('Abbreviated journal', '').replace(' ', '_')
        row['field_missing_reasons'] = issues_for_missing(row, jmap, source) | {
            'country_region': 'workbook_column_not_provided'}
        row['missing_reason'] = 'workbook_missing_values_unexplained' if row['field_missing_reasons'] else ''
        row.update({'xlsx__' + key: value for key, value in source.items()})
        js.append(row)
        if rank in by_rank or identity(source) in by_identity:
            failures.append({'code': 'duplicate_workbook_rank_or_identity', 'source_row': item['row_number'], 'journal_id': jid})
        by_rank[rank] = row
        by_identity[identity(source)] = row
        try:
            entries = json.loads(source['Category quartiles JSON'])
            if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
                raise ValueError
        except ValueError:
            failures.append({'code': 'invalid_workbook_category_json', 'source_row': item['row_number'], 'journal_id': jid})
            continue
        for entry in entries:
            expected_categories.append({'journal_id': jid, **{key: text_value(entry.get(value)) for key, value in JSON_MAP.items()}})
    for item in tables['Category_Quartiles']['records']:
        source = sanitize(item['values'])
        # 两表的 Rank 可能是各自展示顺序；不能据此跨表连接。
        journal = by_identity.get(identity(source))
        jid = journal['journal_id'] if journal else local_id(source)
        if journal is None:
            failures.append({'code': 'workbook_category_without_journal', 'source_row': item['row_number'], 'journal_id': jid})
        row = {key: '' for key in CATEGORY_FIELDS} | common | {
            'journal_id': jid, 'source_sheet': 'Category_Quartiles', 'source_row': item['row_number'],
            'source_rank': source['Rank'], 'membership_evidence': 'user_workbook_year_linked',
            'data_status': 'user_workbook_source_and_revision_unverified'}
        row.update({key: source.get(column, '') for key, column in CATEGORY_MAP.items()})
        for kind in ('jif', 'jci', 'ais'):
            row[kind + '_category_total'] = parse_rank(row[kind + '_rank'])
        row['field_missing_reasons'] = issues_for_missing(row, CATEGORY_MAP, source)
        for kind in ('jif', 'jci', 'ais'):
            if not row[kind + '_category_total']:
                row['field_missing_reasons'][kind+'_category_total'] = 'workbook_rank_denominator_unavailable'
        row['field_missing_reasons']['category_id'] = 'workbook_column_not_provided'
        row['missing_reason'] = 'workbook_missing_values_unexplained' if row['field_missing_reasons'] else ''
        if journal:
            for field in ('jif', 'five_year_jif', 'jci'):
                column = jmap[field]
                if source.get(column, '') != journal[field]:
                    failures.append({'code': 'workbook_journal_category_metric_mismatch', 'journal_id': jid,
                                     'source_row': item['row_number'], 'field': field})
        row.update({'xlsx__' + key: value for key, value in source.items()})
        cs.append(row)
    return js, cs, failures, expected_categories


def import_workbook(path, manifest, file, source_url):
    from .workbook_audit import audit_workbook
    from .workflow import validate
    raw = file.read_bytes()
    checksum = digest(raw)
    if (path / 'import_source.json').exists() and read_json(path / 'import_source.json').get('sha256') != checksum:
        raise JcrError('import_source_changed_create_new_snapshot')
    tables = read_workbook(file)
    journals, categories, failures, expected = normalize_workbook(tables, manifest, source_url)
    audit, audit_failures = audit_workbook(tables, manifest, journals, categories, expected, ['journal_id', *JSON_MAP])
    failures.extend(audit_failures)
    atomic_write(path / 'raw' / (checksum + '.xlsx'), raw)
    for name, table in tables.items():
        # 原工作簿已保留所有内容；另外导出每张表的纯文本原值便于复查。
        from .storage import write_csv
        safe_name = digest(name.encode('utf-8'))[:12]
        rows = [item['values'] | {'source_row': item['row_number']} for item in table['records']]
        write_csv(path / 'raw' / (safe_name + '.csv'), rows, table['headers'])
    write_json(path / 'import_source.json', {'source_kind': 'user_provided_workbook', 'sha256': checksum,
                                           'filename': file.name, 'source_url': source_url, 'imported_at': now(),
                                           'origin': manifest.get('origin_confirmation', 'user_provided_origin_unverified'),
                                           'permission_state': manifest.get('permission', {}),
                                           'worksheets': {name: {'rows': len(table['records']), 'headers': table['headers']} for name, table in tables.items()}})
    write_json(path / 'import_failures.json', failures)
    write_json(path / 'workbook_consistency_report.json', audit)
    export_tables(path, manifest, journals, categories)
    report = validate(path, manifest)
    return report, audit
