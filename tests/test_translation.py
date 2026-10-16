import io
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from support import LocalFilesCase
from mail_literature.dedup import deduplicate
from mail_literature.models import Article, SourceRef
from mail_literature.niutrans import (ENDPOINT, ERROR_CODES, MAX_RESPONSE_BYTES, MAX_SEGMENT_BYTES,
    NEU_ENDPOINT, NEU_MAX_SEGMENT_BYTES, NoRedirect, NiuTransClient, NiuTransConfig, TranslationError,
    TranslationOptions, TranslationResult, load_config, split_text)
from mail_literature.preview import OutboxError, articles_from_payload, render_preview, write_snapshot_previews
from mail_literature.state import ABSTRACT_SCHEMA, BASE_SCHEMA, DB_VERSION, StateError, StateStore
from mail_literature.translate_cli import main
from mail_literature.translation import CachedTranslations, source_hash, translate_articles, translation_key

KEY = "synthetic-test-key-only"


class FakeClient:
    def __init__(self, fail_at=None):
        self.requests = self.characters = 0
        self.stopped = None
        self.fail_at = fail_at

    def translate(self, text):
        self.requests += 1
        self.characters += len(text)
        if self.requests == self.fail_at:
            self.stopped = "translation_network_failed"
            return TranslationResult("failed", error_code=self.stopped)
        return TranslationResult("complete", "合成译文：" + text)


