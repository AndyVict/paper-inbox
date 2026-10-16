"""合成期刊数据验证身份、缓存、冲突、状态保留和默认离线边界。"""
from hashlib import sha256
import io
import json
from unittest.mock import patch
from urllib.error import HTTPError

from tests.support import LocalFilesCase
from mail_literature.abstracts import AbstractError
from mail_literature.dedup import deduplicate
from mail_literature.delivery import render_message
from mail_literature.jcr.matching import JournalIndex
from mail_literature.jcr.storage import CATEGORY_FIELDS, JOURNAL_FIELDS, JcrError, write_csv, write_json
from mail_literature.jcr_enrichment import enrich_journals, write_match_report
from mail_literature.jcr_match_cli import main
from mail_literature.journal_fields import journal_identity_hash, normalize_issn
from mail_literature.journal_metadata import CachedJournals, JournalClient, JournalResult, parse_journal_response
from mail_literature.models import Article
from mail_literature.preview import OutboxError, articles_from_payload, render_preview, write_snapshot_previews
from mail_literature.state import StateError, StateStore

ISSN, EISSN, OTHER = '1234-5679', '2049-3630', '0007-9235'
DOI, PMID = '10.5555/synthetic-journal', '12345678'


def article():
    return deduplicate([Article('Synthetic public title', journal='Synthetic Journal', doi=DOI, pmid=PMID)])[0]


def metadata(entry, identifiers=None):
    values = identifiers or [ISSN, EISSN]
    return {'identity_hash': journal_identity_hash(entry), 'provider': 'PubMed',
            'source_url': 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id=' + PMID,
            'journal_title': 'Synthetic Journal', 'issns': values,
            'issn': values[0], 'eissn': values[1] if len(values) > 1 else None,
            'linking_issn': None, 'doi': DOI, 'pmid': PMID}


def make_snapshot(root, *, journals=None, categories=None, synthetic=False):
    path = root / 'data/jcr/synthetic-fixture-matching'
    path.mkdir(parents=True, exist_ok=True)
    scope = {'metric_year': '2025', 'release_year': '2026', 'snapshot_id': path.name}
    journals = journals or [{'journal_id': 'export:synthetic-a', 'journal_title': 'Synthetic Journal',
                            'journal_abbreviation': 'Synth J', 'issn': ISSN, 'eissn': EISSN, 'jif': '<0.1', 'jci': '0.2'}]
    categories = categories or [{'journal_id': 'export:synthetic-a', 'category_name': 'Synthetic Biology',
                                 'index_or_edition': 'SCIE', 'jif_quartile': 'Q2', 'jif_rank': '12',
                                 'jif_category_total': '50', 'jci_quartile': 'Q1', 'jci_rank': '1', 'jci_category_total': '50'},
                                {'journal_id': 'export:synthetic-a', 'category_name': 'Synthetic Materials',
                                 'index_or_edition': 'ESCI', 'jif_quartile': 'N/A', 'jif_rank': 'N/A',
                                 'jif_category_total': 'N/A', 'jci_quartile': 'Q4', 'jci_rank': '80', 'jci_category_total': '100'}]
    manifest = {**scope, 'metric_year': 2025, 'release_year': 2026, 'revision': 'synthetic-fixture-v1',
                'source_kind': 'synthetic_test_fixture', 'synthetic': synthetic, 'version_confirmed': False,
                'permission': {'full_access_confirmed': False, 'local_retention_confirmed': False}}
    write_json(path / 'snapshot.json', manifest)
    write_json(path / 'coverage_report.json', {**manifest, 'full_complete': False})
    write_csv(path / 'journals.csv', [{**dict.fromkeys(JOURNAL_FIELDS, ''), **scope, **row} for row in journals], JOURNAL_FIELDS)
    write_csv(path / 'journal_categories.csv', [{**dict.fromkeys(CATEGORY_FIELDS, ''), **scope, **row} for row in categories], CATEGORY_FIELDS)
    return path


