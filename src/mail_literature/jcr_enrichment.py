"""公开期刊标识补全、精确匹配与可重复生成的报告。"""
from collections import Counter
from dataclasses import dataclass
import json

from .abstracts import query_url
from .identifiers import normalize_doi, normalize_pmid
from .journal_fields import journal_identity_hash, validate_journal_fields
from .preview import _atomic_write


@dataclass
class JournalStats:
    examined: int = 0
    enriched: int = 0
    failed: int = 0
    deferred: int = 0
    missing: int = 0
    no_identifier: int = 0
    updated: int = 0


def _metadata(article, result, kind, identifier):
    return {'identity_hash': journal_identity_hash(article), 'provider': result.provider,
            'source_url': query_url(result.provider, kind, identifier), 'journal_title': result.journal_title,
            'issns': result.issns, 'issn': result.issn, 'eissn': result.eissn,
            'linking_issn': result.linking_issn, 'doi': result.doi, 'pmid': result.pmid}


def enrich_journals(store, cache, index, *, max_articles=100, progress=None):
    stats = JournalStats()
    for article in store.articles():
        failed_before = stats.failed
        before = article.to_dict()
        article.clear_stale_journal_info()
        old_conflict = (article.jcr_match if article.jcr_match_current() and article.jcr_match['status'] == 'conflict' and
                        article.jcr_match['reason'] in {'journal_identity_mismatch', 'conflicting_public_journal_issns'} else None)
        initial = index.match(article)
        conflict = False
        reason = None
        all_queries_good = False
        # 已有完整匹配直接复用；换快照时仍按当前 ISSN 重新匹配。
        if initial['status'] != 'matched' or old_conflict:
            queries = []
            pmid, doi = normalize_pmid(article.pmid), normalize_doi(article.doi)
            if pmid:
                queries.append(('PubMed', 'pmid', pmid))
            if doi:
                queries.append(('Crossref', 'doi', doi))
            if not queries and not article.journal_metadata_current():
                stats.no_identifier += 1
                reason = 'missing_public_identifier'
            elif queries:
                found, deferred, error = False, False, None
                network_examined, all_queries_good = False, True
                for provider, kind, identifier in queries:
                    # 有效缓存不占本轮网络查询的文章额度，避免小批量被旧负缓存卡住。
                    result = cache.lookup(provider, kind, identifier, allow_fetch=False)
                    if result is None and cache.online:
                        if not network_examined and stats.examined >= max_articles:
                            reason = 'journal_article_budget_reached'
                        else:
                            requests_before = cache.client.requests
                            result = cache.lookup(provider, kind, identifier)
                            if cache.client.requests > requests_before and not network_examined:
                                stats.examined += 1
                                network_examined = True
                    if result is None:
                        deferred = True
                        all_queries_good = False
                        continue
                    if result.status != 'found':
                        all_queries_good = False
                    if ((doi and result.doi and doi != result.doi) or
                            (pmid and result.pmid and pmid != result.pmid) or
                            result.error_code == 'journal_identity_mismatch'):
                        conflict, error = True, 'journal_identity_mismatch'
                        break
                    if result.status == 'found':
                        proposed = _metadata(article, result, kind, identifier)
                        # 两个已核对的公开来源给出不同且不相交的 ISSN 时保留冲突。
                        if (article.journal_metadata_current() and
                                article.journal_metadata['provider'] != provider and
                                not set(article.journal_metadata['issns']) & set(result.issns)):
                            conflict, error = True, 'conflicting_public_journal_issns'
                            break
                        if not article.journal_metadata_current() or index.match(article)['status'] == 'unmatched':
                            article.journal_metadata = proposed
                        found = True
                        if not old_conflict and index.match(article)['status'] in {'matched', 'ambiguous', 'conflict'}:
                            break
                    if result.error_code:
                        error = error or result.error_code
                if conflict:
                    stats.failed += 1
                    reason = error
                elif found or article.journal_metadata_current():
                    stats.enriched += before.get('journal_metadata') != article.journal_metadata
                elif deferred:
                    stats.deferred += 1
                    reason = reason or cache.deferred_reason or 'journal_cache_miss'
                elif error:
                    stats.failed += 1
                    reason = error
                else:
                    stats.missing += 1
                    reason = 'no_public_journal_issn'
                if progress and network_examined:
                    progress(stats)
        article.jcr_match = index.match(article)
        if old_conflict and not all_queries_good and not conflict:
            conflict = True
            reason = old_conflict['reason']
            if stats.failed == failed_before:
                stats.failed += 1
        if conflict:
            candidates = article.jcr_match['candidates'] or ([article.jcr_match['journal']] if article.jcr_match['journal'] else [])
            article.jcr_match.update(status='conflict', journal=None, categories=[], matched_issns=[], reason=reason,
                                     candidates=candidates)
        elif reason and article.jcr_match['status'] == 'missing_issn':
            article.jcr_match['reason'] = reason
        validate_journal_fields(article)
        if before != article.to_dict():
            with store.transaction():
                store._save_article(article)
            stats.updated += 1
    return stats