class TranslationTests(LocalFilesCase):
    def neu_payload(self, source="Synthetic source.", target="合成译文"):
        return {"code": 200, "msg": "成功", "data": [{"order": 0, "sentences": [{"data": target,
            "contrastSourceText": source, "paragraphOrder": 0, "order": 0,
            "sourceLanguageAbbreviation": "en", "targetLanguageAbbreviation": "zh"}]}]}

    def neu_client(self, payload, **kwargs):
        return NiuTransClient(NiuTransConfig(KEY, TranslationOptions(api_variant="neu")),
            opener=lambda *a, **k: io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")), **kwargs)
    def client(self, payload, **kwargs):
        return NiuTransClient(NiuTransConfig(KEY), opener=lambda *a, **k: io.BytesIO(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")), sleep=lambda _: None, **kwargs)

    def store(self, articles):
        store = StateStore(self.local_dir() / "state.sqlite3")
        store.ingest(articles)
        self.addCleanup(store.close)
        return store

    def test_config_utf8_bom_quotes_and_no_secret_repr(self):
        env = self.local_dir() / ".env"
        env.write_text('\ufeff# 中文\nIMAP_APP_PASSWORD=synthetic-unused\nNIUTRANS_API_KEY="' + KEY +
                       '"\nNIUTRANS_DICT_NO=dict1\nNIUTRANS_MEMORY_NO=mem1\nNIUTRANS_CACHE_VERSION=2\n', encoding="utf-8")
        config = load_config(env)
        self.assertEqual(config.api_key, KEY)
        self.assertEqual(config.options, TranslationOptions("dict1", "mem1", "2"))
        self.assertNotIn(KEY, repr(config))

    def test_missing_placeholder_duplicate_and_invalid_options_safe(self):
        env = self.local_dir() / ".env"
        for content in ("", "NIUTRANS_API_KEY=replace_with_local_secret", "NIUTRANS_API_KEY=" + KEY +
                        "\nNIUTRANS_API_KEY=" + KEY, "NIUTRANS_API_KEY=" + KEY + "\nNIUTRANS_DICT_NO=synthetic /invalid"):
            env.write_text(content, encoding="utf-8")
            with self.assertRaises(TranslationError) as caught:
                load_config(env)
            self.assertNotIn(KEY, str(caught.exception))
            self.assertNotIn("synthetic /invalid", str(caught.exception))

    def test_legacy_translation_key_compatibility_and_primary_priority(self):
        env = self.local_dir() / ".env"
        env.write_text("TRANSLATION_API_KEY=" + KEY, encoding="utf-8")
        self.assertEqual(load_config(env).api_key, KEY)
        env.write_text("TRANSLATION_API_KEY=" + KEY + "\nNIUTRANS_API_KEY=synthetic-specific-key", encoding="utf-8")
        self.assertEqual(load_config(env).api_key, "synthetic-specific-key")
        env.write_text("TRANSLATION_API_KEY=" + KEY + "\nNIUTRANS_API_KEY=replace_with_local_secret", encoding="utf-8")
        with self.assertRaises(TranslationError):
            load_config(env)

    def test_https_post_form_utf8_and_exact_language(self):
        calls = []
        def opener(request, **kwargs):
            calls.append((request, kwargs))
            return io.BytesIO(json.dumps({"tgt_text": "合成译文", "apikey": KEY, "src_text": "Study of β & <x>",
                                          "from": "en", "to": "zh"}).encode("utf-8"))
        client = NiuTransClient(NiuTransConfig(KEY, TranslationOptions("dict1", "mem1")), opener=opener)
        result = client.translate("Study of β & <x>")
        request, kwargs = calls[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertNotIn(KEY, request.full_url)
        self.assertEqual(request.get_method(), "POST")
        params = parse_qs(request.data.decode("ascii"))
        self.assertEqual(params["src_text"], ["Study of β & <x>"])
        self.assertEqual(params["from"], ["en"])
        self.assertEqual(params["to"], ["zh"])
        self.assertEqual(params["dictNo"], ["dict1"])
        self.assertEqual(params["memoryNo"], ["mem1"])
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(result.text, "合成译文")
        self.assertNotIn(KEY, repr(result))

    def test_redirect_handler_never_reposts(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 307, "", {}, "https://example.invalid"))

    def test_business_errors_never_log_server_message(self):
        for code, fixed in ERROR_CODES.items():
            with self.subTest(code=code):
                client = self.client({"error_code": code, "error_msg": KEY + " private source", "apikey": KEY})
                result = client.translate("Synthetic source.")
                self.assertEqual(result.error_code, fixed)
                self.assertEqual(client.last_service_code, code)
                self.assertEqual(client.translate("Other synthetic source.").error_code, fixed)
                self.assertEqual(client.requests, 1)
                self.assertNotIn(KEY, repr(result))

    def test_unknown_error_fixed_and_not_assumed_success(self):
        result = self.client({"error_code": KEY, "tgt_text": "伪成功"}).translate("Synthetic source.")
        self.assertEqual(result.error_code, "translation_service_failed")

    def test_diagnostics_do_not_reuse_success_status_after_a_later_failure(self):
        for failure in (URLError("synthetic-network-error"), {"code": 90001}):
            calls = []
            def opener(*args, **kwargs):
                calls.append(1)
                if len(calls) == 2 and isinstance(failure, URLError):
                    raise failure
                payload = self.neu_payload() if len(calls) == 1 else failure
                response = io.BytesIO(json.dumps(payload).encode("utf-8"))
                response.status = 200
                return response
            client = NiuTransClient(NiuTransConfig(KEY, TranslationOptions(api_variant="neu")),
                                    opener=opener, sleep=lambda _: None)
            self.assertEqual(client.translate("Synthetic source.").status, "complete")
            self.assertEqual(client.last_service_code, "200")
            self.assertEqual(client.translate("Other synthetic source.").status, "failed")
            self.assertIsNone(client.last_service_code)
            self.assertEqual(client.last_http_status, None if isinstance(failure, URLError) else 200)

    def test_invalid_response_echo_or_language_rejected(self):
        for payload in ([], {}, {"tgt_text": ""}, {"tgt_text": ["synthetic"]}, {"tgt_text": KEY},
                        {"tgt_text": "译文", "src_text": "wrong"}, {"tgt_text": "译文", "to": "en"},
                        {"tgt_text": "译文", "from": "auto"}):
            with self.subTest(payload_type=type(payload).__name__):
                self.assertEqual(self.client(payload).translate("Synthetic source.").error_code, "invalid_translation_response")

    def test_http_network_timeout_and_no_automatic_retry(self):
        for exception, code in ((URLError(KEY), "translation_network_failed"), (TimeoutError(KEY), "translation_network_failed"),
                                (HTTPError(ENDPOINT, 429, KEY, {}, None), "translation_rate_limited"),
                                (HTTPError(ENDPOINT, 403, KEY, {}, None), "translation_quota_or_access_denied"),
                                (HTTPError(ENDPOINT, 307, KEY, {}, None), "translation_redirect_rejected"),
                                (HTTPError(ENDPOINT, 500, KEY, {}, None), "translation_http_failed")):
            client = NiuTransClient(NiuTransConfig(KEY), opener=lambda *a, **k: (_ for _ in ()).throw(exception))
            self.assertEqual(client.translate("Synthetic source").error_code, code)
            self.assertEqual(client.last_http_status, exception.code if isinstance(exception, HTTPError) else None)
            client.translate("Other source")
            self.assertEqual(client.requests, 1)

    def test_size_invalid_json_and_throttle(self):
        client = NiuTransClient(NiuTransConfig(KEY), opener=lambda *a, **k: io.BytesIO(b"x" * (MAX_RESPONSE_BYTES + 1)))
        self.assertEqual(client.translate("Synthetic source").error_code, "translation_response_too_large")
        client = NiuTransClient(NiuTransConfig(KEY), opener=lambda *a, **k: io.BytesIO(b"not json"))
        self.assertEqual(client.translate("Synthetic source").error_code, "invalid_translation_response")
        sleeps = []
        client = self.client({"tgt_text": "合成译文"}, clock=lambda: 0)
        client.sleep = sleeps.append
        client.translate("First"); client.translate("Second")
        self.assertEqual(sleeps, [1.0])

    def test_split_preserves_all_content_and_utf8_boundaries(self):
        for text in ("Synthetic sentence. " * 800, "β界" * 6000, "First paragraph.\n\n" + "Second. " * 1200,
                     "x" * 11000, "  Synthetic & <value>\n\nSecond.  "):
            parts = split_text(text)
            self.assertEqual("".join(a + b for a, b in parts), text.strip())
            self.assertTrue(all(0 < len(a.encode("utf-8")) <= MAX_SEGMENT_BYTES for a, _ in parts))

    def test_invalid_source_input(self):
        for value in ("", "   ", "x\x00y", "x" * 100001):
            with self.assertRaises(TranslationError):
                split_text(value)

    def test_persistent_cache_no_raw_response_no_repeated_requests(self):
        path = self.local_dir() / "state.sqlite3"
        client = self.client({"tgt_text": "合成译文", "apikey": KEY, "src_text": "Synthetic source"})
        with StateStore(path) as store:
            cache = CachedTranslations(store, online=True, client=client)
            key, result = cache.lookup("Synthetic source")
            payloads = " ".join(row[0] for row in store.connection.execute("SELECT payload FROM translation_cache"))
            self.assertNotIn(KEY, payloads)
            self.assertNotIn("Synthetic source", payloads)
            self.assertEqual(len(key), 64)
        with StateStore(path) as store:
            cache = CachedTranslations(store)
            self.assertEqual(cache.lookup("Synthetic source")[1].text, result.text)
            self.assertEqual(cache.hits, 1)
        self.assertEqual(client.requests, 1)

    def test_failed_cache_ttl_and_no_immediate_paid_retry(self):
        store = self.store([])
        client = FakeClient(fail_at=1)
        cache = CachedTranslations(store, online=True, client=client, clock=lambda: 100)
        self.assertEqual(cache.lookup("Synthetic source")[1].status, "failed")
        fresh = FakeClient()
        cache = CachedTranslations(store, online=True, client=fresh, clock=lambda: 200)
        self.assertEqual(cache.lookup("Synthetic source")[1].status, "failed")
        self.assertEqual(fresh.requests, 0)
        later = FakeClient()
        cache = CachedTranslations(store, online=True, client=later, clock=lambda: 3701)
        self.assertEqual(cache.lookup("Synthetic source")[1].status, "complete")
        self.assertEqual(later.requests, 1)

    def test_partial_long_translation_resumes_using_chunk_cache(self):
        store = self.store([])
        text = "First. " * 900 + "\n\n" + "Second. " * 900
        client = FakeClient(fail_at=2)
        cache = CachedTranslations(store, online=True, client=client, clock=lambda: 100)
        self.assertEqual(cache.lookup(text)[1].status, "failed")
        chunk_count = len(split_text(text))
        later = FakeClient()
        cache = CachedTranslations(store, online=True, client=later, clock=lambda: 3701)
        self.assertEqual(cache.lookup(text)[1].status, "complete")
        self.assertEqual(later.requests, chunk_count - 1)
        self.assertGreaterEqual(cache.hits, 1)

    def test_budget_can_resume_without_losing_successful_chunks(self):
        store = self.store([])
        text = "a" * 4600
        cache = CachedTranslations(store, online=True, client=FakeClient(), max_characters=4500)
        self.assertIsNone(cache.lookup(text)[1])
        self.assertEqual(cache.deferred_reason, "translation_budget_reached")
        next_client = FakeClient()
        cache = CachedTranslations(store, online=True, client=next_client)
        self.assertEqual(cache.lookup(text)[1].status, "complete")
        self.assertEqual(next_client.characters, 100)
        cache = CachedTranslations(store, online=True, client=FakeClient(), max_requests=1)
        self.assertIsNone(cache.lookup("b" * 4600)[1])

    def test_cache_profile_includes_dictionary_memory_and_version(self):
        base = translation_key("Synthetic source")
        for options in (TranslationOptions("dict"), TranslationOptions(memory_no="mem"), TranslationOptions(cache_version="2")):
            self.assertNotEqual(translation_key("Synthetic source", options), base)

    def test_corrupt_cache_refuses_unsafe_payload(self):
        store = self.store([])
        cache = CachedTranslations(store, online=True, client=FakeClient())
        key, _ = cache.lookup("Synthetic source")
        store.connection.execute("UPDATE translation_cache SET payload=? WHERE cache_key=?", (json.dumps({"status": "failed", "error_code": KEY}), key))
        with self.assertRaisesRegex(StateError, "invalid_translation_cache"):
            cache.lookup("Synthetic source")

    def test_article_translations_preview_repeat_and_missing_abstract(self):
        store = self.store([Article("Synthetic title", abstract_en="Synthetic abstract.", doi="10.5555/one"),
                            Article("Other synthetic", doi="10.5555/two")])
        client = FakeClient()
        stats = translate_articles(store, CachedTranslations(store, online=True, client=client))
        self.assertEqual((stats.titles, stats.abstracts, stats.missing_abstracts), (2, 1, 1))
        original = store.articles()[0]
        self.assertIn(original.title_zh, render_preview(original))
        self.assertIn(original.abstract_zh, render_preview(original))
        outbox = self.local_dir() / "outbox"
        write_snapshot_previews(store.articles(), outbox)
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in outbox.iterdir() if p.is_file()}
        repeated = translate_articles(store, CachedTranslations(store, online=True, client=client))
        self.assertEqual(repeated.examined, 0)
        previews = write_snapshot_previews(store.articles(), outbox)
        self.assertEqual(previews.updated, 0)
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in outbox.iterdir() if p.is_file()})
        self.assertEqual(client.requests, 3)
        self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 0)

    def test_ingest_preserves_translations_and_invalidates_changed_source(self):
        source = SourceRef("Stork", "synthetic")
        article = Article("Synthetic title", abstract_en="First abstract.", doi="10.5555/one", sources=[source])
        store = self.store([article])
        translate_articles(store, CachedTranslations(store, online=True, client=FakeClient()))
        old = store.articles()[0]
        store.ingest([article])
        self.assertEqual(store.articles()[0].to_dict(), old.to_dict())
        store.ingest([Article("Changed synthetic title", abstract_en="Changed abstract.", doi=article.doi, sources=[source])])
        current = store.articles()[0]
        self.assertEqual(current.article_id, old.article_id)
        self.assertIsNone(current.title_zh)
        self.assertIsNone(current.abstract_zh)
        stats = translate_articles(store, CachedTranslations(store, online=True, client=FakeClient()))
        self.assertEqual((stats.titles, stats.abstracts), (1, 1))

    def test_bridge_merge_carries_only_matching_translation(self):
        left = Article("Synthetic title", doi="10.5555/one")
        right = Article("Synthetic title", pmid="12345", abstract_en="Synthetic abstract.", abstract_zh="合成摘要",
                        abstract_zh_source_hash=source_hash("Synthetic abstract."))
        merged = deduplicate([left, right])[0]
        self.assertEqual(merged.abstract_zh, "合成摘要")
        right.title_en = "Different title"
        right.doi = left.doi
        right.title_zh = "不匹配的标题"
        right.title_zh_source_hash = source_hash(right.title_en)
        self.assertIsNone(deduplicate([left, right])[0].title_zh)

    def test_stale_translation_never_shown(self):
        article = Article("New title", title_zh="过期译文", title_zh_source_hash=source_hash("Old title"))
        self.assertNotIn("过期译文", render_preview(article))
        self.assertIsNone(deduplicate([article])[0].title_zh)

    def test_preview_loader_validates_translation_pairs(self):
        article = deduplicate([Article("Synthetic title")])[0]
        value = article.to_dict()
        self.assertNotIn("title_zh", value)
        value["title_zh"] = "译文"
        with self.assertRaises(OutboxError):
            articles_from_payload({"schema_version": 1, "articles": [value]})

    def test_field_transaction_rollback_keeps_paid_cache(self):
        store = self.store([Article("Synthetic title")])
        store.connection.execute("""CREATE TRIGGER synthetic_failure BEFORE INSERT ON article_translations
                                  BEGIN SELECT RAISE(ABORT,'synthetic rollback'); END""")
        client = FakeClient()
        with self.assertRaises(StateError):
            translate_articles(store, CachedTranslations(store, online=True, client=client))
        self.assertIsNone(store.articles()[0].title_zh)
        self.assertGreater(store.connection.execute("SELECT COUNT(*) FROM translation_cache").fetchone()[0], 0)
        store.connection.execute("DROP TRIGGER synthetic_failure")
        self.assertEqual(translate_articles(store, CachedTranslations(store)).titles, 1)
        self.assertEqual(client.requests, 1)

    def test_offline_miss_never_invokes_client_or_marks_failure(self):
        store = self.store([Article("Synthetic title")])
        client = FakeClient()
        stats = translate_articles(store, CachedTranslations(store, client=client))
        self.assertEqual(stats.deferred, 1)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(client.requests, 0)

    def test_first_error_stops_batch_but_preserves_successful_title(self):
        store = self.store([Article("Synthetic title", abstract_en="Synthetic abstract."), Article("Another title")])
        client = FakeClient(fail_at=2)
        stats = translate_articles(store, CachedTranslations(store, online=True, client=client))
        self.assertEqual((stats.titles, stats.abstracts, stats.failed, stats.examined), (1, 0, 1, 1))
        self.assertTrue(store.articles()[0].translation_current("title"))

    def test_v2_upgrade_preserves_all_existing_tables(self):
        path = self.local_dir() / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as connection, connection:
            for statement in BASE_SCHEMA + ABSTRACT_SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version=2")
            connection.execute("INSERT INTO metadata VALUES ('synthetic','preserve')")
            connection.execute("INSERT INTO abstract_cache VALUES (?,?,?,?,?,?)", ("a" * 64, "PubMed", "{}", 100, None, 1))
        with StateStore(path) as store:
            self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], DB_VERSION)
            self.assertEqual(store.connection.execute("SELECT value FROM metadata").fetchone()[0], "preserve")
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM abstract_cache").fetchone()[0], 1)

    def test_migration_rolls_back_when_translation_schema_conflicts(self):
        path = self.local_dir() / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as connection, connection:
            for statement in BASE_SCHEMA + ABSTRACT_SCHEMA:
                connection.execute(statement)
            connection.execute("CREATE TABLE article_translations (synthetic TEXT)")
            connection.execute("PRAGMA user_version=2")
        with self.assertRaises(StateError):
            StateStore(path)
        with closing(sqlite3.connect(path)) as connection, connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='translation_cache'").fetchone())

    def test_cli_offline_no_config_no_network_and_privacy(self):
        root = self.local_dir()
        with StateStore(root / "data" / "state.sqlite3") as store:
            store.ingest([Article("Synthetic private text never printed")])
        output = io.StringIO()
        with patch("mail_literature.translate_cli.load_config", side_effect=AssertionError("no configuration")), patch(
                "mail_literature.translate_cli.NiuTransClient", side_effect=AssertionError("no network")), patch("sys.stdout", output):
            self.assertEqual(main([], project_root=root), 0)
        self.assertNotIn("Synthetic private text", output.getvalue())

    def test_cli_check_config_only_and_safe_missing_key(self):
        root = self.local_dir()
        (root / ".env").write_text("NIUTRANS_API_KEY=" + KEY, encoding="utf-8")
        output = io.StringIO()
        with patch("sys.stdout", output), patch("mail_literature.translate_cli.NiuTransClient", side_effect=AssertionError("no network")):
            self.assertEqual(main(["--check-config"], project_root=root), 0)
        self.assertNotIn(KEY, output.getvalue())
        (root / ".env").write_text("", encoding="utf-8")
        with patch("sys.stderr", output):
            self.assertEqual(main(["--fetch"], project_root=root), 2)
        self.assertNotIn(KEY, output.getvalue())

    def test_cli_rejects_dangerous_paths_and_limits(self):
        root = self.local_dir()
        with patch("sys.stderr", io.StringIO()):
            for args in (["--db", str(root / "external.sqlite3")], ["--outbox-dir", str(root / "data" / "samples")],
                         ["--max-characters", "0"], ["--max-requests", "1001"], ["--refresh"],
                         ["--fetch", "--env-file", str(root / "other.env")]):
                self.assertEqual(main(args, project_root=root), 2)

    def test_refresh_uses_new_cache_version_and_retains_previous_on_failure(self):
        store = self.store([Article("Synthetic title")])
        translate_articles(store, CachedTranslations(store, online=True, client=FakeClient()))
        old = store.articles()[0].title_zh
        stats = translate_articles(store, CachedTranslations(store, online=True, client=FakeClient(fail_at=1),
                                    options=TranslationOptions(cache_version="2")), refresh=True)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(store.articles()[0].title_zh, old)

    def test_neu_json_post_and_response(self):
        calls = []
        def opener(request, **kwargs):
            calls.append(request)
            return io.BytesIO(json.dumps(self.neu_payload("Synthetic β."), ensure_ascii=False).encode("utf-8"))
        client = NiuTransClient(NiuTransConfig(KEY, TranslationOptions(api_variant="neu")), opener=opener)
        result = client.translate("Synthetic β.")
        request = calls[0]
        self.assertEqual(client.endpoint, NEU_ENDPOINT)
        self.assertNotIn(KEY, client.endpoint)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)["apikey"], [KEY])
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"from": "en", "to": "zh", "src_text": "Synthetic β."})
        self.assertTrue(request.get_header("Content-type").startswith("application/json"))
        self.assertEqual(result.text, "合成译文")
        self.assertEqual(client.last_service_code, "200")

    def test_neu_sentence_and_paragraph_order(self):
        payload = self.neu_payload("First.", "第一句。")
        first = payload["data"][0]["sentences"][0]
        second = dict(first, data="第二句。", contrastSourceText="Second.", order=1)
        third = dict(first, data="第二段。", contrastSourceText="Third.", paragraphOrder=1)
        payload["data"][0]["sentences"] = [second, first]
        payload["data"].insert(0, {"order": 1, "sentences": [third]})
        result = self.neu_client(payload).translate("First. Second.\n\nThird.")
        self.assertEqual(result.text, "第一句。 第二句。\n\n第二段。")

    def test_neu_response_rejects_missing_duplicate_wrong_source_language(self):
        payloads = [{"code": 200, "data": []}, {"code": 200, "data": {}}, self.neu_payload("Wrong source."),
                    self.neu_payload(target=KEY)]
        for field, value in (("order", True), ("paragraphOrder", 1), ("data", []),
                             ("targetLanguageAbbreviation", "en"), ("sourceLanguageAbbreviation", "auto")):
            payload = self.neu_payload()
            payload["data"][0]["sentences"][0][field] = value
            payloads.append(payload)
        duplicate = self.neu_payload()
        duplicate["data"][0]["sentences"].append(dict(duplicate["data"][0]["sentences"][0]))
        payloads.append(duplicate)
        for payload in payloads:
            self.assertEqual(self.neu_client(payload).translate("Synthetic source.").error_code, "invalid_translation_response")

    def test_neu_unknown_business_code_never_emits_message_or_key(self):
        client = self.neu_client({"code": KEY, "msg": KEY, "data": KEY})
        result = client.translate("Synthetic source.")
        self.assertEqual(result.error_code, "translation_service_failed")
        self.assertIsNone(client.last_service_code)
        self.assertNotIn(KEY, repr(result))

    def test_neu_2088_maps_to_input_length_without_emitting_message(self):
        client = self.neu_client({"code": 2088, "msg": KEY})
        result = client.translate("Synthetic source.")
        self.assertEqual(result.error_code, "translation_input_too_long")
        self.assertEqual(client.last_service_code, "2088")
        self.assertNotIn(KEY, repr(result))
        client.translate("Other synthetic source.")
        self.assertEqual(client.requests, 1)

    def test_neu_client_rejects_oversized_request_before_network(self):
        client = self.neu_client(self.neu_payload())
        with self.assertRaisesRegex(TranslationError, "invalid_translation_input"):
            client.translate("x" * (NEU_MAX_SEGMENT_BYTES + 1))
        self.assertEqual(client.requests, 0)
        self.assertEqual(self.client({"tgt_text": "合成译文"}).translate("x" * 2151).status, "complete")

    def test_custom_split_limit_preserves_unicode_and_paragraphs(self):
        for text in ("Synthetic sentence. " * 150, "β界" * 1000, "First.\n\n" + "Second. " * 400):
            parts = split_text(text, max_bytes=NEU_MAX_SEGMENT_BYTES)
            self.assertEqual("".join(chunk + gap for chunk, gap in parts), text.strip())
            self.assertTrue(all(0 < len(chunk.encode("utf-8")) <= NEU_MAX_SEGMENT_BYTES for chunk, _ in parts))
        for limit in (0, True, MAX_SEGMENT_BYTES + 1):
            with self.assertRaises(TranslationError):
                split_text("Synthetic source.", max_bytes=limit)

    def test_neu_smaller_chunks_resume_around_old_failed_long_chunk(self):
        text = "Synthetic long sentence. " * 100
        options = TranslationOptions(api_variant="neu")
        store = self.store([])
        client = FakeClient()
        submitted = []
        original_translate = client.translate
        def bounded_translate(chunk):
            self.assertLessEqual(len(chunk.encode("utf-8")), NEU_MAX_SEGMENT_BYTES)
            submitted.append(chunk)
            return original_translate(chunk)
        client.translate = bounded_translate
        cache = CachedTranslations(store, client=client, online=True, options=options)
        old_key = translation_key(text.strip(), options, chunk=True)
        cache._save(old_key, TranslationResult("failed", error_code="translation_service_failed"))
        old_row = tuple(store.connection.execute("SELECT * FROM translation_cache WHERE cache_key=?", (old_key,)).fetchone())
        self.assertEqual(cache.lookup(text)[1].status, "complete")
        self.assertGreater(len(submitted), 1)
        self.assertEqual(" ".join(" ".join(submitted).split()), " ".join(text.split()))
        self.assertEqual(tuple(store.connection.execute("SELECT * FROM translation_cache WHERE cache_key=?", (old_key,)).fetchone()), old_row)
        requests = client.requests
        self.assertEqual(cache.lookup(text)[1].status, "complete")
        self.assertEqual(client.requests, requests)

    def test_neu_old_complete_whole_cache_survives_new_chunk_limit(self):
        text = "Synthetic long sentence. " * 100
        client = FakeClient()
        cache = CachedTranslations(self.store([]), client=client, online=True, options=TranslationOptions(api_variant="neu"))
        cache._save(translation_key(text, cache.options), TranslationResult("complete", "既有完整译文"))
        self.assertEqual(cache.lookup(text)[1].text, "既有完整译文")
        self.assertEqual(client.requests, 0)

    def test_neu_empty_line_blocks_preserve_original_paragraph_breaks(self):
        payload = self.neu_payload("First. Second.", "第一段。")
        base = payload["data"][0]["sentences"][0]
        blank = dict(base, data="", contrastSourceText="", paragraphOrder=1)
        third = dict(base, data="第二段。", contrastSourceText="Third.", paragraphOrder=2)
        payload["data"].extend([{"order": 1, "sentences": [blank]}, {"order": 2, "sentences": [third]}])
        result = self.neu_client(payload).translate("First. Second.\n\nThird.")
        self.assertEqual(result.text, "第一段。\n\n第二段。")
        payload["data"][1]["sentences"][0]["data"] = "凭空增加正文"
        self.assertEqual(self.neu_client(payload).translate("First. Second.\n\nThird.").error_code, "invalid_translation_response")

    def test_neu_http_error_secret_url_never_escapes(self):
        url = NEU_ENDPOINT + "?apikey=" + KEY
        client = NiuTransClient(NiuTransConfig(KEY, TranslationOptions(api_variant="neu")), opener=lambda *a, **k:
                               (_ for _ in ()).throw(HTTPError(url, 403, KEY, {}, None)))
        result = client.translate("Synthetic source.")
        self.assertEqual(result.error_code, "translation_quota_or_access_denied")
        self.assertNotIn(KEY, repr(result))
        self.assertNotIn("apikey", repr(result))

    def test_neu_config_variant_validation(self):
        env = self.local_dir() / ".env"
        env.write_text("TRANSLATION_API_KEY=" + KEY + "\nNIUTRANS_API_VARIANT=neu", encoding="utf-8")
        self.assertEqual(load_config(env).options.api_variant, "neu")
        for suffix in ("\nNIUTRANS_DICT_NO=dict1", "\nNIUTRANS_MEMORY_NO=mem1"):
            env.write_text("TRANSLATION_API_KEY=" + KEY + "\nNIUTRANS_API_VARIANT=neu" + suffix, encoding="utf-8")
            with self.assertRaises(TranslationError):
                load_config(env)
        env.write_text("TRANSLATION_API_KEY=" + KEY + "\nNIUTRANS_API_VARIANT=https://example.invalid", encoding="utf-8")
        with self.assertRaises(TranslationError):
            load_config(env)

    def test_neu_cache_partition_preserves_legacy_cloud_key(self):
        text = "Synthetic source."
        profile = json.dumps({"dict_no": "", "memory_no": "", "cache_version": "1"}, sort_keys=True, separators=(",", ":"))
        old_key = source_hash(f"niutrans-en-zh-v1\0{profile}\0whole\0{text}")
        self.assertEqual(translation_key(text), old_key)
        self.assertNotEqual(old_key, translation_key(text, TranslationOptions(api_variant="neu")))

    def test_neu_cache_profile_restores_offline_without_configuration(self):
        path = self.local_dir() / "state.sqlite3"
        client = self.neu_client(self.neu_payload())
        with StateStore(path) as store:
            cache = CachedTranslations(store, online=True, client=client)
            cache.lookup("Synthetic source.")
            profile = store.connection.execute("SELECT value FROM metadata WHERE key='translation_profile'").fetchone()[0]
            self.assertNotIn(KEY, profile)
        with StateStore(path) as store:
            cache = CachedTranslations(store)
            self.assertEqual(cache.options.api_variant, "neu")
            self.assertEqual(cache.lookup("Synthetic source.")[1].text, "合成译文")

    def test_invalid_cached_profile_rejected_safely(self):
        store = self.store([])
        store.connection.execute("INSERT INTO metadata VALUES ('translation_profile',?)", (json.dumps({"api_variant": KEY}),))
        with self.assertRaisesRegex(StateError, "invalid_translation_cache_profile"):
            CachedTranslations(store)

    def test_neu_cli_output_no_secret_url_or_response_message(self):
        root = self.local_dir()
        (root / ".env").write_text("TRANSLATION_API_KEY=" + KEY + "\nNIUTRANS_API_VARIANT=neu", encoding="utf-8")
        with StateStore(root / "data" / "state.sqlite3") as store:
            store.ingest([Article("Synthetic source.")])
        output = io.StringIO()
        client = self.neu_client({"code": 403, "msg": KEY})
        with patch("sys.stdout", output), patch("mail_literature.translate_cli.NiuTransClient", return_value=client):
            self.assertEqual(main(["--fetch"], project_root=root), 3)
        self.assertNotIn(KEY, output.getvalue())
        self.assertNotIn("?apikey", output.getvalue())


if __name__ == "__main__":
    import unittest
    unittest.main()