def pubmed(*, doi=DOI, pmid=PMID, issn=ISSN):
    return f'''<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>{pmid}</PMID>
        <Article><Journal><ISSN IssnType="Print">{issn}</ISSN><Title>Synthetic Journal</Title></Journal></Article>
        <MedlineJournalInfo><ISSNLinking>{EISSN}</ISSNLinking></MedlineJournalInfo></MedlineCitation>
        <PubmedData><ArticleIdList><ArticleId IdType="doi">{doi}</ArticleId></ArticleIdList></PubmedData>
        </PubmedArticle></PubmedArticleSet>'''.encode('utf-8')


def crossref(*, doi=DOI, identifiers=None):
    return json.dumps({'status': 'ok', 'message-type': 'work', 'message': {'DOI': doi, 'type': 'journal-article',
        'container-title': ['Synthetic Journal'], 'ISSN': identifiers or [ISSN, EISSN],
        'issn-type': [{'type': 'print', 'value': ISSN}, {'type': 'electronic', 'value': EISSN}]}}).encode('utf-8')


class FakeClient:
    def __init__(self, result=None):
        self.requests = 0
        self.result = result

    def query(self, provider, kind, identifier):
        self.requests += 1
        return self.result or JournalResult(provider, 'found', 'Synthetic Journal', [ISSN, EISSN],
                                             ISSN, EISSN, None, DOI, PMID if kind == 'pmid' else None)


