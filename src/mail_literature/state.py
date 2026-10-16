"""SQLite 状态库：保存文献字段和哈希化邮箱命名空间，不保存凭据或邮件头。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3

from .dedup import _matches, deduplicate
from .models import Article
from .preview import OutboxError, SCHEMA_VERSION, _load_index, articles_from_payload

DB_VERSION = 3
MAIL_STATES = {"parsed", "ignored", "failed", "vanished"}
PROCESSING_STATES = {"pending", "enriched", "ready", "failed"}
DELIVERY_STATES = {"pending", "inflight", "accepted", "confirmed", "failed", "unknown"}

ABSTRACT_SCHEMA = [
    """CREATE TABLE abstract_cache (
        cache_key TEXT PRIMARY KEY, provider TEXT NOT NULL, payload TEXT NOT NULL,
        checked_at REAL NOT NULL, retry_at REAL, attempts INTEGER NOT NULL)""",
    """CREATE TABLE abstract_enrichment (
        article_id TEXT PRIMARY KEY REFERENCES articles(article_id),
        status TEXT NOT NULL, provider TEXT, cache_key TEXT,
        checked_at REAL NOT NULL, error_code TEXT)""",
]

TRANSLATION_SCHEMA = [
    """CREATE TABLE translation_cache (
        cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, checked_at REAL NOT NULL,
        retry_at REAL, attempts INTEGER NOT NULL)""",
    """CREATE TABLE article_translations (
        article_id TEXT NOT NULL REFERENCES articles(article_id),
        field TEXT NOT NULL CHECK(field IN ('title','abstract')), source_hash TEXT NOT NULL,
        cache_key TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('complete','failed')),
        checked_at REAL NOT NULL, error_code TEXT, PRIMARY KEY(article_id,field))""",
]

BASE_SCHEMA = [
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE articles (
        article_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
        processing_state TEXT NOT NULL DEFAULT 'pending'
            CHECK(processing_state IN ('pending','enriched','ready','failed')),
        merged_into TEXT REFERENCES articles(article_id))""",
    """CREATE TABLE article_aliases (
        article_id TEXT NOT NULL REFERENCES articles(article_id), identity_key TEXT NOT NULL,
        PRIMARY KEY(article_id, identity_key))""",
    """CREATE TABLE article_sources (
        article_id TEXT NOT NULL REFERENCES articles(article_id), source_key TEXT NOT NULL,
        source TEXT NOT NULL, mail_key TEXT NOT NULL, received_at TEXT, email_uid TEXT,
        PRIMARY KEY(article_id, source_key))""",
    """CREATE TABLE mailbox_epochs (
        namespace TEXT NOT NULL, uidvalidity INTEGER NOT NULL, scan_cursor INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(namespace, uidvalidity))""",
    """CREATE TABLE mail_records (
        namespace TEXT NOT NULL, uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('parsed','ignored','failed','vanished')),
        attempts INTEGER NOT NULL, error_code TEXT, mail_key TEXT, internaldate TEXT,
        PRIMARY KEY(namespace, uidvalidity, uid),
        FOREIGN KEY(namespace, uidvalidity) REFERENCES mailbox_epochs(namespace, uidvalidity))""",
    """CREATE TABLE mail_articles (
        namespace TEXT NOT NULL, uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
        article_id TEXT NOT NULL REFERENCES articles(article_id),
        PRIMARY KEY(namespace, uidvalidity, uid, article_id),
        FOREIGN KEY(namespace, uidvalidity, uid) REFERENCES mail_records(namespace, uidvalidity, uid))""",
    """CREATE TABLE delivery_attempts (
        attempt_id INTEGER PRIMARY KEY, article_id TEXT NOT NULL REFERENCES articles(article_id),
        recipient_key TEXT NOT NULL, status TEXT NOT NULL
            CHECK(status IN ('pending','inflight','accepted','confirmed','failed','unknown')),
        error_code TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    "CREATE INDEX alias_lookup ON article_aliases(identity_key)",
    "CREATE INDEX retry_lookup ON mail_records(namespace, uidvalidity, status, uid)",
]
SCHEMA = BASE_SCHEMA + ABSTRACT_SCHEMA + TRANSLATION_SCHEMA


class StateError(Exception):
    """仅携带可安全打印的固定错误码。"""


@dataclass
class IngestStats:
    total: int
    created: int
    updated: int
    merged: int


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_error(code: str | None) -> None:
    if code is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        raise StateError("invalid_error_code")


class StateStore:
    def __init__(self, path: Path, *, readonly: bool = False):
        self.path = path
        self.connection: sqlite3.Connection | None = None
        try:
            if readonly:
                self.connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                                 timeout=5, isolation_level=None)
                self.connection.execute("PRAGMA query_only=ON")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                self.connection = sqlite3.connect(path, timeout=5, isolation_level=None)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if readonly and version != DB_VERSION:
                raise StateError("readonly_requires_current_database")
            if version == 0 and not tables:
                with self.transaction():
                    for statement in SCHEMA:
                        self.connection.execute(statement)
                    self.connection.execute(f"PRAGMA user_version={DB_VERSION}")
            elif version not in {1, 2, DB_VERSION} or not {
                "metadata", "articles", "article_aliases", "article_sources", "mailbox_epochs",
                "mail_records", "mail_articles", "delivery_attempts"
            }.issubset(tables):
                raise StateError("unsupported_state_database")
            if self.connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StateError("invalid_state_database")
            if version in {2, DB_VERSION} and not {"abstract_cache", "abstract_enrichment"}.issubset(tables):
                raise StateError("unsupported_state_database")
            if version in {1, 2}:
                with self.transaction():
                    for statement in (ABSTRACT_SCHEMA if version == 1 else []) + TRANSLATION_SCHEMA:
                        self.connection.execute(statement)
                    self.connection.execute(f"PRAGMA user_version={DB_VERSION}")
            elif version == DB_VERSION and not {"translation_cache", "article_translations"}.issubset(tables):
                raise StateError("unsupported_state_database")
        except (sqlite3.Error, OSError):
            self.close()
            raise StateError("state_open_failed") from None
        except StateError:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    @contextmanager
    def transaction(self):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self.connection.execute("COMMIT")
        except BaseException as exc:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise StateError("state_transaction_failed") from None
            raise

    def articles(self) -> list[Article]:
        try:
            records = []
            for row in self.connection.execute(
                    "SELECT article_id,payload FROM articles WHERE merged_into IS NULL ORDER BY rowid"):
                record = json.loads(row["payload"])
                if not isinstance(record, dict) or record.get("article_id") != row["article_id"]:
                    raise StateError("invalid_state_articles")
                records.append(record)
            payload = {"schema_version": SCHEMA_VERSION, "articles": records}
            return articles_from_payload(payload)
        except (sqlite3.Error, ValueError, OutboxError):
            raise StateError("invalid_state_articles") from None

    def import_legacy(self, index: Path) -> IngestStats | None:
        # The marker and import commit together. A stale JSON can never be imported
        # again after SQLite has retired an ID or collected delivery history.
        with self.transaction():
            if self.connection.execute("SELECT 1 FROM metadata WHERE key='legacy_imported'").fetchone():
                return None
            incoming = _load_index(index)
            existing = self.articles()
            if existing and incoming:
                raise StateError("legacy_import_requires_empty_database")
            # The existing index is already validated and deduplicated. Import
            # exact records, including alias/source order and stable IDs.
            for article in incoming:
                self._save_article(article)
            stats = IngestStats(len(existing) + len(incoming), len(incoming), 0, 0)
            self.connection.execute("INSERT INTO metadata VALUES ('legacy_imported','1')")
            return stats

    def ingest(self, incoming: list[Article]) -> IngestStats:
        with self.transaction():
            return self._ingest(incoming)

    def _ingest(self, incoming: list[Article]) -> IngestStats:
        previous = self.articles()
        old = {article.article_id: article for article in previous}
        current = deduplicate(previous + incoming)
        articles_from_payload({"schema_version": SCHEMA_VERSION,
                               "articles": [article.to_dict() for article in current]})
        active = {article.article_id: article for article in current}
        created = updated = 0
        for article in current:
            key = article.article_id
            created += key not in old
            updated += key in old and old[key].to_dict() != article.to_dict()
            self._save_article(article)
        for retired in old.keys() - active.keys():
            survivor = next((article for article in current if _matches(article, old[retired])), None)
            if survivor is None:
                raise StateError("unresolved_article_merge")
            self.connection.execute("UPDATE articles SET merged_into=? WHERE article_id=?",
                                    (survivor.article_id, retired))
            # Original delivery/mail references remain on their original IDs;
            # resolve() follows the chain so history is retained, never overwritten.
        return IngestStats(len(current), created, updated, len(old.keys() - active.keys()))

    def _save_article(self, article: Article) -> None:
        from hashlib import sha256

        key = article.article_id
        self.connection.execute("""INSERT INTO articles(article_id,payload) VALUES (?,?)
            ON CONFLICT(article_id) DO UPDATE SET payload=excluded.payload""", (key, _json(article.to_dict())))
        for alias in article.identity_keys:
            self.connection.execute("INSERT OR IGNORE INTO article_aliases VALUES (?,?)", (key, alias))
        for ref in article.sources:
            source_key = sha256(_json(ref.__dict__).encode("utf-8")).hexdigest()
            self.connection.execute("INSERT OR IGNORE INTO article_sources VALUES (?,?,?,?,?,?)",
                (key, source_key, ref.source, ref.mail_key, ref.received_at, ref.email_uid))

    def resolve(self, article_id: str) -> str:
        visited = set()
        while article_id not in visited:
            visited.add(article_id)
            row = self.connection.execute("SELECT merged_into FROM articles WHERE article_id=?",
                                          (article_id,)).fetchone()
            if row is None:
                raise StateError("unknown_article_id")
            if row[0] is None:
                return article_id
            article_id = row[0]
        raise StateError("invalid_merge_chain")

    def set_processing_state(self, article_id: str, status: str) -> None:
        if status not in PROCESSING_STATES:
            raise StateError("invalid_processing_state")
        with self.transaction():
            self.connection.execute("UPDATE articles SET processing_state=? WHERE article_id=?",
                                    (status, self.resolve(article_id)))

    def record_delivery(self, article_id: str, recipient_key: str, status: str,
                        error_code: str | None = None) -> None:
        if status not in DELIVERY_STATES or not re.fullmatch(r"[a-f0-9]{64}", recipient_key):
            raise StateError("invalid_delivery_record")
        _safe_error(error_code)
        with self.transaction():
            self.connection.execute("INSERT INTO delivery_attempts(article_id,recipient_key,status,error_code) VALUES (?,?,?,?)",
                                    (self.resolve(article_id), recipient_key, status, error_code))

    def delivery_status(self, article_id: str, recipient_key: str) -> str:
        canonical = self.resolve(article_id)
        history = [row["status"] for row in self.connection.execute(
            "SELECT * FROM delivery_attempts WHERE recipient_key=? ORDER BY attempt_id", (recipient_key,))
            if self.resolve(row["article_id"]) == canonical]
        # Acceptance is not proof of WeChat receipt. Unknown/inflight attempts
        # remain visible and require reconciliation before a future sender retries.
        for permanent in ("confirmed", "accepted"):
            if permanent in history:
                return permanent
        if any(status in {"unknown", "inflight"} for status in history):
            return "unknown"
        return history[-1] if history else "pending"

    def ensure_epoch(self, namespace: str, uidvalidity: int) -> int:
        if not re.fullmatch(r"[a-f0-9]{64}", namespace) or not 1 <= uidvalidity <= 4294967295:
            raise StateError("invalid_mailbox_epoch")
        with self.transaction():
            self.connection.execute("INSERT OR IGNORE INTO mailbox_epochs(namespace,uidvalidity) VALUES (?,?)",
                                    (namespace, uidvalidity))
        return self.connection.execute("SELECT scan_cursor FROM mailbox_epochs WHERE namespace=? AND uidvalidity=?",
                                       (namespace, uidvalidity)).fetchone()[0]

    def retry_uids(self, namespace: str, uidvalidity: int) -> list[int]:
        return [row[0] for row in self.connection.execute(
            "SELECT uid FROM mail_records WHERE namespace=? AND uidvalidity=? AND status='failed' ORDER BY attempts,uid",
            (namespace, uidvalidity))]

    def record_mail(self, namespace: str, uidvalidity: int, uid: int, status: str, *,
                    articles: list[Article] | None = None, error_code: str | None = None,
                    mail_key: str | None = None, internaldate: str | None = None) -> None:
        if status not in MAIL_STATES or not 1 <= uid <= 4294967295:
            raise StateError("invalid_mail_record")
        if bool(articles) != (status == "parsed"):
            raise StateError("invalid_mail_articles")
        _safe_error(error_code)
        with self.transaction():
            if articles:
                self._ingest(articles)
            self.connection.execute("""INSERT INTO mail_records VALUES (?,?,?,?,1,?,?,?)
                ON CONFLICT(namespace,uidvalidity,uid) DO UPDATE SET status=excluded.status,
                attempts=mail_records.attempts+1, error_code=excluded.error_code,
                mail_key=excluded.mail_key, internaldate=excluded.internaldate""",
                (namespace, uidvalidity, uid, status, error_code, mail_key, internaldate))
            if articles:
                current = self.articles()
                for incoming in deduplicate(articles):
                    match = next((article for article in current if _matches(article, incoming)), None)
                    if match is None:
                        raise StateError("unresolved_mail_article")
                    self.connection.execute("INSERT OR IGNORE INTO mail_articles VALUES (?,?,?,?)",
                                            (namespace, uidvalidity, uid, match.article_id))
            # Cursor tracks examined UIDs, including failed ones. Failures have
            # their own durable retry queue and are not lost behind this cursor.
            self.connection.execute("""UPDATE mailbox_epochs SET scan_cursor=MAX(scan_cursor,?)
                WHERE namespace=? AND uidvalidity=?""", (uid, namespace, uidvalidity))
