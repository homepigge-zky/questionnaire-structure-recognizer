"""Fictional source-backed review headings; no real questionnaire data."""
import hashlib
import io
from copy import deepcopy

import fitz
from docx import Document
from fastapi.testclient import TestClient

from questionnaire_structure_recognizer.api.app import create_app
from questionnaire_structure_recognizer.form_positions import build_form_definition, infer_responsibility
from questionnaire_structure_recognizer.workflow import materialize_questionnaire


def scope_fixture(caption='(ASSESSMENTBYEXAMPLEBUYER)', caption_page=2, question_page=3):
    def block(key, text, page, order, y=300):
        return {'block_id': key, 'text': text, 'kind': 'ocr_text', 'order': order,
                'location': {'type': 'pdf', 'page': page, 'bbox': [50, y, 550, y+20]},
                'structure': {}, 'controls': []}
    blocks = [block('title', 'Vendor Questionnaire', 1, 0),
              block('caption', caption, caption_page, 1, 730),
              block('label', 'Conclusion:', question_page, 2),
              block('answer', '', question_page, 3, 330)]
    blocks[-1]['kind'] = 'form_field'
    blocks[-1]['structure'] = {'scan_candidate': True, 'scan_association_valid': True,
                               'blank_state': 'blank'}
    field = {'field_id': 'review-field', 'question_id': 'review-field',
             'question_text_original': 'Conclusion:',
             'label_refs': [{'block_id': 'label', 'start': 0, 'end': 11}],
             'answer_region_refs': [{'block_id': 'answer', 'start': 0, 'end': 0}],
             'context_refs': [{'block_id': 'caption', 'start': 0, 'end': len(caption)}],
             'section_refs': [], 'children': [], 'options': [],
             'answer_value_refs': [], 'answer_text_original': '',
             'answer_status': 'blank', 'pairing_validation': 'valid'}
    return blocks, field


def test_explicit_named_assessment_caption_can_cross_one_page():
    blocks, field = scope_fixture()
    role, evidence = infer_responsibility(field, {b['block_id']: b for b in blocks})
    assert role == 'reviewer'
    assert any('caption' in item for item in evidence)


def test_current_assessment_heading_and_supplier_reset_are_source_scoped():
    blocks, field = scope_fixture('Vendor Questionnaire Assessment by DQA/PQA:', 3, 3)
    blocks[1]['location']['bbox'] = [50, 100, 550, 120]
    field['section_refs'] = field.pop('context_refs')
    lookup = {b['block_id']: b for b in blocks}
    assert infer_responsibility(field, lookup)[0] == 'reviewer'
    # A source-confirmed next section does not inherit an earlier review area.
    blocks[1]['text'] = 'To be completed by supplier'
    field['section_refs'][0]['end'] = len(blocks[1]['text'])
    assert infer_responsibility(field, lookup)[0] == 'our_side'


def test_business_question_mentioning_assessment_is_not_review_ownership():
    blocks, field = scope_fixture()
    question = 'Does the supplier conduct an assessment by auditors every year?'
    blocks[2]['text'] = question
    field['question_text_original'] = question
    field['label_refs'][0]['end'] = len(question)
    field['context_refs'] = []
    assert infer_responsibility(field, {b['block_id']: b for b in blocks})[0] == 'our_side'


def test_future_or_distant_assessment_reference_does_not_own_field():
    for caption_page, question_page in [(4, 3), (1, 5)]:
        blocks, field = scope_fixture(caption_page=caption_page, question_page=question_page)
        assert infer_responsibility(field, {b['block_id']: b for b in blocks})[0] != 'reviewer'


def test_api_review_area_stays_blank_until_explicit_responsibility_correction(tmp_path):
    source = tmp_path / 'fictional-assessment.pdf'
    pdf = fitz.open()
    for text in ['Vendor Questionnaire', '(ASSESSMENT BY EXAMPLE BUYER)', 'Conclusion: __________']:
        page = pdf.new_page(); page.insert_text((50, 100), text)
    pdf.save(source); pdf.close()
    original = source.read_bytes(); digest = hashlib.sha256(original).hexdigest()
    blocks, field = scope_fixture()
    lookup = {b['block_id']: b for b in blocks}
    field['form_definition'] = build_form_definition(field, lookup, digest, 'fictional-physical-regions')
    assert field['form_definition']['fill_responsibility'] == 'reviewer'
    assert not field['form_definition']['auto_fill_eligible']
    result = {'source_hash': digest, 'file_type': 'pdf', 'parser_version': 'fictional-physical-regions',
              'source_blocks': blocks, 'questions': [field], 'sections': [], 'warnings': []}
    app = create_app(tmp_path/'data', 'test', auth_mode='key')
    store = app.state.store; headers = {'X-QSR-Key': 'test'}; identifier = 'review-test'
    (store.files/source.name).write_bytes(original)
    with store.connect() as db:
        db.execute('INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,purpose,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)',
                   (identifier,source.name,source.name,digest,len(original),'ready','fixture','questionnaire','{}'))
        db.commit()
    store.save_version(identifier, result, 'fixture')
    with TestClient(app) as client:
        response = client.post('/api/knowledge/manual', headers=headers,
                               json={'canonical_question':'Conclusion:', 'answer':'FORBIDDEN_HISTORY', 'approved':True})
        assert response.status_code == 200, response.text
        with store.connect() as db:
            materialize_questionnaire(db, store.task(identifier), result, {})
        q = client.get(f'/api/questionnaires/{identifier}', headers=headers).json()['questions'][0]
        assert q['answer_text'] == '' and q['origin'] == 'blank'
        edit = f"/api/questionnaires/{identifier}/questions/{q['id']}"
        blocked = client.patch(edit, headers=headers, json={'answer_text':'UNAUTHORIZED_MANUAL'})
        assert blocked.status_code == 422, blocked.text
        assert client.patch(edit, headers=headers, json={'answer_text':''}).status_code == 200
        meta = client.post(f'/api/questionnaires/{identifier}/exports', headers=headers, json={'mode':'standard'}).json()
        doc = Document(io.BytesIO(client.get(meta['download_url'], headers=headers).content))
        text = '\n'.join(cell.text for table in doc.tables for row in table.rows for cell in row.cells)
        assert 'FORBIDDEN_HISTORY' not in text and 'UNAUTHORIZED_MANUAL' not in text
        approved = client.patch(edit+'/fill-position', headers=headers,
                                json={'action':'confirm_mapping','fill_responsibility':'our_side'})
        assert approved.status_code == 200, approved.text
        assert client.patch(edit, headers=headers, json={'answer_text':'AUTHORIZED_CURRENT_REVIEW'}).status_code == 200
        meta = client.post(f'/api/questionnaires/{identifier}/exports', headers=headers, json={'mode':'standard'}).json()
        doc = Document(io.BytesIO(client.get(meta['download_url'], headers=headers).content))
        assert any('AUTHORIZED_CURRENT_REVIEW' in cell.text for table in doc.tables for row in table.rows for cell in row.cells)
    assert source.read_bytes() == original
