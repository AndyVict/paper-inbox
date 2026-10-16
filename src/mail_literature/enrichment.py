"""摘要缓存与逐篇补全；每个查询和文献更新单独提交，可中断接续。"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
import time

from .abstracts import AbstractClient, AbstractResult, PROVIDERS, abstract_text, query_url
from .identifiers import normalize_doi, normalize_pmid
from .state import StateError, StateStore


@dataclass
class EnrichmentStats:
    examined: int = 0
    enriched: int = 0
    missing: int = 0
    failed: int = 0
    no_identifier: int = 0
    deferred: int = 0
    existing: int = 0
    remaining: int = 0


def cache_key(provider, kind, identifier):
    query_url(provider, kind, identifier)  # Validate even in cache-only mode.
    return sha256(f"abstract-v1\0{provider}\0{kind}\0{identifier}".encode("utf-8")).hexdigest()


def _validate_result(result):
    if (result.provider not in PROVIDERS or result.status not in {"found", "no_abstract", "not_found", "failed"}
        or bool(result.abstract) != (result.status == "found")
        or (result.abstract is not None and (not isinstance(result.abstract, str) or
            len(result.abstract) > 100_000 or abstract_text(result.abstract, plain=True) != result.abstract))
        or (result.doi is not None and (not isinstance(result.doi, str) or normalize_doi(result.doi) != result.doi))
        or (result.pmid is not None and (not isinstance(result.pmid, str) or normalize_pmid(result.pmid) != result.pmid))
        or (result.status == "failed") != bool(result.error_code)
        or (result.error_code is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", result.error_code))):
        raise StateError("invalid_abstract_cache")


class CachedAbstracts:
    def __init__(self, store: StateStore, *, client=None, online=False, clock=time.time):
        self.store, self.client = store, client or AbstractClient()
        self.online, self.clock = online, clock
        self.hits = 0

    def lookup(self, provider, kind, identifier):
        key = cache_key(provider, kind, identifier)
        row = self.store.connection.execute("SELECT * FROM abstract_cache WHERE cache_key=?", (key,)).fetchone()
        if row is not None:
            try:
                result = AbstractResult(**json.loads(row["payload"]))
                _validate_result(result)
                if result.provider != provider or (
                    result.status in {"found", "no_abstract"} and
                    (result.pmid if kind == "pmid" else result.doi) != identifier):
                    raise StateError("invalid_abstract_cache")
            except (ValueError, TypeError):
                raise StateError("invalid_abstract_cache") from None
            if row["retry_at"] is None or self.clock() < row["retry_at"]:
                self.hits += 1
                return key, result
        if not self.online:
            return key, None
        result = self.client.query(provider, kind, identifier)
        _validate_result(result)
        if result.provider != provider or (result.status in {"found", "no_abstract"} and
            (result.pmid if kind == "pmid" else result.doi) != identifier):
            raise StateError("invalid_abstract_cache")
        now = self.clock()
        retry = None if result.status == "found" else now + (3600 if result.status == "failed" else 7 * 86400)
        payload = json.dumps(asdict(result), ensure_ascii=False, separators=(",", ":"))
        with self.store.transaction():
            self.store.connection.execute("""INSERT INTO abstract_cache VALUES (?,?,?,?,?,1)
                ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, checked_at=excluded.checked_at,
                retry_at=excluded.retry_at, attempts=abstract_cache.attempts+1""",
                (key, provider, payload, now, retry))
        return key, result


def _record(store, article, status, provider, key, error, now):
    with store.transaction():
        if status == "complete":
            store._save_article(article)
            store.connection.execute("""UPDATE articles SET processing_state='enriched'
                WHERE article_id=? AND processing_state='pending'""", (article.article_id,))
        # Rotate checked records even when the result is unchanged, so a bounded
        # batch cannot remain stuck on the same permanently missing abstracts.
        store.connection.execute("""INSERT INTO abstract_enrichment VALUES (?,?,?,?,?,?)
            ON CONFLICT(article_id) DO UPDATE SET status=excluded.status, provider=excluded.provider,
            cache_key=excluded.cache_key, checked_at=excluded.checked_at, error_code=excluded.error_code""",
            (article.article_id, status, provider, key, now, error))


def enrich_abstracts(store: StateStore, cache: CachedAbstracts, *, max_articles=100, progress=None):
    if not 1 <= max_articles <= 1000:
        raise StateError("invalid_enrichment_limit")
    records = store.articles()
    stats = EnrichmentStats(existing=sum(bool(article.abstract_en) for article in records))
    checked = {row["article_id"]: row["checked_at"] for row in
               store.connection.execute("SELECT article_id,checked_at FROM abstract_enrichment")}
    pending = sorted((article for article in records if not article.abstract_en),
                     key=lambda article: (not bool(normalize_doi(article.doi) or normalize_pmid(article.pmid)),
                                          checked.get(article.article_id, -1)))
    for article in pending[:max_articles]:
        stats.examined += 1
        queries = []
        pmid, doi = normalize_pmid(article.pmid), normalize_doi(article.doi)
        if pmid:
            queries.extend([("PubMed", "pmid", pmid), ("EuropePMC", "pmid", pmid)])
        if doi:
            queries.extend([("EuropePMC", "doi", doi), ("Crossref", "doi", doi), ("OpenAlex", "doi", doi)])
        if not queries:
            _record(store, article, "no_identifier", None, None, "missing_public_identifier", cache.clock())
            stats.no_identifier += 1
            continue
        error = None
        deferred = False
        found = False
        for provider, kind, identifier in queries:
            key, result = cache.lookup(provider, kind, identifier)
            if result is None:
                deferred = True
                continue
            if (doi and result.doi and doi != result.doi) or (pmid and result.pmid and pmid != result.pmid):
                error = error or "abstract_identity_mismatch"
                continue
            if result.abstract:
                article.abstract_en = result.abstract
                article.parse_warnings = [warning for warning in article.parse_warnings if warning != "missing_abstract_en"]
                _record(store, article, "complete", provider, key, None, cache.clock())
                stats.enriched += 1
                found = True
                break
            if result.error_code:
                error = error or result.error_code
        if not found:
            if deferred:
                stats.deferred += 1
            else:
                status = "failed" if error else "missing"
                _record(store, article, status, None, key, error or "no_public_abstract", cache.clock())
                setattr(stats, status, getattr(stats, status) + 1)
        if progress and stats.examined % 10 == 0:
            progress(stats)
    stats.remaining = len(pending) - stats.enriched
    return stats
