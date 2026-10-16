"""按精确 DOI/PMID 查询公开期刊标识；复用现有 HTTPS/节流/错误保护。"""
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import re
import time
import xml.etree.ElementTree as ET

from .abstracts import AbstractClient, AbstractError, query_url
from .identifiers import normalize_doi, normalize_pmid
from .journal_fields import normalize_issn
from .state import StateError

PROVIDERS = {'PubMed', 'Crossref'}


@dataclass(frozen=True)
class JournalResult:
    provider: str
    status: str
    journal_title: str | None = field(default=None, repr=False)
    issns: list[str] = field(default_factory=list, repr=False)
    issn: str | None = None
    eissn: str | None = None
    linking_issn: str | None = None
    doi: str | None = field(default=None, repr=False)
    pmid: str | None = field(default=None, repr=False)
    error_code: str | None = None


def _ident(value, normalizer):
    if value is None:
        return None
    if not isinstance(value, str) or not normalizer(value):
        raise AbstractError('invalid_journal_response')
    return normalizer(value)


def _issns(provider, title, values, doi=None, pmid=None):
    if title is not None and (not isinstance(title, str) or len(title) > 2000 or '\ufffd' in title):
        raise AbstractError('invalid_journal_response')
    title = ' '.join(title.split()) if title else None
    typed, identifiers = {}, []
    for kind, value in values:
        value = normalize_issn(value)
        if not value:
            raise AbstractError('invalid_journal_issn')
        if kind in {'print', 'electronic', 'linking'}:
            typed.setdefault(kind, set()).add(value)
        if value not in identifiers:
            identifiers.append(value)
    def single(kind):
        values = typed.get(kind, set())
        return next(iter(values)) if len(values) == 1 else None

    return JournalResult(provider, 'found' if identifiers else 'no_issn', title, sorted(identifiers),
                         single('print'), single('electronic'), single('linking'), doi, pmid)


def parse_journal_response(provider, kind, identifier, raw):
    try:
        if provider == 'PubMed' and kind == 'pmid':
            if b'<!ENTITY' in raw.upper():
                raise AbstractError('invalid_journal_response')
            root = ET.fromstring(raw)
            if root.tag != 'PubmedArticleSet' or root.find('.//ERROR') is not None:
                raise AbstractError('invalid_journal_response')
            records = root.findall('PubmedArticle')
            matching = [record for record in records
                        if normalize_pmid(record.findtext('MedlineCitation/PMID')) == identifier]
            if not matching:
                if records:
                    raise AbstractError('journal_identity_mismatch')
                return JournalResult(provider, 'not_found')
            if len(matching) != 1:
                raise AbstractError('invalid_journal_response')
            record = matching[0]
            dois = {_ident(node.text, normalize_doi) for node in
                    record.findall('PubmedData/ArticleIdList/ArticleId') if node.get('IdType') == 'doi'}
            if len(dois) > 1:
                raise AbstractError('journal_identity_mismatch')
            journal = record.find('MedlineCitation/Article/Journal')
            if journal is None:
                return JournalResult(provider, 'no_issn', doi=next(iter(dois), None), pmid=identifier)
            values = [(node.get('IssnType', '').lower(), node.text) for node in journal.findall('ISSN')]
            linking = record.findtext('MedlineCitation/MedlineJournalInfo/ISSNLinking')
            if linking:
                values.append(('linking', linking))
            return _issns(provider, journal.findtext('Title'), values, next(iter(dois), None), identifier)
        if provider == 'Crossref' and kind == 'doi':
            data = json.loads(raw.decode('utf-8'))
            if data.get('status') != 'ok' or data.get('message-type') != 'work':
                raise AbstractError('invalid_journal_response')
            record = data['message']
            doi = _ident(record.get('DOI'), normalize_doi)
            if doi != identifier:
                raise AbstractError('journal_identity_mismatch')
            if record.get('type') not in {'journal-article', None}:
                return JournalResult(provider, 'no_issn', doi=doi)
            titles, identifiers, typed = record.get('container-title', []), record.get('ISSN', []), record.get('issn-type', [])
            if (not isinstance(titles, list) or any(not isinstance(title, str) for title in titles) or
                    not isinstance(identifiers, list) or not isinstance(typed, list)):
                raise AbstractError('invalid_journal_response')
            values = [('', value) for value in identifiers]
            for entry in typed:
                if not isinstance(entry, dict) or entry.get('type') not in {'print', 'electronic'}:
                    raise AbstractError('invalid_journal_response')
                values.append((entry['type'], entry['value']))
            return _issns(provider, titles[0] if titles else None, values, doi=doi)
        raise AbstractError('invalid_journal_query')
    except (ValueError, UnicodeError, KeyError, TypeError, AttributeError, ET.ParseError):
        raise AbstractError('invalid_journal_response') from None


