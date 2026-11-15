from copy import deepcopy
from docx import Document
from questionnaire_structure_recognizer.source_notes import group_readonly_source_tables
from questionnaire_structure_recognizer.exporter import standard_questionnaire_docx


def fixture():
    blocks = {}
    for r, row in enumerate([['User', 'Date (EST)', 'Justification'], ['TEST_SIGNER', '2026-01-01', 'TEST_APPROVAL']]):
        for c, text in enumerate(row):
            bid = f'b{r}{c}'
            blocks[bid] = {'text': text, 'order': 10 + r * 3 + c, 'location': {'type': 'pdf', 'page': 2},
                'structure': {'resolved_layout_role': 'readonly_record_cell', 'readonly_record': {
                    'table_id': 'physical-signature-table', 'source_hash': 'a' * 64,
                    'row_index': r, 'column_index': c, 'row_count': 2, 'column_count': 3,
                    'read_only': True, 'cell_ref': {'block_id': bid, 'start': 0, 'end': 0}}}}
    return blocks


def test_readonly_source_table_retains_physical_rows_without_new_answers(tmp_path):
    blocks = fixture(); before = deepcopy(blocks)
    notes = group_readonly_source_tables(blocks, 'a' * 64)
    assert len(notes) == 1 and notes[0]['role'] == 'readonly_table'
    assert notes[0]['source_hash'] == 'a' * 64
    assert [[c['text'] for c in row] for row in notes[0]['rows']] == [
        ['User', 'Date (EST)', 'Justification'], ['TEST_SIGNER', '2026-01-01', 'TEST_APPROVAL']]
    path = tmp_path / 'readonly.docx'
    standard_questionnaire_docx({'task': {}, 'questions': [], 'document_content': notes}, path)
    tables = [t for t in Document(path).tables if len(t.columns) == 3]
    assert len(tables) == 1
    assert [[c.text for c in row.cells] for row in tables[0].rows] == [
        ['User', 'Date (EST)', 'Justification'], ['TEST_SIGNER', '2026-01-01', 'TEST_APPROVAL']]
    assert blocks == before


def test_unrelated_hash_and_conflicting_cells_do_not_invent_table_pairings():
    blocks = fixture()
    notes = group_readonly_source_tables(blocks, 'b' * 64)
    assert len(notes) == 6 and all(n['role'] == 'readonly_unresolved_prose' for n in notes)
    assert all(n['diagnostic_reason'] == 'readonly_record_source_hash_mismatch' for n in notes)
    blocks = fixture()
    blocks['b11']['structure']['readonly_record']['column_index'] = 0
    notes = group_readonly_source_tables(blocks, 'a' * 64)
    assert all(n['role'] == 'readonly_unresolved_prose' for n in notes)
    assert 'TEST_APPROVAL' in '\n'.join(n['text'] for n in notes)


def test_multiple_source_fragments_in_one_physical_cell_remain_in_reading_order():
    blocks = fixture()
    second = deepcopy(blocks['b11'])
    second.update(text='10:06:04', order=14.5)
    blocks['time'] = second
    notes = group_readonly_source_tables(blocks, 'a' * 64)
    assert notes[0]['rows'][1][1]['text'] == '2026-01-01\n10:06:04'
    assert notes[0]['rows'][1][1]['block_ids'] == ['b11', 'time']
