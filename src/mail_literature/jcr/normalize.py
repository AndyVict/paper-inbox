"""官方 JSON/网页 CSV 映射；原值保留，未知字段自动扩展。"""
import csv
import io
import re
from .storage import (CATEGORY_FIELDS, JOURNAL_FIELDS, JcrError, digest, flatten,
                      now, safe_url, sanitize, text_value)


def base(manifest, source):
    return {k: manifest[k] for k in ('metric_year', 'release_year', 'snapshot_id')} | {
        'source_url': safe_url(source), 'retrieved_at': now()}


def path_value(data, path):
    for key in path.split('.'):
        if not isinstance(data, dict):
            return ''
        data = data.get(key)
    return text_value(data)


def parse_rank(value):
    # 保留 rank 原字符串；仅从明确的官方 r/n 显示值提取分母，不计算分区。
    match = re.fullmatch(r'\s*(?:=)?\d+/(\d+)\s*', text_value(value))
    return match.group(1) if match else ''


def api_record(detail, report, manifest, retrieved_at=None):
    detail, report = sanitize(detail), sanitize(report)
    journal_id = detail.get('id')
    if not journal_id or report.get('journal', {}).get('id') != journal_id:
        raise JcrError('api_identity_mismatch')
    if report.get('year') != manifest['metric_year']:
        raise JcrError('api_metric_year_mismatch')
    url = f'https://api.clarivate.com/apis/wos-journals/v1/journals/{journal_id}'
    common = base(manifest, url)
    if retrieved_at:
        common['retrieved_at'] = retrieved_at
    row = {k: '' for k in JOURNAL_FIELDS} | common | {
        'journal_id': journal_id, 'journal_title': detail.get('name', ''),
        'journal_abbreviation': detail.get('jcrTitle', ''), 'issn': detail.get('issn', ''),
        'eissn': detail.get('eIssn', ''), 'publisher': path_value(detail, 'publisher.name'),
        'country_region': path_value(detail, 'publisher.countryRegion'), 'identity_status': 'official_api_id',
        'record_status': 'official', 'source_url': url + f'/reports/year/{manifest["metric_year"]}'}
    row.update(flatten(detail, 'official_metadata'))
    row.update(flatten(report, 'official_report'))
    mappings = {'jif': 'impactMetrics.jif', 'five_year_jif': 'impactMetrics.jif5Years',
                'jci': 'impactMetrics.jci', 'total_citations': 'impactMetrics.totalCites',
                'eigenfactor': 'influenceMetrics.eigenFactor.score',
                'article_influence_score': 'influenceMetrics.articleInfluence'}
    row.update({k: path_value(report.get('metrics', {}), p) for k, p in mappings.items()})
    statuses = [k for k in ('suppressed', 'onHold', 'delisted', 'forthcoming') if report.get(k) is True]
    if statuses:
        row['record_status'] = ';'.join(statuses)
    row['field_missing_reasons'] = {k: 'api_field_not_returned' for k in mappings if row[k] == ''}
    row['missing_reason'] = ';'.join(statuses) if statuses else ('api_fields_absent' if row['field_missing_reasons'] else '')
    categories = {}
    # 当年 ranks 优先；metadata 分类可能是当前分类，因此明确记录来源维度。
    for category in detail.get('categories', []):
        key = (category.get('name', ''), category.get('edition', ''))
        categories[key] = {k: '' for k in CATEGORY_FIELDS} | common | {
            'journal_id': journal_id, 'category_name': key[0], 'index_or_edition': key[1],
            'category_id': category.get('id') or category.get('url', '').split('/')[-1],
            'membership_evidence': 'current_metadata_requires_year_confirmation'}
    for kind in ('jif', 'jci'):
        for rank in report.get('ranks', {}).get(kind, []):
            key = (rank.get('category', ''), rank.get('edition', ''))
            c = categories.setdefault(key, {k: '' for k in CATEGORY_FIELDS} | common | {
                'journal_id': journal_id, 'category_name': key[0], 'index_or_edition': key[1]})
            c['category_id'] = rank.get('self', '').split('/')[-1] or c.get('category_id', '')
            c['membership_evidence'] = 'official_year_report'
            # 同一维度重复 rank 不静默丢弃。
            if c.get(kind + '_rank') or c.get(kind + '_quartile'):
                raise JcrError('duplicate_api_category_rank')
            c[kind + '_rank'] = text_value(rank.get('rank'))
            c[kind + '_category_total'] = parse_rank(rank.get('rank'))
            c[kind + '_quartile'] = text_value(rank.get('quartile'))
            if kind == 'jif':
                c['jif_percentile'] = text_value(rank.get('jifPercentile'))
            c.update(flatten(rank, 'official_' + kind))
    editions = {k[1] for k in categories if k[1]}
    row['citation_indices'] = ';'.join(sorted(editions))
    for c in categories.values():
        fields = 'jif_quartile jif_rank jif_category_total jif_percentile jci_quartile jci_rank jci_category_total'.split()
        c['field_missing_reasons'] = {k: 'api_rank_field_not_returned' for k in fields if c[k] == ''}
        c['data_status'] = 'rank_fields_missing' if c['field_missing_reasons'] else 'official'
        c['missing_reason'] = 'api_rank_fields_absent' if c['field_missing_reasons'] else ''
        c['source_url'] = row['source_url']
    return row, list(categories.values())