class JournalClient(AbstractClient):
    def __init__(self, **kwargs):
        super().__init__(response_parser=parse_journal_response, **kwargs)

    def query(self, provider, kind, identifier):
        if provider not in PROVIDERS or (provider, kind) not in {('PubMed', 'pmid'), ('Crossref', 'doi')}:
            raise AbstractError('invalid_journal_query')
        result = super().query(provider, kind, identifier)
        if isinstance(result, JournalResult):
            return result
        return JournalResult(provider, result.status, error_code=(
            result.error_code.replace('abstract_', 'journal_', 1) if result.error_code else None))


def validate_result(result, provider, kind, identifier):
    if (result.provider != provider or result.status not in {'found', 'no_issn', 'not_found', 'failed'} or
            (result.status == 'found') != bool(result.issns) or
            not isinstance(result.issns, list) or any(normalize_issn(value) != value for value in result.issns) or
            len(result.issns) != len(set(result.issns)) or
            any(value and value not in result.issns for value in (result.issn, result.eissn, result.linking_issn)) or
            (result.status in {'found', 'no_issn'} and (result.pmid if kind == 'pmid' else result.doi) != identifier) or
            any(value is not None and (not isinstance(value, str) or normalizer(value) != value)
                for value, normalizer in ((result.doi, normalize_doi), (result.pmid, normalize_pmid))) or
            (result.journal_title is not None and (not isinstance(result.journal_title, str) or
                                                 len(result.journal_title) > 2000)) or
            (result.status == 'failed') != bool(result.error_code) or
            (result.error_code and not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', result.error_code))):
        raise StateError('invalid_journal_cache')


class CachedJournals:
    """复用 metadata 键值表，不升级或替换已有数据库架构。"""
    def __init__(self, store, *, client=None, online=False, clock=time.time, max_requests=200):
        self.store, self.client = store, client or JournalClient()
        self.online, self.clock, self.max_requests = online, clock, max_requests
        self.hits = 0
        self.deferred_reason = None

    def lookup(self, provider, kind, identifier, *, allow_fetch=True):
        if (provider, kind) not in {('PubMed', 'pmid'), ('Crossref', 'doi')}:
            raise StateError('invalid_journal_query')
        query_url(provider, kind, identifier)
        # v2 保留同类型的多个合法 ISSN，旧负缓存保留但不沿用旧解析结论。
        key = 'journal-cache-v2:' + sha256(f'{provider}\0{kind}\0{identifier}'.encode('utf-8')).hexdigest()
        row = self.store.connection.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        if row is not None:
            try:
                item = json.loads(row['value'])
                result = JournalResult(**item['result'])
                validate_result(result, provider, kind, identifier)
                if (type(item['checked_at']) not in {int, float} or
                        (item['retry_at'] is not None and type(item['retry_at']) not in {int, float})):
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise StateError('invalid_journal_cache') from None
            if item['retry_at'] is None or self.clock() < item['retry_at']:
                self.hits += 1
                return result
        if not self.online or not allow_fetch:
            self.deferred_reason = 'journal_cache_miss'
            return None
        # 为网络层最多两次尝试保留预算，HTTP 次数不会超过上限。
        if self.client.requests + 2 > self.max_requests:
            self.deferred_reason = 'journal_request_budget_reached'
            return None
        result = self.client.query(provider, kind, identifier)
        validate_result(result, provider, kind, identifier)
        checked = self.clock()
        item = {'result': asdict(result), 'checked_at': checked,
                'retry_at': None if result.status == 'found' else checked + (
                    3600 if result.status == 'failed' else 7 * 86400)}
        with self.store.transaction():
            self.store.connection.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                          (key, json.dumps(item, ensure_ascii=False, separators=(',', ':'))))
        return result