class JournalMetadataTests(LocalFilesCase):
    def test_normalization_requires_checksum_and_preserves_leading_zero(self):
        self.assertEqual(normalize_issn('00079235'), OTHER)
        self.assertEqual(normalize_issn('2049-3630'), EISSN)
        for value in ('1234-5678', 'not-an-issn', None, 12345679):
            self.assertIsNone(normalize_issn(value))

    def test_pubmed_gets_typed_and_linking_identifiers(self):
        result = parse_journal_response('PubMed', 'pmid', PMID, pubmed())
        self.assertEqual((result.status, result.issn, result.linking_issn), ('found', ISSN, EISSN))
        self.assertEqual(result.doi, DOI)

    def test_crossref_gets_print_and_electronic_identifiers(self):
        result = parse_journal_response('Crossref', 'doi', DOI, crossref())
        self.assertEqual((result.issn, result.eissn), (ISSN, EISSN))

    def test_wrong_public_identifiers_are_rejected(self):
        for provider, kind, identifier, raw in [('PubMed', 'pmid', PMID, pubmed(pmid='98765432')),
                                               ('Crossref', 'doi', DOI, crossref(doi='10.5555/other'))]:
            with self.subTest(provider=provider), self.assertRaisesRegex(AbstractError, 'journal_identity_mismatch'):
                parse_journal_response(provider, kind, identifier, raw)

    def test_invalid_issn_and_entity_payloads_are_failures(self):
        for raw in (pubmed(issn='1234-5678'), b'<!ENTITY x "private"><PubmedArticleSet/>', b'not xml'):
            with self.assertRaises(AbstractError):
                parse_journal_response('PubMed', 'pmid', PMID, raw)

    def test_no_issn_is_distinct_from_not_found(self):
        raw = json.dumps({'status': 'ok', 'message-type': 'work', 'message': {'DOI': DOI}}).encode()
        self.assertEqual(parse_journal_response('Crossref', 'doi', DOI, raw).status, 'no_issn')
        self.assertEqual(parse_journal_response('PubMed', 'pmid', PMID, b'<PubmedArticleSet/>').status, 'not_found')

    def test_http_failure_is_safe_and_stops_rate_limited_provider(self):
        def opener(*args, **kwargs):
            raise HTTPError('https://api.crossref.org', 429, 'private service text', {}, io.BytesIO(b'private'))
        client = JournalClient(opener=opener, sleep=lambda _: None)
        self.assertEqual(client.query('Crossref', 'doi', DOI).error_code, 'journal_rate_limited')
        self.assertEqual(client.query('Crossref', 'doi', DOI).error_code, 'journal_rate_limited')
        self.assertEqual(client.requests, 1)

    def test_multiple_print_issns_are_preserved_for_matching(self):
        value = json.loads(crossref())
        value['message']['issn-type'].append({'type': 'print', 'value': OTHER})
        value['message']['ISSN'].append(OTHER)
        result = parse_journal_response('Crossref', 'doi', DOI, json.dumps(value).encode())
        self.assertEqual(set(result.issns), {ISSN, EISSN, OTHER})
        self.assertIsNone(result.issn)
        self.assertEqual(result.eissn, EISSN)

    def test_cache_repeat_and_reopen_need_no_http(self):
        path = self.local_dir() / 'state.sqlite3'
        with StateStore(path) as store:
            client = FakeClient()
            cache = CachedJournals(store, client=client, online=True)
            self.assertEqual(cache.lookup('PubMed', 'pmid', PMID).status, 'found')
            cache.lookup('PubMed', 'pmid', PMID)
            self.assertEqual((client.requests, cache.hits), (1, 1))
        with StateStore(path) as store:
            cache = CachedJournals(store, client=FakeClient(), online=True)
            cache.lookup('PubMed', 'pmid', PMID)
            self.assertEqual((cache.client.requests, cache.hits), (0, 1))

    def test_negative_cache_expires_and_offline_never_fetches(self):
        with StateStore(self.local_dir() / 'state.sqlite3') as store:
            client = FakeClient(JournalResult('Crossref', 'not_found'))
            cache = CachedJournals(store, client=client, online=True, clock=lambda: 10)
            cache.lookup('Crossref', 'doi', DOI)
            cache.clock = lambda: 11
            cache.lookup('Crossref', 'doi', DOI)
            self.assertEqual(client.requests, 1)
            cache.clock = lambda: 10 + 7 * 86400
            cache.online = False
            self.assertIsNone(cache.lookup('Crossref', 'doi', DOI))
            self.assertEqual(client.requests, 1)
            cache.online = True
            cache.lookup('Crossref', 'doi', DOI)
            self.assertEqual(client.requests, 2)

    def test_request_budget_is_bounded(self):
        with StateStore(self.local_dir() / 'state.sqlite3') as store:
            cache = CachedJournals(store, client=FakeClient(), online=True, max_requests=2)
            cache.lookup('PubMed', 'pmid', PMID)
            self.assertIsNone(cache.lookup('Crossref', 'doi', DOI))
            self.assertEqual(cache.client.requests, 1)

    def test_corrupt_cache_is_rejected_without_overwrite(self):
        with StateStore(self.local_dir() / 'state.sqlite3') as store:
            cache = CachedJournals(store, client=FakeClient(), online=True)
            cache.lookup('PubMed', 'pmid', PMID)
            store.connection.execute("UPDATE metadata SET value='{}' WHERE key LIKE 'journal-cache-v2:%'")
            with self.assertRaisesRegex(StateError, 'invalid_journal_cache'):
                cache.lookup('PubMed', 'pmid', PMID)
            self.assertEqual(cache.client.requests, 1)


