from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from unittest.mock import patch

from tests.support import LocalFilesCase, email_bytes, fixture
from mail_literature import cli


class CliTests(LocalFilesCase):
    def test_end_to_end_offline_no_config_no_network_no_private_stdout(self):
        root = self.local_dir()
        inputs = root / "data" / "samples"
        inputs.mkdir(parents=True)
        (inputs / "xmol.eml").write_bytes(email_bytes(fixture("xmol.html")))
        (inputs / "stork.eml").write_bytes(email_bytes(fixture("stork.html"), "Stork"))
        stdout, stderr = io.StringIO(), io.StringIO()
        original_open = Path.open

        def guard(path, *args, **kwargs):
            if path.name == ".env":
                raise AssertionError("Offline CLI must never open .env")
            return original_open(path, *args, **kwargs)

        with patch.object(cli, "ROOT", root), patch.object(Path, "open", guard), patch("socket.socket", side_effect=AssertionError("Network prohibited")), redirect_stdout(stdout), redirect_stderr(stderr):
            first = cli.main([])
            second = cli.main([])
        self.assertEqual((first, second), (0, 0))
        text = stdout.getvalue() + stderr.getvalue()
        self.assertIn("本批候选 6 篇，去重后 5 篇", text)
        self.assertIn("预览新增 0，更新 0，未变 5", text)
        for private in ["SYNTHETIC_PRIVATE_MARKER", "synthetic-recipient@example.invalid", "Synthetic A & B", "10.5555/"]:
            self.assertNotIn(private, text)
        outbox = root / "data" / "outbox"
        payload = json.loads((outbox / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(payload["articles"]), 5)
        self.assertEqual(len(list((outbox / "messages").glob("*.md"))), 5)
        self.assertNotIn("SYNTHETIC_PRIVATE_MARKER", str(payload))

    def test_unknown_mail_does_not_block_supported_mail_but_returns_failure(self):
        root = self.local_dir()
        inputs = root / "data" / "samples"
        inputs.mkdir(parents=True)
        (inputs / "good.eml").write_bytes(email_bytes("Title: Synthetic A", subtype="plain"))
        (inputs / "unknown.eml").write_bytes(email_bytes("Title: Synthetic B", "Unknown", "plain"))
        with patch.object(cli, "ROOT", root), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = cli.main([])
        self.assertEqual(status, 3)
        self.assertEqual(len(list((root / "data" / "outbox" / "messages").glob("*.md"))), 1)

    def test_empty_input_does_not_create_outbox(self):
        root = self.local_dir()
        (root / "data" / "samples").mkdir(parents=True)
        with patch.object(cli, "ROOT", root), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([]), 2)
        self.assertFalse((root / "data" / "outbox").exists())

    def test_output_must_be_ignored_data_and_separate_from_inputs(self):
        root = self.local_dir()
        for outbox in [root / "tracked-output", root / "data", root / "data" / "samples"]:
            with self.subTest(outbox=outbox), patch.object(cli, "ROOT", root), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["--outbox-dir", str(outbox)]), 2)
