from __future__ import annotations

from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from hashlib import sha256
import re

from .html_body import Node, body_tree, following_node, hidden, lines_from_tree
from .identifiers import article_link, clean_text, normalize_doi, normalize_pmid, public_article_url
from .models import Article, ParseResult, SourceRef

YEAR = r"(?:19|20)\d{2}"
NUMBERED_TITLE = re.compile(r"^\s*\d+\s*[.、)]\s*(.+)")
TITLE_LABEL = re.compile(r"^(?:Title|标题|英文标题)\s*[:：]\s*(.+)", re.I)


def _label(lines: list[str], names: str) -> str | None:
    pattern = re.compile(rf"^(?:{names})\s*[:：]\s*(.+)", re.I)
    for line in lines:
        match = pattern.match(line)
        if match:
            return clean_text(match[1])
    return None


def _authors(value: str | None) -> list[str]:
    if not value:
        return []
    return [clean_text(x) for x in re.split(r"[,;，；]", value) if clean_text(x)]


def _article(title: str, lines: list[str], ref: SourceRef, url: str | None = None) -> Article:
    text = "\n".join(lines)
    doi = normalize_doi(text)
    pmid = normalize_pmid(text)
    urls = [match[0] for match in re.finditer(r"https?://[^\s<>]+", text)]
    title_url = public_article_url(url) if url else None
    if title_url:
        doi = doi or normalize_doi(title_url)
        pmid = pmid or normalize_pmid(title_url)
    if not pmid:
        pmid = next((value for candidate in urls if (value := normalize_pmid(candidate))), None)
    if not url:
        url = next((candidate for candidate in urls if article_link(None, None, candidate)), None)
    date_match = re.search(rf"\b({YEAR})[-/](\d{{1,2}})[-/](\d{{1,2}})\b", text)
    year_label = _label(lines, "Year|年份")
    year = int(year_label) if year_label and re.fullmatch(YEAR, year_label) else None
    if date_match:
        year = year or int(date_match[1])
    if_match = re.search(r"(?:\bIF|Impact\s*Factor|影响因子)\s*[:：]?\s*(\d+(?:\.\d+)?)", text, re.I)
    quartile_match = re.search(r"(?:\bQ[1-4]\b|[1-4]\s*区)(?:\s*(?:Top|TOP|top))?", text)
    article = Article(
        title_en=clean_text(title), authors=_authors(_label(lines, "Authors?|作者")),
        journal=_label(lines, "Journal|期刊"), year=year,
        published_at=f"{date_match[1]}-{int(date_match[2]):02d}-{int(date_match[3]):02d}" if date_match else None,
        doi=doi, pmid=pmid, article_url=article_link(doi, pmid, url),
        impact_factor=if_match[1] if if_match else None,
        quartile_claimed=clean_text(quartile_match[0]) if quartile_match and ref.source == "Stork" else None,
        quartile_source="Stork（第三方标注，来源未核验）" if quartile_match and ref.source == "Stork" else None,
        abstract_en=_label(lines, "Abstract|英文摘要|摘要"), sources=[ref],
    )
    return article


def _finish(article: Article) -> Article:
    for field in ["doi", "pmid", "journal", "year", "authors", "article_url"]:
        if not getattr(article, field):
            article.parse_warnings.append("missing_" + field)
    return article


def _labelled(lines: list[str], ref: SourceRef) -> list[Article]:
    starts = [(i, match[1]) for i, line in enumerate(lines) if (match := TITLE_LABEL.match(line))]
    articles = []
    for position, (start, title) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        articles.append(_finish(_article(title, lines[start + 1:end], ref)))
    return articles


def parse_xmol(lines: list[str], ref: SourceRef) -> list[Article]:
    if any(TITLE_LABEL.match(line) for line in lines):
        return _labelled(lines, ref)
    starts = [(i, match[1]) for i, line in enumerate(lines) if (match := NUMBERED_TITLE.match(line))]
    articles = []
    for position, (start, title) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        record = lines[start + 1:end]
        # The real X-MOL layout is title / journal+IF+date+DOI+link / authors.
        metadata_index = next((i for i, line in enumerate(record)
                               if re.search(r"\b(?:DOI|IF)\b|\b\d{4}-\d{2}-\d{2}\b", line, re.I)), None)
        author_index = None
        if metadata_index is not None:
            author_index = metadata_index + 1
            if author_index < len(record) and re.search(r"https?://", record[author_index]):
                author_index += 1
            record = record[:author_index + 1]
        article = _article(title, record, ref)
        if metadata_index is not None:
            metadata = record[metadata_index]
            if not article.journal:
                journal = re.split(r"\s*\(\s*IF\b|\bDOI\s*[:：]|\b\d{4}[-/]\d{1,2}[-/]\d{1,2}", metadata, maxsplit=1, flags=re.I)[0]
                article.journal = clean_text(journal).rstrip(" (（") or None
            if not article.authors and author_index is not None and author_index < len(record):
                candidate = record[author_index]
                if not re.search(r"https?://|订阅|退订|欢迎|祝您", candidate):
                    article.authors = _authors(candidate)
        articles.append(_finish(article))
    return articles


