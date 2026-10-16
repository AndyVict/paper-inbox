"""全部夹具合成；不访问真实邮箱、密钥或网络。"""
from copy import deepcopy
import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from tests.support import LocalFilesCase
from mail_literature.jcr.client import JournalsClient
from mail_literature.jcr.normalize import api_record, web_export
from mail_literature.jcr.report import ckey, make_report, export_tables
from mail_literature.jcr.storage import (JcrError, initialize, locked, read_csv, read_json,
                                        sanitize, snapshot_path, valid_issn, write_json)
from mail_literature.jcr.workflow import collect, import_web, import_mapped, preflight


def manifest(synthetic=True):
    return {'snapshot_id': 'synthetic-jcr-fixture', 'metric_year': 2025, 'release_year': 2026,
            'revision': 'synthetic-v1', 'version_confirmed': True, 'source_kind': 'synthetic',
            'synthetic': synthetic, 'permission': {'full_access_confirmed': True,
                 'local_retention_confirmed': True, 'evidence': 'SYNTHETIC permission fixture',
                 'indices': ['SCIE', 'SSCI', 'AHCI', 'ESCI']}}


def fixtures(jid='SYNTHETIC_0', category='SYNTHETIC CATEGORY 0'):
    detail = {'id': jid, 'name': 'Synthetic Journal ' + jid, 'jcrTitle': jid,
              'issn': '0007-9235', 'eIssn': '1542-4863',
              'publisher': {'name': 'Synthetic Publisher', 'countryRegion': 'SYNTHETIC'},
              'categories': [{'name': category, 'edition': 'SCIE', 'url': '/categories/SYN0'}],
              'newMetadata': {'someNewField': 'retained'}}
    rank = {'category': category, 'edition': 'SCIE', 'self': '/categories/SYN0',
            'rank': '2/10', 'quartile': 'Q1', 'jifPercentile': '85.000'}
    report = {'year': 2025, 'journal': {'id': jid}, 'suppressed': False,
              'metrics': {'impactMetrics': {'jif': '<0.1', 'jif5Years': '0.200', 'jci': '1.230', 'totalCites': 123},
                          'influenceMetrics': {'eigenFactor': {'score': '0.01000'}, 'articleInfluence': '0.5'},
                          'newMetric': {'officialUnknown': '7.0000'}},
              'ranks': {'jif': [rank], 'jci': [dict(rank)]}}
    return detail, report


def export_bytes():
    return ('"Selected JCR Year: 2025 Selected Editions: SCIE;SSCI;AHCI;ESCI"\n\n'
            'Journal name,JCR Abbreviation,Publisher,ISSN,eISSN,Category,Edition,Total Citations,2025 JIF,JIF Quartile,2025 JCI,% of Citable OA\n'
            '"Synthetic Journal","SYN JOURNAL","Synthetic",0007-9235,1542-4863,"Synthetic Category A",SCIE,"1,234",<0.1,Q1,1.230,12%,\n'
            '"Synthetic Journal","SYN JOURNAL","Synthetic",0007-9235,1542-4863,"Synthetic Category B",SSCI,"1,234",<0.1,N/A,1.230,12%,\n'
            'Synthetic footer\n').encode('utf-8-sig')


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(json.dumps(body).encode('utf-8'))
        self.headers = headers or {}


class Transport:
    def __init__(self):
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append(request.full_url)
        endpoint = request.full_url.split('/v1')[1]
        if endpoint.startswith('/journals?'):
            return Response({'metadata': {'total': 2, 'page': 1, 'limit': 50},
                             'hits': [{'id': 'SYNTHETIC_0'}, {'id': 'SYNTHETIC_1'}]})
        jid = endpoint.split('/')[2]
        detail, report = fixtures(jid)
        return Response(report if '/reports/year/' in endpoint else detail)


