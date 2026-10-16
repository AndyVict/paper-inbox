"""期刊身份与匹配字段：旧文献 JSON 保持兼容，身份变化后拒用旧结果。"""
from hashlib import sha256
import json
import re

from .jcr.storage import valid_issn


def normalize_issn(value):
    if not isinstance(value, str):
        return None
    value = value.strip().upper().replace('-', '')
    if not re.fullmatch(r'\d{7}[\dX]', value):
        return None
    value = value[:4] + '-' + value[4:]
    return value if valid_issn(value) else None


def journal_identity_hash(article):
    value = [article.doi, article.pmid, article.journal]
    return sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def metadata_hash(metadata):
    return sha256(json.dumps(metadata, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':')).encode('utf-8')).hexdigest()


def validate_journal_fields(article):
    """在状态库/预览边界校验新字段，不信任任意持久化对象。"""
    metadata, match = article.journal_metadata, article.jcr_match
    if metadata is not None:
        if (not isinstance(metadata, dict) or
                set(metadata) != {'identity_hash', 'provider', 'source_url', 'journal_title', 'issns',
                                  'issn', 'eissn', 'linking_issn', 'doi', 'pmid'} or
                metadata.get('provider') not in {'PubMed', 'Crossref'} or
                not re.fullmatch(r'[a-f0-9]{64}', metadata.get('identity_hash', ''))):
            raise ValueError('invalid_journal_metadata')
        for field in ('source_url', 'journal_title', 'issn', 'eissn', 'linking_issn', 'doi', 'pmid'):
            if metadata[field] is not None and not isinstance(metadata[field], str):
                raise ValueError('invalid_journal_metadata')
        issns = metadata['issns']
        if (not isinstance(issns, list) or not issns or any(not isinstance(value, str) for value in issns) or
                len(issns) != len(set(issns)) or any(normalize_issn(value) != value for value in issns) or
                any(metadata[field] and metadata[field] not in issns for field in ('issn', 'eissn', 'linking_issn'))):
            raise ValueError('invalid_journal_metadata')
    if match is not None:
        required = {'status', 'identity_hash', 'metadata_hash', 'snapshot_id', 'snapshot_hash',
                    'metric_year', 'release_year', 'revision', 'source_kind', 'full_complete',
                    'version_confirmed', 'source_verified', 'matched_issns', 'journal', 'categories',
                    'candidates', 'reason'}
        if (not isinstance(match, dict) or set(match) != required or
                match['status'] not in {'matched', 'missing_issn', 'unmatched', 'ambiguous', 'conflict'} or
                any(not isinstance(match[field], str) for field in
                    ('snapshot_id', 'revision', 'source_kind', 'reason')) or
                any(not isinstance(match[field], str) or not re.fullmatch(r'[a-f0-9]{64}', match[field])
                    for field in ('identity_hash', 'metadata_hash', 'snapshot_hash')) or
                any(type(match[field]) is not int for field in ('metric_year', 'release_year')) or
                any(type(match[field]) is not bool for field in ('full_complete', 'version_confirmed', 'source_verified')) or
                not isinstance(match['matched_issns'], list) or
                any(not isinstance(value, str) or normalize_issn(value) != value for value in match['matched_issns']) or
                not isinstance(match['categories'], list) or not isinstance(match['candidates'], list)):
            raise ValueError('invalid_jcr_match')
        journal_keys = {'journal_id', 'journal_title', 'issn', 'eissn', 'jif', 'jci'}
        category_keys = {'category_name', 'index_or_edition', 'jif_quartile', 'jif_rank',
                         'jif_category_total', 'jci_quartile', 'jci_rank', 'jci_category_total'}
        if (match['status'] == 'matched') != bool(match['journal']):
            raise ValueError('invalid_jcr_match')
        if match['status'] == 'matched' and (not match['categories'] or not match['matched_issns']):
            raise ValueError('invalid_jcr_match')
        for journal in ([match['journal']] if match['journal'] else []) + match['candidates']:
            if (not isinstance(journal, dict) or set(journal) != journal_keys or
                    any(not isinstance(value, str) for value in journal.values())):
                raise ValueError('invalid_jcr_match')
        for category in match['categories']:
            if (not isinstance(category, dict) or set(category) != category_keys or
                    any(not isinstance(value, str) for value in category.values()) or
                    any(category[field] not in {'', 'N/A', 'NA', 'N/R', 'Not Ranked', 'Not ranked',
                                                'Forthcoming', 'n/a', '-', '—', 'Q1', 'Q2', 'Q3', 'Q4'}
                        for field in ('jif_quartile', 'jci_quartile'))):
                raise ValueError('invalid_jcr_match')
        if match['status'] != 'matched' and (match['categories'] or match['matched_issns']):
            raise ValueError('invalid_jcr_match')
