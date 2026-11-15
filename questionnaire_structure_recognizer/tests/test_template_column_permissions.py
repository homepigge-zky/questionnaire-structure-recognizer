"""Fictional OOXML permission contracts, independently probed in native Word.

The application intentionally rejects Word's ambiguous cross-table, named-user
and out-of-bounds fallbacks. Column indices address physical tc, not tblGrid.
"""
import hashlib
import json

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.table import _Cell
from lxml import etree

from questionnaire_structure_recognizer.exporter import TemplateFillError
from questionnaire_structure_recognizer.template_permissions import ReadonlyRangeGuard, protected_events


def protected_document(columns=3, rows=1):
    document = Document()
    table = document.add_table(rows=rows + 1, cols=columns)
    for index, cell in enumerate(table.rows[0].cells):
        cell.text = f'FICTIONAL FIXED LABEL {index + 1}'
    protection = OxmlElement('w:documentProtection')
    protection.set(qn('w:edit'), 'readOnly')
    protection.set(qn('w:enforcement'), '1')
    document.settings._element.append(protection)
    return document, table


def grant(table, first_row, last_row, first_column, last_column, key='1', editor=None, end_table=None):
    start = OxmlElement('w:permStart')
    for name, value in {'id': key, 'edGrp': 'everyone', 'colFirst': str(first_column), 'colLast': str(last_column)}.items():
        start.set(qn('w:' + name), value)
    if editor:
        start.set(qn('w:ed'), editor)
    table._tbl.tr_lst[first_row - 1].tc_lst[0].p_lst[0].append(start)
    end = OxmlElement('w:permEnd')
    end.set(qn('w:id'), key)
    (end_table or table)._tbl.tr_lst[last_row - 1].addnext(end)


def fixture(name):
    columns = 7 if name == 'five_cells_seven_grid' else 4 if name == 'merged_middle' else 2 if name.startswith('vertical') or name == 'cross_table' else 3
    document, table = protected_document(columns, 2 if name.startswith('vertical') else 1)
    if name.startswith('merged_first'):
        table.cell(1, 0).merge(table.cell(1, 1))
    elif name == 'merged_middle':
        table.cell(1, 1).merge(table.cell(1, 2))
    elif name == 'five_cells_seven_grid':
        table.cell(1, 1).merge(table.cell(1, 2))
        table.cell(1, 3).merge(table.cell(1, 4))
    elif name.startswith('vertical'):
        table.cell(1, 0).merge(table.cell(2, 0))
    if name == 'cross_table':
        document.add_paragraph('FIXED TABLE SEPARATOR')
        other = document.add_table(rows=2, cols=2)
        grant(table, 2, 2, 0, 0, end_table=other)
    elif name == 'five_cells_seven_grid':
        for index in range(6):
            grant(table, 2, 2, index, index, str(100 + index))
    elif name.startswith('vertical'):
        first, last = (3, 3) if name == 'vertical_continuation' else (2, 2) if name == 'vertical_partial_anchor' else (2, 3)
        grant(table, first, last, 0, 0)
    else:
        column = 0 if name == 'merged_first_zero' else 99 if name == 'outside_columns' else 1
        grant(table, 2, 2, column, column, editor='fictional@example.invalid' if name == 'named_editor' else None)
    return document


CASES = [
    ('ordinary', 1, 2, 1, False), ('ordinary', 1, 2, 2, True), ('ordinary', 1, 2, 3, False), ('ordinary', 1, 1, 1, False),
    ('merged_first_one', 1, 2, 1, False), ('merged_first_one', 1, 2, 2, True),
    ('merged_middle', 1, 2, 1, False), ('merged_middle', 1, 2, 2, True), ('merged_middle', 1, 2, 3, False),
    ('merged_first_zero', 1, 2, 1, True), ('merged_first_zero', 1, 2, 2, False),
    ('vertical_complete', 1, 2, 1, True), ('vertical_complete', 1, 2, 2, False), ('vertical_complete', 1, 3, 2, False),
    ('vertical_continuation', 1, 2, 1, False), ('vertical_continuation', 1, 3, 1, False), ('vertical_partial_anchor', 1, 2, 1, False),
    ('cross_table', 1, 2, 1, False), ('cross_table', 2, 2, 1, False), ('named_editor', 1, 2, 2, False),
    ('outside_columns', 1, 2, 1, False), ('outside_columns', 1, 2, 2, False), ('outside_columns', 1, 2, 3, False),
    *[('five_cells_seven_grid', 1, 2, index, True) for index in range(1, 6)],
]


