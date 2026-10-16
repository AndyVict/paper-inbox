"""冻结快照的 ISSN 精确匹配；名称候选不自动写成成功匹配。"""
from collections import defaultdict
import csv
from difflib import get_close_matches
from hashlib import sha256
import io
import json
import re
import unicodedata

from ..journal_fields import journal_identity_hash, metadata_hash, normalize_issn
from .storage import JcrError
from .report import PLACEHOLDERS
from .normalize import parse_rank

JOURNAL_KEYS = ('journal_id', 'journal_title', 'issn', 'eissn', 'jif', 'jci')
CATEGORY_KEYS = ('category_name', 'index_or_edition', 'jif_quartile', 'jif_rank', 'jif_category_total',
                 'jci_quartile', 'jci_rank', 'jci_category_total')
MATCH_STATUS_LABELS = {'missing_issn': '缺少已核对的 ISSN/eISSN', 'unmatched': 'ISSN 未命中本地快照',
                       'ambiguous': 'ISSN 对应多个期刊，待核对', 'conflict': '期刊标识冲突，待核对'}


def name_key(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value or '').casefold()).strip()


class JournalIndex:
    def __init__(self, path):
        try:
            names = ('snapshot.json', 'coverage_report.json', 'journals.csv', 'journal_categories.csv')
            before = {name: (path / name).stat().st_mtime_ns for name in names}
            raw = {name: (path / name).read_bytes() for name in names}
            if any((path / name).stat().st_mtime_ns != before[name] for name in names):
                raise JcrError('jcr_snapshot_changed_during_read')
            manifest = json.loads(raw['snapshot.json'].decode('utf-8-sig'))
            coverage = json.loads(raw['coverage_report.json'].decode('utf-8-sig'))
            journals = list(csv.DictReader(io.StringIO(raw['journals.csv'].decode('utf-8-sig'))))
            categories = list(csv.DictReader(io.StringIO(raw['journal_categories.csv'].decode('utf-8-sig'))))
            if manifest.get('synthetic') or not journals:
                raise JcrError('jcr_real_snapshot_required')
            if manifest['snapshot_id'] != path.name:
                raise JcrError('jcr_snapshot_id_mismatch')
            for field in ('metric_year', 'release_year'):
                if type(manifest[field]) is not int or not 1900 <= manifest[field] <= 2099:
                    raise JcrError('invalid_jcr_snapshot')
            if any(not isinstance(manifest[field], str) for field in ('snapshot_id', 'revision', 'source_kind')):
                raise JcrError('invalid_jcr_snapshot')
            if any(str(row[field]) != str(manifest[field]) for row in journals + categories
                   for field in ('snapshot_id', 'metric_year', 'release_year')):
                raise JcrError('mixed_jcr_snapshot_rows')
            if any(str(coverage[field]) != str(manifest[field]) for field in
                   ('snapshot_id', 'metric_year', 'release_year', 'revision')):
                raise JcrError('jcr_coverage_snapshot_mismatch')
            self.manifest = manifest
            self.fingerprint = sha256(b'\0'.join(raw[name] for name in names)).hexdigest()
            permission = manifest.get('permission', {})
            self.source_verified = bool(permission.get('full_access_confirmed') is True and
                                        permission.get('local_retention_confirmed') is True and permission.get('evidence'))
            self.full_complete = bool(coverage.get('full_complete') is True and self.source_verified and
                                      manifest.get('version_confirmed') is True and
                                      coverage.get('official_universe_verified') is True)
            self.journals, self.categories = {}, defaultdict(list)
            self.by_issn, self.by_name = defaultdict(set), defaultdict(set)
            self.invalid_identifiers = 0
            for row in journals:
                record = {field: row[field] for field in JOURNAL_KEYS}
                key = row['journal_id']
                if not key or key in self.journals:
                    raise JcrError('duplicate_jcr_journal')
                self.journals[key] = record
                for field in ('issn', 'eissn'):
                    value = normalize_issn(row[field])
                    if value:
                        self.by_issn[value].add(key)
                    elif row[field] not in PLACEHOLDERS:
                        self.invalid_identifiers += 1
                for title in (row['journal_title'], row.get('journal_abbreviation', '')):
                    if name_key(title):
                        self.by_name[name_key(title)].add(key)
            seen = set()
            for row in categories:
                key = row['journal_id']
                identity = (key, row['category_name'], row['index_or_edition'])
                if key not in self.journals or identity in seen:
                    raise JcrError('duplicate_or_orphan_jcr_category')
                seen.add(identity)
                record = {field: row[field] for field in CATEGORY_KEYS}
                if any(record[field] not in PLACEHOLDERS | {'Q1', 'Q2', 'Q3', 'Q4'}
                       for field in ('jif_quartile', 'jci_quartile')):
                    raise JcrError('invalid_jcr_quartile')
                self.categories[key].append(record)
            if set(self.categories) != set(self.journals):
                raise JcrError('jcr_journal_without_categories')
        except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError):
            raise JcrError('invalid_jcr_snapshot') from None

    def candidates(self, title):
        key = name_key(title)
        names = [key] if key in self.by_name else get_close_matches(key, self.by_name, n=3, cutoff=0.8) if key else []
        ids = sorted({identifier for name in names for identifier in self.by_name[name]})[:5]
        return [self.journals[identifier] for identifier in ids]

    def match(self, article):
        metadata = article.journal_metadata if article.journal_metadata_current() else None
        identifiers = metadata['issns'] if metadata else []
        groups = [self.by_issn[value] for value in identifiers if value in self.by_issn]
        selected, candidates = None, []
        status, reason = 'missing_issn', 'missing_verified_journal_issn'
        if identifiers and not groups:
            status, reason = 'unmatched', 'issn_not_in_snapshot'
        elif groups:
            intersection = set.intersection(*(set(group) for group in groups))
            if len(intersection) == 1:
                selected = next(iter(intersection))
                status, reason = 'matched', 'exact_issn_unique_journal'
            else:
                status, reason = ('ambiguous', 'issn_multiple_journals') if intersection else (
                    'conflict', 'issn_points_to_different_journals')
                candidates = [self.journals[key] for key in sorted(set.union(*(set(group) for group in groups)))[:5]]
        if status != 'matched' and not candidates:
            candidates = self.candidates(metadata.get('journal_title') if metadata else article.journal)
        manifest = self.manifest
        return {'status': status, 'identity_hash': journal_identity_hash(article),
                'metadata_hash': metadata_hash(article.journal_metadata),
                'snapshot_id': manifest['snapshot_id'], 'snapshot_hash': self.fingerprint,
                'metric_year': manifest['metric_year'], 'release_year': manifest['release_year'],
                'revision': manifest['revision'], 'source_kind': manifest['source_kind'],
                'full_complete': self.full_complete, 'version_confirmed': manifest.get('version_confirmed') is True,
                'source_verified': self.source_verified,
                'matched_issns': sorted(value for value in identifiers if selected in self.by_issn.get(value, set())) if selected else [],
                'journal': self.journals[selected] if selected else None,
                'categories': sorted(self.categories[selected], key=lambda row: (row['category_name'], row['index_or_edition'])) if selected else [],
                'candidates': candidates, 'reason': reason}