def _stork_html(root: Node, ref: SourceRef) -> list[Article]:
    articles = []
    for node in root.nodes():
        if node.tag != "a" or not node.text():
            continue
        ancestor = node
        if_hidden = False
        while ancestor:
            if_hidden |= hidden(ancestor)
            ancestor = ancestor.parent
        if if_hidden:
            continue
        metadata = following_node(node)
        if not metadata or metadata.tag not in {"div", "p"}:
            continue
        metadata_nodes = [metadata]
        lines = lines_from_tree(metadata)
        sibling = following_node(metadata)
        # Some Stork templates put authors and journal/IDs in adjacent divs.
        for _ in range(2):
            if not sibling or sibling.tag not in {"div", "p"}:
                break
            sibling_lines = lines_from_tree(sibling)
            if not re.search(r"\b(?:PMID|IF)\s*[:：]?\s*\d+|10\.\d{4,9}/|[1-4]\s*区", "\n".join(sibling_lines), re.I):
                break
            metadata_nodes.append(sibling)
            lines.extend(sibling_lines)
            sibling = following_node(sibling)
        text = "\n".join(lines)
        if not re.search(rf"(?:[·•]\s*{YEAR}\b|\bPMID\s*[:：]?\s*\d+|\bIF\s*[:：]?\s*\d+)", text, re.I) and not public_article_url(node.attrs.get("href", "")):
            continue
        # A metadata block cannot contain another complete title+metadata block.
        if any(child.tag == "a" and following_node(child) and following_node(child).tag == "div"
               for child in metadata.nodes()):
            continue
        spans = [child for block in metadata_nodes for child in block.nodes() if child.tag == "span"]
        field_lines = lines + [span.text() for span in spans]
        article = _article(node.text(), field_lines, ref, node.attrs.get("href"))
        author_match = re.search(rf"^(.*?)\s*[·•]\s*({YEAR})\s*$", lines[0]) if lines else None
        if author_match:
            article.authors = _authors(author_match[1])
            article.year = int(author_match[2])
        # Real Stork metadata uses separate spans for journal, IF and claimed division.
        journal_node = next((span for span in spans if span.text() and
                             not re.search(r"\b(?:PMID|DOI)\b|\bIF\s*[:：]?\s*\d|[1-4]\s*区|^Q[1-4]", span.text(), re.I)), None)
        if not article.journal and journal_node:
            article.journal = journal_node.text()
        if not article.journal and len(lines) > 1:
            article.journal = clean_text(re.split(r"\bIF\s*[:：]?\s*\d|[1-4]\s*区|\bQ[1-4]\b", lines[1], maxsplit=1)[0]).rstrip(" (（") or None
        articles.append(_finish(article))
    return articles


def parse_stork_plain(lines: list[str], ref: SourceRef) -> list[Article]:
    if any(TITLE_LABEL.match(line) for line in lines):
        return _labelled(lines, ref)
    articles = []
    for i, line in enumerate(lines):
        match = re.match(rf"^(.+?)\s*\(({YEAR})\)\s+(.+)$", line)
        if not match:
            continue
        prefix, year, rest = match.groups()
        # Stork plain text concatenates title and surname+initials authors.
        explicit_authors = re.match(r"^(.+[.!?])\s+(?:by\s+|Authors?\s*[:：]\s*|作者\s*[:：]\s*)(.+)$", prefix, re.I)
        author_match = re.search(r"((?:[A-Z][A-Za-z'’\-]+\s+)+[A-Z]{1,6}(?:,\s*(?:(?:[A-Z][A-Za-z'’\-]+\s+)+[A-Z]{1,6}|[.…]+))*)$", prefix)
        if explicit_authors:
            title, authors = explicit_authors.groups()
        elif author_match:
            title = prefix[:author_match.start()].strip()
            authors = author_match[1]
        else:
            split = re.match(r"^(.+[.!?])\s+(.+)$", prefix)
            title, authors = split.groups() if split else (prefix, None)
        if not title:
            continue
        record = [rest]
        for following in lines[i + 1:i + 4]:
            if re.search(r"\bPMID\b|\bdoi\b|^https?://", following, re.I):
                record.append(following)
            else:
                break
        article = _article(title, record, ref)
        article.year = int(year)
        article.authors = _authors(authors)
        article.journal = clean_text(re.split(r"\s*\(\s*(?:Impact\s*Factor|影响因子)|\bIF\b|[1-4]\s*区|\bQ[1-4]\b", rest, maxsplit=1, flags=re.I)[0]) or None
        if not authors:
            article.parse_warnings.append("ambiguous_plain_title_authors")
        articles.append(_finish(article))
    return articles


