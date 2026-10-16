from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

from .identifiers import article_link, normalize_doi, normalize_pmid, title_key
from .models import Article


def identity_keys(article: Article) -> list[str]:
    keys = []
    if article.doi:
        keys.append("doi:" + article.doi)
    if article.pmid:
        keys.append("pmid:" + article.pmid)
    keys.append(title_key(article.title_en, article.year))
    return list(dict.fromkeys(keys + article.identity_keys))


def _matches(left: Article, right: Article) -> bool:
    left_keys, right_keys = set(identity_keys(left)), set(identity_keys(right))
    shared = left_keys & right_keys
    if any(key.startswith("doi:") for key in shared):
        return True
    # An explicit conflicting DOI must not be overridden by a weaker identity.
    if left.doi and right.doi and left.doi != right.doi:
        return False
    if any(key.startswith("pmid:") for key in shared):
        return True
    if left.pmid and right.pmid and left.pmid != right.pmid:
        return False
    return any(key.startswith("title:") for key in shared)


def _merge(target: Article, incoming: Article) -> None:
    keys = identity_keys(target) + identity_keys(incoming)
    refresh_primary = bool(target.sources and incoming.sources and target.sources[0] == incoming.sources[0])
    if target.pmid and incoming.pmid and target.pmid != incoming.pmid:
        target.parse_warnings.append("conflicting_pmid")
    for name in ["authors", "journal", "year", "published_at", "doi", "pmid", "article_url",
                 "impact_factor", "quartile_claimed", "quartile_source", "abstract_en"]:
        refresh_field = refresh_primary and name not in {"doi", "pmid", "article_url"}
        if (not getattr(target, name) or refresh_field) and getattr(incoming, name):
            setattr(target, name, deepcopy(getattr(incoming, name)))
    if refresh_primary:
        target.title_en = incoming.title_en
    target.clear_stale_translations()
    for name in ("title", "abstract"):
        if (not target.translation_current(name) and incoming.translation_current(name) and
                getattr(target, name + "_en") == getattr(incoming, name + "_en")):
            setattr(target, name + "_zh", getattr(incoming, name + "_zh"))
            setattr(target, name + "_zh_source_hash", getattr(incoming, name + "_zh_source_hash"))
    target.sources = list(dict.fromkeys(target.sources + incoming.sources))
    target.parse_warnings = list(dict.fromkeys(target.parse_warnings + incoming.parse_warnings))
    target.parse_warnings = [warning for warning in target.parse_warnings
                             if not warning.startswith("missing_") or not getattr(target, warning[8:], None)]
    target.identity_keys = list(dict.fromkeys(keys))
    target.article_url = article_link(target.doi, target.pmid, target.article_url)
    target.clear_stale_journal_info()
    if (not target.journal_metadata and incoming.journal_metadata_current() and
            (target.doi, target.pmid, target.journal) == (incoming.doi, incoming.pmid, incoming.journal)):
        target.journal_metadata = deepcopy(incoming.journal_metadata)
    if (not target.jcr_match and incoming.jcr_match_current() and
            target.journal_metadata == incoming.journal_metadata and
            (target.doi, target.pmid, target.journal) == (incoming.doi, incoming.pmid, incoming.journal)):
        target.jcr_match = deepcopy(incoming.jcr_match)


def deduplicate(articles: list[Article]) -> list[Article]:
    """DOI → PMID → 标准化标题+年份；保留来源及后续补全字段。"""
    unique: list[Article] = []
    for raw in articles:
        incoming = deepcopy(raw)
        incoming.clear_stale_translations()
        incoming.doi = normalize_doi(incoming.doi)
        incoming.pmid = normalize_pmid(incoming.pmid)
        incoming.clear_stale_journal_info()
        incoming.article_url = article_link(incoming.doi, incoming.pmid, incoming.article_url)
        incoming.identity_keys = identity_keys(incoming)
        matches = [entry for entry in unique if _matches(entry, incoming)]
        if matches:
            target = matches[0]
            _merge(target, incoming)
            for other in matches[1:]:
                if _matches(target, other):
                    _merge(target, other)
                    unique.remove(other)
        else:
            if not incoming.article_id:
                incoming.article_id = sha256(incoming.identity_keys[0].encode("utf-8")).hexdigest()[:24]
            unique.append(incoming)
    return unique
