"""只读 IMAP over TLS；服务端响应和凭据绝不进入异常信息。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesHeaderParser
from email.utils import parseaddr
from hashlib import sha256
import ipaddress
import imaplib
from pathlib import Path
import re
import ssl

MAX_UID = 4294967295
HEADER_QUERY = "(UID INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
BODY_QUERY = "(UID INTERNALDATE RFC822.SIZE BODY.PEEK[])"


class ImapError(Exception):
    """固定安全错误码；禁止透传包含邮箱地址/服务端响应的原始异常。"""


def _hostname(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    try:
        host = value.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        raise ImapError("invalid_imap_endpoint") from None
    if len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                              for label in host.split(".")):
        raise ImapError("invalid_imap_endpoint")
    return host


@dataclass(frozen=True)
class ImapConfig:
    user: str = field(repr=False)
    password: str = field(repr=False)
    host: str = "imap.163.com"
    port: int = 993
    mailbox: str = "INBOX"
    timeout: int = 30
    max_message_bytes: int = 10 * 1024 * 1024

    @property
    def namespace(self) -> str:
        # Separate accounts/folders without persisting a private address.
        return sha256("\0".join([self.host.lower(), str(self.port), self.user.lower(), self.mailbox])
                      .encode("utf-8")).hexdigest()


def load_config(path: Path) -> ImapConfig:
    """只读 UTF-8 本地配置；不插值，不执行代码，不读取或显示其他服务密钥。"""
    try:
        values = {}
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ImapError("invalid_imap_config")
            key, value = line.split("=", 1)
            if not key.strip().startswith("IMAP_"):
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key.strip()] = value
        user = values.get("IMAP_USER", "")
        password = values.get("IMAP_APP_PASSWORD", "")
        if any(not value or value.startswith(("your_", "replace_with_")) or
               any(char in value for char in "\r\n\0") for value in (user, password)):
            raise ImapError("missing_imap_credentials")
        host = _hostname(values.get("IMAP_HOST", "imap.163.com"))
        port = int(values.get("IMAP_PORT", "993"))
        mailbox = values.get("IMAP_MAILBOX", "INBOX")
        timeout = int(values.get("IMAP_TIMEOUT", "30"))
        limit = int(values.get("IMAP_MAX_MESSAGE_BYTES", str(10 * 1024 * 1024)))
        if not 1 <= port <= 65535:
            raise ImapError("invalid_imap_endpoint")
        # Unicode folder names must be supplied in the server's modified UTF-7
        # form. Quoting below protects spaces, quotes and IMAP special chars.
        if not mailbox or len(mailbox) > 512 or any(not 32 <= ord(char) < 127 for char in mailbox):
            raise ImapError("invalid_imap_mailbox")
        if not 1 <= timeout <= 60 or not 1024 <= limit <= 50 * 1024 * 1024:
            raise ImapError("invalid_imap_limits")
        return ImapConfig(user, password, host, port,
                          "INBOX" if mailbox.upper() == "INBOX" else mailbox, timeout, limit)
    except (OSError, UnicodeError):
        raise ImapError("imap_config_read_failed") from None
    except ValueError:
        raise ImapError("invalid_imap_config") from None


def header_source(raw: bytes) -> str | None:
    """综合显示名、地址域和主题；只匹配明确的订阅信号，不持久化私有头。"""
    try:
        mail = BytesHeaderParser(policy=policy.default).parsebytes(raw)
        sender = str(mail.get("From", "")).casefold()
        name, address = parseaddr(sender)
        domain = address.rpartition("@")[2]
        subject = str(mail.get("Subject", "")).casefold()
        sources = set()
        for source, token, domains in [
            ("X-MOL", r"x-mol|\bxmol\b", ("x-mol.com", "x-mol.net")),
            ("Stork", r"stork", ("storkapp.me", "storkapp.com", "storkapp.cn", "stork.com")),
        ]:
            sender_match = bool(re.search(token, name)) or any(
                domain == known or domain.endswith("." + known) for known in domains)
            subject_match = bool(re.search(token, subject)) or bool(re.search(
                r"文献|推荐|订阅|研究|paper|literature|alert|recommend", subject))
            if sender_match and subject_match:
                sources.add(source)
        # A contradictory source in the subject must not be treated as a match.
        if len(sources) == 1:
            source = next(iter(sources))
            other = r"stork" if source == "X-MOL" else r"x-mol|\bxmol\b"
            return None if re.search(other, subject) else source
    except (ValueError, TypeError, IndexError, RecursionError):
        pass
    return None


def _internaldate(metadata: bytes) -> str:
    match = re.search(rb'INTERNALDATE "([ 0-9]\d)-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})"', metadata, re.I)
    if not match:
        raise ImapError("invalid_internaldate")
    try:
        day, month, year, hour, minute, second, sign, zone_hour, zone_minute = match.groups()
        months = {name: number for number, name in enumerate(
            [b"jan", b"feb", b"mar", b"apr", b"may", b"jun", b"jul", b"aug", b"sep", b"oct", b"nov", b"dec"], 1)}
        offset = (int(zone_hour) * 60 + int(zone_minute)) * (-1 if sign == b"-" else 1)
        if int(zone_minute) > 59 or abs(offset) >= 1440:
            raise ValueError
        return datetime(int(year), months[month.lower()], int(day), int(hour), int(minute), int(second),
                        tzinfo=timezone(timedelta(minutes=offset))).isoformat()
    except (ValueError, KeyError):
        raise ImapError("invalid_internaldate") from None


@dataclass(frozen=True)
class FetchedMail:
    raw: bytes = field(repr=False)
    internaldate: str
    size: int


class ReadOnlyImap:
    def __init__(self, config: ImapConfig, *, factory=None):
        self.config = config
        self.factory = factory or imaplib.IMAP4_SSL
        self.client = None
        self.uidvalidity: int | None = None

    def _call(self, method, *args, code="imap_command_failed", **kwargs):
        try:
            status, data = method(*args, **kwargs)
            if status != "OK":
                raise ImapError(code)
            return data
        except (imaplib.IMAP4.abort, OSError, EOFError):
            raise ImapError("imap_connection_failed") from None
        except imaplib.IMAP4.error:
            raise ImapError(code) from None

    def __enter__(self):
        try:
            self.client = self.factory(self.config.host, self.config.port,
                                       ssl_context=ssl.create_default_context(), timeout=self.config.timeout)
            self.client.debug = 0
            self._call(self.client.login, self.config.user, self.config.password, code="imap_login_failed")
            capabilities = self._call(self.client.capability)
            if b"ID" in b" ".join(capabilities).upper().split():
                # Public imaplib extension entry point; ID carries only product
                # information, no user/address/password/OS/environment details.
                self._call(self.client.xatom, "ID",
                           '("name" "mail-literature" "version" "0.1.0" "vendor" "local-project")',
                           code="imap_id_failed")
            mailbox = self.config.mailbox
            if mailbox != "INBOX":
                mailbox = '"' + mailbox.replace('\\', '\\\\').replace('"', '\\"') + '"'
            self._call(self.client.select, mailbox, readonly=True, code="imap_select_failed")
            _, values = self.client.response("UIDVALIDITY")
            if not values or len(values) != 1 or not isinstance(values[0], bytes) or not values[0].isdigit():
                raise ImapError("invalid_uidvalidity")
            self.uidvalidity = int(values[0])
            if not 1 <= self.uidvalidity <= MAX_UID:
                raise ImapError("invalid_uidvalidity")
            return self
        except ImapError:
            self._logout()
            raise
        except (imaplib.IMAP4.error, OSError, EOFError, ValueError, TypeError):
            self._logout()
            raise ImapError("imap_connection_failed") from None

    def _logout(self):
        if self.client is not None:
            try:
                self.client.logout()
            except (imaplib.IMAP4.error, OSError, EOFError):
                pass
            self.client = None

    def __exit__(self, *_):
        # Never CLOSE/EXPUNGE/STORE: LOGOUT ends the read-only session.
        self._logout()

    def uids_after(self, cursor: int) -> list[int]:
        if cursor >= MAX_UID:
            return []
        data = self._call(self.client.uid, "SEARCH", None, "UID", f"{cursor + 1}:*")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], bytes):
            raise ImapError("invalid_uid_search")
        tokens = data[0].split()
        if any(not token.isdigit() or not 1 <= int(token) <= MAX_UID for token in tokens):
            raise ImapError("invalid_uid_search")
        # RFC sequence-set ranges reverse when start exceeds '*'; the server may
        # return the last existing UID even when no new mail exists.
        return sorted({int(token) for token in tokens if int(token) > cursor})

    def _fetch(self, uid: int, query: str, *, body: bool) -> FetchedMail | None:
        data = self._call(self.client.uid, "FETCH", str(uid), query, code="imap_fetch_failed")
        if not isinstance(data, list):
            raise ImapError("invalid_fetch_response")
        tuples = [item for item in data if isinstance(item, tuple)]
        if not tuples:
            return None  # UID may have been expunged since SEARCH.
        matching = []
        for item in tuples:
            if len(item) != 2 or not isinstance(item[0], bytes) or not isinstance(item[1], bytes):
                raise ImapError("invalid_fetch_response")
            match = re.search(rb"\bUID (\d+)\b", item[0])
            if match and int(match[1]) == uid:
                matching.append(item)
        if len(matching) != 1:
            raise ImapError("fetch_uid_mismatch")
        metadata, raw = matching[0]
        expected = rb"BODY\[\]" if body else rb"BODY\[HEADER\.FIELDS"
        if not re.search(expected, metadata, re.I):
            raise ImapError("invalid_fetch_section")
        size = re.search(rb"\bRFC822\.SIZE (\d+)\b", metadata, re.I)
        if not size:
            raise ImapError("missing_message_size")
        return FetchedMail(raw, _internaldate(metadata), int(size[1]))

    def headers(self, uid: int) -> FetchedMail | None:
        return self._fetch(uid, HEADER_QUERY, body=False)

    def message(self, uid: int) -> FetchedMail | None:
        fetched = self._fetch(uid, BODY_QUERY, body=True)
        if fetched and (fetched.size > self.config.max_message_bytes or len(fetched.raw) > self.config.max_message_bytes):
            raise ImapError("message_too_large")
        if fetched and fetched.size != len(fetched.raw):
            raise ImapError("message_size_mismatch")
        return fetched
