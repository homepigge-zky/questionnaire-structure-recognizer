"""A conditional alternate entity cannot supply the ordinary contact answer."""
from docx import Document
from fastapi.testclient import TestClient
import hashlib
import pytest

from questionnaire_structure_recognizer.api.app import create_app
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.workflow import materialize_questionnaire


@pytest.mark.parametrize('alternate', [
    'Manufacturer details (if not identical with supplier)',
    'Manufacturerdetails(ifnotidenticalwithsupplier)',
    'Contract packaging facility (if different from the supplier)',
])
def test_contact_source_scope_is_preserved_in_both_directions(tmp_path, alternate):
    sections = ['Supplier details', alternate]
    source = tmp_path / 'FICTIONAL-contact-scopes.docx'
    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    for row in table.rows:
        row.cells[0].text = 'E-Mail:'
    doc.save(source)
    result = extract_document(source).model_dump()
    cells = {(b['structure']['row_start'], b['structure']['column_start']): b
             for b in result['source_blocks'] if b['kind'] == 'table_cell'}
    def ref(cell):
        return {'block_id': cell['block_id'], 'start': 0, 'end': len(cell['text'])}
    result['questions'] = []
    for row, section in enumerate(sections, 1):
        result['questions'].append({'field_id': str(row), 'question_id': str(row),
            'question_text_original': 'E-Mail:', 'section_text_original': section,
            'label_refs': [ref(cells[row, 1])], 'question_refs': [ref(cells[row, 1])],
            'answer_region_refs': [ref(cells[row, 2])], 'answer_value_refs': [],
            'answer_status': 'blank', 'answer_text_original': '', 'children': [], 'options': []})
    app = create_app(tmp_path / 'db', 'test', auth_mode='key')
    store = app.state.store
    headers = {'X-QSR-Key': 'test'}
    task_id = 'fictional-contact-task'
    with TestClient(app) as client:
        for section, answer in zip(sections, ['supplier@example.invalid', 'manufacturer@example.invalid']):
            reply = client.post('/api/knowledge/manual', headers=headers, json={'canonical_question': 'E-Mail:',
                'answer': answer, 'section': section, 'approved': True})
            assert reply.status_code == 200, reply.text
        (store.files / source.name).write_bytes(source.read_bytes())
        with store.connect() as db:
            db.execute('INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,purpose,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)',
                       (task_id,source.name,source.name,hashlib.sha256(source.read_bytes()).hexdigest(),source.stat().st_size,'ready','test','questionnaire','{}'))
        store.save_version(task_id, result, 'model')
        with store.connect() as db:
            materialize_questionnaire(db, store.task(task_id), result, {})
        def values():
            payload = client.get(f'/api/questionnaires/{task_id}', headers=headers).json()
            return {q['section']: q['answer_text'] for q in payload['questions']}
        # The alternate-entity heading alone gives no proof that this
        # questionnaire's supplier and manufacturer are different.
        assert values() == {sections[0]: 'supplier@example.invalid', alternate: ''}
        reply = client.post('/api/knowledge/manual', headers=headers, json={'canonical_question': 'E-Mail:',
            'answer': 'other-supplier@example.invalid', 'section': sections[0], 'approved': True})
        assert reply.status_code == 200
        reply = client.post(f'/api/questionnaires/{task_id}/rematch', headers=headers)
        assert reply.status_code == 200
        assert values() == {sections[0]: '', alternate: ''}
