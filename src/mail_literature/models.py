from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any


@dataclass(frozen=True)
class SourceRef:
    source: str
    mail_key: str
    received_at: str | None = None
    email_uid: str | None = None


@dataclass
class Article:
    title_en: str
    authors: list[str] = field(default_factory=list)
    journal: str | None = None
    year: int | None = None
    published_at: str | None = None
    doi: str | None = None
    pmid: str | None = None
    article_url: str | None = None
    impact_factor: str | None = None
    quartile_claimed: str | None = None
    quartile_source: str | None = None
    abstract_en: str | None = None
    sources: list[SourceRef] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)
    article_id: str | None = None
    identity_keys: list[str] = field(default_factory=list)
    title_zh: str | None = None
    abstract_zh: str | None = None
    title_zh_source_hash: str | None = None
    abstract_zh_source_hash: str | None = None
    journal_metadata: dict | None = None
    jcr_match: dict | None = None

    def journal_metadata_current(self) -> bool:
        from .journal_fields import journal_identity_hash
        return bool(isinstance(self.journal_metadata, dict) and self.journal_metadata and
                    self.journal_metadata.get('identity_hash') == journal_identity_hash(self))

    def jcr_match_current(self) -> bool:
        from .journal_fields import journal_identity_hash, metadata_hash
        return bool(isinstance(self.jcr_match, dict) and self.jcr_match and
                    (self.journal_metadata is None or self.journal_metadata_current()) and
                    self.jcr_match.get('identity_hash') == journal_identity_hash(self) and
                    self.jcr_match.get('metadata_hash') == metadata_hash(self.journal_metadata))

    def clear_stale_journal_info(self) -> None:
        if not self.journal_metadata_current():
            self.journal_metadata = None
        if not self.jcr_match_current():
            self.jcr_match = None

    def translation_current(self, name: str) -> bool:
        source = getattr(self, name + "_en")
        return bool(source and getattr(self, name + "_zh") and
                    getattr(self, name + "_zh_source_hash") == sha256(source.encode("utf-8")).hexdigest())

    def clear_stale_translations(self) -> None:
        for name in ("title", "abstract"):
            if not self.translation_current(name):
                setattr(self, name + "_zh", None)
                setattr(self, name + "_zh_source_hash", None)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        # Preserve the exact legacy JSON shape until translations exist.
        for name in ("title_zh", "abstract_zh", "title_zh_source_hash", "abstract_zh_source_hash",
                     "journal_metadata", "jcr_match"):
            if value[name] is None:
                value.pop(name)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Article:
        fields = dict(value)
        fields["sources"] = [SourceRef(**ref) for ref in fields.get("sources", [])]
        return cls(**fields)


@dataclass
class ParseResult:
    source: str | None
    mail_key: str
    articles: list[Article] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    body_type: str | None = None
