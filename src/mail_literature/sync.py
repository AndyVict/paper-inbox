"""按 UID 增量同步；文献、邮件结果和游标在同一事务中提交。"""
from dataclasses import dataclass, replace
from hashlib import sha256

from .imap_reader import ImapError, ReadOnlyImap, header_source
from .parsers import is_empty_literature_notice, parse_eml
from .state import StateStore


@dataclass
class SyncStats:
    examined: int = 0
    parsed: int = 0
    ignored: int = 0
    failed: int = 0
    vanished: int = 0
    candidates: int = 0
    remaining: int = 0


def sync_mail(store: StateStore, reader: ReadOnlyImap, *, max_mails: int = 100) -> SyncStats:
    namespace, validity = reader.config.namespace, reader.uidvalidity
    cursor = store.ensure_epoch(namespace, validity)
    # Persisted failed UIDs are retried independently of the scan cursor.
    retries = store.retry_uids(namespace, validity)
    new = reader.uids_after(cursor)
    # Reserve space for new mail while retrying the least-attempted failures;
    # one permanently broken subscription cannot starve all later messages.
    new_budget = min(len(new), max(1, (max_mails + 1) // 2)) if retries else min(len(new), max_mails)
    selected = new[:new_budget] + retries[:max_mails - new_budget]
    if len(selected) < max_mails:
        selected += new[new_budget:new_budget + max_mails - len(selected)]
    stats = SyncStats(remaining=len(set(retries + new)) - len(selected))
    for uid in sorted(selected):
        status, code, articles, mail_key, internaldate = "ignored", None, None, None, None
        fatal = None
        try:
            headers = reader.headers(uid)
            if headers is None:
                status = "vanished"
            else:
                internaldate = headers.internaldate
                source = header_source(headers.raw)
                if source:
                    if headers.size > reader.config.max_message_bytes:
                        raise ImapError("message_too_large")
                    message = reader.message(uid)
                    if message is None:
                        status = "vanished"
                    else:
                        mail_key = sha256(message.raw).hexdigest()
                        internaldate = message.internaldate
                        result = parse_eml(message.raw, email_uid=str(uid))
                        if not result.articles and result.source == source and is_empty_literature_notice(message.raw, source):
                            status, code = "ignored", "empty_literature_notice"
                        elif not result.articles or result.source != source:
                            status, code = "failed", "subscription_parse_failed"
                        else:
                            articles = result.articles
                            for article in articles:
                                article.sources = [replace(ref, received_at=internaldate) for ref in article.sources]
                            status = "parsed"
                            stats.candidates += len(articles)
        except ImapError as exc:
            status, code = "failed", str(exc)
            if code == "imap_connection_failed":
                fatal = exc
        store.record_mail(namespace, validity, uid, status, articles=articles,
                          error_code=code, mail_key=mail_key, internaldate=internaldate)
        stats.examined += 1
        setattr(stats, status, getattr(stats, status) + 1)
        if fatal:
            raise fatal
    return stats
