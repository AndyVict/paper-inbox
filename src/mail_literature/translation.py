"""译文及分段缓存，每个请求/字段独立提交；源文哈希避免复用过期译文。"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import time

from .niutrans import (SAFE_ERRORS, TranslationError, TranslationOptions, TranslationResult,
                      segment_byte_limit, split_text, validate_options)
from .state import StateError, StateStore


def source_hash(text):
    return sha256(text.encode("utf-8")).hexdigest()


def translation_key(text, options=TranslationOptions(), *, chunk=False):
    values = asdict(options)
    if values["api_variant"] == "cloud":
        values.pop("api_variant")  # Preserve already accepted cloud-cache keys.
    profile = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return source_hash(f"niutrans-en-zh-v1\0{profile}\0{'chunk' if chunk else 'whole'}\0{text}")


def _validate_result(result):
    if (result.status not in {"complete", "failed"} or
            (result.status == "complete" and (not isinstance(result.text, str) or not result.text.strip() or
                                             len(result.text) > 500_000 or result.error_code is not None)) or
            (result.status == "failed" and (result.text is not None or result.error_code not in SAFE_ERRORS))):
        raise StateError("invalid_translation_cache")


class CachedTranslations:
    def __init__(self, store: StateStore, *, client=None, online=False, options=None,
                 max_characters=20_000, max_requests=200, clock=time.time):
        if not 1 <= max_characters <= 1_000_000 or not 1 <= max_requests <= 1000 or (online and client is None):
            raise StateError("invalid_translation_limits_or_client")
        row = store.connection.execute("SELECT value FROM metadata WHERE key='translation_profile'").fetchone()
        if options is None:
            if online and client is not None:
                options = getattr(getattr(client, "config", None), "options", TranslationOptions())
            elif row:
                try:
                    options = TranslationOptions(**json.loads(row[0]))
                except (ValueError, TypeError):
                    raise StateError("invalid_translation_cache_profile") from None
            else:
                options = TranslationOptions()
        try:
            validate_options(options)
        except (TranslationError, TypeError):
            raise StateError("invalid_translation_cache_profile") from None
        if online:
            value = json.dumps(asdict(options), sort_keys=True)
            if row is None or row[0] != value:
                with store.transaction():
                    store.connection.execute("INSERT INTO metadata VALUES ('translation_profile',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
        self.store, self.client, self.online, self.options = store, client, online, options
        self.max_characters, self.max_requests, self.clock = max_characters, max_requests, clock
        self.hits = 0
        self.deferred_reason = None

    def _load(self, key):
        row = self.store.connection.execute("SELECT payload,retry_at FROM translation_cache WHERE cache_key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            result = TranslationResult(**json.loads(row["payload"]))
            _validate_result(result)
        except (ValueError, TypeError):
            raise StateError("invalid_translation_cache") from None
        if row["retry_at"] is not None and row["retry_at"] <= self.clock():
            return None
        self.hits += 1
        return result

    def _save(self, key, result):
        _validate_result(result)
        now = self.clock()
        with self.store.transaction():
            self.store.connection.execute("""INSERT INTO translation_cache VALUES (?,?,?,?,1)
                ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, checked_at=excluded.checked_at,
                retry_at=excluded.retry_at, attempts=translation_cache.attempts+1""",
                (key, json.dumps(asdict(result), ensure_ascii=False, sort_keys=True), now,
                 now + 3600 if result.status == "failed" else None))

    def lookup(self, text):
        key = translation_key(text, self.options)
        result = self._load(key)
        if result is not None:
            return key, result
        translated = []
        for chunk, separator in split_text(text, max_bytes=segment_byte_limit(self.options)):
            chunk_key = translation_key(chunk, self.options, chunk=True)
            result = self._load(chunk_key)
            if result is None:
                if not self.online:
                    self.deferred_reason = "translation_cache_miss"
                    return key, None
                if self.client.stopped:
                    self.deferred_reason = self.client.stopped
                    return key, None
                if (self.client.characters + len(chunk) > self.max_characters or
                        self.client.requests >= self.max_requests):
                    self.deferred_reason = "translation_budget_reached"
                    return key, None
                result = self.client.translate(chunk)
                self._save(chunk_key, result)
            if result.status == "failed":
                if self.online:
                    self.client.stopped = result.error_code
                return key, result
            translated.append(result.text + separator)
        result = TranslationResult("complete", "".join(translated).strip())
        self._save(key, result)
        return key, result


@dataclass
class TranslationStats:
    examined: int = 0
    titles: int = 0
    abstracts: int = 0
    failed: int = 0
    deferred: int = 0
    title_total: int = 0
    abstract_total: int = 0
    remaining_titles: int = 0
    remaining_abstracts: int = 0
    missing_abstracts: int = 0


def _record(store, article, name, key, result, now):
    digest = source_hash(getattr(article, name + "_en"))
    with store.transaction():
        if result.status == "complete":
            setattr(article, name + "_zh", result.text)
            setattr(article, name + "_zh_source_hash", digest)
            store._save_article(article)
            store.connection.execute("UPDATE articles SET processing_state='enriched' WHERE article_id=? AND processing_state='pending'",
                                     (article.article_id,))
        store.connection.execute("""INSERT INTO article_translations VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(article_id,field) DO UPDATE SET source_hash=excluded.source_hash, cache_key=excluded.cache_key,
            status=excluded.status, checked_at=excluded.checked_at, error_code=excluded.error_code""",
            (article.article_id, name, digest, key, result.status, now, result.error_code))


def translate_articles(store, cache, *, max_articles=100, refresh=False, progress=None):
    if not 1 <= max_articles <= 1000:
        raise StateError("invalid_translation_article_limit")
    records = store.articles()
    stats = TranslationStats()
    checked = {row["article_id"]: row["checked_at"] for row in store.connection.execute(
        "SELECT article_id,MIN(checked_at) AS checked_at FROM article_translations GROUP BY article_id")}
    pending = sorted((a for a in records if refresh or not a.translation_current("title") or
                      (a.abstract_en and not a.translation_current("abstract"))), key=lambda a: checked.get(a.article_id, -1))
    for article in pending[:max_articles]:
        stats.examined += 1
        before = article.to_dict()
        article.clear_stale_translations()
        if before != article.to_dict():
            with store.transaction():
                store._save_article(article)
        for name in ("title", "abstract"):
            text = getattr(article, name + "_en")
            if not text or (not refresh and article.translation_current(name)):
                continue
            key, result = cache.lookup(text)
            if result is None:
                stats.deferred += 1
                continue
            _record(store, article, name, key, result, cache.clock())
            if result.status == "complete":
                if name == "title":
                    stats.titles += 1
                else:
                    stats.abstracts += 1
            else:
                stats.failed += 1
            if progress:
                progress(stats)
        if cache.online and (cache.client.stopped or cache.deferred_reason == "translation_budget_reached"):
            break
    stats.title_total = sum(a.translation_current("title") for a in records)
    stats.abstract_total = sum(a.translation_current("abstract") for a in records)
    stats.missing_abstracts = sum(not a.abstract_en for a in records)
    stats.remaining_titles = len(records) - stats.title_total
    stats.remaining_abstracts = len(records) - stats.missing_abstracts - stats.abstract_total
    return stats