def render_jcr(article):
    if not article.jcr_match_current():
        return ['JCR：JCR 未核验']
    match = article.jcr_match
    if match['status'] != 'matched':
        return ['JCR：JCR 未核验（' + MATCH_STATUS_LABELS[match['status']] + '）']
    lines = ['JCR：' + ('已核验本地快照' if match['full_complete'] else 'JCR 未核验（本地文件来源/版本待核验）'),
             f"JCR 文件匹配：{match['metric_year']} 年指标 / {match['release_year']} 发布年；ISSN/eISSN 精确匹配",
             '匹配期刊：' + match['journal']['journal_title'],
             '匹配标识：' + '、'.join(match['matched_issns'])]
    jif = match['journal']['jif']
    lines.append('文件 JIF：' + (jif if jif not in PLACEHOLDERS else 'N/A（文件未提供有效值）'))
    for category in match['categories']:
        label = category['category_name'] + ('（' + category['index_or_edition'] + '）' if category['index_or_edition'] else '')
        for metric in ('jif', 'jci'):
            quartile, rank, total = (category[metric + suffix] for suffix in ('_quartile', '_rank', '_category_total'))
            text = quartile if quartile in {'Q1', 'Q2', 'Q3', 'Q4'} else 'N/A（文件未提供分区）'
            if rank not in PLACEHOLDERS and (parse_rank(rank) or total not in PLACEHOLDERS):
                displayed_rank = rank if parse_rank(rank) else f'{rank}/{total}'
                text += f'；排名 {displayed_rank}'
            lines.append(f'文件 {metric.upper()} 分区：{label} / {text}')
    source = {'user_provided_workbook': '用户提供的本地工作簿', 'official_web_export': '本地期刊导出文件',
              'official_api': '期刊指标 API 快照'}.get(match['source_kind'], '本地期刊指标快照')
    lines.append('JCR 数据来源：' + source + '；数据指纹 ' + match['snapshot_hash'][:12])
    return lines


def render_journal_summary(article):
    """正式推送的三行期刊信息；全部学科保留，核验明细仍由 render_jcr 展示。"""
    journal = article.journal or '未提取'
    jif, note, quartiles = '未核验', 'JCR 未核验', ['未核验', '未核验']
    if article.jcr_match_current():
        match = article.jcr_match
        if match['status'] == 'matched':
            journal = article.journal or match['journal']['journal_title']
            value = match['journal']['jif']
            jif = value if value not in PLACEHOLDERS else 'N/A'
            note = f"{match['metric_year']} 年指标"
            quartiles = []
            for metric in ('jif', 'jci'):
                categories = []
                for category in match['categories']:
                    label = category['category_name']
                    if category['index_or_edition']:
                        label += '（' + category['index_or_edition'] + '）'
                    value = category[metric + '_quartile']
                    categories.append(label + ' / ' + (value if value in {'Q1', 'Q2', 'Q3', 'Q4'} else 'N/A'))
                quartiles.append('；'.join(categories) or '未提供分区')
        else:
            note += '（' + MATCH_STATUS_LABELS[match['status']] + '）'
    return ['期刊名称：' + journal + '（JIF=' + jif + '）；' + note,
            'JIF分区：' + quartiles[0], 'JCI 分区：' + quartiles[1]]
