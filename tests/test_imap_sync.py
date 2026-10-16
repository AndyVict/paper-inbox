from contextlib import redirect_stderr, redirect_stdout
import imaplib
import io
from pathlib import Path
import ssl
from unittest.mock import patch

from tests.support import LocalFilesCase, email_bytes, fixture
from mail_literature.imap_reader import ImapConfig, ImapError, ReadOnlyImap, header_source, load_config
from mail_literature.state import StateStore
from mail_literature.sync import sync_mail
from mail_literature import sync_cli

PRIVATE = "SYNTHETIC_PRIVATE_CREDENTIAL"


class FakeImap:
    """只实现允许的只读命令；写命令/非 PEEK FETCH 直接令测试失败。"""
    def __init__(self, mails=None, validity=7):
        self.mails = mails or {}
        self.validity = validity
        self.calls = []
        self.failures = {}
        self.bad_uid = False
        self.truncate_body = False
        self.supports_id = True

    def factory(self, host, port, *, ssl_context, timeout):
        assert host == "imap.163.com" and port == 993
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED and ssl_context.check_hostname
        assert timeout > 0
        return self

    def login(self, user, password):
        self.calls.append(("LOGIN",))
        return "OK", [b"authenticated"]

    def capability(self):
        return "OK", [b"IMAP4rev1 ID" if self.supports_id else b"IMAP4rev1"]

    def xatom(self, command, data):
        assert command == "ID" and PRIVATE not in data
        self.calls.append(("ID",))
        return "OK", [b"ID completed"]

    def select(self, mailbox, readonly=False):
        assert readonly and mailbox == "INBOX"
        assert (("ID",) in self.calls) == self.supports_id
        self.calls.append(("EXAMINE",))
        return "OK", [str(len(self.mails)).encode()]

    def response(self, name):
        assert name == "UIDVALIDITY"
        return name, [str(self.validity).encode()]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        assert command in {"SEARCH", "FETCH"}
        if command == "SEARCH":
            assert args[:2] == (None, "UID")
            first = int(args[2].split(":")[0])
            # Emulate the RFC's reversed n:* interval when n > max UID.
            largest = max(self.mails, default=0)
            uids = [uid for uid in sorted(self.mails) if min(first, largest) <= uid <= largest]
            return "OK", [" ".join(map(str, uids)).encode()]
        uid, query = int(args[0]), args[1]
        assert "BODY.PEEK[" in query and "RFC822" not in query.replace("RFC822.SIZE", "")
        if uid in self.failures:
            raise self.failures[uid]
        if uid not in self.mails:
            return "OK", [None]
        raw = self.mails[uid]
        section = "BODY[]" if "BODY.PEEK[]" in query else "BODY[HEADER.FIELDS (FROM SUBJECT DATE)]"
        literal = raw if section == "BODY[]" else raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        if section == "BODY[]" and self.truncate_body:
            literal = literal[:-10]
        response_uid = uid + 100 if self.bad_uid else uid
        metadata = f'1 (UID {response_uid} INTERNALDATE "11-Oct-2026 09:00:00 +0800" RFC822.SIZE {len(raw)} {section} {{{len(literal)}}}'.encode()
        return "OK", [(metadata, literal), b")"]

    def logout(self):
        self.calls.append(("LOGOUT",))
        return "BYE", [b"logout"]


def config():
    return ImapConfig("synthetic@example.invalid", PRIVATE)


