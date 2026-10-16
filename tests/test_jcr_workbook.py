"""JCR XLSX 夹具完全合成，不读取用户工作簿或网络。"""
from collections import Counter
from copy import deepcopy
import json
import xml.etree.ElementTree as ET
from zipfile import ZipFile
from contextlib import redirect_stdout
import io
from unittest.mock import patch
from tests.support import LocalFilesCase
from mail_literature.jcr.storage import JcrError, initialize, read_csv, read_json
from mail_literature.jcr.workbook import (JOURNAL_MAP, CATEGORY_MAP, JSON_MAP,
                                         normalize_workbook, import_workbook)
from mail_literature.jcr.xlsx_reader import read_workbook, column_index, cell_text
from mail_literature.jcr.workbook_audit import audit_workbook

S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P = 'http://schemas.openxmlformats.org/package/2006/relationships'


def manifest():
    return {'snapshot_id': 'synthetic-workbook-test', 'metric_year': 2025, 'release_year': 2026,
            'revision': 'SYNTHETIC', 'version_confirmed': False, 'synthetic': True,
            'source_kind': 'synthetic_workbook', 'headline_release_journal_count': 2,
            'permission': {'user_authorized_local_processing': True, 'full_access_confirmed': False}}


def tables():
    js, cs = [], []
    for index in range(2):
        j = {v.format(year=2025): '2.300' for v in JOURNAL_MAP.values()}
        j.update({'Rank': str(index+1), 'Journal name': f'Synthetic Journal {index}',
                  'Abbreviated journal': f'SYN {index}', 'ISSN': '0007-9235', 'eISSN': '1542-4863',
                  'Publisher': 'Synthetic Publisher', 'JCR year': '2025', '2025 JIF': '<0.1' if index == 0 else 'N/A',
                  'Editions': 'SCIE;SSCI' if index == 0 else 'AHCI', 'Categories': 'Multiple',
                  'Future metric': '7.0000', 'Category quartiles JSON': ''})
        entries = []
        for offset in range(2 if index == 0 else 1):
            c = {column: '' for column in CATEGORY_MAP.values()}
            c.update({'Category': f'SYN CATEGORY {index}-{offset}', 'Edition': 'SCIE' if index == 0 else 'AHCI'})
            for kind in ('JIF', 'JCI', 'AIS'):
                c[kind+' rank'] = '1/20' if index == 0 else 'N/A'
                c[kind+' quartile'] = 'Q1' if index == 0 else 'N/A'
            c['JIF percentile'] = c['JCI percentile'] = '99.000' if index == 0 else 'N/A'
            c['5-year JIF quartile'] = 'Q1' if index == 0 else 'N/A'
            for field in ('2025 JIF', '5-year JIF', 'JCI', 'Journal name', 'ISSN', 'eISSN'):
                c[field] = j[field]
            c['Rank'] = str(500+len(cs))  # 分类表展示顺序不等于主表排序。
            entries.append({key: c[CATEGORY_MAP[field]] for field, key in JSON_MAP.items()})
            cs.append({'row_number': len(cs)+2, 'values': c})
        j['Category quartiles JSON'] = json.dumps(entries)
        js.append({'row_number': index+2, 'values': j})
    summary = [{'row_number': i+2, 'values': {'Category': c['values']['Category'], 'Journal-category records': '1'}}
               for i, c in enumerate(cs)]
    return {name: {'headers': list(records[0]['values']), 'records': records, 'state': 'visible'}
            for name, records in [('Journals', js), ('Category_Quartiles', cs), ('Category_Summary', summary)]}


def column_name(index):
    result = ''
    index += 1
    while index:
        index, remainder = divmod(index-1, 26)
        result = chr(65+remainder) + result
    return result


