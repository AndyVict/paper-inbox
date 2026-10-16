from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile

from .dedup import deduplicate
from .identifiers import article_link
from .models import Article
from .journal_fields import validate_journal_fields

SCHEMA_VERSION = 1
ID_PATTERN = re.compile(r"[a-f0-9]{24}")


class OutboxError(Exception):
    """可安全显示的离线输出错误。"""


@dataclass
class PreviewStats:
    total: int
    created: int
    updated: int
    unchanged: int
    archived: int


def render_preview(article: Article) -> str:
    link = article_link(article.doi, article.pmid, article.article_url)
    lines = [
        "【文献推荐 · 离线预览】", "", "中文标题：" + (article.title_zh if article.translation_current("title") else "待翻译"),
        f"英文标题：{article.title_en}", f"期刊：{article.journal or '未提取'}",
        f"年份：{article.year or '未提取'}",
    ]
    from .jcr.matching import render_jcr
    lines.extend(render_jcr(article))
    if article.quartile_claimed:
        lines.append(f"第三方分区：{article.quartile_claimed}（Stork 标注，来源未核验）")
    if article.authors:
        lines.append("作者：" + "; ".join(article.authors))
    if article.impact_factor:
        lines.append(f"影响因子：{article.impact_factor}（邮件标注，年份与来源未核验）")
    lines.append("中文摘要：" + article.abstract_zh if article.translation_current("abstract") else
                 ("中文摘要：待翻译（已提取英文摘要）" if article.abstract_en else "中文摘要：摘要待补全"))
    if article.abstract_en:
        lines.append("英文摘要：" + article.abstract_en)
    lines.append(f"链接：{link or '未提取公共文献链接'}")
    if article.doi:
        lines.append("DOI：" + article.doi)
    if article.pmid:
        lines.append("PMID：" + article.pmid)
    lines.extend(["来源：" + "、".join(dict.fromkeys(ref.source for ref in article.sources)),
                  "", "状态：仅供本地预览，未发送。"])
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                     dir=path.parent, suffix=".tmp", delete=False) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    os.replace(temporary, path)


@contextmanager
def _lock(outbox: Path):
    # A crash leaves this single file for deliberate recovery; never guess that a
    # previous process has stopped or let concurrent writers overwrite the index.
    lock_path = outbox / ".preview.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise OutboxError("outbox_locked") from None
    try:
        os.close(descriptor)
        yield
    finally:
        lock_path.unlink()  # Only the exact lock created by this invocation.


def articles_from_payload(payload: dict) -> list[Article]:
    """严格验证持久化数据；不把无效数据或原始异常带入错误信息。"""
    try:
        if payload["schema_version"] != SCHEMA_VERSION or not isinstance(payload["articles"], list):
            raise ValueError
        articles = [Article.from_dict(item) for item in payload["articles"]]
        ids = [entry.article_id for entry in articles]
        if any(not isinstance(key, str) or not ID_PATTERN.fullmatch(key) for key in ids) or len(set(ids)) != len(ids):
            raise ValueError
        if any(not isinstance(entry.title_en, str) or not entry.title_en.strip() for entry in articles):
            raise ValueError
        for entry in articles:
            validate_journal_fields(entry)
            for field in ["journal", "published_at", "doi", "pmid", "article_url", "impact_factor",
                          "quartile_claimed", "quartile_source", "abstract_en", "title_zh", "abstract_zh",
                          "title_zh_source_hash", "abstract_zh_source_hash"]:
                value = getattr(entry, field)
                if value is not None and not isinstance(value, str):
                    raise ValueError
            for name in ("title", "abstract"):
                translated, digest = getattr(entry, name + "_zh"), getattr(entry, name + "_zh_source_hash")
                if bool(translated) != bool(digest) or (digest and not re.fullmatch(r"[a-f0-9]{64}", digest)):
                    raise ValueError
            if entry.year is not None and (type(entry.year) is not int or not 1900 <= entry.year <= 2099):
                raise ValueError
            for field in ["authors", "parse_warnings", "identity_keys"]:
                value = getattr(entry, field)
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError
            for ref in entry.sources:
                if ref.source not in {"X-MOL", "Stork"} or not isinstance(ref.mail_key, str):
                    raise ValueError
                if any(value is not None and not isinstance(value, str) for value in [ref.received_at, ref.email_uid]):
                    raise ValueError
        return articles
    except (KeyError, TypeError, ValueError):
        raise OutboxError("invalid_outbox_index") from None


def _load_index(path: Path) -> list[Article]:
    if not path.exists():
        return []
    try:
        return articles_from_payload(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, UnicodeError):
        raise OutboxError("invalid_outbox_index") from None


def write_previews(articles: list[Article], outbox: Path) -> PreviewStats:
    outbox.mkdir(parents=True, exist_ok=True)
    with _lock(outbox):
        return _write_previews(articles, outbox)


def write_snapshot_previews(articles: list[Article], outbox: Path) -> PreviewStats:
    """数据库的完整活动快照；旧 JSON 只用于统计及归档，不能复活合并记录。"""
    articles_from_payload({"schema_version": SCHEMA_VERSION,
                           "articles": [article.to_dict() for article in articles]})
    outbox.mkdir(parents=True, exist_ok=True)
    with _lock(outbox):
        return _write_previews(articles, outbox, authoritative=True)


def _write_previews(articles: list[Article], outbox: Path, *, authoritative: bool = False) -> PreviewStats:
    index_path = outbox / "index.json"
    previous = _load_index(index_path)
    old = {entry.article_id: entry.to_dict() for entry in previous}
    current = articles if authoritative else deduplicate(previous + articles)
    active_ids = {entry.article_id for entry in current}
    messages = outbox / "messages"
    messages.mkdir(exist_ok=True)
    created = updated = unchanged = archived = 0
    for article in current:
        key = article.article_id
        if not key or not ID_PATTERN.fullmatch(key):
            raise OutboxError("invalid_article_id")
        preview_path = messages / f"{key}.md"
        content = render_preview(article)
        if key not in old:
            created += 1
        elif old[key] != article.to_dict() or not preview_path.exists() or preview_path.read_text(encoding="utf-8") != content:
            updated += 1
        else:
            unchanged += 1
        _atomic_write(preview_path, content)
    # A newly discovered DOI+PMID may connect two previously independent records.
    # Archive that individual generated preview; never delete user mail or data.
    for key in old.keys() - active_ids:
        path = messages / f"{key}.md"
        if path.exists():
            archive = outbox / "archive"
            archive.mkdir(exist_ok=True)
            digest = sha256(path.read_bytes()).hexdigest()[:12]
            os.replace(path, archive / f"{key}-{digest}.md")
            archived += 1
    payload = {"schema_version": SCHEMA_VERSION, "articles": [entry.to_dict() for entry in current]}
    _atomic_write(index_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return PreviewStats(len(current), created, updated, unchanged, archived)
