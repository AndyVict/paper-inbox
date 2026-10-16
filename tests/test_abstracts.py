import io
from contextlib import closing
import json
import sqlite3
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from tests.support import LocalFilesCase
from mail_literature.abstracts import AbstractClient, AbstractError, AbstractResult, MAX_RESPONSE, parse_response, query_url
from mail_literature.dedup import deduplicate
from mail_literature.enrich_cli import main
from mail_literature.enrichment import CachedAbstracts, enrich_abstracts
from mail_literature.models import Article, SourceRef
from mail_literature.preview import write_snapshot_previews
from mail_literature.state import BASE_SCHEMA, DB_VERSION, SCHEMA, StateError, StateStore

DOI = "10.5555/synthetic"
PMID = "1234567"


def raw_json(value):
    return json.dumps(value).encode("utf-8")


def crossref(abstract="<jats:p>Synthetic <jats:italic>abstract</jats:italic>.</jats:p>", doi=DOI):
    return raw_json({"status": "ok", "message-type": "work", "message": {"DOI": doi, "abstract": abstract}})


class FakeClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.requests = 0
        self.calls = []

    def query(self, provider, kind, identifier):
        self.requests += 1
        self.calls.append((provider, kind, identifier))
        return self.responses.get((provider, kind, identifier), AbstractResult(provider, "not_found"))