@pytest.mark.parametrize('name,table_no,row_no,column_no,expected', CASES)
def test_physical_column_permission_contract(name, table_no, row_no, column_no, expected, tmp_path):
    document = fixture(name)
    source = tmp_path / 'FICTIONAL_ORIGINAL.docx'
    document.save(source)
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    table = document.tables[table_no - 1]
    paragraph = _Cell(table._tbl.tr_lst[row_no - 1].tc_lst[column_no - 1], table).paragraphs[0]
    guard = ReadonlyRangeGuard(document)
    evidence = guard.evidence()
    before = protected_events(document._element, guard.allowed)
    if not expected:
        with pytest.raises(TemplateFillError):
            guard.authorize_append(paragraph)
        with pytest.raises(TemplateFillError):
            guard.append(paragraph, 'FORBIDDEN')
        assert protected_events(document._element, guard.allowed) == before
        return
    authorization = guard.authorize_append(paragraph)
    assert authorization['kind'] == 'physical_table_columns'
    assert paragraph.text == ''  # preflight never writes
    guard.append(paragraph, 'TEST_AUTHORIZED_ONLY')
    guard.verify()
    assert paragraph.text == 'TEST_AUTHORIZED_ONLY'
    assert protected_events(document._element, guard.allowed) == before
    assert guard.evidence() == evidence
    destination = tmp_path / 'FICTIONAL_EXPORT.docx'
    document.save(destination)
    read = Document(destination)
    reread = _Cell(read.tables[table_no - 1]._tbl.tr_lst[row_no - 1].tc_lst[column_no - 1], read.tables[table_no - 1])
    assert reread.text == 'TEST_AUTHORIZED_ONLY'
    assert read.settings._element.find(qn('w:documentProtection')).get(qn('w:enforcement')) == '1'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == fingerprint


@pytest.mark.parametrize('mutation', ['neighbor_value', 'paragraph_properties', 'cell_width', 'permission_columns', 'settings', 'nested_table', 'field_instruction'])
def test_authorization_does_not_exempt_neighbor_or_structural_xml(mutation):
    document = fixture('ordinary')
    cell = document.tables[0].cell(1, 1)
    paragraph = cell.paragraphs[0]
    guard = ReadonlyRangeGuard(document)
    guard.append(paragraph, 'TEST_ALLOWED_ONLY')
    guard.verify()
    if mutation == 'neighbor_value':
        document.tables[0].cell(1, 0).paragraphs[0].add_run('FORBIDDEN')
    elif mutation == 'paragraph_properties':
        paragraph.paragraph_format.keep_with_next = True
    elif mutation == 'cell_width':
        cell._tc.tcPr.find(qn('w:tcW')).set(qn('w:w'), '9999')
    elif mutation == 'permission_columns':
        next(document._element.iter(qn('w:permStart'))).set(qn('w:colLast'), '2')
    elif mutation == 'nested_table':
        cell.add_table(rows=1, cols=1).cell(0, 0).text = 'FORBIDDEN'
    elif mutation == 'field_instruction':
        instruction = OxmlElement('w:instrText'); instruction.text = 'FORBIDDEN FIELD'
        paragraph.runs[-1]._r.append(instruction)
    else:
        document.settings._element.find(qn('w:documentProtection')).set(qn('w:enforcement'), '0')
    with pytest.raises(TemplateFillError):
        guard.verify()