def write_fixture(path, source_tables, shared=True, formula=False, cell_error=False):
    workbook = ET.Element('{' + S + '}workbook')
    sheets = ET.SubElement(workbook, '{' + S + '}sheets')
    rels = ET.Element('{' + P + '}Relationships')
    shared_root, strings = ET.Element('{' + S + '}sst'), []
    with ZipFile(path, 'w') as archive:
        for number, (name, table) in enumerate(source_tables.items(), 1):
            ET.SubElement(sheets, '{' + S + '}sheet', {'name': name, 'sheetId': str(number), '{'+R+'}id': f'rId{number}', 'state': table.get('state', 'visible')})
            ET.SubElement(rels, '{' + P + '}Relationship', {'Id': f'rId{number}', 'Target': f'worksheets/sheet{number}.xml'})
            doc = ET.Element('{' + S + '}worksheet')
            data = ET.SubElement(doc, '{' + S + '}sheetData')
            all_rows = [(1, table['headers'])] + [(item['row_number'], [item['values'].get(h, '') for h in table['headers']]) for item in table['records']]
            for row_number, values in all_rows:
                row = ET.SubElement(data, '{' + S + '}row', {'r': str(row_number)})
                for col, value in enumerate(values):
                    if value == '':
                        continue
                    attrs = {'r': column_name(col)+str(row_number)}
                    # Rank 用数字词面，其余指标与 ISSN 用源字符串。
                    numeric = row_number > 1 and table['headers'][col] == 'Rank'
                    attrs['t'] = 'n' if numeric else ('s' if shared else 'inlineStr')
                    cell = ET.SubElement(row, '{'+S+'}c', attrs)
                    if formula and name == 'Journals' and row_number == 2 and col == 0:
                        ET.SubElement(cell, '{'+S+'}f').text = '1+1'
                    if cell_error and name == 'Journals' and row_number == 2 and col == 0:
                        cell.set('t', 'e')
                        ET.SubElement(cell, '{'+S+'}v').text = '#REF!'
                    elif numeric:
                        ET.SubElement(cell, '{'+S+'}v').text = value
                    elif shared:
                        strings.append(value)
                        si = ET.SubElement(shared_root, '{'+S+'}si')
                        ET.SubElement(si, '{'+S+'}t').text = value
                        ET.SubElement(cell, '{'+S+'}v').text = str(len(strings)-1)
                    else:
                        inline = ET.SubElement(cell, '{'+S+'}is')
                        ET.SubElement(inline, '{'+S+'}t').text = value
            archive.writestr(f'xl/worksheets/sheet{number}.xml', ET.tostring(doc, encoding='utf-8'))
        archive.writestr('xl/workbook.xml', ET.tostring(workbook, encoding='utf-8'))
        archive.writestr('xl/_rels/workbook.xml.rels', ET.tostring(rels, encoding='utf-8'))
        if shared:
            archive.writestr('xl/sharedStrings.xml', ET.tostring(shared_root, encoding='utf-8'))


