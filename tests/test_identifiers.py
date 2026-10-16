import unittest

from tests import support
from mail_literature.identifiers import normalize_doi, normalize_pmid, public_article_url, title_key


class IdentifierTests(unittest.TestCase):
    def test_doi_url_query_case_encoding_and_punctuation(self):
        variants = ["DOI: 10.5555/AbC.", "https://doi.org/10.5555%2FABC?track=synthetic#fragment",
                    "(10.5555/ABC).", "doi:10.5555/ABC，", "10.5555/ABC; PMID: 1234567",
                    'https://dx.doi.org/10.5555/ABC?x=synthetic&amp;y=synthetic']
        for value in variants:
            with self.subTest(value=value):
                self.assertEqual(normalize_doi(value), "10.5555/abc")

    def test_balanced_parentheses_and_intrinsic_semicolon_are_kept(self):
        self.assertEqual(normalize_doi("DOI:10.5555/(test)A;B)."), "10.5555/(test)a;b")
        self.assertIsNone(normalize_doi("10.5555/"))

    def test_pmid_label_url_and_leading_zeros(self):
        for value in ["0001234567", "PMID: 1234567, doi: 10.5555/test", "PMID 1234567 · doi: 10.5555/test",
                      "https://pubmed.ncbi.nlm.nih.gov/1234567/?track=synthetic",
                      "https://www.ncbi.nlm.nih.gov/pubmed/1234567#fragment"]:
            with self.subTest(value=value):
                self.assertEqual(normalize_pmid(value), "1234567")
        for value in ["0", "no PMID here", "1234567?track=synthetic", "https://example.invalid/1234567/"]:
            self.assertIsNone(normalize_pmid(value))

    def test_public_links_discard_tracking_and_reject_opaque_urls(self):
        self.assertEqual(public_article_url("https://www.x-mol.com/paper/42?subscriber=synthetic"), "https://www.x-mol.com/paper/42")
        self.assertIsNone(public_article_url("https://www.storkapp.me/users/synthetic"))
        self.assertIsNone(public_article_url("https://x-mol.com.example.invalid/paper/42"))
        self.assertIsNone(public_article_url("https://synthetic@www.x-mol.com/paper/42"))

    def test_title_normalization_and_year(self):
        self.assertEqual(title_key("  Synthetic A ＆ B: TITLE. ", 2026), title_key("synthetic a & b title", 2026))
        self.assertNotEqual(title_key("Synthetic title", 2025), title_key("Synthetic title", 2026))
