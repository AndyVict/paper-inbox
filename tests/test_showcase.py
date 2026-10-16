import contextlib
import io
from unittest.mock import patch

from mail_literature import demo_cli, showcase_cli
from mail_literature.delivery import render_message
from mail_literature.message_report import LABELS, message_fields, render_html
from tests.support import LocalFilesCase


class ShowcaseTests(LocalFilesCase):
    def test_examples_retain_complete_translations_and_production_message_fields(self):
        articles, credits = showcase_cli.showcase_articles()
        self.assertEqual(len(articles), 2)
        for article in articles:
            self.assertTrue(article.translation_current("abstract"))
            self.assertTrue(article.jcr_match_current())
            self.assertGreater(len(article.abstract_zh), 400)
            fields = message_fields(article)
            self.assertEqual(tuple(label for label, _ in fields), LABELS)
            self.assertEqual("\n".join(label + "：" + value for label, value in fields) + "\n",
                             render_message(article)[0])
            self.assertEqual(dict(fields)["中文摘要"], article.abstract_zh)
            self.assertEqual(credits[article.doi]["license"], "CC-BY-4.0")
            self.assertFalse(article.sources)

    def test_showcase_is_offline_repeatable_and_preserves_live_database(self):
        root = self.local_dir()
        (root / "data").mkdir()
        live = root / "data/state.sqlite3"
        live.write_bytes(b"live sentinel")
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(showcase_cli.main([], project_root=root), 0)
            output = root / "data/showcase"
            before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in output.iterdir()}
            self.assertEqual(showcase_cli.main([], project_root=root), 0)
        self.assertEqual(live.read_bytes(), b"live sentinel")
        self.assertEqual(before, {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in output.iterdir()})
        self.assertFalse((root / ".env").exists())

    def test_output_paths_cannot_target_live_data(self):
        root = self.local_dir()
        for output in (root / "data/outbox", root / "data", root / "outside"):
            with self.subTest(output=output), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(showcase_cli.main(["--output-dir", str(output)], project_root=root), 2)
            self.assertFalse(output.exists())

    def test_report_escapes_fields_and_preserves_multiple_abstract_paragraphs(self):
        articles, _ = showcase_cli.showcase_articles()
        article = articles[0]
        article.title_zh = '<script>alert("example")</script>'
        article.abstract_zh = "第一段\n\n第二段"
        page = render_html([article], title="例子", notice="本地预览")
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertEqual(dict(message_fields(article))["中文摘要"], "第一段\n\n第二段")

    def test_synthetic_parser_demo_uses_the_same_seven_fields(self):
        articles, stats = demo_cli.demo_articles()
        self.assertEqual((stats["emails"], stats["candidates"], stats["unique"]), (2, 4, 3))
        for article in articles:
            self.assertEqual(tuple(label for label, _ in message_fields(article)), LABELS)
            self.assertIn("未核验", dict(message_fields(article))["JIF分区"])

    def test_installed_command_dispatches_showcase_without_a_database(self):
        from mail_literature import app
        with patch.object(showcase_cli, "main", return_value=0) as main:
            self.assertEqual(app.main(["showcase"]), 0)
        main.assert_called_once_with([])