def web_export(raw, manifest, source_url):
    encoding = 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    try:
        lines = raw.decode(encoding).splitlines(keepends=True)
    except UnicodeError:
        raise JcrError('unsupported_export_encoding') from None
    header_line = next((i for i, l in enumerate(lines) if l.startswith('Journal name,')), None)
    if header_line is None:
        raise JcrError('unsupported_export_header')
    preamble = ''.join(lines[:header_line]).strip()
    year = re.search(r'Selected JCR Year:\s*(\d{4})', preamble)
    if not year or int(year.group(1)) != manifest['metric_year']:
        raise JcrError('export_metric_year_mismatch')
    reader = csv.DictReader(io.StringIO(''.join(lines[header_line:]), newline=''))
    mappings = {'journal_title': 'Journal name', 'journal_abbreviation': 'JCR Abbreviation',
                'publisher': 'Publisher', 'issn': 'ISSN', 'eissn': 'eISSN',
                'total_citations': 'Total Citations', 'jif': f'{year.group(1)} JIF',
                'jci': f'{year.group(1)} JCI'}
    journals, categories, failures, footers = {}, [], [], []
    for line_number, source in enumerate(reader, header_line + 2):
        overflow = source.pop(None, [])
        if not source.get('Journal name'):
            if any(source.values()) or any(overflow):
                failures.append({'row': line_number, 'code': 'unrecognized_export_row'})
            continue
        if any(source.get(column) is None for column in ('JCR Abbreviation', 'ISSN', 'eISSN', 'Category', 'Edition')):
            if source['Journal name'].startswith(('Copyright (c) ', 'By exporting the selected data;')):
                footers.append(source['Journal name'])
                continue
            failures.append({'row': line_number, 'code': 'short_or_footer_row'})
            continue
        # 本机导出每行末尾有额外逗号；只接受全部为空的溢出列。
        if any(overflow):
            failures.append({'row': line_number, 'code': 'nonempty_overflow_columns'})
        source = sanitize(source)
        identity = (source.get('ISSN', ''), source.get('eISSN', ''), source['Journal name'])
        jid = 'export:' + digest('\x1f'.join(identity).encode('utf-8'))[:24]
        row = {k: '' for k in JOURNAL_FIELDS} | base(manifest, source_url) | {
            'journal_id': jid, 'identity_status': 'export_id_requires_official_crosswalk',
            'record_status': 'existing_export_version_unverified'}
        row.update({k: source.get(v, '') for k, v in mappings.items()})
        row['official_id_candidate'] = source.get('JCR Abbreviation', '').replace(' ', '_')
        row['field_missing_reasons'] = {k: 'export_column_not_provided' for k in
                                      ('five_year_jif', 'eigenfactor', 'article_influence_score', 'country_region')}
        row['missing_reason'] = 'export_lacks_metrics_and_ranks;revision_unverified'
        # 将所有源列保存在主表/分类表，确保百分比、显示格式及新列没有丢失。
        row.update({'export__' + k: v for k, v in source.items() if k not in ('Category', 'Edition', 'JIF Quartile')})
        if jid in journals:
            previous = journals[jid]
            for field in mappings:
                if previous[field] != row[field]:
                    failures.append({'journal_id': jid, 'row': line_number, 'code': 'conflicting_journal_values', 'field': field})
        else:
            journals[jid] = row
        edition = source.get('Edition', '')
        editions = set(journals[jid]['citation_indices'].split(';')) - {''}
        editions.update(v.strip() for v in re.split(r'[,;]', edition))
        journals[jid]['citation_indices'] = ';'.join(sorted(editions - {''}))
        category = {k: '' for k in CATEGORY_FIELDS} | base(manifest, source_url) | {
            'journal_id': jid, 'category_name': source.get('Category', ''), 'index_or_edition': edition,
            'jif_quartile': source.get('JIF Quartile', ''), 'data_status': 'export_missing_ranks',
            'membership_evidence': 'export_metric_year', 'missing_reason': 'export_column_not_provided',
            'field_missing_reasons': {k: 'export_column_not_provided' for k in
                                    ('jif_rank', 'jif_category_total', 'jif_percentile', 'jci_quartile', 'jci_rank', 'jci_category_total')},
            'source_row': line_number}
        category.update({'export__' + k: v for k, v in source.items()})
        categories.append(category)
    return list(journals.values()), categories, failures, {'preamble': preamble, 'encoding': encoding,
                                                         'source_data_rows': len(categories), 'source_footers': footers}
