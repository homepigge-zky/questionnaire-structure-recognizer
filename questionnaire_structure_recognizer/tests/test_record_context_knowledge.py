"""Same-column contacts keep source-backed business roles through KB reuse."""
import hashlib
from docx import Document
from fastapi.testclient import TestClient
from questionnaire_structure_recognizer.api.app import create_app
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.workflow import materialize_questionnaire


def test_record_contacts_do_not_cross_responsibility_rows(tmp_path):
    doc=Document();doc.add_heading('Vendor Contact Information',1)
    doc.add_paragraph('To be completed by supplier')
    table=doc.add_table(rows=5,cols=3)
    for cell,text in zip(table.rows[0].cells,['Responsibility','Name','Telephone']):cell.text=text
    for row,role in zip(table.rows[1:],['Product Release','Change Control','Affiliate','Affiliate']):row.cells[0].text=role
    source=tmp_path/'fictional.docx';doc.save(source)
    result=validate_and_assemble(extract_document(source).model_dump(),[],{'provider':'deterministic-fixture'})
    def flatten(items):
        for field in items:yield field;yield from flatten(field.get('children',[]))
    names=[f for f in flatten(result['questions']) if f['question_text_original']=='Name']
    assert len(names)==4
    assert [f['form_definition'].get('record_identity_status') for f in names]==['identified','identified','ambiguous','ambiguous']
    assert all(not f['form_definition']['auto_fill_eligible'] for f in names[2:])
    app=create_app(tmp_path/'data','test',auth_mode='key');store=app.state.store;headers={'X-QSR-Key':'test'}
    identifier='record-role-test';(store.files/source.name).write_bytes(source.read_bytes())
    with store.connect() as db:
        db.execute('INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,purpose,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)',
            (identifier,source.name,source.name,hashlib.sha256(source.read_bytes()).hexdigest(),source.stat().st_size,'ready','fixture','questionnaire','{}'));db.commit()
    store.save_version(identifier,result,'model')
    with TestClient(app) as client:
        # Distinct values, not a shared token that would conceal a role swap.
        for field,value in zip(names[:3],['TEST_RELEASE_PERSON','TEST_CHANGE_PERSON','TEST_UNKNOWN_AFFILIATE']):
            assert client.post('/api/knowledge/manual',headers=headers,json={'canonical_question':field['question_text_original'],
                'answer':value,'section':field['section_text_original'],'context':field['form_definition']['semantic_context'],'approved':True}).status_code==200
        with store.connect() as db:materialize_questionnaire(db,store.task(identifier),result,{})
        snapshot=client.get(f'/api/questionnaires/{identifier}',headers=headers).json()
        byfield={q['field_id']:q for q in snapshot['questions']}
        corrected=client.patch(f"/api/questionnaires/{identifier}/questions/{byfield[names[2]['field_id']]['id']}/fill-position",headers=headers,
            json={'action':'confirm_mapping','fill_responsibility':'our_side'})
        assert corrected.status_code==200
        assert corrected.json()['form_definition']['auto_fill_eligible'] is False
        assert [byfield[f['field_id']]['answer_text'] for f in names]==['TEST_RELEASE_PERSON','TEST_CHANGE_PERSON','','']
        # Removing one reviewed role cannot fall back to the other contact.
        with store.connect() as db:
            db.execute("UPDATE knowledge_records SET active=0 WHERE answer='TEST_RELEASE_PERSON'");db.commit()
        client.post(f'/api/questionnaires/{identifier}/rematch',headers=headers)
        after={q['field_id']:q for q in client.get(f'/api/questionnaires/{identifier}',headers=headers).json()['questions']}
        assert after[names[0]['field_id']]['answer_text']==''
        assert after[names[1]['field_id']]['answer_text']=='TEST_CHANGE_PERSON'
