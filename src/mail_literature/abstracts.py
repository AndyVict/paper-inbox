"""官方摘要接口：只查询公开标识符，不读取邮箱配置或输出响应。"""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.client import HTTPException
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

from .identifiers import normalize_doi, normalize_pmid

PROVIDERS = {"PubMed", "EuropePMC", "Crossref", "OpenAlex"}
HOSTS = {"eutils.ncbi.nlm.nih.gov", "www.ebi.ac.uk", "api.crossref.org", "api.openalex.org"}
MAX_RESPONSE = 4 * 1024 * 1024


class AbstractError(Exception):
    """仅携带固定错误码。"""


@dataclass(frozen=True)
class AbstractResult:
    provider: str
    status: str
    abstract: str | None = field(default=None, repr=False)
    doi: str | None = field(default=None, repr=False)
    pmid: str | None = field(default=None, repr=False)
    error_code: str | None = None


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.split(":")[-1]
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "sec", "title", "br", "div", "abstract"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.split(":")[-1]
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in {"p", "sec", "title", "div", "abstract"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def abstract_text(value, *, plain=False) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 100_000 or "\ufffd" in value:
        raise AbstractError("invalid_abstract_response")
    if not plain:
        parser = _Text()
        parser.feed(value)
        value = "".join(parser.parts)
    # XML itertext and HTMLParser already decoded their entities. A second
    # unescape/HTML parse would corrupt literal tags or mathematical symbols.
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text or None


def _doi(value):
    if value is not None and not isinstance(value, str):
        raise AbstractError("invalid_abstract_response")
    return normalize_doi(value)


def _pmid(value):
    if value is not None and not isinstance(value, str):
        raise AbstractError("invalid_abstract_response")
    return normalize_pmid(value)


def _result(provider, text, doi=None, pmid=None, *, plain=False):
    abstract = abstract_text(text, plain=plain)
    return AbstractResult(provider, "found" if abstract else "no_abstract", abstract, doi, pmid)


def parse_response(provider: str, kind: str, identifier: str, raw: bytes) -> AbstractResult:
    """返回结果须精确匹配请求标识符；畸形响应不能当作“无摘要”。"""
    try:
        if provider == "PubMed":
            if b"<!ENTITY" in raw.upper():
                raise AbstractError("invalid_abstract_response")
            root = ET.fromstring(raw)
            if root.tag != "PubmedArticleSet" or root.find(".//ERROR") is not None:
                raise AbstractError("invalid_abstract_response")
            records = root.findall("PubmedArticle")
            for record in records:
                pmid = _pmid(record.findtext("MedlineCitation/PMID"))
                if pmid != identifier:
                    continue
                doi = next((_doi(node.text) for node in record.findall("PubmedData/ArticleIdList/ArticleId")
                            if node.get("IdType") == "doi"), None)
                sections = []
                for node in record.findall("MedlineCitation/Article/Abstract/AbstractText"):
                    text = re.sub(r"\s+", " ", "".join(node.itertext())).strip()
                    label = node.get("Label")
                    if text:
                        sections.append(f"{label}: {text}" if label else text)
                return _result(provider, "\n".join(sections), doi, pmid, plain=True)
            if records:
                raise AbstractError("abstract_identity_mismatch")
            return AbstractResult(provider, "not_found")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise AbstractError("invalid_abstract_response")
        if provider == "EuropePMC":
            records = value["resultList"]["result"]
            if not isinstance(records, list) or type(value["hitCount"]) is not int:
                raise AbstractError("invalid_abstract_response")
            if not records and value["hitCount"] > 0:
                raise AbstractError("invalid_abstract_response")
            matches = []
            for record in records:
                doi = _doi(record.get("doi"))
                pmid = _pmid(record.get("id")) if record.get("source") == "MED" else None
                if (pmid if kind == "pmid" else doi) == identifier:
                    matches.append(_result(provider, record.get("abstractText"), doi, pmid))
            if matches:
                return next((item for item in matches if item.abstract), matches[0])
            if records:
                raise AbstractError("abstract_identity_mismatch")
            return AbstractResult(provider, "not_found")
        if provider == "Crossref":
            if value.get("status") != "ok" or value.get("message-type") != "work":
                raise AbstractError("invalid_abstract_response")
            record = value["message"]
            doi = _doi(record.get("DOI"))
            if doi != identifier:
                raise AbstractError("abstract_identity_mismatch")
            return _result(provider, record.get("abstract"), doi)
        if provider == "OpenAlex":
            doi = _doi(value.get("doi"))
            if doi != identifier:
                raise AbstractError("abstract_identity_mismatch")
            index = value.get("abstract_inverted_index")
            if index is None:
                return AbstractResult(provider, "no_abstract", doi=doi)
            if not isinstance(index, dict):
                raise AbstractError("invalid_abstract_response")
            words = {}
            for word, positions in index.items():
                if not isinstance(word, str) or not word or not isinstance(positions, list) or not positions:
                    raise AbstractError("invalid_abstract_response")
                for pos in positions:
                    if type(pos) is not int or not 0 <= pos < 20_000 or pos in words:
                        raise AbstractError("invalid_abstract_response")
                    words[pos] = word
            if set(words) != set(range(len(words))):
                raise AbstractError("invalid_abstract_response")
            ids = value.get("ids") or {}
            pmid = _pmid(ids.get("pmid"))
            return _result(provider, " ".join(words[pos] for pos in range(len(words))), doi, pmid, plain=True)
        raise AbstractError("invalid_abstract_provider")
    except (ET.ParseError, ValueError, UnicodeError, KeyError, TypeError, AttributeError, RecursionError):
        raise AbstractError("invalid_abstract_response") from None


def query_url(provider: str, kind: str, identifier: str) -> str:
    if provider not in PROVIDERS or kind not in {"pmid", "doi"}:
        raise AbstractError("invalid_abstract_query")
    normalized = normalize_pmid(identifier) if kind == "pmid" else normalize_doi(identifier)
    if normalized != identifier:
        raise AbstractError("invalid_abstract_query")
    if provider == "PubMed" and kind == "pmid":
        return "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urlencode(
            {"db": "pubmed", "id": identifier, "retmode": "xml", "tool": "mail-literature"})
    if provider == "EuropePMC":
        query = f"EXT_ID:{identifier} AND SRC:MED" if kind == "pmid" else f'DOI:"{identifier}"'
        return "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urlencode(
            {"query": query, "format": "json", "resultType": "core", "pageSize": 10})
    if kind == "doi" and provider in {"Crossref", "OpenAlex"}:
        prefix = "https://api.crossref.org/works/" if provider == "Crossref" else "https://api.openalex.org/works/https://doi.org/"
        return prefix + quote(identifier, safe="")
    raise AbstractError("invalid_abstract_query")


