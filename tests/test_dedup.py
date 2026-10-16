import unittest

from tests import support
from mail_literature.dedup import deduplicate
from mail_literature.models import Article, SourceRef


class DedupTests(unittest.TestCase):
    def test_cross_source_doi_enriches_and_preserves_provenance(self):
        a = Article("Synthetic title", doi="10.5555/ALPHA.", sources=[SourceRef("X-MOL", "synthetic-a")], parse_warnings=["missing_pmid", "missing_journal"])
        b = Article("A different supplied title", doi="https://doi.org/10.5555/alpha?track=synthetic", pmid="1234567", journal="Example Journal", sources=[SourceRef("Stork", "synthetic-b")])
        result = deduplicate([a, b, b])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].pmid, "1234567")
        self.assertEqual(result[0].journal, "Example Journal")
        self.assertEqual(len(result[0].sources), 2)
        self.assertEqual(result[0].parse_warnings, [])
        self.assertEqual(a.doi, "10.5555/ALPHA.")

    def test_pmid_and_later_doi_keep_existing_id(self):
        first = deduplicate([Article("Synthetic first title", pmid="0001234567")])[0]
        result = deduplicate([first, Article("Synthetic title variant", pmid="1234567", doi="10.5555/alpha")])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].article_id, first.article_id)
        self.assertEqual(result[0].doi, "10.5555/alpha")

    def test_title_year_fallback_and_different_years(self):
        records = [Article("Synthetic A & B: TITLE.", year=2026), Article("synthetic a b title", year=2026), Article("Synthetic A & B: TITLE.", year=2025)]
        self.assertEqual(len(deduplicate(records)), 2)

    def test_title_does_not_merge_conflicting_identifiers(self):
        self.assertEqual(len(deduplicate([Article("Synthetic title", year=2026, doi="10.5555/a"), Article("Synthetic title", year=2026, doi="10.5555/b")])), 2)
        self.assertEqual(len(deduplicate([Article("Synthetic title", pmid="1234567"), Article("Synthetic title", pmid="7654321")])), 2)

    def test_conflicting_doi_blocks_pmid_merge(self):
        records = [Article("Synthetic A", doi="10.5555/a", pmid="1234567"), Article("Synthetic B", doi="10.5555/b", pmid="1234567")]
        self.assertEqual(len(deduplicate(records)), 2)

    def test_identifier_bridge_merges_two_previous_records(self):
        a = Article("Synthetic A", doi="10.5555/a")
        b = Article("Synthetic B", pmid="1234567")
        previous = deduplicate([a, b])
        result = deduplicate(previous + [Article("Synthetic C", doi="10.5555/a", pmid="1234567")])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].article_id, previous[0].article_id)
        self.assertEqual(result[0].pmid, "1234567")

    def test_merged_title_alias_is_retained_for_future_fallback(self):
        records = deduplicate([Article("Synthetic A", year=2026, doi="10.5555/a"), Article("Synthetic B", year=2026, doi="10.5555/a")])
        self.assertEqual(len(deduplicate(records + [Article("Synthetic B", year=2026)])), 1)