class WorkbookTests(LocalFilesCase):
    def setUp(self):
        self.path = self.local_dir()
        self.manifest = initialize(self.path, manifest())
        self.file = self.path / 'synthetic.xlsx'

    def test_shared_strings_and_inline_strings_preserve_raw_values(self):
        for shared in (True, False):
            write_fixture(self.file, tables(), shared=shared)
            result = read_workbook(self.file)
            source = result['Journals']['records'][0]['values']
            self.assertEqual(source['ISSN'], '0007-9235')
            self.assertEqual(source['2025 JIF'], '<0.1')
            self.assertEqual(source['5-year JIF'], '2.300')
            self.assertEqual(source['Future metric'], '7.0000')

    def test_all_categories_join_by_identity_not_display_rank(self):
        js, cs, errors, expected = normalize_workbook(tables(), self.manifest, 'local-synthetic')
        self.assertEqual((len(js), len(cs)), (2, 3))
        self.assertFalse(errors)
        self.assertEqual(cs[0]['journal_id'], cs[1]['journal_id'])
        self.assertEqual(cs[0]['jif_rank'], '1/20')
        self.assertEqual(cs[0]['source_rank'], '500')
        self.assertEqual(cs[2]['jif_quartile'], 'N/A')

    def test_three_sheet_audit_and_full_import_bom(self):
        write_fixture(self.file, tables())
        report, audit = import_workbook(self.path, self.manifest, self.file, 'local-synthetic')
        self.assertTrue(audit['file_ingestion_complete'])
        self.assertTrue(report['file_ingestion_complete'])
        self.assertFalse(report['full_complete'])
        self.assertEqual(report['status'], 'synthetic_only')
        self.assertIsNone(report['journal_coverage_percent'])
        self.assertEqual(read_csv(self.path/'journals.csv')[0]['xlsx__Future metric'], '7.0000')
        for name in ('journals.csv', 'journal_categories.csv'):
            self.assertTrue((self.path/name).read_bytes().startswith(b'\xef\xbb\xbf'))

    def test_category_json_mismatch_detected_not_repaired(self):
        source = tables()
        source['Category_Quartiles']['records'][0]['values']['JIF rank'] = '2/20'
        js, cs, errors, expected = normalize_workbook(source, self.manifest, 'local-synthetic')
        audit, failures = audit_workbook(source, self.manifest, js, cs, expected, ['journal_id', *JSON_MAP])
        self.assertFalse(audit['file_ingestion_complete'])
        self.assertTrue(audit['json_rows_missing_from_category_sheet'])
        self.assertIn('workbook_category_json_sheet_mismatch', [v['code'] for v in failures])
        self.assertEqual(cs[0]['jif_rank'], '2/20')

    def test_summary_count_mismatch_detected(self):
        source = tables()
        source['Category_Summary']['records'][0]['values']['Journal-category records'] = '999'
        js, cs, errors, expected = normalize_workbook(source, self.manifest, 'local-synthetic')
        audit, failures = audit_workbook(source, self.manifest, js, cs, expected, ['journal_id', *JSON_MAP])
        self.assertFalse(audit['file_ingestion_complete'])
        self.assertTrue(audit['summary_mismatches'])

    def test_metric_year_refused_and_original_output_not_overwritten(self):
        write_fixture(self.file, tables())
        import_workbook(self.path, self.manifest, self.file, 'local-synthetic')
        before = (self.path/'journals.csv').read_bytes()
        source = tables()
        source['Journals']['records'][0]['values']['JCR year'] = '2024'
        with self.assertRaisesRegex(JcrError, 'metric_year_mismatch'):
            normalize_workbook(source, self.manifest, 'local-synthetic')
        self.assertEqual(before, (self.path/'journals.csv').read_bytes())

    def test_main_category_metric_conflict_retained_and_reported(self):
        source = tables()
        source['Category_Quartiles']['records'][0]['values']['2025 JIF'] = '99'
        js, cs, errors, expected = normalize_workbook(source, self.manifest, 'local-synthetic')
        self.assertIn('workbook_journal_category_metric_mismatch', [v['code'] for v in errors])
        self.assertEqual(cs[0]['xlsx__2025 JIF'], '99')
        self.assertEqual(js[0]['jif'], '<0.1')

    def test_orphan_category_not_silently_dropped(self):
        source = tables()
        source['Category_Quartiles']['records'][0]['values']['ISSN'] = '9999-9999'
        js, cs, errors, expected = normalize_workbook(source, self.manifest, 'local-synthetic')
        self.assertEqual(len(cs), 3)
        self.assertIn('workbook_category_without_journal', [v['code'] for v in errors])

    def test_invalid_category_json_remains_in_source_column(self):
        source = tables()
        source['Journals']['records'][0]['values']['Category quartiles JSON'] = '{broken'
        js, cs, errors, expected = normalize_workbook(source, self.manifest, 'local-synthetic')
        self.assertEqual(js[0]['xlsx__Category quartiles JSON'], '{broken')
        self.assertIn('invalid_workbook_category_json', [v['code'] for v in errors])

    def test_changed_source_requires_new_snapshot(self):
        write_fixture(self.file, tables())
        import_workbook(self.path, self.manifest, self.file, 'local-synthetic')
        source = tables()
        source['Journals']['records'][0]['values']['Future metric'] = '8.0000'
        write_fixture(self.file, source)
        with self.assertRaisesRegex(JcrError, 'import_source_changed'):
            import_workbook(self.path, self.manifest, self.file, 'local-synthetic')

    def test_formula_and_excel_error_refused(self):
        for flag in ('formula', 'cell_error'):
            write_fixture(self.file, tables(), **{flag: True})
            with self.assertRaises(JcrError):
                read_workbook(self.file)

    def test_sparse_empty_cells_do_not_shift_columns(self):
        source = tables()
        source['Journals']['records'][0]['values']['Publisher'] = ''
        write_fixture(self.file, source)
        row = read_workbook(self.file)['Journals']['records'][0]['values']
        self.assertEqual(row['Publisher'], '')
        self.assertEqual(row['ISSN'], '0007-9235')

    def test_column_references_and_negative_shared_index(self):
        self.assertEqual(column_index('AG22644'), 32)
        with self.assertRaises(JcrError):
            column_index('A0')
        cell = ET.fromstring(f'<c xmlns="{S}" r="A1" t="s"><v>-1</v></c>')
        with self.assertRaises(JcrError):
            cell_text(cell, ['synthetic'])

    def test_hidden_source_sheet_preserved(self):
        source = tables()
        source['Category_Summary']['state'] = 'hidden'
        write_fixture(self.file, source)
        result = read_workbook(self.file)
        self.assertEqual(result['Category_Summary']['state'], 'hidden')
        self.assertEqual(len(result['Category_Summary']['records']), 3)

    def test_offline_query_returns_all_ambiguous_matches_and_categories(self):
        import scripts.jcr_data as cli
        root = self.local_dir()
        snapshot = root / 'data/jcr/synthetic-workbook-test'
        m = initialize(snapshot, manifest())
        write_fixture(self.file, tables())
        import_workbook(snapshot, m, self.file, 'local-synthetic')
        output = io.StringIO()
        with (patch.object(cli, 'ROOT', root),
              patch.object(cli, 'key_from_env', side_effect=AssertionError('offline_key_access')),
              patch('sys.argv', ['jcr_data.py','query','--snapshot','synthetic-workbook-test','--issn','0007-9235']),
              redirect_stdout(output)):
            code = cli.main()
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(len(result['journals']), 2)
        self.assertEqual(len(result['journal_categories']), 3)