class _OfficialRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https" or urlsplit(newurl).hostname not in HOSTS:
            raise AbstractError("abstract_redirect_rejected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class AbstractClient:
    def __init__(self, *, opener=None, sleep=time.sleep, clock=time.monotonic, timeout=15,
                 response_parser=parse_response):
        self.opener = opener or build_opener(_OfficialRedirects()).open
        self.sleep, self.clock, self.timeout = sleep, clock, timeout
        self.last = {}
        self.response_parser = response_parser
        self.failures = {}
        self.blocked = {}
        self.requests = 0

    def query(self, provider: str, kind: str, identifier: str) -> AbstractResult:
        url = query_url(provider, kind, identifier)
        if provider in self.blocked:
            return AbstractResult(provider, "failed", error_code=self.blocked[provider])
        host = urlsplit(url).hostname
        for attempt in range(2):
            delay = self.last.get(host, -1e9) + 1.0 - self.clock()
            if delay > 0:
                self.sleep(delay)
            self.last[host] = self.clock()
            self.requests += 1
            try:
                request = Request(url, headers={"User-Agent": "mail-literature/0.1 (public abstract lookup)",
                                                "Accept": "application/json, application/xml"})
                with self.opener(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise AbstractError("abstract_response_too_large")
                result = self.response_parser(provider, kind, identifier, raw)
                self.failures[provider] = 0
                return result
            except HTTPError as exc:
                status = exc.code
                retry = exc.headers.get("Retry-After", "") if exc.headers else ""
                exc.close()
                if status == 404:
                    return AbstractResult(provider, "not_found")
                code = "abstract_rate_limited" if status == 429 else (
                    "abstract_access_denied" if status in {401, 403} else "abstract_http_failed")
                if status in {401, 403, 429}:
                    # Do not repeatedly spend a provider's quota in the same run.
                    self.blocked[provider] = code
                    return AbstractResult(provider, "failed", error_code=code)
                transient = status >= 500
                wait = max(1.0, float(retry)) if re.fullmatch(r"\d+(?:\.\d+)?", retry) else 2.0
                if retry and not re.fullmatch(r"\d+(?:\.\d+)?", retry):
                    transient = False  # Defer instead of retrying before an HTTP-date.
                if wait > 30:
                    transient = False
            except (URLError, TimeoutError, OSError, HTTPException):
                code, transient, wait = "abstract_network_failed", True, 2.0
            except AbstractError as exc:
                return AbstractResult(provider, "failed", error_code=str(exc))
            if transient and attempt == 0:
                self.sleep(wait)
                continue
            self.failures[provider] = self.failures.get(provider, 0) + 1
            if self.failures[provider] >= 3:
                self.blocked[provider] = code
            return AbstractResult(provider, "failed", error_code=code)
        raise AbstractError("abstract_network_failed")