class ImapSyncTests(LocalFilesCase):
    def test_readonly_peek_uid_and_repeat_sync_without_duplicate_or_private_storage(self):
        root = self.local_dir()
        fake = FakeImap({1: email_bytes(fixture("xmol.html")),
                         2: email_bytes("PRIVATE_BODY_MUST_NOT_BE_FETCHED", "Unknown", "plain"),
                         3: email_bytes(fixture("stork.html"), "Stork")})
        database = root / "state.sqlite3"
        with StateStore(database) as store:
            with ReadOnlyImap(config(), factory=fake.factory) as reader:
                first = sync_mail(store, reader)
            self.assertEqual((first.examined, first.parsed, first.ignored, first.failed), (3, 2, 1, 0))
            ids = [article.article_id for article in store.articles()]
            self.assertEqual(len(ids), 5)
            self.assertTrue(all(ref.received_at == "2026-10-11T09:00:00+08:00"
                                for article in store.articles() for ref in article.sources))
            with ReadOnlyImap(config(), factory=fake.factory) as reader:
                second = sync_mail(store, reader)
            self.assertEqual(second.examined, 0)
            self.assertEqual([article.article_id for article in store.articles()], ids)
            self.assertEqual(store.ensure_epoch(config().namespace, 7), 3)
        self.assertFalse(any(call[0] == "FETCH" and call[1] == "2" and "BODY.PEEK[]" in call[2] for call in fake.calls))
        for private in [PRIVATE, "synthetic@example.invalid", "synthetic-recipient@example.invalid", "SYNTHETIC_PRIVATE_MARKER", "PRIVATE_BODY_MUST_NOT_BE_FETCHED"]:
            self.assertNotIn(private.encode(), database.read_bytes())

    def test_uidvalidity_change_restarts_scan_preserving_articles_and_old_epoch(self):
        fake = FakeImap({1: email_bytes("Title: Synthetic A\nDOI: 10.5555/a", subtype="plain")})
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            with ReadOnlyImap(config(), factory=fake.factory) as reader:
                sync_mail(store, reader)
            initial = store.articles()[0].article_id
            fake.validity = 8
            with ReadOnlyImap(config(), factory=fake.factory) as reader:
                stats = sync_mail(store, reader)
            self.assertEqual(stats.parsed, 1)
            self.assertEqual(store.articles()[0].article_id, initial)
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM mailbox_epochs").fetchone()[0], 2)
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM mail_records").fetchone()[0], 2)

    def test_failed_uid_retried_after_cursor_has_advanced(self):
        fake = FakeImap({1: email_bytes("Title: Synthetic A", subtype="plain"),
                         2: email_bytes("Title: Synthetic B", subtype="plain")})
        fake.failures[1] = imaplib.IMAP4.error(PRIVATE)
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            with ReadOnlyImap(config(), factory=fake.factory) as reader:
                first = sync_mail(store, reader)
            self.assertEqual((first.failed, first.parsed), (1, 1))
            self.assertEqual(store.ensure_epoch(config().namespace, 7), 2)
            self.assertEqual(store.retry_uids(config().namespace, 7), [1])
            fake.failures.clear()
            with ReadOnlyImap(config(), factory=fake.factory) as reader:
                second = sync_mail(store, reader)
            self.assertEqual((second.examined, second.parsed), (1, 1))
            self.assertEqual(len(store.articles()), 2)
            self.assertEqual(store.retry_uids(config().namespace, 7), [])

    def test_abort_persists_failure_and_stops_connection_without_skipping_later_uids(self):
        fake = FakeImap({1: email_bytes("Title: Synthetic A", subtype="plain"),
                         2: email_bytes("Title: Synthetic B", subtype="plain")})
        fake.failures[1] = imaplib.IMAP4.abort(PRIVATE)
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            with self.assertRaisesRegex(ImapError, "imap_connection_failed"), ReadOnlyImap(config(), factory=fake.factory) as reader:
                sync_mail(store, reader)
            self.assertEqual(store.ensure_epoch(config().namespace, 7), 1)
            self.assertEqual(store.retry_uids(config().namespace, 7), [1])
            fake.failures.clear()
            with ReadOnlyImap(config(), factory=fake.factory) as reader:
                stats = sync_mail(store, reader)
            self.assertEqual(stats.parsed, 2)

    def test_failed_expunged_uid_is_terminal_and_does_not_retry_forever(self):
        fake = FakeImap()
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            store.ensure_epoch(config().namespace, 7)
            store.record_mail(config().namespace, 7, 1, "failed", error_code="imap_fetch_failed")
            with ReadOnlyImap(config(), factory=fake.factory) as reader:
                stats = sync_mail(store, reader)
            self.assertEqual(stats.vanished, 1)
            self.assertEqual(store.retry_uids(config().namespace, 7), [])

    def test_uid_mismatch_is_rejected_without_importing_other_mail(self):
        fake = FakeImap({1: email_bytes("Title: Synthetic A", subtype="plain")})
        fake.bad_uid = True
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            with ReadOnlyImap(config(), factory=fake.factory) as reader:
                stats = sync_mail(store, reader)
            self.assertEqual(stats.failed, 1)
            self.assertEqual(store.articles(), [])

    def test_bad_uidvalidity_is_rejected_before_search(self):
        fake = FakeImap(validity=0)
        with self.assertRaisesRegex(ImapError, "invalid_uidvalidity"):
            with ReadOnlyImap(config(), factory=fake.factory):
                pass
        self.assertFalse(any(call[0] == "SEARCH" for call in fake.calls))
        self.assertIn(("LOGOUT",), fake.calls)

    def test_oversized_subscription_body_is_not_requested(self):
        fake = FakeImap({1: email_bytes("Title: Synthetic A\n" + "x" * 2000, subtype="plain")})
        small = ImapConfig("synthetic@example.invalid", PRIVATE, max_message_bytes=1024)
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            with ReadOnlyImap(small, factory=fake.factory) as reader:
                stats = sync_mail(store, reader)
            self.assertEqual(stats.failed, 1)
        self.assertFalse(any(call[0] == "FETCH" and "BODY.PEEK[]" in call[2] for call in fake.calls))

    def test_failed_retries_cannot_starve_new_mail_and_retry_attempts_rotate(self):
        fake = FakeImap({uid: email_bytes(f"Title: Synthetic {uid}", subtype="plain") for uid in range(1, 6)})
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            store.ensure_epoch(config().namespace, 7)
            for uid in (1, 2, 3):
                store.record_mail(config().namespace, 7, uid, "failed", error_code="imap_fetch_failed")
                fake.failures[uid] = imaplib.IMAP4.error(PRIVATE)
            with ReadOnlyImap(config(), factory=fake.factory) as reader:
                stats = sync_mail(store, reader, max_mails=2)
            self.assertEqual((stats.examined, stats.parsed, stats.failed, stats.remaining), (2, 1, 1, 3))
            self.assertEqual(store.retry_uids(config().namespace, 7)[0], 2)

    def test_multiple_limited_batches_do_not_skip_unexamined_uids(self):
        fake = FakeImap({uid: email_bytes(f"Title: Synthetic {uid}", subtype="plain") for uid in (2, 5, 8, 11, 15)})
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            examined = []
            for _ in range(4):
                with ReadOnlyImap(config(), factory=fake.factory) as reader:
                    examined.append(sync_mail(store, reader, max_mails=2).examined)
            self.assertEqual(examined, [2, 2, 1, 0])
            self.assertEqual(store.ensure_epoch(config().namespace, 7), 15)
            self.assertEqual(len(store.articles()), 5)

    def test_truncated_message_does_not_commit_partial_articles(self):
        fake = FakeImap({1: email_bytes("Title: Synthetic A", subtype="plain")})
        fake.truncate_body = True
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            with ReadOnlyImap(config(), factory=fake.factory) as reader:
                stats = sync_mail(store, reader)
            self.assertEqual(stats.failed, 1)
            self.assertEqual(store.articles(), [])
            self.assertEqual(store.retry_uids(config().namespace, 7), [1])

    def test_recognized_subscription_parse_failure_remains_retryable(self):
        fake = FakeImap({1: email_bytes("No synthetic literature records", subtype="plain")})
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            with ReadOnlyImap(config(), factory=fake.factory) as reader:
                stats = sync_mail(store, reader)
            self.assertEqual(stats.failed, 1)
            self.assertEqual(store.retry_uids(config().namespace, 7), [1])

    def test_xmol_empty_notice_leaves_retry_queue_and_is_not_reprocessed(self):
        fake = FakeImap({1: email_bytes("<p>本期暂无匹配的文献。</p>")})
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            store.ensure_epoch(config().namespace, 7)
            store.record_mail(config().namespace, 7, 1, "failed", error_code="subscription_parse_failed")
            with ReadOnlyImap(config(), factory=fake.factory) as reader:
                first = sync_mail(store, reader)
                second = sync_mail(store, reader)
            self.assertEqual((first.ignored, first.failed, second.examined), (1, 0, 0))
            self.assertEqual(store.retry_uids(config().namespace, 7), [])
            self.assertEqual(store.articles(), [])
            row = store.connection.execute("SELECT status,error_code,attempts FROM mail_records").fetchone()
            self.assertEqual(tuple(row), ("ignored", "empty_literature_notice", 2))

    def test_empty_notice_with_literature_markers_stays_failed(self):
        fake = FakeImap({1: email_bytes("<p>暂无匹配的文献。</p><p>DOI: 10.5555/synthetic</p>")})
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            with ReadOnlyImap(config(), factory=fake.factory) as reader:
                stats = sync_mail(store, reader)
            self.assertEqual((stats.ignored, stats.failed), (0, 1))
            self.assertEqual(store.retry_uids(config().namespace, 7), [1])

    def test_server_without_id_capability_still_uses_readonly_session(self):
        fake = FakeImap()
        fake.supports_id = False
        with ReadOnlyImap(config(), factory=fake.factory) as reader:
            self.assertEqual(reader.uids_after(0), [])
        self.assertNotIn(("ID",), fake.calls)
        self.assertIn(("EXAMINE",), fake.calls)

    def test_config_placeholders_repr_and_utf8_bom(self):
        path = self.local_dir() / "config.local.env"
        path.write_text(f"IMAP_USER=synthetic@example.invalid\nIMAP_APP_PASSWORD='{PRIVATE}'\nRUN_MODE=wxpusher\n", encoding="utf-8-sig")
        loaded = load_config(path)
        self.assertNotIn(PRIVATE, repr(loaded))
        self.assertNotIn("synthetic@example.invalid", repr(loaded))
        path.write_text("IMAP_USER=your_email@example.com\nIMAP_APP_PASSWORD=replace_with_secret", encoding="utf-8")
        with self.assertRaisesRegex(ImapError, "missing_imap_credentials"):
            load_config(path)

    def test_header_filter_combines_sender_and_subject_signals(self):
        self.assertEqual(header_source(email_bytes("Title: Synthetic A", subtype="plain")), "X-MOL")
        self.assertIsNone(header_source(b"From: unrelated@example.invalid\r\nSubject: X-MOL\r\n\r\n"))
        self.assertIsNone(header_source(b"From: X-MOL <synthetic@example.invalid>\r\nSubject: Stork recommendation\r\n\r\n"))

    def test_default_cli_never_opens_configuration_or_network_even_run_mode_is_wxpusher(self):
        root = self.local_dir()
        (root / ".env").write_text("RUN_MODE=wxpusher", encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        original_open = Path.open

        def guard(path, *args, **kwargs):
            if path.name == ".env":
                raise AssertionError("Default sync must not read .env")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guard), patch("socket.socket", side_effect=AssertionError("Network prohibited")), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(sync_cli.main([], project_root=root), 0)
            self.assertEqual(sync_cli.main([], project_root=root), 0)
        self.assertIn("不读取配置、不联网、不发送", stdout.getvalue())

    def test_fetch_cli_only_prints_counts_and_fixed_errors(self):
        root = self.local_dir()
        (root / ".env").write_text(f"IMAP_USER=synthetic@example.invalid\nIMAP_APP_PASSWORD={PRIVATE}", encoding="utf-8")
        fake = FakeImap({1: email_bytes(fixture("xmol.html"))})
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sync_cli, "ReadOnlyImap", lambda cfg: ReadOnlyImap(cfg, factory=fake.factory)), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(sync_cli.main(["--fetch"], project_root=root), 0)
        for private in [PRIVATE, "synthetic@example.invalid", "Synthetic A & B", "10.5555/", "SYNTHETIC_PRIVATE_MARKER"]:
            self.assertNotIn(private, stdout.getvalue() + stderr.getvalue())
        fake.login = lambda *_: (_ for _ in ()).throw(imaplib.IMAP4.error(PRIVATE))
        with patch.object(sync_cli, "ReadOnlyImap", lambda cfg: ReadOnlyImap(cfg, factory=fake.factory)), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(sync_cli.main(["--fetch"], project_root=root), 5)
        self.assertIn("imap_login_failed", stderr.getvalue())
        self.assertNotIn(PRIVATE, stderr.getvalue())

    def test_cli_paths_and_existing_lock_refuse_unsafe_writes(self):
        root = self.local_dir()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(sync_cli.main(["--db", str(root / "tracked.sqlite3")], project_root=root), 2)
            self.assertEqual(sync_cli.main(["--db", str(root / "data" / "samples" / "state.sqlite3")], project_root=root), 2)
        data = root / "data"
        data.mkdir(exist_ok=True)
        lock = data / "state.sqlite3.sync.lock"
        lock.write_text("synthetic existing lock", encoding="utf-8")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(sync_cli.main([], project_root=root), 4)
        self.assertEqual(lock.read_text(encoding="utf-8"), "synthetic existing lock")
        self.assertFalse((data / "state.sqlite3").exists())