def _source(mail, content: str) -> str | None:
    signals = " ".join(str(mail.get(header, "")) for header in ["From", "Subject"]).casefold()
    candidates = set()
    if re.search(r"x-mol|\bxmol\b", signals) or re.search(r"https?://(?:www\.)?x-mol\.(?:com|net)/paper/", content, re.I):
        candidates.add("X-MOL")
    if "stork" in signals or re.search(r"https?://(?:[\w-]+\.)?stork[a-z]*\.(?:com|me|cn)/", content, re.I):
        candidates.add("Stork")
    return next(iter(candidates)) if len(candidates) == 1 else None


def is_empty_literature_notice(raw: bytes, source: str) -> bool:
    """识别已观察到的 X-MOL 空结果通知；未知模板继续保留解析失败状态。"""
    if source != "X-MOL":
        return False
    try:
        mail = BytesParser(policy=policy.default).parsebytes(raw)
        if any(part.defects for part in mail.walk()):
            return False
        found = False
        for kind in ("html", "plain"):
            body = mail.get_body(preferencelist=(kind,))
            if body is None:
                continue
            content = body.get_content()
            if not isinstance(content, str) or "\ufffd" in content or _source(mail, content) != source:
                return False
            if kind == "html":
                tree = body_tree(content)
                lines = lines_from_tree(tree)
                urls = [node.attrs.get("href", "") for node in tree.nodes() if node.tag == "a"]
            else:
                lines = [clean_text(line) for line in content.splitlines() if clean_text(line)]
                urls = []
            text = " ".join(lines)
            urls.extend(match[0] for match in re.finditer(r"""https?://[^\s<>"']+""", content))
            if any(public_article_url(url) for url in urls):
                return False
            if (re.search(r"10\.\d{4,9}/|\b(?:DOI|PMID|IF)\b|影响因子|(?:Journal|期刊)\s*[:：]", text, re.I)
                or any(TITLE_LABEL.match(line) or NUMBERED_TITLE.match(line) for line in lines)):
                return False
            found |= bool(re.search(
                r"(?:暂无|没有|未发现|未找到|未检索到|没有找到|没有发现)[^。！？.!?]{0,80}(?:文献|论文|文章)",
                text,
            ))
        return found
    except (ValueError, TypeError, IndexError, RecursionError, LookupError, UnicodeError):
        return False


def parse_eml(raw: bytes, email_uid: str | None = None) -> ParseResult:
    mail_key = sha256(raw).hexdigest()
    result = ParseResult(None, mail_key)
    try:
        mail = BytesParser(policy=policy.default).parsebytes(raw)
        received_at = None
        if mail.get("Date"):
            try:
                received_at = parsedate_to_datetime(str(mail["Date"])).isoformat()
            except (TypeError, ValueError, OverflowError):
                result.warnings.append("invalid_mail_date")
        bodies = [mail.get_body(preferencelist=(kind,)) for kind in ["html", "plain"]]
        bodies = [body for body in bodies if body is not None]
        if any(part.defects for part in mail.walk()):
            result.warnings.append("mime_defect")
        for body in bodies:
            try:
                content = body.get_content()
            except (LookupError, UnicodeError):
                result.warnings.append("body_decode_failed")
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            if "\ufffd" in content:
                result.warnings.append("body_decode_replacement")
            source = _source(mail, content)
            result.source = source
            if not source:
                continue
            ref = SourceRef(source, mail_key, received_at, email_uid)
            body_type = body.get_content_type()
            if body_type == "text/html":
                tree = body_tree(content)
                articles = parse_xmol(lines_from_tree(tree), ref) if source == "X-MOL" else _stork_html(tree, ref)
                if source == "Stork" and not articles:
                    articles = parse_stork_plain(lines_from_tree(tree), ref)
            else:
                lines = [clean_text(line) for line in content.splitlines() if clean_text(line)]
                articles = parse_xmol(lines, ref) if source == "X-MOL" else parse_stork_plain(lines, ref)
            if articles:
                result.articles = articles
                result.body_type = body_type
                return result
        result.warnings.append("no_articles" if result.source else "unknown_or_ambiguous_source")
    except (ValueError, TypeError, IndexError, RecursionError):
        # Never surface parser exception values: they may embed private mail data.
        result.warnings.append("mail_parse_failed")
    return result
