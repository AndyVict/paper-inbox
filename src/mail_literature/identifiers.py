from __future__ import annotations

from html import unescape
import re
import unicodedata
from urllib.parse import quote, unquote, urlsplit


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    text = unquote(unescape(value)).strip()
    # A URL's query/fragment and prose separators are never DOI suffix data.
    match = re.search(r"10\.\d{4,9}/[-A-Z0-9._;()/:\[\]{}+]+", text, re.I)
    if not match:
        return None
    doi = match[0].rstrip(".,;:")
    for opening, closing in [("(", ")"), ("[", "]"), ("{", "}")]:
        while doi.endswith(closing) and doi.count(closing) > doi.count(opening):
            doi = doi[:-1]
    doi = doi.rstrip(".,;:")
    if not doi.split("/", 1)[1]:
        return None
    return doi.casefold()


def normalize_pmid(value: str | None) -> str | None:
    if not value:
        return None
    text = unquote(unescape(value)).strip()
    match = re.fullmatch(r"\d{1,10}", text)
    if not match:
        match = re.search(r"\bPMID\s*[:：]?\s*(\d{1,10})\b", text, re.I)
        if match:
            text = match[1]
        else:
            try:
                url = urlsplit(text)
            except ValueError:
                return None
            host = (url.hostname or "").casefold()
            if host == "pubmed.ncbi.nlm.nih.gov":
                match = re.fullmatch(r"/(\d{1,10})/?", url.path)
            elif host in {"www.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov"}:
                match = re.fullmatch(r"/pubmed/(\d{1,10})/?", url.path)
            else:
                return None
            if not match:
                return None
            text = match[1]
    return str(int(text)) if int(text) > 0 else None


def title_key(title: str, year: int | None) -> str:
    title = unicodedata.normalize("NFKC", clean_text(title)).casefold()
    title = " ".join(re.findall(r"[^\W_]+", title, re.UNICODE))
    return f"title:{title}|{year if year is not None else 'unknown'}"


def public_article_url(value: str) -> str | None:
    """只保留明确的公共文献地址，丢弃追踪参数和不透明的跳转地址。"""
    try:
        url = urlsplit(unescape(value).strip())
        host = (url.hostname or "").casefold()
        if url.scheme not in {"http", "https"} or url.username or url.password:
            return None
        if host in {"doi.org", "dx.doi.org", "www.doi.org"}:
            doi = normalize_doi(url.path)
            return doi_url(doi) if doi else None
        pmid = normalize_pmid(value)
        if pmid:
            return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        if host in {"x-mol.com", "www.x-mol.com", "x-mol.net", "www.x-mol.net"}:
            match = re.fullmatch(r"/paper/(\d+)/?", url.path)
            if match:
                return f"https://www.x-mol.com/paper/{match[1]}"
    except ValueError:
        pass
    return None


def doi_url(doi: str) -> str:
    return "https://doi.org/" + quote(doi, safe="/():;._-+")


def article_link(doi: str | None, pmid: str | None, url: str | None) -> str | None:
    if doi:
        return doi_url(doi)
    if pmid:
        return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    return public_article_url(url) if url else None
