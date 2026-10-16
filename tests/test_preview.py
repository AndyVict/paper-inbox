import json
from pathlib import Path
from unittest.mock import patch

from tests.support import LocalFilesCase
from mail_literature.models import Article, SourceRef
from mail_literature.preview import OutboxError, render_preview, write_previews


class PreviewTests(LocalFilesCase):
    def test_preview_explicitly_marks_missing_translation_abstract_and_jcr(self):
        text = render_preview(Article("Synthetic title", quartile_claimed="1区 Top", sources=[SourceRef("Stork", "synthetic")]))
        for expected in ["中文标题：待翻译", "摘要待补全", "JCR 未核验", "第三方分区", "未发送", "未提取公共文献链接"]:
            self.assertIn(expected, text)
        self.assertNotIn("None", text)

    def test_supplied_abstract_is_retained_without_inventing_translation(self):
        text = render_preview(Article("Synthetic title", abstract_en="A synthetic abstract."))
        self.assertIn("中文摘要：待翻译", text)
        self.assertIn("英文摘要：A synthetic abstract.", text)

    def test_repeated_input_preserves_count_and_does_not_rewrite_files(self):
        outbox = self.local_dir()
        records = [Article("Synthetic A", doi="10.5555/a"), Article("Synthetic B", pmid="1234567")]
        first = write_previews(records + records, outbox)
        self.assertEqual(first.created, 2)
        paths = [outbox / "index.json", *sorted((outbox / "messages").glob("*.md"))]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        second = write_previews(records, outbox)
        self.assertEqual((second.created, second.updated, second.unchanged), (0, 0, 2))
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths})

    def test_enrichment_updates_one_existing_preview(self):
        outbox = self.local_dir()
        write_previews([Article("Synthetic A", pmid="1234567", parse_warnings=["missing_doi"])], outbox)
        initial_name = next((outbox / "messages").glob("*.md")).name
        stats = write_previews([Article("Synthetic B", pmid="1234567", doi="10.5555/a")], outbox)
        self.assertEqual((stats.total, stats.created, stats.updated), (1, 0, 1))
        self.assertEqual([path.name for path in (outbox / "messages").glob("*.md")], [initial_name])

    def test_bridge_archives_exactly_one_generated_preview(self):
        outbox = self.local_dir()
        write_previews([Article("Synthetic A", doi="10.5555/a"), Article("Synthetic B", pmid="1234567")], outbox)
        stats = write_previews([Article("Synthetic C", doi="10.5555/a", pmid="1234567")], outbox)
        self.assertEqual((stats.total, stats.archived), (1, 1))
        self.assertEqual(len(list((outbox / "messages").glob("*.md"))), 1)
        self.assertEqual(len(list((outbox / "archive").glob("*.md"))), 1)
        self.assertEqual(len(json.loads((outbox / "index.json").read_text(encoding="utf-8"))["articles"]), 1)

    def test_reparsing_same_primary_mail_refreshes_metadata_once(self):
        outbox = self.local_dir()
        ref = SourceRef("Stork", "synthetic-same-mail")
        write_previews([Article("Synthetic A", doi="10.5555/a", journal="Synthetic stale journal", sources=[ref])], outbox)
        corrected = Article("Synthetic A", doi="10.5555/a", journal="Example Journal", sources=[ref])
        stats = write_previews([corrected], outbox)
        self.assertEqual((stats.created, stats.updated), (0, 1))
        self.assertIn("期刊：Example Journal", next((outbox / "messages").glob("*.md")).read_text(encoding="utf-8"))
        stats = write_previews([corrected], outbox)
        self.assertEqual((stats.created, stats.updated, stats.unchanged), (0, 0, 1))

    def test_bad_index_is_not_overwritten(self):
        outbox = self.local_dir()
        index = outbox / "index.json"
        for content in ["invalid synthetic JSON", '{"schema_version": 999, "articles": []}',
                        '{"schema_version": 1, "articles": [{"title_en": "Synthetic", "article_id": "../synthetic"}]}']:
            with self.subTest(content=content):
                index.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(OutboxError, "invalid_outbox_index"):
                    write_previews([Article("Synthetic A")], outbox)
                self.assertEqual(index.read_text(encoding="utf-8"), content)

    def test_existing_lock_blocks_write_without_removing_lock(self):
        outbox = self.local_dir()
        lock = outbox / ".preview.lock"
        lock.write_text("synthetic lock", encoding="utf-8")
        with self.assertRaisesRegex(OutboxError, "outbox_locked"):
            write_previews([Article("Synthetic A")], outbox)
        self.assertTrue(lock.exists())
        self.assertFalse((outbox / "index.json").exists())

    def test_wrong_index_field_types_are_safe_failures(self):
        outbox = self.local_dir()
        valid = Article("Synthetic A", article_id="a" * 24).to_dict()
        for field, wrong in [("doi", 123), ("authors", "synthetic string"), ("year", True),
                             ("parse_warnings", [123]), ("identity_keys", [None])]:
            with self.subTest(field=field):
                item = dict(valid, **{field: wrong})
                content = json.dumps({"schema_version": 1, "articles": [item]})
                index = outbox / "index.json"
                index.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(OutboxError, "invalid_outbox_index"):
                    write_previews([Article("Synthetic A")], outbox)
                self.assertEqual(index.read_text(encoding="utf-8"), content)

    def test_interrupted_index_write_can_be_repeated_without_duplicate(self):
        outbox = self.local_dir()
        from mail_literature import preview
        original = preview._atomic_write

        def interrupt(path: Path, content: str):
            if path.name == "index.json":
                raise OSError("synthetic interruption")
            original(path, content)

        with patch.object(preview, "_atomic_write", side_effect=interrupt):
            with self.assertRaises(OSError):
                write_previews([Article("Synthetic A", doi="10.5555/a")], outbox)
        stats = write_previews([Article("Synthetic A", doi="10.5555/a")], outbox)
        self.assertEqual(stats.total, 1)
        self.assertEqual(len(list((outbox / "messages").glob("*.md"))), 1)