class AbstractTests(LocalFilesCase):
    def test_pubmed_structured_sections_and_inline_markup(self):
        raw = b'''<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1234567</PMID><Article><Abstract>
        <AbstractText Label="BACKGROUND">Synthetic <i>inline</i> text.</AbstractText>
        <AbstractText Label="RESULTS">Second result.</AbstractText></Abstract></Article></MedlineCitation>
        <PubmedData><ArticleIdList><ArticleId IdType="doi">10.5555/SYNTHETIC</ArticleId></ArticleIdList></PubmedData>
        </PubmedArticle></PubmedArticleSet>'''
        result = parse_response("PubMed", "pmid", PMID, raw)
        self.assertEqual(result.abstract, "BACKGROUND: Synthetic inline text.\nRESULTS: Second result.")
        self.assertEqual((result.doi, result.pmid), (DOI, PMID))

    def test_pubmed_missing_abstract_and_wrong_identifier_are_distinct(self):
        raw = b'<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1234567</PMID></MedlineCitation></PubmedArticle></PubmedArticleSet>'
        self.assertEqual(parse_response("PubMed", "pmid", PMID, raw).status, "no_abstract")
        self.assertEqual(parse_response("PubMed", "pmid", PMID, b"<PubmedArticleSet/>").status, "not_found")
        with self.assertRaisesRegex(AbstractError, "abstract_identity_mismatch"):
            parse_response("PubMed", "pmid", "7654321", raw)

    def test_plain_xml_and_inverted_words_preserve_literal_symbols_in_cache(self):
        raw = b'''<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1234567</PMID><Article><Abstract>
        <AbstractText>Literal &lt;symbol&gt; &amp;amp; concentration &lt; 2.</AbstractText>
        </Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>'''
        result = parse_response("PubMed", "pmid", PMID, raw)
        self.assertEqual(result.abstract, "Literal <symbol> &amp; concentration < 2.")
        client = FakeClient({("PubMed", "pmid", PMID): result})
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            cache = CachedAbstracts(store, client=client, online=True)
            cache.lookup("PubMed", "pmid", PMID)
            self.assertEqual(cache.lookup("PubMed", "pmid", PMID)[1].abstract, result.abstract)
        raw = raw_json({"doi": DOI, "abstract_inverted_index": {"<symbol>": [0], "&amp;": [1], "<": [2], "2.": [3]}})
        self.assertEqual(parse_response("OpenAlex", "doi", DOI, raw).abstract, "<symbol> &amp; < 2.")

    def test_europepmc_exact_match_and_full_abstract(self):
        raw = raw_json({"hitCount": 1, "resultList": {"result": [
            {"id": PMID, "source": "MED", "doi": DOI, "abstractText": "<h4>Summary</h4><p>Synthetic text.</p>"}]}})
        for kind, value in [("doi", DOI), ("pmid", PMID)]:
            self.assertEqual(parse_response("EuropePMC", kind, value, raw).abstract, "Summary\nSynthetic text.")
        with self.assertRaisesRegex(AbstractError, "abstract_identity_mismatch"):
            parse_response("EuropePMC", "doi", "10.5555/other", raw)

    def test_europepmc_pmc_record_id_is_not_a_pmid(self):
        raw = raw_json({"hitCount": 1, "resultList": {"result": [
            {"id": "PMC1234567", "source": "PMC", "doi": DOI, "abstractText": "Synthetic text"}]}})
        self.assertIsNone(parse_response("EuropePMC", "doi", DOI, raw).pmid)

    def test_crossref_jats_cleaning_and_absence(self):
        result = parse_response("Crossref", "doi", DOI, crossref())
        self.assertEqual(result.abstract, "Synthetic abstract.")
        self.assertEqual(parse_response("Crossref", "doi", DOI, crossref(None)).status, "no_abstract")
        with self.assertRaisesRegex(AbstractError, "abstract_identity_mismatch"):
            parse_response("Crossref", "doi", DOI, crossref(doi="10.5555/other"))

    def test_openalex_reconstructs_repeated_words_in_position_order(self):
        raw = raw_json({"doi": "https://doi.org/" + DOI, "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/" + PMID},
                        "abstract_inverted_index": {"word": [1, 3], "Synthetic": [0], "repeated": [2]}})
        result = parse_response("OpenAlex", "doi", DOI, raw)
        self.assertEqual((result.abstract, result.pmid), ("Synthetic word repeated word", PMID))

    def test_openalex_invalid_index_is_not_a_missing_abstract(self):
        for index in [{"a": [0], "b": [0]}, {"a": [1]}, {"a": [True]}, {"a": [20_000]}, [], {"a": []}]:
            with self.subTest(index=index), self.assertRaises(AbstractError):
                parse_response("OpenAlex", "doi", DOI, raw_json({"doi": DOI, "abstract_inverted_index": index}))
        self.assertEqual(parse_response("OpenAlex", "doi", DOI, raw_json({"doi": DOI})).status, "no_abstract")

    def test_malformed_service_payloads_and_entities_are_rejected(self):
        for provider, kind, value, raw in [
            ("PubMed", "pmid", PMID, b'<PubmedArticleSet><ERROR>synthetic</ERROR></PubmedArticleSet>'),
            ("PubMed", "pmid", PMID, b'<!DOCTYPE foo [<!ENTITY x "synthetic">]><PubmedArticleSet/>'),
            ("EuropePMC", "doi", DOI, b'{"hitCount":1,"resultList":{"result":[]}}'),
            ("Crossref", "doi", DOI, b'{"status":"error","message":{}}'),
            ("Crossref", "doi", DOI, crossref(123)),
            ("OpenAlex", "doi", DOI, b'[]'),
        ]:
            with self.subTest(provider=provider), self.assertRaises(AbstractError):
                parse_response(provider, kind, value, raw)

    def test_query_urls_escape_doi_and_do_not_search_title(self):
        self.assertIn("10.5555%2Fsynthetic", query_url("Crossref", "doi", DOI))
        self.assertIn("resultType=core", query_url("EuropePMC", "pmid", PMID))
        with self.assertRaises(AbstractError):
            query_url("PubMed", "doi", DOI)
        with self.assertRaises(AbstractError):
            query_url("Crossref", "doi", "https://doi.org/" + DOI)

    def test_http_network_retry_then_success_and_safe_failure(self):
        calls = []
        def opener(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                raise URLError("synthetic private response")
            return io.BytesIO(crossref())
        client = AbstractClient(opener=opener, sleep=lambda _: None)
        self.assertEqual(client.query("Crossref", "doi", DOI).status, "found")
        self.assertEqual(client.requests, 2)
        with patch.object(client, "opener", side_effect=URLError("synthetic private response")):
            result = client.query("Crossref", "doi", DOI)
        self.assertEqual(result.error_code, "abstract_network_failed")
        self.assertNotIn("private", repr(result))

    def test_rate_limit_or_access_denied_stops_provider_for_run(self):
        for status in [401, 403, 429]:
            calls = []
            def opener(request, timeout):
                calls.append(request)
                raise HTTPError(request.full_url, status, "synthetic private response", {"Retry-After": "3600"}, io.BytesIO())
            client = AbstractClient(opener=opener, sleep=lambda _: None)
            self.assertEqual(client.query("Crossref", "doi", DOI).status, "failed")
            self.assertEqual(client.query("Crossref", "doi", "10.5555/other").status, "failed")
            self.assertEqual(len(calls), 1)

    def test_response_size_and_404(self):
        client = AbstractClient(opener=lambda *a, **k: io.BytesIO(b"x" * (MAX_RESPONSE + 1)), sleep=lambda _: None)
        self.assertEqual(client.query("Crossref", "doi", DOI).error_code, "abstract_response_too_large")
        with patch.object(client, "opener", side_effect=HTTPError("https://api.crossref.org", 404, "synthetic", {}, io.BytesIO())):
            self.assertEqual(client.query("Crossref", "doi", DOI).status, "not_found")

    def test_v1_database_upgrade_preserves_payload_and_delivery_history(self):
        path = self.local_dir() / "legacy.sqlite3"
        article = deduplicate([Article("Synthetic legacy", doi=DOI)])[0]
        payload = json.dumps(article.to_dict())
        with closing(sqlite3.connect(path)) as connection, connection:
            for statement in BASE_SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version=1")
            connection.execute("INSERT INTO articles(article_id,payload) VALUES (?,?)", (article.article_id, payload))
            connection.execute("INSERT INTO delivery_attempts(article_id,recipient_key,status) VALUES (?,?,?)",
                               (article.article_id, "b" * 64, "accepted"))
        with StateStore(path) as store:
            self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], DB_VERSION)
            self.assertEqual(store.connection.execute("SELECT payload FROM articles").fetchone()[0], payload)
            self.assertEqual(store.delivery_status(article.article_id, "b" * 64), "accepted")

    def test_positive_cache_survives_reopen_and_negative_cache_expires(self):
        path = self.local_dir() / "state.sqlite3"
        responses = {("Crossref", "doi", DOI): AbstractResult("Crossref", "found", "Synthetic abstract.", DOI)}
        client = FakeClient(responses)
        with StateStore(path) as store:
            cache = CachedAbstracts(store, client=client, online=True, clock=lambda: 100)
            self.assertEqual(cache.lookup("Crossref", "doi", DOI)[1].status, "found")
            cache.lookup("EuropePMC", "doi", DOI)
        with StateStore(path) as store:
            cache = CachedAbstracts(store, client=client, online=True, clock=lambda: 101)
            cache.lookup("Crossref", "doi", DOI)
            cache.lookup("EuropePMC", "doi", DOI)
            self.assertEqual((client.requests, cache.hits), (2, 2))
            cache.clock = lambda: 100 + 7 * 86400
            cache.lookup("EuropePMC", "doi", DOI)
            self.assertEqual(client.requests, 3)

    def test_error_cache_expires_after_one_hour(self):
        client = FakeClient({("Crossref", "doi", DOI): AbstractResult("Crossref", "failed", error_code="abstract_network_failed")})
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            cache = CachedAbstracts(store, client=client, online=True, clock=lambda: 100)
            cache.lookup("Crossref", "doi", DOI)
            cache.clock = lambda: 3699
            cache.lookup("Crossref", "doi", DOI)
            self.assertEqual(client.requests, 1)
            cache.clock = lambda: 3700
            cache.lookup("Crossref", "doi", DOI)
            self.assertEqual(client.requests, 2)

    def test_corrupt_cache_is_not_silently_overwritten(self):
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            cache = CachedAbstracts(store, client=FakeClient(), online=True)
            key, _ = cache.lookup("Crossref", "doi", DOI)
            store.connection.execute("UPDATE abstract_cache SET payload='synthetic invalid JSON' WHERE cache_key=?", (key,))
            with self.assertRaisesRegex(StateError, "invalid_abstract_cache"):
                cache.lookup("Crossref", "doi", DOI)

    def test_fallback_enrichment_preserves_identity_sources_and_delivery(self):
        root = self.local_dir()
        client = FakeClient({("EuropePMC", "pmid", PMID): AbstractResult("EuropePMC", "found", "Synthetic abstract.", DOI, PMID)})
        with StateStore(root / "state.sqlite3") as store:
            store.ingest([Article("Synthetic title", doi=DOI, pmid=PMID, sources=[SourceRef("Stork", "synthetic")])])
            old = store.articles()[0].to_dict()
            store.record_delivery(old["article_id"], "b" * 64, "accepted")
            cache = CachedAbstracts(store, client=client, online=True)
            stats = enrich_abstracts(store, cache)
            current = store.articles()[0].to_dict()
            current.pop("abstract_en")
            old.pop("abstract_en")
            self.assertEqual(current, old)
            self.assertEqual((stats.enriched, stats.remaining, client.requests), (1, 0, 2))
            self.assertEqual(store.delivery_status(old["article_id"], "b" * 64), "accepted")
            self.assertEqual(store.connection.execute("SELECT provider FROM abstract_enrichment").fetchone()[0], "EuropePMC")
            write_snapshot_previews(store.articles(), root / "outbox")
            index = root / "outbox" / "index.json"
            old_bytes, old_mtime = index.read_bytes(), index.stat().st_mtime_ns
            again = enrich_abstracts(store, cache)
            previews = write_snapshot_previews(store.articles(), root / "outbox")
            self.assertEqual((again.examined, client.requests, previews.updated), (0, 2, 0))
            self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), (old_bytes, old_mtime))

    def test_conflicting_secondary_identifier_cannot_supply_abstract(self):
        client = FakeClient({("PubMed", "pmid", PMID): AbstractResult("PubMed", "found", "Wrong synthetic abstract", "10.5555/conflict", PMID)})
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            store.ingest([Article("Synthetic title", doi=DOI, pmid=PMID)])
            stats = enrich_abstracts(store, CachedAbstracts(store, client=client, online=True))
            self.assertEqual((stats.failed, stats.enriched), (1, 0))
            self.assertIsNone(store.articles()[0].abstract_en)

    def test_missing_no_identifier_and_network_failure_are_distinct(self):
        client = FakeClient({("PubMed", "pmid", PMID): AbstractResult("PubMed", "failed", error_code="abstract_network_failed")})
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            store.ingest([Article("Synthetic missing", doi=DOI), Article("Synthetic no identifier"),
                          Article("Synthetic network", pmid=PMID)])
            stats = enrich_abstracts(store, CachedAbstracts(store, client=client, online=True))
            self.assertEqual((stats.missing, stats.no_identifier, stats.failed, stats.remaining), (1, 1, 1, 3))
            self.assertEqual(len(store.articles()), 3)

    def test_batch_limit_prioritizes_unchecked_articles(self):
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            store.ingest([Article("Synthetic first", doi=DOI), Article("Synthetic second", doi="10.5555/second")])
            cache = CachedAbstracts(store, client=FakeClient(), online=True)
            enrich_abstracts(store, cache, max_articles=1)
            enrich_abstracts(store, cache, max_articles=1)
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM abstract_enrichment").fetchone()[0], 2)

    def test_failed_article_transaction_keeps_cache_and_rolls_back_article(self):
        responses = {("Crossref", "doi", DOI): AbstractResult("Crossref", "found", "Synthetic abstract.", DOI)}
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            store.ingest([Article("Synthetic title", doi=DOI)])
            store.connection.execute("""CREATE TRIGGER synthetic_failure BEFORE INSERT ON abstract_enrichment
                BEGIN SELECT RAISE(ABORT, 'synthetic interruption'); END""")
            with self.assertRaises(StateError):
                enrich_abstracts(store, CachedAbstracts(store, client=FakeClient(responses), online=True))
            self.assertIsNone(store.articles()[0].abstract_en)
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM abstract_cache WHERE retry_at IS NULL").fetchone()[0], 1)

    def test_bounded_repeat_rotates_already_checked_missing_records(self):
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            store.ingest([Article("Synthetic first", doi=DOI), Article("Synthetic second", doi="10.5555/second"),
                          Article("Synthetic no identifier")])
            cache = CachedAbstracts(store, client=FakeClient(), online=True)
            for now in [100, 200, 300, 400]:
                cache.clock = lambda: now
                enrich_abstracts(store, cache, max_articles=1)
            rows = store.connection.execute("SELECT checked_at FROM abstract_enrichment ORDER BY checked_at").fetchall()
            self.assertEqual([row[0] for row in rows], [300, 400])
            self.assertEqual(cache.client.requests, 6)

    def test_cache_only_does_not_call_network_and_can_apply_existing_success(self):
        responses = {("Crossref", "doi", DOI): AbstractResult("Crossref", "found", "Synthetic abstract.", DOI)}
        with StateStore(self.local_dir() / "state.sqlite3") as store:
            store.ingest([Article("Synthetic title", doi=DOI)])
            online = CachedAbstracts(store, client=FakeClient(responses), online=True)
            online.lookup("Crossref", "doi", DOI)
            client = FakeClient()
            offline = CachedAbstracts(store, client=client)
            self.assertEqual(enrich_abstracts(store, offline).enriched, 1)
            self.assertEqual(client.requests, 0)

    def test_cli_default_is_offline_and_prints_only_statistics(self):
        root = self.local_dir()
        (root / ".env").write_text("synthetic secret must not be read", encoding="utf-8")
        with StateStore(root / "data" / "state.sqlite3") as store:
            store.ingest([Article("Synthetic private title must not be printed", doi=DOI)])
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr), patch.object(AbstractClient, "query", side_effect=AssertionError("network forbidden")):
            self.assertEqual(main([], project_root=root), 0)
        self.assertNotIn("Synthetic private", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(DOI, stdout.getvalue() + stderr.getvalue())
        self.assertIn("HTTP 请求 0", stdout.getvalue())

    def test_cli_rejects_sample_or_missing_database_paths(self):
        root = self.local_dir()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["--db", str(root / "data" / "samples" / "state.sqlite3")], project_root=root), 2)
            self.assertEqual(main([], project_root=root), 2)
