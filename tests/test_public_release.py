import contextlib
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
from unittest.mock import patch
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request

from tests.support import LocalFilesCase
from tests.test_delivery import Client
from tests.test_imap_sync import FakeImap, PRIVATE
from mail_literature import app, demo_cli, sync_cli, weekly_cli
from mail_literature.delivery import prepare_delivery, render_message, send_articles
from mail_literature.imap_reader import ImapConfig, ImapError, ReadOnlyImap, load_config
from mail_literature.models import Article
from mail_literature.state import StateStore
from mail_literature.wxpusher import ENDPOINT, PushConfig, PushError, WxPusherClient
from scripts.check_release import check, public_files


class PublicReleaseTests(LocalFilesCase):
    def config_file(self, extra=""):
        path = self.local_dir() / ".env"
        path.write_text("IMAP_USER=reader@example.invalid\nIMAP_APP_PASSWORD=" + PRIVATE + "\n" + extra,
                        encoding="utf-8")
        return path

    def test_custom_tls_endpoint_and_ascii_folder_are_configurable(self):
        config = load_config(self.config_file("IMAP_HOST=mail.example.invalid\nIMAP_PORT=1993\nIMAP_MAILBOX=Research Alerts\n"))
        self.assertEqual((config.host, config.port, config.mailbox), ("mail.example.invalid", 1993, "Research Alerts"))
        self.assertNotEqual(config.namespace, ImapConfig(config.user, config.password).namespace)

    def test_imap_format_check_does_not_connect_or_create_database(self):
        root = self.local_dir()
        (root / ".env").write_text("IMAP_USER=reader@example.invalid\nIMAP_APP_PASSWORD=" + PRIVATE,
                                   encoding="utf-8")
        with patch.object(sync_cli, "ReadOnlyImap", side_effect=AssertionError), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync_cli.main(["--check-config"], project_root=root), 0)
        self.assertFalse((root / "data/state.sqlite3").exists())

    def test_invalid_endpoint_and_folder_fail_with_safe_codes(self):
        for extra in ("IMAP_HOST=https://mail.example.invalid\n", "IMAP_HOST=-bad.example\n",
                      "IMAP_PORT=0\n", "IMAP_MAILBOX=文献\n", "IMAP_MAILBOX=\n"):
            with self.subTest(extra=extra), self.assertRaises(ImapError) as caught:
                load_config(self.config_file(extra))
            self.assertNotIn(PRIVATE, str(caught.exception))

    def test_folder_is_quoted_and_selected_readonly(self):
        fake = FakeImap()
        folder = 'Research "Alerts"'
        config = ImapConfig("reader@example.invalid", PRIVATE, mailbox=folder)
        with patch.object(fake, "select", return_value=("OK", [b"0"])) as select:
            with ReadOnlyImap(config, factory=fake.factory):
                pass
        select.assert_called_once_with('"Research \\"Alerts\\""', readonly=True)
        self.assertEqual(fake.calls[-1], ("LOGOUT",))

    def test_demo_parses_two_providers_and_merges_shared_doi(self):
        articles, stats = demo_cli.demo_articles()
        self.assertEqual((stats["emails"], stats["candidates"], stats["unique"], stats["duplicates"]), (2, 4, 3, 1))
        shared = next(a for a in articles if a.doi.endswith("alpha"))
        self.assertEqual({ref.source for ref in shared.sources}, {"X-MOL", "Stork"})
        self.assertTrue(all(a.translation_current("abstract") for a in articles))

    def test_demo_is_offline_repeatable_and_separate_from_live_state(self):
        root = self.local_dir()
        (root / "data").mkdir()
        state = root / "data/state.sqlite3"
        state.write_bytes(b"live sentinel")
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(demo_cli.main([], project_root=root), 0)
            output = root / "data/demo"
            before = {p.relative_to(output): (p.read_bytes(), p.stat().st_mtime_ns)
                      for p in output.rglob("*") if p.is_file()}
            self.assertEqual(demo_cli.main([], project_root=root), 0)
        self.assertEqual(state.read_bytes(), b"live sentinel")
        self.assertEqual(before, {p.relative_to(output): (p.read_bytes(), p.stat().st_mtime_ns)
                                  for p in output.rglob("*") if p.is_file()})
        self.assertEqual(json.loads((output / "report.json").read_text(encoding="utf-8"))["messages_sent"], 0)

    def test_demo_refuses_live_output_paths(self):
        root = self.local_dir()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(demo_cli.main(["--output-dir", str(root / "data/outbox")], project_root=root), 2)
        self.assertFalse((root / "data/outbox").exists())

    def test_demo_html_escapes_paper_fields(self):
        articles, stats = demo_cli.demo_articles()
        articles[0].title_zh = '<script>alert("synthetic")</script>'
        page = demo_cli.render_html(articles, stats)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_selected_pipeline_never_calls_disabled_stages(self):
        root = self.local_dir()
        with patch.object(weekly_cli.sync_cli, "main", return_value=0), \
             patch.object(weekly_cli.enrich_cli, "main", return_value=0), \
             patch.object(weekly_cli.translate_cli, "main", side_effect=AssertionError("translation disabled")), \
             patch.object(weekly_cli.jcr_match_cli, "main", side_effect=AssertionError("jcr disabled")), \
             patch.object(weekly_cli.delivery_cli, "main", return_value=0) as delivery, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(weekly_cli.main(["--fetch"], project_root=root), 0)
        self.assertIn("en", delivery.call_args.args[0])
        self.assertNotIn("--send", delivery.call_args.args[0])

    def test_default_pipeline_runs_without_any_credentials_or_jcr_snapshot(self):
        root = self.local_dir()
        with patch("socket.create_connection", side_effect=AssertionError), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(weekly_cli.main([], project_root=root), 0)
        self.assertTrue((root / "data/state.sqlite3").is_file())
        self.assertFalse((root / ".env").exists())

    def test_send_without_delivery_stage_rejected_before_readiness_check(self):
        root = self.local_dir()
        with patch.object(weekly_cli, "push_ready", side_effect=AssertionError), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(weekly_cli.main(["--send", "--stages", "sync"], project_root=root), 2)

    def test_invalid_stage_lists_are_rejected(self):
        for value in ("", "sync,sync", "unknown", "sync,", "sync, delivery"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                weekly_cli.main(["--stages", value], project_root=self.local_dir())
            self.assertEqual(caught.exception.code, 2)

    def test_english_delivery_works_without_translation_and_does_not_resend(self):
        root = self.local_dir()
        with StateStore(root / "state.sqlite3") as store:
            store.ingest([Article("Synthetic English paper", abstract_en="Synthetic English abstract.")])
            prepare_delivery(store, root)
            client = Client()
            self.assertEqual(send_articles(store, client, store.articles()).deferred, 1)
            self.assertEqual(send_articles(store, client, store.articles(), language="en").accepted, 1)
            self.assertEqual(send_articles(store, client, store.articles(), language="en").skipped, 1)
            self.assertEqual(len(client.calls), 1)
            self.assertIn("Synthetic English abstract.", client.calls[0][1])
            self.assertNotIn("待翻译", client.calls[0][1])

    def test_english_mode_keeps_receipt_gate(self):
        root = self.local_dir()
        with StateStore(root / "state.sqlite3") as store:
            prepare_delivery(store, root)
            with self.assertRaisesRegex(PushError, "receipt_not_confirmed"):
                send_articles(store, Client(), [], language="en", require_receipt=True)

    def test_invalid_language_is_rejected_before_delivery(self):
        with self.assertRaisesRegex(PushError, "invalid_delivery_language"):
            send_articles(None, None, [], language="invalid")

    def test_http_error_body_is_closed_and_not_exposed(self):
        body = io.BytesIO(b"synthetic private response")
        error = HTTPError(ENDPOINT, 403, "synthetic private echo", {}, body)
        def fail(*args, **kwargs):
            raise error
        client = WxPusherClient(PushConfig("AT_synthetic", ("UID_synthetic",)),
                                opener=SimpleNamespace(open=fail))
        with self.assertRaises(PushError) as caught:
            client._request(Request(ENDPOINT))
        self.assertTrue(body.closed)
        self.assertEqual(str(caught.exception), "wxpusher_network_or_http_failed")

    def test_subcommand_help_reaches_its_own_parser(self):
        with patch("mail_literature.demo_cli.main", return_value=0) as demo:
            self.assertEqual(app.main(["demo", "--help"]), 0)
        demo.assert_called_once_with(["--help"])

    def test_release_scan_skips_ignored_credentials_but_rejects_forced_tracking(self):
        root = self.local_dir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        (root / ".gitignore").write_text(".env\n", encoding="utf-8")
        (root / ".env").write_text("synthetic private configuration", encoding="utf-8")
        (root / "pyproject.toml").write_text('[project]\nlicense = "MIT"\n', encoding="utf-8")
        self.assertNotIn(Path(".env"), public_files(root))
        subprocess.run(["git", "add", "--force", ".env"], cwd=root, check=True, capture_output=True)
        _, errors = check(root)
        self.assertIn((".env", "private_or_unsafe_file"), errors)
