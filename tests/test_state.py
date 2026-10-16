import json
from contextlib import closing
from pathlib import Path
import sqlite3
from unittest.mock import patch

from tests.support import LocalFilesCase
from mail_literature.models import Article, SourceRef
from mail_literature.preview import OutboxError, write_previews, write_snapshot_previews
from mail_literature.state import StateError, StateStore

NAMESPACE = "a" * 64
RECIPIENT = "b" * 64


class StateTests(LocalFilesCase):
    def test_json_migration_preserves_ids_aliases_sources_and_is_once_only(self):
        root = self.local_dir()
        outbox = root / "outbox"
        ref = SourceRef("Stork", "synthetic-hash", "2026-10-09T08:00:00+08:00")
        write_previews([Article("Synthetic A", pmid="1234567", sources=[ref])], outbox)
        write_previews([Article("Synthetic renamed", pmid="1234567", doi="10.5555/a")], outbox)
        original = (outbox / "index.json").read_bytes()
        old = json.loads(original)["articles"][0]
        db = root / "state.sqlite3"
        with StateStore(db) as store:
            self.assertEqual(store.import_legacy(outbox / "index.json").total, 1)
            self.assertEqual(store.articles()[0].to_dict(), old)
            store.set_processing_state(old["article_id"], "ready")
            store.record_delivery(old["article_id"], RECIPIENT, "accepted")
            self.assertIsNone(store.import_legacy(outbox / "index.json"))
            self.assertEqual(store.delivery_status(old["article_id"], RECIPIENT), "accepted")
            self.assertEqual(store.connection.execute("SELECT processing_state FROM articles").fetchone()[0], "ready")
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM article_sources").fetchone()[0], 1)
            aliases = {row[0] for row in store.connection.execute("SELECT identity_key FROM article_aliases")}
            self.assertEqual(aliases, set(old["identity_keys"]))
            unchanged = write_snapshot_previews(store.articles(), outbox)
            self.assertEqual((unchanged.created, unchanged.updated, unchanged.unchanged), (0, 0, 1))
        self.assertEqual((outbox / "index.json").read_bytes(), original)
        with StateStore(db) as store:
            self.assertEqual(store.articles()[0].article_id, old["article_id"])
            self.assertEqual(store.delivery_status(old["article_id"], RECIPIENT), "accepted")

    def test_enrichment_and_bridge_keep_delivery_history_and_snapshot_canonical_id(self):
        root = self.local_dir()
        outbox = root / "outbox"
        with StateStore(root / "state.sqlite3") as store:
            store.ingest([Article("Synthetic DOI", doi="10.5555/a"), Article("Synthetic PMID", pmid="1234567")])
            first, retired = store.articles()
            write_snapshot_previews(store.articles(), outbox)
            store.record_delivery(retired.article_id, RECIPIENT, "accepted")
            store.record_delivery(first.article_id, "c" * 64, "unknown")
            stats = store.ingest([Article("Synthetic bridge", doi="10.5555/a", pmid="1234567")])
            self.assertEqual((stats.total, stats.merged), (1, 1))
            self.assertEqual(store.resolve(retired.article_id), first.article_id)
            self.assertEqual(store.delivery_status(first.article_id, RECIPIENT), "accepted")
            self.assertEqual(store.delivery_status(retired.article_id, "c" * 64), "unknown")
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 2)
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 2)
            snapshot = write_snapshot_previews(store.articles(), outbox)
            self.assertEqual((snapshot.total, snapshot.archived), (1, 1))
            self.assertEqual(next((outbox / "messages").glob("*.md")).stem, first.article_id)

    def test_mail_transaction_rolls_back_articles_result_and_cursor_together(self):
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            store.ensure_epoch(NAMESPACE, 7)
            store.connection.execute("""CREATE TRIGGER synthetic_failure BEFORE INSERT ON mail_records
                BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
            with self.assertRaisesRegex(StateError, "state_transaction_failed"):
                store.record_mail(NAMESPACE, 7, 15, "parsed", articles=[Article("Synthetic A", doi="10.5555/a")])
            self.assertEqual(store.articles(), [])
            self.assertEqual(store.ensure_epoch(NAMESPACE, 7), 0)
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM mail_records").fetchone()[0], 0)

    def test_bad_json_does_not_set_migration_marker_or_overwrite_input(self):
        root = self.local_dir()
        index = root / "index.json"
        index.write_text("synthetic invalid JSON", encoding="utf-8")
        with StateStore(root / "state.sqlite3") as store:
            with self.assertRaises(OutboxError):
                store.import_legacy(index)
            self.assertFalse(store.connection.execute("SELECT * FROM metadata").fetchall())
            self.assertEqual(store.articles(), [])
        self.assertEqual(index.read_text(encoding="utf-8"), "synthetic invalid JSON")

    def test_invalid_or_foreign_database_is_not_overwritten(self):
        root = self.local_dir()
        corrupt = root / "corrupt.sqlite3"
        corrupt.write_bytes(b"synthetic corrupt database")
        with self.assertRaises(StateError):
            StateStore(corrupt)
        self.assertEqual(corrupt.read_bytes(), b"synthetic corrupt database")
        foreign = root / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection, connection:
            connection.execute("CREATE TABLE synthetic_user_data (value TEXT)")
            connection.execute("INSERT INTO synthetic_user_data VALUES ('preserve')")
        before = foreign.read_bytes()
        with self.assertRaisesRegex(StateError, "unsupported_state_database"):
            StateStore(foreign)
        self.assertEqual(foreign.read_bytes(), before)

    def test_repeated_ingest_and_conflicting_identifiers(self):
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            records = [Article("Synthetic same", doi="10.5555/a", year=2026),
                       Article("Synthetic same", doi="10.5555/b", year=2026)]
            store.ingest(records)
            stats = store.ingest(records)
            self.assertEqual((stats.total, stats.created, stats.updated, stats.merged), (2, 0, 0, 0))
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM article_aliases").fetchone()[0], 4)

    def test_retry_queue_is_separate_from_cursor_and_epoch(self):
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            self.assertEqual(store.ensure_epoch(NAMESPACE, 7), 0)
            store.record_mail(NAMESPACE, 7, 10, "failed", error_code="imap_fetch_failed")
            store.record_mail(NAMESPACE, 7, 20, "ignored")
            self.assertEqual(store.ensure_epoch(NAMESPACE, 7), 20)
            self.assertEqual(store.retry_uids(NAMESPACE, 7), [10])
            self.assertEqual(store.ensure_epoch(NAMESPACE, 8), 0)
            self.assertEqual(store.retry_uids(NAMESPACE, 8), [])
            store.record_mail(NAMESPACE, 7, 10, "ignored")
            self.assertEqual(store.ensure_epoch(NAMESPACE, 7), 20)
            self.assertEqual(store.retry_uids(NAMESPACE, 7), [])

    def test_delivery_status_distinguishes_acceptance_confirmation_and_unknown(self):
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            store.ingest([Article("Synthetic A")])
            key = store.articles()[0].article_id
            self.assertEqual(store.delivery_status(key, RECIPIENT), "pending")
            store.record_delivery(key, RECIPIENT, "inflight")
            store.record_delivery(key, RECIPIENT, "failed", "synthetic_timeout")
            self.assertEqual(store.delivery_status(key, RECIPIENT), "unknown")
            store.record_delivery(key, RECIPIENT, "accepted")
            self.assertEqual(store.delivery_status(key, RECIPIENT), "accepted")
            store.record_delivery(key, RECIPIENT, "confirmed")
            self.assertEqual(store.delivery_status(key, RECIPIENT), "confirmed")
            with self.assertRaises(StateError):
                store.record_delivery(key, "private-real-uid-must-not-be-stored", "accepted")

    def test_snapshot_interruption_can_be_recovered_from_committed_database(self):
        root = self.local_dir()
        with StateStore(root / "state.sqlite3") as store:
            store.ingest([Article("Synthetic A", doi="10.5555/a")])
            from mail_literature import preview
            original = preview._atomic_write

            def interrupt(path, text):
                if path.name == "index.json":
                    raise OSError("synthetic interruption")
                original(path, text)

            with patch.object(preview, "_atomic_write", side_effect=interrupt), self.assertRaises(OSError):
                write_snapshot_previews(store.articles(), root / "outbox")
            write_snapshot_previews(store.articles(), root / "outbox")
            self.assertEqual(len(list((root / "outbox" / "messages").glob("*.md"))), 1)
            self.assertEqual(len(store.articles()), 1)
