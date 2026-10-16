import contextlib
import io
import json
from unittest.mock import patch

from mail_literature import jcr_match_cli, journals_cli
from mail_literature.jcr.bundled import DEFAULT_SNAPSHOT, resolve_snapshot
from mail_literature.jcr.matching import JournalIndex
from mail_literature.journal_fields import journal_identity_hash
from mail_literature.models import Article
from mail_literature.state import StateStore
from tests.support import LocalFilesCase


class BundledJournalTests(LocalFilesCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.index = JournalIndex(resolve_snapshot(None))

    def run_query(self, args, root):
        output = io.StringIO()
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")), \
             contextlib.redirect_stdout(output):
            code = journals_cli.main(args + ["--json"], project_root=root)
        return code, json.loads(output.getvalue())

    def test_all_journals_and_categories_load_without_claiming_official_verification(self):
        self.assertEqual(len(self.index.journals), 22643)
        self.assertEqual(sum(map(len, self.index.categories.values())), 32215)
        self.assertEqual(len({row["category_name"] for rows in self.index.categories.values() for row in rows}), 254)
        self.assertFalse(self.index.full_complete)
        self.assertFalse(self.index.source_verified)
        self.assertFalse(self.index.manifest["version_confirmed"])
        self.assertEqual(self.index.invalid_identifiers, 3)

    def test_issn_query_works_without_credentials_database_or_network(self):
        root = self.local_dir()
        code, result = self.run_query(["--issn", "1664462X"], root)
        self.assertEqual(code, 0)
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["results"][0]["journal_title"], "Frontiers in Plant Science")
        self.assertEqual(result["results"][0]["jif"], "5.9")
        self.assertEqual(list(root.iterdir()), [])

    def test_name_query_is_bounded_and_returns_all_categories_per_result(self):
        root = self.local_dir()
        code, result = self.run_query(["Nature", "--limit", "2"], root)
        self.assertEqual(code, 0)
        self.assertGreater(result["total_matches"], 2)
        self.assertEqual(len(result["results"]), 2)
        for journal in result["results"]:
            self.assertEqual(journal["categories"], self.index.categories[journal["journal_id"]])

    def test_info_and_unmatched_queries_are_readonly(self):
        root = self.local_dir()
        code, result = self.run_query(["--info"], root)
        self.assertEqual((code, result["journals"], result["category_records"]), (0, 22643, 32215))
        code, result = self.run_query(["Synthetic nonexistent journal zzq"], root)
        self.assertEqual((code, result["total_matches"], result["results"]), (0, 0, []))
        self.assertEqual(list(root.iterdir()), [])

    def test_invalid_arguments_fail_before_querying(self):
        root = self.local_dir()
        for args in ([], ["--issn", "bad"], ["Nature", "--limit", "0"], ["Nature", "--issn", "1664-462X"],
                     ["Nature", "--info"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(journals_cli.main(args, project_root=root), 2)
        self.assertEqual(list(root.iterdir()), [])

    def test_explicit_missing_custom_snapshot_does_not_fall_back_silently(self):
        root = self.local_dir()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(journals_cli.main(["Nature", "--snapshot", "missing-local"], project_root=root), 4)
        self.assertEqual(list(root.iterdir()), [])

    def test_default_article_matching_uses_bundled_data_without_import_or_network(self):
        root = self.local_dir()
        article = Article("Synthetic article for matching", doi="10.5555/synthetic-bundled-match", journal="Frontiers in Plant Science")
        article.journal_metadata = {
            "identity_hash": journal_identity_hash(article), "provider": "Crossref",
            "source_url": "https://api.crossref.org/works/10.5555/synthetic-bundled-match",
            "journal_title": article.journal, "issns": ["1664-462X"], "issn": "1664-462X",
            "eissn": None, "linking_issn": None, "doi": article.doi, "pmid": None,
        }
        with StateStore(root / "data/state.sqlite3") as store:
            store.ingest([article])
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(jcr_match_cli.main([], project_root=root), 0)
        with StateStore(root / "data/state.sqlite3", readonly=True) as store:
            matched = store.articles()[0]
            self.assertEqual(matched.jcr_match["status"], "matched")
            self.assertEqual(matched.jcr_match["snapshot_id"], DEFAULT_SNAPSHOT)
            self.assertFalse(matched.jcr_match["full_complete"])
        self.assertFalse((root / "data/jcr").exists())
