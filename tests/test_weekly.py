import argparse
import contextlib
import io
from unittest.mock import patch
from tests.support import LocalFilesCase

from mail_literature import weekly_cli
from mail_literature.models import Article
from mail_literature.state import StateStore
from mail_literature.wxpusher import PushError


class WeeklyTests(LocalFilesCase):
    def arguments(self, root, **overrides):
        values = dict(fetch=False, send=False, env_file=root / ".env", max_mails=100,
                      max_articles=100, max_messages=10, daily_limit=10, max_abstract=1000)
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_stage_order_and_independent_online_send_flags(self):
        root = self.local_dir()
        calls = []
        modules = [weekly_cli.sync_cli, weekly_cli.enrich_cli, weekly_cli.translate_cli,
                   weekly_cli.jcr_match_cli, weekly_cli.delivery_cli]
        with contextlib.ExitStack() as stack:
            for index, module in enumerate(modules):
                def invoke(parameters, project_root, index=index):
                    calls.append((index, parameters))
                    return 0
                stack.enter_context(patch.object(module, "main", side_effect=invoke))
            self.assertEqual(weekly_cli.run_stages(root, self.arguments(root, fetch=True, send=True), io.StringIO()), 0)
        self.assertEqual([index for index, _ in calls], list(range(5)))
        self.assertTrue(all("--fetch" in parameters for _, parameters in calls[:4]))
        self.assertTrue(all("--send" not in parameters for _, parameters in calls[:4]))
        self.assertIn("--require-receipt", calls[-1][1])

    def test_partial_enrichment_continues_but_state_error_stops(self):
        root = self.local_dir()
        with patch.object(weekly_cli.sync_cli, "main", return_value=0), patch.object(weekly_cli.enrich_cli, "main", return_value=3), patch.object(weekly_cli.translate_cli, "main", return_value=4), patch.object(weekly_cli.jcr_match_cli, "main") as after:
            self.assertEqual(weekly_cli.run_stages(root, self.arguments(root), io.StringIO()), 4)
            after.assert_not_called()

    def test_default_does_not_read_config(self):
        root = self.local_dir()
        with patch.object(weekly_cli, "push_ready", side_effect=AssertionError), patch.object(weekly_cli, "run_stages", return_value=0) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(weekly_cli.main([], project_root=root), 0)
        self.assertFalse(run.call_args.args[1].fetch)
        self.assertFalse(run.call_args.args[1].send)
        self.assertFalse((root / "data/.weekly.sync.lock").exists())

    def test_unconfirmed_send_stops_before_any_stage(self):
        root = self.local_dir()
        with patch.object(weekly_cli, "push_ready", side_effect=PushError("wxpusher_receipt_not_confirmed")), patch.object(weekly_cli, "run_stages") as run, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(weekly_cli.main(["--fetch", "--send"], project_root=root), 4)
        run.assert_not_called()

    def test_concurrent_weekly_run_is_rejected(self):
        root = self.local_dir()
        (root / "data").mkdir()
        lock = root / "data/.weekly.sync.lock"
        lock.write_text("synthetic lock", encoding="utf-8")
        with patch.object(weekly_cli, "run_stages") as run, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(weekly_cli.main([], project_root=root), 4)
        run.assert_not_called()
        self.assertEqual(lock.read_text(encoding="utf-8"), "synthetic lock")

    def test_readonly_refuses_old_schema_without_upgrading(self):
        root = self.local_dir()
        path = root / "state.sqlite3"
        with StateStore(path) as setup:
            setup.ingest([Article("Synthetic")])
            setup.connection.execute("PRAGMA user_version=2")
        before = path.read_bytes()
        with self.assertRaisesRegex(Exception, "readonly_requires_current_database"):
            StateStore(path, readonly=True)
        self.assertEqual(path.read_bytes(), before)
