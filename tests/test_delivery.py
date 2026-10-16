import contextlib
from hashlib import sha256
import io
import json
from unittest.mock import patch
from tests.support import LocalFilesCase

from mail_literature import delivery_cli
from mail_literature.delivery import confirm_received, prepare_delivery, reconcile, render_message, send_articles
from mail_literature.models import Article
from mail_literature.state import StateError, StateStore
from mail_literature.wxpusher import ENDPOINT, PushConfig, PushError, PushResult, WxPusherClient, load_config, recipient_key

CONFIG = PushConfig("AT_synthetic", ("UID_synthetic",))


class Client:
    def __init__(self, result=None, config=CONFIG):
        self.config, self.calls = config, []
        self.result = result or PushResult("accepted", send_record_id=12, provider_code=1000)

    def send(self, uid, content, summary):
        self.calls.append((uid, content, summary))
        return self.result


class Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def geturl(self):
        return ENDPOINT

    def read(self, _limit):
        return json.dumps(self.payload).encode("utf-8")


class DeliveryTests(LocalFilesCase):
    def store(self):
        root = self.local_dir()
        store = StateStore(root / "state.sqlite3")
        self.addCleanup(store.close)
        store.ingest([Article("Synthetic paper", doi="10.1000/a"), Article("Second paper", doi="10.1000/b")])
        for article in store.articles():
            article.title_zh = "合成中文标题"
            article.title_zh_source_hash = sha256(article.title_en.encode("utf-8")).hexdigest()
            store.ingest([article])
        prepare_delivery(store, root)
        return store

    def test_repeat_and_changed_content_do_not_resend(self):
        store, client = self.store(), Client()
        first = send_articles(store, client, store.articles(), max_messages=1)
        self.assertEqual((first.accepted, first.deferred), (1, 1))
        article = store.articles()[0]
        article.journal = "Updated public journal"
        store.ingest([article])
        second = send_articles(store, client, store.articles())
        self.assertEqual((second.accepted, second.skipped, len(client.calls)), (1, 1, 2))

    def test_timeout_unknown_is_not_retried(self):
        store, client = self.store(), Client(PushResult("unknown", "wxpusher_network_or_http_failed"))
        first = send_articles(store, client, store.articles())
        second = send_articles(store, client, store.articles()[:1], retry_failed=True)
        self.assertEqual((first.unknown, second.blocked, len(client.calls)), (1, 1, 1))

    def test_crash_after_claim_leaves_no_automatic_replay(self):
        store, client = self.store(), Client()
        with patch.object(client, "send", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                send_articles(store, client, store.articles())
        self.assertEqual(store.delivery_status(store.articles()[0].article_id, recipient_key(CONFIG.uids[0])), "unknown")
        stats = send_articles(store, client, store.articles()[:1])
        self.assertEqual((stats.blocked, len(client.calls)), (1, 0))

    def test_explicit_business_failure_retry(self):
        store, client = self.store(), Client(PushResult("failed", "wxpusher_business_rejected"))
        send_articles(store, client, store.articles()[:1])
        self.assertEqual(send_articles(store, client, store.articles()[:1]).attempted, 0)
        client.result = PushResult("accepted", send_record_id=15)
        self.assertEqual(send_articles(store, client, store.articles()[:1], retry_failed=True).accepted, 1)

    def test_per_recipient_and_daily_limit(self):
        store = self.store()
        client = Client(config=PushConfig("AT_synthetic", ("UID_one", "UID_two")))
        first = send_articles(store, client, store.articles(), daily_limit=1, clock=lambda: 100000)
        again = send_articles(store, client, store.articles(), daily_limit=1, clock=lambda: 100001)
        later = send_articles(store, client, store.articles(), daily_limit=1, clock=lambda: 200000)
        self.assertEqual((first.accepted, again.attempted, later.accepted), (2, 0, 2))

    def test_merged_article_retains_delivery_history(self):
        store, client = self.store(), Client()
        store.ingest([Article("PMID copy", pmid="123")])
        copy = next(a for a in store.articles() if a.pmid)
        copy.title_zh = "合成标题"
        copy.title_zh_source_hash = sha256(copy.title_en.encode("utf-8")).hexdigest()
        store.ingest([copy])
        send_articles(store, client, [copy])
        store.ingest([Article("Synthetic paper", doi="10.1000/a", pmid="123")])
        canonical = next(a for a in store.articles() if a.doi == "10.1000/a")
        self.assertEqual(send_articles(store, client, [canonical]).attempted, 0)

    def test_receipt_gate_and_manual_confirmation(self):
        store, client = self.store(), Client()
        with self.assertRaisesRegex(PushError, "wxpusher_receipt_not_confirmed"):
            send_articles(store, client, store.articles(), require_receipt=True)
        sent = send_articles(store, client, store.articles()[:1])
        with self.assertRaises(StateError):
            confirm_received(store, sent.last_attempt_id, {recipient_key("UID_wrong")})
        confirm_received(store, sent.last_attempt_id, {recipient_key(CONFIG.uids[0])})
        self.assertEqual(send_articles(store, client, store.articles(), require_receipt=True).accepted, 1)

    def test_backup_once_and_readonly_database(self):
        store = self.store()
        prepare_delivery(store, store.path.parent)
        self.assertEqual(len(list((store.path.parent / "backups").glob("*.sqlite3"))), 1)
        before = store.path.read_bytes()
        with StateStore(store.path, readonly=True) as reader:
            self.assertEqual(len(reader.articles()), 2)
        self.assertEqual(store.path.read_bytes(), before)

    def test_truncation_and_stale_translation(self):
        article = Article("Synthetic", article_id="a" * 24, abstract_en="original", abstract_zh="中" * 2000,
                          abstract_zh_source_hash=sha256(b"original").hexdigest())
        content, _ = render_message(article, 100)
        self.assertIn("摘要已截断", content)
        self.assertIn("JCR 未核验", content)
        self.assertNotIn("英文摘要", content)
        article.abstract_en = "changed"
        self.assertIn("待翻译", render_message(article)[0])

    def test_incomplete_chinese_translation_waits_without_delivery_record(self):
        store, client = self.store(), Client()
        article = store.articles()[0]
        article.abstract_en = "New public abstract"
        store.ingest([article])
        self.assertEqual(send_articles(store, client, [article]).deferred, 1)
        self.assertEqual(len(client.calls), 0)
        self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 0)

    def test_status_queries_rotate_after_a_network_failure(self):
        store, client = self.store(), Client()
        def send(uid, content, summary):
            client.calls.append(uid)
            return PushResult("accepted", send_record_id=100 + len(client.calls))
        client.send = send
        send_articles(store, client, store.articles(), clock=lambda: 100000)
        with patch.object(client, "query", create=True, side_effect=[PushError("wxpusher_network_or_http_failed"), (1000, 2)]) as query:
            self.assertEqual(reconcile(store, client, max_queries=1, clock=lambda: 100001), (0, 1))
            self.assertEqual(reconcile(store, client, max_queries=1, clock=lambda: 100002), (1, 0))
        self.assertEqual([call.args[0] for call in query.call_args_list], [101, 102])

    def test_default_cli_never_loads_credentials_or_network(self):
        # A dedicated synthetic project gives the CLI its normal data layout.
        project = self.local_dir()
        (project / "data").mkdir()
        database = project / "data/state.sqlite3"
        with StateStore(database) as setup:
            setup.ingest([Article("Synthetic", doi="10.1000/test")])
        before = database.read_bytes()
        with patch.object(delivery_cli, "load_config", side_effect=AssertionError), patch.object(delivery_cli, "WxPusherClient", side_effect=AssertionError), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(delivery_cli.main([], project_root=project), 0)
        self.assertEqual(database.read_bytes(), before)

    def test_invalid_output_path_rejected(self):
        project = self.local_dir()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(delivery_cli.main(["--preview-dir", str(project / "data/outbox")], project_root=project), 2)


