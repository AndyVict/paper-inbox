"""逐接收者发送状态与预览；先持久化 inflight，再进行唯一一次 POST。"""
from dataclasses import dataclass
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time

from .identifiers import article_link
from .jcr.matching import render_journal_summary
from .preview import ID_PATTERN, _atomic_write, _lock
from .state import StateError, StateStore
from .wxpusher import PushError, recipient_key, validate_content

BACKUP_MARKER = "wxpusher-v1:backup"
DETAIL_SCHEMA = """CREATE TABLE IF NOT EXISTS wxpusher_attempt_details (
    attempt_id INTEGER PRIMARY KEY REFERENCES delivery_attempts(attempt_id),
    content_hash TEXT NOT NULL, send_record_id INTEGER,
    provider_code INTEGER, provider_status INTEGER, query_code INTEGER,
    updated_at TEXT NOT NULL, received_at TEXT)"""


def timestamp(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def render_message(article, max_abstract=1000, *, language="zh"):
    if language not in {"zh", "en"}:
        raise PushError("invalid_delivery_language")
    if not 100 <= max_abstract <= 10000:
        raise PushError("invalid_abstract_length")
    title = article.title_zh if article.translation_current("title") else "待翻译"
    abstract = article.abstract_zh if article.translation_current("abstract") else (
        "待翻译（已有英文摘要）" if article.abstract_en else "未找到公开摘要，待补全")
    if language == "en":
        title = article.title_en
        abstract = article.abstract_en or "No public abstract available."
    if len(abstract) > max_abstract:
        abstract = abstract[:max_abstract].rstrip() + "……（摘要已截断，完整内容见文献链接）"
    lines = (["中文标题：" + title, "英文标题：" + article.title_en] if language == "zh"
             else ["英文标题：" + title])
    lines.extend(render_journal_summary(article))
    link = article_link(article.doi, article.pmid, article.article_url)
    lines.extend([("中文摘要：" if language == "zh" else "英文摘要：") + abstract,
                  "链接：" + (link or "未提取公共文献链接")])
    content = "\n".join(lines) + "\n"
    summary = (title if title != "待翻译" else article.title_en).replace("\n", " ")[:100]
    validate_content(content, summary)
    return content, summary


def write_send_previews(articles, directory: Path, max_abstract=1000, *, language="zh"):
    directory.mkdir(parents=True, exist_ok=True)
    written = oversized = 0
    with _lock(directory):
        for article in articles:
            if not ID_PATTERN.fullmatch(article.article_id):
                raise StateError("invalid_article_id")
            try:
                content, _ = render_message(article, max_abstract, language=language)
            except PushError:
                oversized += 1
                continue
            _atomic_write(directory / (article.article_id + ".txt"), content)
            written += 1
        _atomic_write(directory / "manifest.json", json.dumps(
            {"article_ids": [a.article_id for a in articles], "max_abstract": max_abstract, "language": language,
             "generated": written, "oversized": oversized}, ensure_ascii=False, indent=2) + "\n")
    return written, oversized


def prepare_delivery(store: StateStore, data: Path):
    if not store.connection.execute("SELECT 1 FROM metadata WHERE key=?", (BACKUP_MARKER,)).fetchone():
        directory = data / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        name = "pre-wxpusher-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3"
        with closing(sqlite3.connect(directory / name)) as destination:
            store.connection.backup(destination)
            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StateError("wxpusher_backup_failed")
        with store.transaction():
            store.connection.execute(DETAIL_SCHEMA)
            store.connection.execute("INSERT INTO metadata VALUES (?,?)", (BACKUP_MARKER, name))
    else:
        tables = {row[0] for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "wxpusher_attempt_details" not in tables:
            raise StateError("invalid_wxpusher_state")


def receipt_confirmed(store, key):
    return store.connection.execute("SELECT 1 FROM metadata WHERE key=?",
                                    ("wxpusher-v1:receipt:" + key,)).fetchone() is not None


def confirm_received(store, attempt_id, keys, *, clock=time.time):
    with store.transaction():
        row = store.connection.execute("SELECT * FROM delivery_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None or row["recipient_key"] not in keys or row["status"] not in {"accepted", "unknown", "inflight", "confirmed"}:
            raise StateError("invalid_receipt_confirmation")
        detail = store.connection.execute("SELECT 1 FROM wxpusher_attempt_details WHERE attempt_id=?", (attempt_id,)).fetchone()
        if detail is None:
            raise StateError("invalid_receipt_confirmation")
        store.connection.execute("UPDATE delivery_attempts SET status='confirmed',error_code=NULL WHERE attempt_id=?", (attempt_id,))
        store.connection.execute("UPDATE wxpusher_attempt_details SET received_at=?,updated_at=? WHERE attempt_id=?",
                                 (timestamp(clock()), timestamp(clock()), attempt_id))
        store.connection.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)",
                                 ("wxpusher-v1:receipt:" + row["recipient_key"], str(attempt_id)))


def reconcile(store, client, *, max_queries=10, clock=time.time):
    keys = {recipient_key(uid) for uid in client.config.uids}
    queried = unavailable = requests = 0
    rows = store.connection.execute("""SELECT d.attempt_id,d.recipient_key,x.send_record_id
        FROM delivery_attempts d JOIN wxpusher_attempt_details x USING(attempt_id)
        WHERE d.status IN ('accepted','unknown','inflight') AND x.send_record_id IS NOT NULL
        ORDER BY x.updated_at,d.attempt_id""").fetchall()
    for row in rows:
        if row["recipient_key"] not in keys:
            continue
        if requests >= max_queries:
            break
        requests += 1
        try:
            code, status = client.query(row["send_record_id"])
        except PushError:
            unavailable += 1
            with store.transaction():
                store.connection.execute("UPDATE wxpusher_attempt_details SET updated_at=? WHERE attempt_id=?",
                                         (timestamp(clock()), row["attempt_id"]))
            continue
        with store.transaction():
            store.connection.execute("UPDATE wxpusher_attempt_details SET query_code=?,provider_status=?,updated_at=? WHERE attempt_id=?",
                                     (code, status, timestamp(clock()), row["attempt_id"]))
        queried += 1
        unavailable += code != 1000
    return queried, unavailable


@dataclass
class DeliveryStats:
    attempted: int = 0
    accepted: int = 0
    failed: int = 0
    unknown: int = 0
    skipped: int = 0
    deferred: int = 0
    blocked: int = 0
    last_attempt_id: int | None = None


def send_articles(store, client, articles, *, max_messages=20, daily_limit=200,
                  max_abstract=1000, retry_failed=False, require_receipt=False,
                  language="zh", clock=time.time):
    if language not in {"zh", "en"}:
        raise PushError("invalid_delivery_language")
    if not 1 <= max_messages <= 1000 or not 1 <= daily_limit <= 1000:
        raise PushError("invalid_delivery_limits")
    keys = {uid: recipient_key(uid) for uid in client.config.uids}
    if require_receipt and any(not receipt_confirmed(store, key) for key in keys.values()):
        raise PushError("wxpusher_receipt_not_confirmed")
    stats = DeliveryStats()
    # 网络异常或业务拒绝后停止本批，避免把同样的问题扩散到整个队列。
    stopped = False
    for article in articles:
        try:
            content, summary = render_message(article, max_abstract, language=language)
        except PushError:
            stats.deferred += len(keys)
            continue
        for uid, key in keys.items():
            with store.transaction():
                status = store.delivery_status(article.article_id, key)
                if status in {"accepted", "confirmed", "unknown"} or (status == "failed" and not retry_failed):
                    stats.skipped += 1
                    stats.blocked += status in {"unknown", "failed"}
                    continue
                if language == "zh" and (not article.translation_current("title") or
                        (article.abstract_en and not article.translation_current("abstract"))):
                    stats.deferred += 1
                    continue
                recent = store.connection.execute("""SELECT COUNT(*) FROM delivery_attempts
                    WHERE recipient_key=? AND created_at>=? AND status IN ('inflight','unknown','accepted','confirmed')""",
                    (key, timestamp(clock() - 86400))).fetchone()[0]
                if stopped or stats.attempted >= max_messages or recent >= daily_limit:
                    stats.deferred += 1
                    continue
                cursor = store.connection.execute("INSERT INTO delivery_attempts(article_id,recipient_key,status,created_at) VALUES (?,?,'inflight',?)",
                                                   (store.resolve(article.article_id), key, timestamp(clock())))
                attempt_id = cursor.lastrowid
                store.connection.execute("INSERT INTO wxpusher_attempt_details(attempt_id,content_hash,updated_at) VALUES (?,?,?)",
                                         (attempt_id, sha256(content.encode("utf-8")).hexdigest(), timestamp(clock())))
            stats.attempted += 1
            stats.last_attempt_id = attempt_id
            result = client.send(uid, content, summary)
            with store.transaction():
                store.connection.execute("UPDATE delivery_attempts SET status=?,error_code=? WHERE attempt_id=?",
                                         (result.status, result.error_code, attempt_id))
                store.connection.execute("UPDATE wxpusher_attempt_details SET send_record_id=?,provider_code=?,updated_at=? WHERE attempt_id=?",
                                         (result.send_record_id, result.provider_code, timestamp(clock()), attempt_id))
            setattr(stats, result.status, getattr(stats, result.status) + 1)
            if result.status in {"failed", "unknown"}:
                stopped = True
    return stats
