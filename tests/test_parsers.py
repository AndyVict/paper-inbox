from email import policy
from email.message import EmailMessage
import unittest

from tests.support import email_bytes, fixture
from mail_literature.identifiers import title_key
from mail_literature.parsers import is_empty_literature_notice, parse_eml


class ParserTests(unittest.TestCase):
    def test_explicit_xmol_empty_notice_html_and_plain(self):
        for body, kind in [("<p>本期暂时没有匹配的文献。</p>", "html"),
                           ("本期未找到匹配的文章。", "plain")]:
            with self.subTest(kind=kind):
                self.assertTrue(is_empty_literature_notice(email_bytes(body, subtype=kind), "X-MOL"))

    def test_empty_notice_rejects_literature_markers_and_title_only_records(self):
        for body in [
            "暂时没有匹配的文献。\nDOI: 10.5555/synthetic",
            "暂时没有匹配的文献。\nPMID: 1234567",
            "暂时没有匹配的文献。\nTitle: Synthetic title without identifiers",
            "暂时没有匹配的文献。\n1. Synthetic title without identifiers",
            "暂时没有匹配的文献。\nJournal: Example Journal",
            "暂时没有匹配的文献。\nhttps://www.x-mol.com/paper/123456",
            "暂时没有匹配的文献。\nhttps://pubmed.ncbi.nlm.nih.gov/1234567/",
        ]:
            with self.subTest(body=body):
                self.assertFalse(is_empty_literature_notice(email_bytes(body, subtype="plain"), "X-MOL"))

    def test_empty_notice_rejects_article_anchor_and_conflicting_alternative(self):
        raw = email_bytes('<p>没有匹配的文献。</p><a href="https://doi.org/10.5555/synthetic">Synthetic title</a>')
        self.assertFalse(is_empty_literature_notice(raw, "X-MOL"))
        mail = EmailMessage(policy=policy.default)
        mail["From"] = "X-MOL <notices@example.invalid>"
        mail["Subject"] = "X-MOL synthetic"
        mail.set_content("Title: Synthetic title without identifiers")
        mail.add_alternative("<p>没有匹配的文献。</p>", subtype="html")
        self.assertFalse(is_empty_literature_notice(mail.as_bytes(), "X-MOL"))

    def test_empty_notice_keeps_unknown_source_template_and_bad_charset_retryable(self):
        self.assertFalse(is_empty_literature_notice(email_bytes("暂无匹配文献。", "Stork", "plain"), "Stork"))
        self.assertFalse(is_empty_literature_notice(email_bytes("No synthetic literature records", subtype="plain"), "X-MOL"))
        self.assertFalse(is_empty_literature_notice(email_bytes("暂无匹配文献。", "Unknown", "plain"), "X-MOL"))
        raw = email_bytes("暂无匹配文献。", subtype="plain").replace(b'charset="utf-8"', b'charset="synthetic-unknown-codec"')
        self.assertFalse(is_empty_literature_notice(raw, "X-MOL"))

    def test_xmol_html_splits_and_keeps_missing_identifiers(self):
        result = parse_eml(email_bytes(fixture("xmol.html")), email_uid="42")
        self.assertEqual(result.source, "X-MOL")
        self.assertEqual(len(result.articles), 3)
        first, second, third = result.articles
        self.assertEqual(first.title_en, "Synthetic A & B: Highlighted title")
        self.assertEqual(first.journal, "Example Journal")
        self.assertEqual(first.authors, ["Example A", "Sample B"])
        self.assertEqual(first.doi, "10.5555/offline.alpha")
        self.assertEqual(first.published_at, "2026-10-08")
        self.assertEqual(first.impact_factor, "2.5")
        self.assertEqual(first.sources[0].email_uid, "42")
        self.assertEqual(first.sources[0].received_at, "2026-10-09T08:00:00+08:00")
        self.assertEqual(second.article_url, "https://www.x-mol.com/paper/23456")
        self.assertIn("missing_doi", second.parse_warnings)
        self.assertEqual(third.authors, [])
        self.assertIn("missing_year", third.parse_warnings)
        self.assertNotIn("SYNTHETIC_PRIVATE_MARKER", str([a.to_dict() for a in result.articles]))

    def test_stork_adjacent_blocks_inline_spans_and_hidden_footer(self):
        result = parse_eml(email_bytes(fixture("stork.html"), "Stork"))
        self.assertEqual(len(result.articles), 3)
        first, second, third = result.articles
        self.assertEqual(first.journal, "Example Journal")
        self.assertEqual(first.impact_factor, "2.5")
        self.assertEqual(first.pmid, "1234567")
        self.assertEqual(first.doi, "10.5555/offline.alpha")
        self.assertEqual(first.quartile_claimed, "1区 Top")
        self.assertIn("第三方", first.quartile_source)
        self.assertEqual(second.doi, "10.5555/offline.beta")
        self.assertEqual(second.quartile_claimed, "Q3")
        self.assertIsNone(third.doi)
        self.assertIsNone(third.pmid)
        self.assertIn("missing_article_url", third.parse_warnings)
        self.assertNotIn("SYNTHETIC_PRIVATE_MARKER", str([a.to_dict() for a in result.articles]))

    def test_stork_plain_agrees_with_html(self):
        html = parse_eml(email_bytes(fixture("stork.html"), "Stork")).articles
        plain = parse_eml(email_bytes(fixture("stork.txt"), "Stork", "plain")).articles
        self.assertEqual(len(plain), 3)
        for a, b in zip(html, plain):
            self.assertEqual(title_key(a.title_en, a.year), title_key(b.title_en, b.year))
            for field in ["authors", "journal", "doi", "pmid", "year", "impact_factor"]:
                self.assertEqual(getattr(a, field), getattr(b, field))

    def test_plain_author_marker_does_not_split_by_within_title(self):
        content = "A synthetic effect by controlled input. by Sample A (2026) Example Journal (Impact Factor: 2.0) 2 区\nPMID: 1234567 doi: 10.5555/by.test"
        article = parse_eml(email_bytes(content, "Stork", "plain")).articles[0]
        self.assertEqual(article.title_en, "A synthetic effect by controlled input.")
        self.assertEqual(article.authors, ["Sample A"])

    def test_labelled_plain_missing_metadata_and_explicit_abstract(self):
        for source in ["Stork", "X-MOL"]:
            with self.subTest(source=source):
                content = "Title: Minimal synthetic title\nTitle: Synthetic abstract example\nJournal: Example Journal\nAbstract: This is a synthetic abstract, not a research claim."
                result = parse_eml(email_bytes(content, source, "plain"))
                self.assertEqual(len(result.articles), 2)
                self.assertIsNone(result.articles[0].abstract_en)
                self.assertIn("missing_doi", result.articles[0].parse_warnings)
                self.assertIsNotNone(result.articles[1].abstract_en)

    def test_xmol_numbered_plain(self):
        content = "1. Synthetic title\nExample Journal (IF 2.5)2026-10-08 DOI:10.5555/plain.alpha.\nExample A, Sample B\n2. Another title\nOther Journal (IF 1.2)2025-12-31\nSample C\n退订设置"
        result = parse_eml(email_bytes(content, subtype="plain"))
        self.assertEqual(len(result.articles), 2)
        self.assertEqual(result.articles[0].doi, "10.5555/plain.alpha")
        self.assertEqual(result.articles[1].authors, ["Sample C"])

    def test_multipart_prefers_html_ignores_plain_duplicate_and_attachment(self):
        mail = EmailMessage(policy=policy.default)
        mail["From"] = "Stork <notices@example.invalid>"
        mail["Subject"] = "Stork synthetic"
        mail.set_content(fixture("stork.txt"))
        mail.add_alternative(fixture("stork.html"), subtype="html")
        mail.add_attachment(b"Title: Attached fake literature", maintype="text", subtype="plain", filename="synthetic.txt")
        result = parse_eml(mail.as_bytes())
        self.assertEqual(result.body_type, "text/html")
        self.assertEqual(len(result.articles), 3)

    def test_broken_html_charset_falls_back_to_plain(self):
        mail = EmailMessage(policy=policy.default)
        mail["Subject"] = "Stork synthetic"
        mail.set_content(fixture("stork.txt"))
        mail.add_alternative("<p>synthetic</p>", subtype="html")
        mail.get_payload()[1].set_param("charset", "nonexistent-synthetic-codec")
        result = parse_eml(mail.as_bytes())
        self.assertEqual(len(result.articles), 3)
        self.assertEqual(result.body_type, "text/plain")
        self.assertIn("body_decode_failed", result.warnings)

    def test_gb18030_base64_and_encoded_headers(self):
        content = "标题：Synthetic 编码 &amp; entity\n期刊：Example Journal\nDOI：10.5555/encoding.test。"
        result = parse_eml(email_bytes(content, subtype="plain", charset="gb18030", cte="base64"))
        self.assertEqual(result.articles[0].title_en, "Synthetic 编码 & entity")
        self.assertEqual(result.articles[0].doi, "10.5555/encoding.test")

    def test_unknown_source_is_a_safe_failure(self):
        result = parse_eml(email_bytes("Title: Synthetic title", "Unknown", "plain"))
        self.assertEqual(result.articles, [])
        self.assertIn("unknown_or_ambiguous_source", result.warnings)

    def test_public_pmid_link_without_label(self):
        result = parse_eml(email_bytes("Title: Synthetic title\nLink: https://pubmed.ncbi.nlm.nih.gov/1234567/?track=synthetic", subtype="plain"))
        self.assertEqual(result.articles[0].pmid, "1234567")
        self.assertEqual(result.articles[0].article_url, "https://pubmed.ncbi.nlm.nih.gov/1234567/")

    def test_stork_title_link_can_supply_identifier_without_other_metadata(self):
        html = '<a href="https://doi.org/10.5555/link.only?track=synthetic">Synthetic title</a><div>Journal: Example Journal</div>'
        result = parse_eml(email_bytes(html, "Stork"))
        self.assertEqual(len(result.articles), 1)
        self.assertEqual(result.articles[0].doi, "10.5555/link.only")
        self.assertEqual(result.articles[0].journal, "Example Journal")
        self.assertIn("missing_year", result.articles[0].parse_warnings)

    def test_invalid_date_keeps_literature(self):
        raw = email_bytes("Title: Synthetic title", subtype="plain").replace(b"Fri, 09 Oct 2026 08:00:00 +0800", b"invalid synthetic date")
        result = parse_eml(raw)
        self.assertEqual(len(result.articles), 1)
        self.assertIn("invalid_mail_date", result.warnings)


if __name__ == "__main__":
    unittest.main()