class JcrTests(LocalFilesCase):
    def setUp(self):
        self.path = self.local_dir()
        self.manifest = initialize(self.path, manifest())

    def test_issn_check_digit_leading_zero_and_x(self):
        self.assertTrue(valid_issn('0007-9235'))
        self.assertTrue(valid_issn('1474-547X'))
        self.assertFalse(valid_issn('0007-9236'))
        self.assertFalse(valid_issn('007-9235'))

    def test_raw_metric_values_unknown_fields_and_two_ranks(self):
        j, cs = api_record(*fixtures(), self.manifest)
        self.assertEqual(j['jif'], '<0.1')
        self.assertEqual(j['five_year_jif'], '0.200')
        self.assertEqual(j['official_report__metrics__newMetric__officialUnknown'], '7.0000')
        self.assertEqual(cs[0]['jci_quartile'], 'Q1')
        self.assertEqual(cs[0]['jif_rank'], '2/10')
        self.assertEqual(cs[0]['jif_category_total'], '10')

    def test_missing_not_q4_and_metadata_categories_remain(self):
        detail, report = fixtures()
        detail['categories'].append({'name': 'SYNTHETIC UNRANKED', 'edition': 'AHCI'})
        report['ranks'] = {}
        j, cs = api_record(detail, report, self.manifest)
        self.assertEqual(len(cs), 2)
        self.assertTrue(all(c['jif_quartile'] == c['jci_quartile'] == '' for c in cs))
        self.assertTrue(all(c['field_missing_reasons'] for c in cs))

    def test_official_footer_and_multiedition_are_preserved(self):
        raw = export_bytes().replace(b'Synthetic footer', b'Copyright (c) 2026 Clarivate').replace(b',SCIE,', b',"SCIE, SSCI",')
        js, cs, failures, metadata = web_export(raw, self.manifest, '')
        self.assertFalse(failures)
        self.assertEqual(js[0]['citation_indices'], 'SCIE;SSCI')
        self.assertEqual(cs[0]['index_or_edition'], 'SCIE, SSCI')
        self.assertEqual(len(metadata['source_footers']), 1)

    def test_missing_issn_marker_is_not_bad_check_digit(self):
        j, cs = api_record(*fixtures(), self.manifest)
        j['issn'] = 'N/A'
        r = make_report(self.path, self.manifest, [j], cs)
        self.assertFalse(r['invalid_issns'])
        self.assertEqual(len(r['missing_issn_markers']), 1)

    def test_mixed_csv_snapshot_blocked(self):
        j, cs = api_record(*fixtures(), self.manifest)
        cs[0]['metric_year'] = 2024
        r = make_report(self.path, self.manifest, [j], cs)
        self.assertIn('mixed_row_year_or_snapshot', r['blockers'])

    def test_api_year_and_identity_mismatch_refused(self):
        detail, report = fixtures()
        for field, value in [('year', 2024), ('journal', {'id': 'OTHER_SYNTHETIC'})]:
            bad = deepcopy(report)
            bad[field] = value
            with self.assertRaises(JcrError):
                api_record(detail, bad, self.manifest)

    def test_duplicate_rank_not_silently_removed(self):
        detail, report = fixtures()
        report['ranks']['jif'] *= 2
        with self.assertRaisesRegex(JcrError, 'duplicate_api_category_rank'):
            api_record(detail, report, self.manifest)

    def test_web_export_multicategory_and_raw_values(self):
        js, cs, failures, metadata = web_export(export_bytes(), self.manifest, 'https://example.invalid/export')
        self.assertEqual(len(js), 1)
        self.assertEqual(len(cs), 2)
        self.assertEqual(js[0]['issn'], '0007-9235')
        self.assertEqual(js[0]['total_citations'], '1,234')
        self.assertEqual(js[0]['jif'], '<0.1')
        self.assertEqual(cs[1]['jif_quartile'], 'N/A')
        self.assertTrue(js[0]['journal_id'].startswith('export:'))
        self.assertEqual(failures, [{'row': 6, 'code': 'short_or_footer_row'}])
        self.assertEqual(js[0]['export__% of Citable OA'], '12%')

    def test_wrong_export_year_refused(self):
        with self.assertRaisesRegex(JcrError, 'export_metric_year_mismatch'):
            web_export(export_bytes().replace(b'2025', b'2024'), self.manifest, '')

    def test_conflicting_export_values_recorded(self):
        raw = export_bytes().replace(b'SSCI,"1,234",<0.1', b'SSCI,"9,999",<0.1')
        js, cs, failures, _ = web_export(raw, self.manifest, '')
        self.assertIn('conflicting_journal_values', [v['code'] for v in failures])
        self.assertEqual(len(cs), 2)

    def test_bom_csv_roundtrip_nested_values(self):
        j, cs = api_record(*fixtures(), self.manifest)
        export_tables(self.path, self.manifest, [j], cs)
        for filename in ('journals.csv', 'journal_categories.csv'):
            self.assertTrue((self.path / filename).read_bytes().startswith(b'\xef\xbb\xbf'))
        self.assertEqual(read_csv(self.path / 'journals.csv')[0]['jif'], '<0.1')

    def test_changed_import_rejected_and_history_preserved(self):
        source = self.path / 'synthetic.csv'
        source.write_bytes(export_bytes())
        import_web(self.path, self.manifest, source, 'https://example.invalid/export')
        prior = (self.path / 'journals.csv').read_bytes()
        source.write_bytes(export_bytes() + b'\n')
        with self.assertRaisesRegex(JcrError, 'import_source_changed'):
            import_web(self.path, self.manifest, source, '')
        self.assertEqual(prior, (self.path / 'journals.csv').read_bytes())

    def test_snapshot_revision_conflict(self):
        altered = dict(self.manifest, revision='synthetic-v2')
        with self.assertRaisesRegex(JcrError, 'snapshot_version_conflict'):
            initialize(self.path, altered)

    def test_lock_prevents_concurrent_writer(self):
        with locked(self.path):
            with self.assertRaisesRegex(JcrError, 'snapshot_locked'):
                with locked(self.path):
                    pass
        self.assertFalse((self.path / '.jcr.lock').exists())

    def test_path_traversal_refused(self):
        for value in ('../state', 'a/b', '..', 'x\\state'):
            with self.assertRaises(JcrError):
                snapshot_path(self.path, value)

    def test_cache_replay_no_http_and_sensitive_echo_removed(self):
        calls = []
        def transport(req, timeout):
            calls.append(req)
            return Response({'id': 'SYN', 'keyEcho': 'SYNTHETIC_SECRET',
                             'url': 'https://user:pass@example.invalid/path?APIKEY=SYNTHETIC_SECRET',
                             'cookie': 'SYNTHETIC_COOKIE', 'X-ApiKey': 'SYNTHETIC_SECRET'})
        client = JournalsClient(self.path, 'SYNTHETIC_SECRET', transport=transport, sleep=lambda n: None)
        response = client.request('/journals/SYN')
        client.request('/journals/SYN')
        self.assertEqual(len(calls), 1)
        self.assertEqual(response['body']['url'], 'https://example.invalid/path')
        self.assertNotIn('cookie', response['body'])
        self.assertNotIn('SYNTHETIC_SECRET', ''.join(p.read_text(encoding='utf-8') for p in (self.path/'cache').glob('*.json')))

    def test_cache_corruption_refused(self):
        client = JournalsClient(self.path, 'SYNTHETIC', transport=Transport())
        client.request('/journals/SYN')
        file = next((self.path / 'cache').glob('*.json'))
        envelope = read_json(file)
        envelope['body']['id'] = 'CORRUPTED'
        write_json(file, envelope)
        with self.assertRaisesRegex(JcrError, 'cache_integrity_failed'):
            client.request('/journals/SYN')

    def test_401_403_429_stop_and_no_raw_error_log(self):
        for status in (401, 403, 429):
            def transport(req, timeout):
                raise HTTPError(req.full_url, status, 'SYNTHETIC_SECRET_ECHO', {}, None)
            client = JournalsClient(self.path, 'SYNTHETIC_SECRET_ECHO', transport=transport)
            with self.assertRaisesRegex(JcrError, 'http_' + str(status)):
                client.request('/journals/SYN')
            with self.assertRaises(JcrError):
                client.request('/journals/OTHER')
            self.assertEqual(client.requests, 1)
            self.assertNotIn('SYNTHETIC_SECRET_ECHO', (self.path / 'failures.json').read_text(encoding='utf-8'))

    def test_budget_stops_and_throttle_has_lower_bound(self):
        client = JournalsClient(self.path, 'SYNTHETIC', max_requests=1, transport=Transport())
        client.request('/journals/SYN')
        with self.assertRaisesRegex(JcrError, 'request_budget_reached'):
            client.request('/journals/OTHER')
        with self.assertRaises(JcrError):
            JournalsClient(self.path, 'SYNTHETIC', interval=0.19)

    def test_quota_zero_stops_next_http(self):
        def transport(req, timeout):
            return Response({'id': 'SYN'}, {'X-RateLimit-Remaining': '0'})
        client = JournalsClient(self.path, 'SYNTHETIC', transport=transport)
        client.request('/journals/SYN')
        with self.assertRaisesRegex(JcrError, 'api_quota_exhausted'):
            client.request('/journals/OTHER')
        self.assertEqual(client.requests, 1)

    def test_transient_failure_can_resume_and_history_kept(self):
        def offline(req, timeout):
            raise URLError('SYNTHETIC error')
        bad = JournalsClient(self.path, 'SYNTHETIC', transport=offline)
        with self.assertRaises(JcrError):
            bad.request('/journals/SYN')
        good = JournalsClient(self.path, 'SYNTHETIC', transport=Transport())
        good.request('/journals/SYN')
        self.assertTrue(all(v.get('resolved_at') for v in read_json(self.path/'failures.json').values()))

    def test_preflight_and_budget_resume_all_categories(self):
        transport = Transport()
        config = {'quota_confirmed': True, 'quota_evidence': 'SYNTHETIC', 'max_requests_per_run': 3,
                  'request_interval_seconds': 0.21}
        result = preflight(self.path, self.manifest, config, 'UNIQUE_TEST_CREDENTIAL_ABC123', transport)
        self.assertEqual(result['catalog_total'], 2)
        self.assertEqual(result['estimated_requests_including_catalog_recheck'], 6)
        config['max_requests_per_run'] = 1
        report, checkpoint = collect(self.path, self.manifest, config, 'UNIQUE_TEST_CREDENTIAL_ABC123', transport)
        self.assertEqual(checkpoint['state'], 'paused_budget')
        self.assertEqual(report['journal_rows'], 1)
        config['max_requests_per_run'] = 10
        report, checkpoint = collect(self.path, self.manifest, config, 'UNIQUE_TEST_CREDENTIAL_ABC123', transport)
        self.assertEqual(checkpoint['state'], 'collected')
        self.assertEqual(report['journal_rows'], 2)
        self.assertFalse(report['full_complete'])
        previous_calls = len(transport.calls)
        report, checkpoint = collect(self.path, self.manifest, config, 'UNIQUE_TEST_CREDENTIAL_ABC123', transport)
        self.assertEqual(len(transport.calls), previous_calls)
        self.assertEqual(checkpoint['requests'], 0)

    def test_404_journal_stays_and_other_journals_continue(self):
        transport = Transport()
        config = {'quota_confirmed': True, 'quota_evidence': 'SYNTHETIC', 'max_requests_per_run': 10,
                  'request_interval_seconds': 0.21}
        preflight(self.path, self.manifest, config, 'UNIQUE_TEST_CREDENTIAL_ABC123', transport)
        def unavailable(req, timeout):
            if '/journals/SYNTHETIC_1/reports/' in req.full_url:
                raise HTTPError(req.full_url, 404, 'SYNTHETIC', {}, None)
            return transport(req, timeout)
        report, checkpoint = collect(self.path, self.manifest, config, 'UNIQUE_TEST_CREDENTIAL_ABC123', unavailable)
        self.assertEqual(report['journal_rows'], 2)
        rows = read_csv(self.path/'journals.csv')
        self.assertEqual(rows[1]['record_status'], 'report_unavailable_http404')
        self.assertEqual(rows[1]['jif'], '')
        self.assertFalse(report['full_complete'])
        report, checkpoint = collect(self.path, self.manifest, config, 'UNIQUE_TEST_CREDENTIAL_ABC123', transport)
        self.assertEqual(read_csv(self.path/'journals.csv')[1]['jif'], '<0.1')
        self.assertFalse(report['unresolved_failures'])

    def test_pagination_changed_on_final_recheck_stops(self):
        transport = Transport()
        config = {'quota_confirmed': True, 'quota_evidence': 'SYNTHETIC', 'max_requests_per_run': 10,
                  'request_interval_seconds': 0.21}
        preflight(self.path, self.manifest, config, 'UNIQUE_TEST_CREDENTIAL_ABC123', transport)
        def changed(req, timeout):
            if '/journals?' in req.full_url:
                return Response({'metadata': {'total': 3, 'page': 1, 'limit': 50},
                                 'hits': [{'id': 'SYNTHETIC_0'}, {'id': 'SYNTHETIC_1'}, {'id': 'NEW_SYNTHETIC'}]})
            return transport(req, timeout)
        report, checkpoint = collect(self.path, self.manifest, config, 'UNIQUE_TEST_CREDENTIAL_ABC123', changed)
        self.assertEqual(checkpoint['code'], 'catalog_changed_create_new_snapshot')
        self.assertFalse(report['full_complete'])

    def test_api_refused_without_specific_license_and_quota(self):
        config = {'quota_confirmed': True, 'quota_evidence': 'SYNTHETIC', 'max_requests_per_run': 3}
        bad = deepcopy(self.manifest)
        bad['permission']['indices'] = ['OTHER']
        with self.assertRaisesRegex(JcrError, 'full_api_authorization'):
            preflight(self.path, bad, config, 'SYNTHETIC', Transport())
        with self.assertRaisesRegex(JcrError, 'quota_confirmation'):
            preflight(self.path, self.manifest, {}, 'SYNTHETIC', Transport())

    def test_same_year_wrong_revision_reference_not_accepted(self):
        j, cs = api_record(*fixtures(), self.manifest)
        ref = dict(self.manifest, official=True, complete=True, journal_ids=[j['journal_id']],
                   indices=['SCIE', 'SSCI', 'AHCI', 'ESCI'], source_url='https://example.invalid', evidence='SYNTHETIC', revision='wrong')
        r = make_report(self.path, self.manifest, [j], cs, reference=ref)
        self.assertFalse(r['official_universe_verified'])
        self.assertIsNone(r['journal_coverage_percent'])

    def test_duplicates_orphans_and_invalid_issn_detected(self):
        j, cs = api_record(*fixtures(), self.manifest)
        j['issn'] = '0007-9236'
        orphan = dict(cs[0], journal_id='SYNTHETIC_ORPHAN')
        r = make_report(self.path, self.manifest, [j, j], cs + cs + [orphan])
        self.assertTrue(r['duplicate_journal_keys'])
        self.assertTrue(r['duplicate_category_keys'])
        self.assertTrue(r['invalid_issns'])
        self.assertEqual(r['orphan_category_journals'], ['SYNTHETIC_ORPHAN'])

    def test_full_gate_requires_reference_metrics_sampling_and_real_data(self):
        js, cs = [], []
        m = manifest(synthetic=False)  # 合成夹具模拟正式输入，产物仅在 data/test-runs。
        samples = []
        for index in range(10):
            jid, cname = 'SYNTHETIC_' + str(index), 'SYNTHETIC CATEGORY ' + str(index)
            j, categories = api_record(*fixtures(jid, cname), m)
            js.append(j)
            cs.extend(categories)
            samples.append({**{k: m[k] for k in ('snapshot_id', 'metric_year', 'release_year', 'revision')},
                            'journal_id': jid, 'categories': [cname], 'passed': True,
                            'identity_checked': True, 'jif_checked': True, 'all_categories_checked': True, 'ranks_checked': True,
                            'independent_evidence': 'SYNTHETIC independent fixture',
                            'cases': ['multi_category', 'multiple_issn', 'unranked_or_missing']})
        ref = {**{k: m[k] for k in ('snapshot_id', 'metric_year', 'release_year', 'revision')},
               'official': True, 'complete': True, 'source_url': 'https://example.invalid/reference',
               'evidence': 'SYNTHETIC', 'indices': ['SCIE', 'SSCI', 'AHCI', 'ESCI'],
               'journal_ids': [j['journal_id'] for j in js], 'category_keys': [ckey(c) for c in cs]}
        self.assertTrue(make_report(self.path, m, js, cs, reference=ref, samples=samples)['full_complete'])
        self.assertFalse(make_report(self.path, dict(m, synthetic=True), js, cs, reference=ref, samples=samples)['full_complete'])
        js[0]['jci'] = ''
        js[0]['official_report__metrics__impactMetrics__jci'] = ''
        self.assertFalse(make_report(self.path, m, js, cs, reference=ref, samples=samples)['full_complete'])
        js[0]['field_missing_reasons'] = {'jci': 'official: not applicable; synthetic source evidence',
                                         'official_report__metrics__impactMetrics__jci': 'official: not applicable; synthetic source evidence'}
        self.assertTrue(make_report(self.path, m, js, cs, reference=ref, samples=samples)['full_complete'])
        self.assertFalse(make_report(self.path, m, js, cs, reference=ref, samples=samples[:9])['full_complete'])

    def test_core_value_must_match_retained_source_metric(self):
        j, cs = api_record(*fixtures(), self.manifest)
        j['jif'] = '0'
        report = make_report(self.path, self.manifest, [j], cs)
        self.assertIn('normalized_source_value_mismatch', report['blockers'])

    def test_institution_mapping_keeps_extra_fields(self):
        journal = self.path / 'input-j.csv'
        category = self.path / 'input-c.csv'
        journal.write_text('ID,Year,Name,New metric\nSYN,2025,Synthetic,7.000\n', encoding='utf-8-sig')
        category.write_text('ID,Year,Category,Edition\nSYN,2025,Synthetic Category,SCIE\n', encoding='utf-8-sig')
        mapping = {'journals': {'journal_id': 'ID', 'metric_year': 'Year', 'journal_title': 'Name'},
                   'categories': {'journal_id': 'ID', 'metric_year': 'Year', 'category_name': 'Category', 'index_or_edition': 'Edition'}}
        report = import_mapped(self.path, self.manifest, journal, category, mapping)
        self.assertEqual(report['journal_rows'], 1)
        self.assertEqual(read_csv(self.path/'journals.csv')[0]['export__New metric'], '7.000')
        self.assertFalse(report['full_complete'])