def write_match_report(articles, index, directory):
    """内容无变更时保留报告内容与修改时间；明细仅写入被忽略的 data/。"""
    import csv
    import io

    statuses, reasons = Counter(), Counter()
    rows, multi_category = [], 0
    for article in articles:
        match = article.jcr_match
        statuses[match['status']] += 1
        reasons[match['reason']] += 1
        multi_category += match['status'] == 'matched' and len(match['categories']) > 1
        metadata = article.journal_metadata if article.journal_metadata_current() else {}
        rows.append({'article_id': article.article_id, 'doi': article.doi or '', 'pmid': article.pmid or '',
                     'journal_from_mail': article.journal or '', 'metadata_provider': metadata.get('provider', ''),
                     'metadata_source_url': metadata.get('source_url', ''),
                     'issn': metadata.get('issn') or '', 'eissn': metadata.get('eissn') or '',
                     'linking_issn': metadata.get('linking_issn') or '',
                     'issns': ';'.join(metadata.get('issns', [])), 'status': match['status'], 'reason': match['reason'],
                     'matched_journal': match['journal']['journal_title'] if match['journal'] else '',
                     'matched_journal_id': match['journal']['journal_id'] if match['journal'] else '',
                     'categories': match['categories'], 'candidates': match['candidates']})
    report = {'snapshot_id': index.manifest['snapshot_id'], 'snapshot_hash': index.fingerprint,
              'metric_year': index.manifest['metric_year'], 'release_year': index.manifest['release_year'],
              'full_complete': index.full_complete, 'source_verified': index.source_verified,
              'version_confirmed': index.manifest.get('version_confirmed') is True,
              'summary': {'articles': len(articles), 'with_public_issns': sum(article.journal_metadata_current() for article in articles),
                          'statuses': dict(sorted(statuses.items())), 'reasons': dict(sorted(reasons.items())),
                          'multi_category_articles': multi_category,
                          'skipped_invalid_snapshot_identifiers': index.invalid_identifiers}, 'articles': rows}
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_write(directory / 'match_report.json', json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    columns = list(rows[0]) if rows else ['article_id', 'status', 'reason']
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator='\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({key: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
                         if isinstance(value, list) else value for key, value in row.items()})
    _atomic_write(directory / 'match_report.csv', '\ufeff' + buffer.getvalue())
    summary = report['summary']
    text = '\n'.join(['# JCR 期刊匹配报告', '', '快照：`' + report['snapshot_id'] + '`',
                      f"指标年份：{report['metric_year']}；文件声明发布年份：{report['release_year']}。",
                      '官方全量核验：' + ('通过' if index.full_complete else '尚未通过；文件匹配结果保留来源/版本待核验状态。'), '',
                      f"文献 {len(articles)} 篇，已补公开期刊标识 {summary['with_public_issns']} 篇，多学科匹配 {multi_category} 篇。", '',
                      '| 状态 | 篇数 |', '|---|---:|'] +
                     [f'| {status} | {count} |' for status, count in sorted(statuses.items())] + ['',
                      f'本地快照无效 ISSN/eISSN 字段 {index.invalid_identifiers} 个，已跳过，不用于自动匹配。',
                      '逐篇分区、公开元数据来源及名称候选见同目录 JSON/CSV；候选不视为已匹配。', ''])
    _atomic_write(directory / 'match_report.md', text)
    return report['summary']