class ClientTests(LocalFilesCase):
    def request_result(self, payload):
        class Opener:
            def open(self, request, timeout):
                return Response(payload)
        return WxPusherClient(CONFIG, opener=Opener()).send(CONFIG.uids[0], "合成消息", "合成")

    def test_top_success_requires_exact_recipient_result(self):
        self.assertEqual(self.request_result({"code": 1000, "data": []}).status, "unknown")
        self.assertEqual(self.request_result({"code": 1000, "data": [{"uid": "UID_wrong", "code": 1000}]}).status, "unknown")
        result = self.request_result({"code": 1000, "data": [{"uid": CONFIG.uids[0], "code": 1000, "sendRecordId": 55}]})
        self.assertEqual((result.status, result.send_record_id), ("accepted", 55))

    def test_business_failure_does_not_echo_provider_message(self):
        result = self.request_result({"code": 1001, "msg": "AT_private UID_private"})
        self.assertEqual(result.status, "failed")
        self.assertNotIn("private", repr(result))

    def test_network_failure_has_one_call_and_safe_error(self):
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                raise OSError("AT_private UID_private")
        opener = Opener()
        result = WxPusherClient(CONFIG, opener=opener).send(CONFIG.uids[0], "合成", "合成")
        self.assertEqual((result.status, opener.calls), ("unknown", 1))
        self.assertNotIn("private", repr(result))

    def test_large_body_rejected_before_network(self):
        with self.assertRaises(PushError):
            WxPusherClient(CONFIG).send(CONFIG.uids[0], "中" * 10000, "合成")

    def test_config_repr_hides_credentials(self):
        project = self.local_dir()
        path = project / ".env"
        path.write_text("WXPUSHER_APP_TOKEN=AT_synthetic\nWXPUSHER_UIDS=UID_one,UID_two,UID_one\n", encoding="utf-8")
        config = load_config(path)
        self.assertEqual(len(config.uids), 2)
        self.assertNotIn("synthetic", repr(config))