class JournalMatchingTests(LocalFilesCase):
    def test_complete_rank_is_not_given_a_second_denominator(self):
        index = JournalIndex(make_snapshot(self.local_dir()))
        entry = article()
        entry.journal_metadata = metadata(entry)
        entry.jcr_match = index.match(entry)
        for rank, total, expected in [('12/50', '50', '12/50'), ('=12/50', '', '=12/50'), ('12', '50', '12/50')]:
            with self.subTest(rank=rank, total=total):
                category = entry.jcr_match['categories'][0]
                category['jif_rank'], category['jif_category_total'] = rank, total
                text = render_preview(entry)
                self.assertIn('；排名 ' + expected + '\n', text)
                self.assertNotIn('12/50/50', text)

    def test_multi_category_match_preserves_jif_and_jci_and_na(self):
        index = JournalIndex(make_snapshot(self.local_dir()))
        entry = article()
        entry.journal_metadata = metadata(entry)
        entry.jcr_match = index.match(entry)
        self.assertEqual(entry.jcr_match['status'], 'matched')
        self.assertEqual(len(entry.jcr_match['categories']), 2)
        text = render_preview(entry)
        self.assertIn('来源/版本待核验', text)
        self.assertIn('文件 JIF：<0.1', text)
        self.assertIn('Synthetic Biology（SCIE） / Q2', text)
        self.assertIn('Synthetic Materials（ESCI） / N/A', text)
        self.assertIn('文件 JCI 分区', text)
        self.assertFalse(entry.jcr_match['full_complete'])
        message, _ = render_message(entry)
        self.assertIn('期刊名称：Synthetic Journal（JIF=<0.1）；2025 年指标\n', message)
        self.assertNotIn('来源/版本待核验', message)
        self.assertIn('JIF分区：Synthetic Biology（SCIE） / Q2；Synthetic Materials（ESCI） / N/A\n', message)
        self.assertIn('JCI 分区：Synthetic Biology（SCIE） / Q1；Synthetic Materials（ESCI） / Q4\n', message)
        self.assertNotIn('排名', message)
        self.assertNotIn('数据指纹', message)

    def test_delivery_rejects_stale_and_unmatched_jcr_metrics(self):
        index = JournalIndex(make_snapshot(self.local_dir()))
        entry = article()
        entry.impact_factor = '999'
        entry.quartile_claimed = '1区'
        entry.journal_metadata = metadata(entry)
        entry.jcr_match = index.match(entry)
        entry.journal = 'Changed journal'
        cases = [(entry, 'JCR 未核验')]
        for status in ('missing_issn', 'unmatched', 'ambiguous', 'conflict'):
            unmatched = article()
            unmatched.impact_factor = '999'
            unmatched.quartile_claimed = '1区'
            unmatched.journal_metadata = metadata(unmatched)
            unmatched.jcr_match = index.match(unmatched)
            unmatched.jcr_match.update(status=status, journal=None, categories=[])
            cases.append((unmatched, 'JCR 未核验（'))
        for candidate, note in cases:
            with self.subTest(status=candidate.jcr_match['status'], journal=candidate.journal):
                message, _ = render_message(candidate)
                self.assertIn('（JIF=未核验）', message)
                self.assertIn(note, message)
                self.assertIn('JIF分区：未核验\nJCI 分区：未核验\n', message)
                for obsolete in ('999', '<0.1', 'Q2', '1区'):
                    self.assertNotIn(obsolete, message)

    def test_delivery_missing_jif_does_not_use_mail_impact_factor(self):
        index = JournalIndex(make_snapshot(self.local_dir()))
        entry = article()
        entry.impact_factor = '999'
        entry.journal_metadata = metadata(entry)
        entry.jcr_match = index.match(entry)
        entry.jcr_match['journal']['jif'] = 'N/A'
        message, _ = render_message(entry)
        self.assertIn('（JIF=N/A）', message)
        self.assertNotIn('999', message)
        self.assertIn('Synthetic Materials（ESCI） / N/A', message)

    def test_name_only_is_a_candidate_never_a_match(self):
        index = JournalIndex(make_snapshot(self.local_dir()))
        result = index.match(article())
        self.assertEqual(result['status'], 'missing_issn')
        self.assertEqual(len(result['candidates']), 1)
        self.assertIsNone(result['journal'])

    def test_unknown_issn_keeps_unmatched(self):
        index = JournalIndex(make_snapshot(self.local_dir()))
        entry = article()
        entry.journal_metadata = metadata(entry, [OTHER])
        self.assertEqual(index.match(entry)['status'], 'unmatched')

    def test_duplicate_issn_is_ambiguous_not_first_match(self):
        journals = [{'journal_id': key, 'journal_title': key, 'issn': ISSN, 'eissn': EISSN, 'jif': '1', 'jci': '1'} for key in ('a', 'b')]
        categories = [{'journal_id': key, 'category_name': 'Test', 'index_or_edition': 'SCIE', 'jif_quartile': 'Q1'} for key in ('a', 'b')]
        index = JournalIndex(make_snapshot(self.local_dir(), journals=journals, categories=categories))
        entry = article()
        entry.journal_metadata = metadata(entry)
        self.assertEqual(index.match(entry)['status'], 'ambiguous')

    def test_conflicting_issns_do_not_match(self):
        journals = [{'journal_id': 'a', 'journal_title': 'A', 'issn': ISSN, 'eissn': '', 'jif': '1', 'jci': '1'},
                    {'journal_id': 'b', 'journal_title': 'B', 'issn': OTHER, 'eissn': '', 'jif': '1', 'jci': '1'}]
        categories = [{'journal_id': key, 'category_name': 'Test', 'index_or_edition': 'SCIE', 'jif_quartile': 'Q1'} for key in ('a', 'b')]
        index = JournalIndex(make_snapshot(self.local_dir(), journals=journals, categories=categories))
        entry = article()
        entry.journal_metadata = metadata(entry, [ISSN, OTHER])
        self.assertEqual(index.match(entry)['status'], 'conflict')

    def test_stale_metadata_and_match_are_not_rendered(self):
        index = JournalIndex(make_snapshot(self.local_dir()))
        entry = article()
        entry.journal_metadata = metadata(entry)
        entry.jcr_match = index.match(entry)
        entry.doi = '10.5555/changed'
        self.assertNotIn('文件 JIF', render_preview(entry))
        entry.clear_stale_journal_info()
        self.assertIsNone(entry.journal_metadata)
        self.assertIsNone(entry.jcr_match)

    def test_merge_keeps_current_fields_and_invalidates_on_journal_change(self):
        index = JournalIndex(make_snapshot(self.local_dir()))
        entry = article()
        entry.journal_metadata = metadata(entry)
        entry.jcr_match = index.match(entry)
        self.assertEqual(deduplicate([entry, article()])[0].jcr_match['status'], 'matched')
        incoming = article()
        incoming.journal = 'Different Journal'
        incoming.sources = entry.sources = []
        entry.journal = None
        merged = deduplicate([entry, incoming])[0]
        self.assertIsNone(merged.journal_metadata)
        self.assertIsNone(merged.jcr_match)

    def test_malformed_new_fields_are_rejected_at_state_boundary(self):
        value = article().to_dict()
        value['journal_metadata'] = {'issns': ['private']}
        with self.assertRaisesRegex(OutboxError, 'invalid_outbox_index'):
            articles_from_payload({'schema_version': 1, 'articles': [value]})

    def test_snapshot_rejects_synthetic_and_mixed_years(self):
        root = self.local_dir()
        path = make_snapshot(root, synthetic=True)
        with self.assertRaisesRegex(JcrError, 'jcr_real_snapshot_required'):
            JournalIndex(path)
        path = make_snapshot(root)
        text = (path / 'journals.csv').read_text(encoding='utf-8-sig').replace(',2025,2026,', ',2024,2026,')
        (path / 'journals.csv').write_text(text, encoding='utf-8-sig')
        with self.assertRaisesRegex(JcrError, 'mixed_jcr_snapshot_rows'):
            JournalIndex(path)

    def test_invalid_snapshot_issn_is_skipped_and_reported(self):
        root = self.local_dir()
        journals = [{'journal_id': 'export:synthetic-a', 'journal_title': 'Synthetic Journal', 'issn': '1234-5678', 'eissn': EISSN, 'jif': '1', 'jci': '1'}]
        index = JournalIndex(make_snapshot(root, journals=journals))
        self.assertEqual(index.invalid_identifiers, 1)
        self.assertNotIn('1234-5678', index.by_issn)

    def test_enrichment_preserves_translations_states_and_repeat_files(self):
        root = self.local_dir()
        index = JournalIndex(make_snapshot(root))
        with StateStore(root / 'state.sqlite3') as store:
            entry = article()
            entry.title_zh = '合成中文标题'
            entry.title_zh_source_hash = sha256(entry.title_en.encode()).hexdigest()
            store.ingest([entry])
            store.set_processing_state(entry.article_id, 'ready')
            recipient = sha256(b'synthetic-recipient').hexdigest()
            store.record_delivery(entry.article_id, recipient, 'accepted')
            cache = CachedJournals(store, client=FakeClient(), online=True)
            self.assertEqual(enrich_journals(store, cache, index).enriched, 1)
            entries = store.articles()
            self.assertEqual(entries[0].title_zh, '合成中文标题')
            self.assertEqual(store.delivery_status(entry.article_id, recipient), 'accepted')
            self.assertEqual(store.connection.execute('SELECT processing_state FROM articles').fetchone()[0], 'ready')
            write_match_report(entries, index, root / 'reports')
            write_snapshot_previews(entries, root / 'outbox')
            files = list((root / 'reports').iterdir()) + list((root / 'outbox/messages').iterdir())
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
            stats = enrich_journals(store, cache, index)
            self.assertEqual((stats.updated, cache.client.requests), (0, 1))
            write_match_report(store.articles(), index, root / 'reports')
            self.assertEqual(write_snapshot_previews(store.articles(), root / 'outbox').unchanged, 1)
            self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files})

    def test_public_doi_pmid_conflict_is_not_applied(self):
        root = self.local_dir()
        index = JournalIndex(make_snapshot(root))
        with StateStore(root / 'state.sqlite3') as store:
            store.ingest([article()])
            result = JournalResult('PubMed', 'found', 'Synthetic Journal', [ISSN], ISSN, doi='10.5555/other', pmid=PMID)
            stats = enrich_journals(store, CachedJournals(store, client=FakeClient(result), online=True), index)
            self.assertEqual(stats.failed, 1)
            self.assertIsNone(store.articles()[0].journal_metadata)
            self.assertEqual(store.articles()[0].jcr_match['status'], 'conflict')

    def test_expired_failure_cache_offline_keeps_unresolved_conflict(self):
        root = self.local_dir()
        index = JournalIndex(make_snapshot(root))
        with StateStore(root / 'state.sqlite3') as store:
            store.ingest([article()])
            cache = CachedJournals(store, client=FakeClient(JournalResult('PubMed', 'failed', error_code='journal_identity_mismatch')),
                                   online=True, clock=lambda: 0)
            enrich_journals(store, cache, index)
            cache.online, cache.clock = False, lambda: 3601
            enrich_journals(store, cache, index)
            result = store.articles()[0].jcr_match
            self.assertEqual((result['status'], result['reason']), ('conflict', 'journal_identity_mismatch'))
            self.assertEqual(cache.client.requests, 1)

    def test_request_budget_keeps_unresolved_public_conflict(self):
        root = self.local_dir()
        index = JournalIndex(make_snapshot(root))
        with StateStore(root / 'state.sqlite3') as store:
            store.ingest([article()])
            cache = CachedJournals(store, client=FakeClient(JournalResult('PubMed', 'failed', error_code='journal_identity_mismatch')),
                                   online=True, clock=lambda: 0)
            enrich_journals(store, cache, index)
            cache.clock, cache.max_requests = lambda: 3601, 2
            enrich_journals(store, cache, index)
            self.assertEqual(store.articles()[0].jcr_match['reason'], 'journal_identity_mismatch')
            self.assertEqual(cache.client.requests, 1)

    def test_negative_cache_does_not_starve_next_article_with_small_batch(self):
        root = self.local_dir()
        index = JournalIndex(make_snapshot(root))
        class SelectiveClient(FakeClient):
            def query(self, provider, kind, identifier):
                self.requests += 1
                if identifier == '10.5555/no-issn':
                    return JournalResult(provider, 'no_issn', doi=identifier)
                return JournalResult(provider, 'found', 'Synthetic Journal', [ISSN], ISSN, doi=identifier)
        with StateStore(root / 'state.sqlite3') as store:
            first = Article('Synthetic missing journal', doi='10.5555/no-issn')
            second = Article('Synthetic valid journal', doi=DOI)
            store.ingest([first, second])
            cache = CachedJournals(store, client=SelectiveClient(), online=True)
            enrich_journals(store, cache, index, max_articles=1)
            self.assertEqual([entry.jcr_match['status'] for entry in store.articles()], ['missing_issn', 'missing_issn'])
            enrich_journals(store, cache, index, max_articles=1)
            self.assertEqual([entry.jcr_match['status'] for entry in store.articles()], ['missing_issn', 'matched'])
            self.assertEqual(cache.client.requests, 2)

    def test_retry_network_failure_counts_old_conflict_once(self):
        root = self.local_dir()
        index = JournalIndex(make_snapshot(root))
        class NetworkFailed(FakeClient):
            def query(self, provider, kind, identifier):
                self.requests += 1
                return JournalResult(provider, 'failed', error_code='journal_network_failed')
        with StateStore(root / 'state.sqlite3') as store:
            store.ingest([article()])
            cache = CachedJournals(store, client=FakeClient(JournalResult('PubMed', 'failed', error_code='journal_identity_mismatch')),
                                   online=True, clock=lambda: 0)
            enrich_journals(store, cache, index)
            cache.client, cache.clock = NetworkFailed(), lambda: 3601
            stats = enrich_journals(store, cache, index)
            self.assertEqual(stats.failed, 1)
            self.assertEqual(store.articles()[0].jcr_match['reason'], 'journal_identity_mismatch')

    def test_no_identifier_keeps_candidate_and_report(self):
        root = self.local_dir()
        index = JournalIndex(make_snapshot(root))
        with StateStore(root / 'state.sqlite3') as store:
            entry = article()
            entry.doi = entry.pmid = None
            store.ingest([entry])
            cache = CachedJournals(store, client=FakeClient(), online=True)
            self.assertEqual(enrich_journals(store, cache, index).no_identifier, 1)
            self.assertEqual(cache.client.requests, 0)
            self.assertEqual(store.articles()[0].jcr_match['reason'], 'missing_public_identifier')

    def test_cli_default_offline_and_backup_once(self):
        root = self.local_dir()
        path = make_snapshot(root)
        with StateStore(root / 'data/state.sqlite3') as store:
            store.ingest([article()])
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('unexpected network')), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(['--snapshot', path.name], project_root=root), 0)
            self.assertEqual(main(['--snapshot', path.name], project_root=root), 0)
        self.assertEqual(len(list((root / 'data/backups').glob('pre-jcr-matching-*.sqlite3'))), 1)
        self.assertTrue((root / 'data/jcr-matching/match_report.json').is_file())

    def test_cli_rejects_output_into_jcr_source_and_private_paths(self):
        root = self.local_dir()
        path = make_snapshot(root)
        with StateStore(root / 'data/state.sqlite3') as store:
            store.ingest([article()])
        for option, folder in (('--outbox-dir', 'jcr'), ('--report-dir', 'private'), ('--outbox-dir', 'samples')):
            with self.subTest(folder=folder), patch('sys.stderr', new_callable=io.StringIO):
                self.assertEqual(main(['--snapshot', path.name, option, str(root / 'data' / folder)], project_root=root), 2)
        self.assertFalse((root / 'data/backups').exists())
