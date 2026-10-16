"""只读 OOXML 表格读取；保留 XML 数字词面，不执行公式或外部链接。"""
from pathlib import PurePosixPath
import posixpath
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile, BadZipFile
from .storage import JcrError

S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'


def column_index(reference):
    match = re.fullmatch(r'([A-Z]+)[1-9][0-9]*', reference)
    if not match:
        raise JcrError('invalid_xlsx_cell_reference')
    value = 0
    for character in match.group(1):
        value = value * 26 + ord(character) - ord('A') + 1
    return value - 1


def cell_text(cell, strings):
    if cell.find(S + 'f') is not None:
        # 不把缓存值或公式文本当作已经核验的源指标。
        raise JcrError('xlsx_formula_requires_verified_values')
    kind = cell.get('t', 'n')
    value = cell.find(S + 'v')
    raw = value.text or '' if value is not None else ''
    if kind == 's':
        try:
            index = int(raw)
            if index < 0:
                raise ValueError
            return strings[index]
        except (ValueError, IndexError):
            raise JcrError('invalid_xlsx_shared_string') from None
    if kind == 'inlineStr':
        return ''.join(part.text or '' for part in cell.iter(S + 't'))
    if kind == 'e':
        raise JcrError('xlsx_source_cell_error')
    return raw


def read_workbook(file):
    """返回工作表及物理行号；空单元格不移位，隐藏表也读入。"""
    try:
        with ZipFile(file) as archive:
            strings = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                with archive.open('xl/sharedStrings.xml') as stream:
                    for event, element in ET.iterparse(stream, events=('end',)):
                        if element.tag == S + 'si':
                            strings.append(''.join(part.text or '' for part in element.iter(S + 't')))
                            element.clear()
            workbook = ET.fromstring(archive.read('xl/workbook.xml'))
            relationships = {rel.get('Id'): rel for rel in
                             ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
            result = {}
            for sheet in workbook.findall(S + 'sheets/' + S + 'sheet'):
                name = sheet.get('name')
                if name in result:
                    raise JcrError('duplicate_xlsx_sheet_name')
                rel = relationships.get(sheet.get(R))
                if rel is None or rel.get('TargetMode') == 'External':
                    raise JcrError('invalid_or_external_xlsx_sheet')
                target = rel.get('Target', '')
                member = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
                if not member.startswith('xl/') or '..' in PurePosixPath(member).parts:
                    raise JcrError('invalid_xlsx_sheet_path')
                headers, records, seen_rows = None, [], set()
                with archive.open(member) as stream:
                    for event, element in ET.iterparse(stream, events=('end',)):
                        if element.tag != S + 'row':
                            continue
                        number = int(element.get('r', '0'))
                        if number in seen_rows or number < 1:
                            raise JcrError('duplicate_or_invalid_xlsx_row')
                        seen_rows.add(number)
                        values = {}
                        for cell in element.findall(S + 'c'):
                            col = column_index(cell.get('r', ''))
                            if col in values:
                                raise JcrError('duplicate_xlsx_cell')
                            values[col] = cell_text(cell, strings)
                        if values and any(values.values()):
                            if headers is None:
                                headers = [values.get(i, '') for i in range(max(values)+1)]
                                if any(not v for v in headers) or len(set(headers)) != len(headers):
                                    raise JcrError('empty_or_duplicate_xlsx_header')
                            else:
                                if any(col >= len(headers) and text for col, text in values.items()):
                                    raise JcrError('xlsx_unheaded_data_column')
                                records.append({'row_number': number,
                                                'values': {key: values.get(i, '') for i, key in enumerate(headers)}})
                        element.clear()
                result[name] = {'headers': headers or [], 'records': records, 'state': sheet.get('state', 'visible')}
            return result
    except JcrError:
        raise
    except (OSError, BadZipFile, ET.ParseError, KeyError, ValueError):
        raise JcrError('invalid_or_unsupported_xlsx') from None
