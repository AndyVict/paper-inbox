"""导入/采集编排；快照内容与年度不可混写。"""
import json
from urllib.parse import quote
from .client import JournalsClient
from .normalize import api_record, web_export
from .report import export_tables, make_report
from .storage import (JcrError, digest, now, read_csv, read_json, sanitize, write_json,
                      atomic_write)


def reference_files(path):
    ref = read_json(path / 'official_reference.json') if (path / 'official_reference.json').exists() else None
    samples = read_json(path / 'sample_checks.json') if (path / 'sample_checks.json').exists() else None
    return ref, samples


def validate(path, manifest):
    ref, samples = reference_files(path)
    failures_path = path / 'import_failures.json'
    failures = read_json(failures_path) if failures_path.exists() else []
    if (path / 'failures.json').exists():
        failures += [v for v in read_json(path / 'failures.json').values() if not v.get('resolved_at')]
    if (path / 'checkpoint.json').exists():
        checkpoint = read_json(path / 'checkpoint.json')
        if checkpoint.get('state') != 'collected':
            failures += [{'code': 'collection_incomplete', 'state': checkpoint.get('state')}]
    return make_report(path, manifest, read_csv(path / 'journals.csv'),
                       read_csv(path / 'journal_categories.csv'), failures, ref, samples)


def import_web(path, manifest, file, source_url):
    raw = file.read_bytes()
    checksum = digest(raw)
    existing = path / 'import_source.json'
    if existing.exists() and read_json(existing).get('sha256') != checksum:
        raise JcrError('import_source_changed_create_new_snapshot')
    journals, categories, failures, metadata = web_export(raw, manifest, source_url)
    atomic_write(path / 'raw' / (checksum + '.csv'), raw)
    provenance = {'source_url': source_url, 'sha256': checksum, 'imported_at': now(),
                  'source_kind': 'existing_local_web_export', 'filename': file.name,
                  'permission_state': manifest.get('permission', {}), **metadata}
    write_json(existing, sanitize(provenance))
    write_json(path / 'import_failures.json', failures)
    export_tables(path, manifest, journals, categories)
    return validate(path, manifest)


def import_mapped(path, manifest, journal_file, category_file, mapping):
    """机构 CSV 显式列映射；未知列另存 export__，不推导指标。"""
    digests = {name: digest(file.read_bytes()) for name, file in
               (('journals', journal_file), ('categories', category_file))}
    config_hash = digest(json.dumps(mapping, sort_keys=True).encode('utf-8'))
    fingerprints = digests | {'mapping': config_hash}
    if (path / 'import_source.json').exists() and read_json(path / 'import_source.json').get('sha256') != fingerprints:
        raise JcrError('import_source_changed_create_new_snapshot')
    tables = []
    for name, file in (('journals', journal_file), ('categories', category_file)):
        source_rows = read_csv(file)
        column_map = mapping.get(name, {})
        if not column_map.get('journal_id'):
            raise JcrError('mapping_requires_official_id_column')
        rows = []
        for source in source_rows:
            if any(col not in source for col in column_map.values()):
                raise JcrError('mapped_column_missing')
            row = {key: source[column] for key, column in column_map.items()}
            if not row.get('journal_id') or row.get('metric_year') != str(manifest['metric_year']):
                raise JcrError('mapped_identity_or_metric_year_mismatch')
            row |= {'release_year': manifest['release_year'], 'snapshot_id': manifest['snapshot_id'],
                    'source_url': mapping.get('source_url', ''), 'retrieved_at': mapping.get('retrieved_at') or now()}
            if name == 'journals':
                row['identity_status'] = mapping.get('identity_status', 'official_id_unverified')
            row.update({'export__' + k: v for k, v in source.items()})
            rows.append(sanitize(row))
        tables.append(rows)
        atomic_write(path / 'raw' / (digests[name] + '.csv'), file.read_bytes())
    write_json(path / 'import_source.json', {'sha256': fingerprints, 'imported_at': now(),
                                           'mapping': sanitize(mapping), 'source_kind': 'institution_csv'})
    write_json(path / 'import_failures.json', [])
    export_tables(path, manifest, *tables)
    return validate(path, manifest)


def api_authorization(manifest, config):
    auth = manifest.get('permission', {})
    if (not auth.get('full_access_confirmed') or not auth.get('local_retention_confirmed') or
            not auth.get('evidence') or not set(auth.get('indices', [])) >= {'SCIE', 'SSCI', 'AHCI', 'ESCI'}):
        raise JcrError('full_api_authorization_and_retention_required')
    if not config.get('max_requests_per_run') or not config.get('quota_confirmed') or not config.get('quota_evidence'):
        raise JcrError('account_quota_confirmation_required')
    # 试调用也需要实际 API 许可/本地保存许可；不能借用其他服务 KEY。


def list_page(client, year, page):
    envelope = client.request('/journals', {'jcrYear': year, 'page': page, 'limit': 50})
    body = envelope['body']
    meta, hits = body.get('metadata', {}), body.get('hits')
    total = meta.get('total')
    if (not isinstance(hits, list) or not isinstance(total, int) or isinstance(total, bool) or total < 1 or
            meta.get('page') != page or meta.get('limit') != 50):
        raise JcrError('api_pagination_contract_mismatch')
    expected = min(50, max(0, total-(page-1)*50))
    if len(hits) != expected or any(not isinstance(h, dict) or not h.get('id') for h in hits):
        raise JcrError('api_page_count_mismatch')
    return hits, total, envelope


def preflight(path, manifest, config, key, transport=None):
    api_authorization(manifest, config)
    client = JournalsClient(path, key, min(3, config['max_requests_per_run']),
                            config.get('request_interval_seconds', 0.5), transport)
    hits, total, envelope = list_page(client, manifest['metric_year'], 1)
    sample_id = config.get('preflight_sample_journal_id') or hits[0]['id']
    if sample_id not in {h['id'] for h in hits}:
        raise JcrError('preflight_sample_not_in_first_page')
    jid = quote(sample_id, safe='')
    detail = client.request('/journals/' + jid)
    report = client.request('/journals/' + jid + '/reports/year/' + str(manifest['metric_year']))
    row, categories = api_record(detail['body'], report['body'], manifest, report['retrieved_at'])
    estimated = total * 2 + ((total + 49) // 50) * 2
    result = {'checked_at': now(), 'success': True, 'snapshot_id': manifest['snapshot_id'],
              'metric_year': manifest['metric_year'], 'catalog_total': total, 'limit': 50,
              'sample_journal_id': row['journal_id'], 'sample_category_rows': len(categories),
              'estimated_requests_including_catalog_recheck': estimated,
              'quota_headers': envelope['quota_headers'], 'requests': client.requests,
              'cache_hits': client.cache_hits,
              'note': '仅证实少量调用；账户总额度、许可覆盖与保存方式仍由本机许可证据约束。'}
    write_json(path / 'preflight.json', result)
    return result


def collect(path, manifest, config, key, transport=None):
    api_authorization(manifest, config)
    if not (path / 'preflight.json').exists() or not read_json(path / 'preflight.json').get('success'):
        raise JcrError('successful_preflight_required')
    client = JournalsClient(path, key, config['max_requests_per_run'],
                            config.get('request_interval_seconds', 0.5), transport)
    journals, categories, catalog = [], [], []
    page, total, state, code = 1, None, 'running', None
    try:
        while total is None or len(catalog) < total:
            hits, page_total, envelope = list_page(client, manifest['metric_year'], page)
            if total is not None and page_total != total:
                raise JcrError('catalog_changed_create_new_snapshot')
            total = page_total
            prior_ids = {h['id'] for h in catalog}
            if len({h['id'] for h in hits}) != len(hits) or prior_ids & {h['id'] for h in hits}:
                raise JcrError('duplicate_catalog_ids')
            catalog.extend(hits)
            write_json(path / 'catalog_progress.json', {'total': total, 'hits': catalog, 'next_page': page+1})
            for hit in hits:
                jid = quote(hit['id'], safe='')
                detail = client.request('/journals/' + jid)
                try:
                    report = client.request('/journals/' + jid + '/reports/year/' + str(manifest['metric_year']))
                except JcrError as error:
                    if str(error) != 'http_404':
                        raise
                    # 文档：404 可能是未找到或受抑制，不能推断为 Q4/零。
                    # 已从当年官方名录和 metadata 确认的期刊仍保留，失败独立重试。
                    placeholder = {'year': manifest['metric_year'], 'journal': {'id': detail['body']['id']}}
                    row, cs = api_record(detail['body'], placeholder, manifest, detail['retrieved_at'])
                    row['record_status'] = 'report_unavailable_http404'
                    row['missing_reason'] = 'http_404_not_found_or_suppressed_requires_official_explanation'
                    for c in cs:
                        c['data_status'] = 'report_unavailable_http404'
                        c['missing_reason'] = row['missing_reason']
                    journals.append(row)
                    categories.extend(cs)
                    continue
                row, cs = api_record(detail['body'], report['body'], manifest, report['retrieved_at'])
                journals.append(row)
                categories.extend(cs)
            page += 1
        # 另一缓存命名空间重新读取目录，捕捉采集中总数/ID 变化；保留两次响应。
        client.namespace = 'catalog_recheck'
        checked_ids = []
        for check_page in range(1, (total+49)//50+1):
            check_hits, check_total, _ = list_page(client, manifest['metric_year'], check_page)
            if check_total != total:
                raise JcrError('catalog_changed_create_new_snapshot')
            checked_ids.extend(h['id'] for h in check_hits)
        if len(checked_ids) != len(set(checked_ids)) or set(checked_ids) != {h['id'] for h in catalog}:
            raise JcrError('catalog_changed_create_new_snapshot')
        state = 'collected'
        # 该名录来自带年度过滤的官方 API，但分类全集仍需独立同快照证据。
        ref = {k: manifest[k] for k in ('snapshot_id', 'metric_year', 'release_year', 'revision')}
        ref.update({'journal_ids': [h['id'] for h in catalog], 'official': True, 'complete': True,
                    'indices': manifest['permission']['indices'], 'source_url': 'https://api.clarivate.com/apis/wos-journals/v1/journals',
                    'evidence': 'catalog_progress.json + cache/*: all unfiltered year pages', 'collected_at': now()})
        # 不能覆盖用户提供的更完整/独立分类参考。
        if not (path / 'official_reference.json').exists():
            write_json(path / 'official_reference.json', ref)
    except JcrError as e:
        code = str(e)
        state = 'paused_budget' if code == 'request_budget_reached' else 'stopped_error'
    # 重跑从缓存回放恢复；若输入缓存损坏，不覆盖之前成功导出的更长表。
    old = read_csv(path / 'journals.csv')
    if len(journals) >= len(old):
        export_tables(path, manifest, journals, categories)
    checkpoint = {'state': state, 'code': code, 'updated_at': now(), 'next_page': page,
                  'catalog_total': total, 'normalized_journals_this_replay': len(journals),
                  'requests': client.requests, 'cache_hits': client.cache_hits,
                  'note': '缓存保留；重跑从第 1 页回放缓存，仅请求尚未缓存的 endpoint。'}
    write_json(path / 'checkpoint.json', checkpoint)
    return validate(path, manifest), checkpoint
