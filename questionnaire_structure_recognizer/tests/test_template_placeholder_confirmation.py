"""Unmarked contract variables require explicit source-bound approval."""
import hashlib
import io
from docx import Document
from fastapi.testclient import TestClient
from questionnaire_structure_recognizer.api.app import create_app
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.workflow import materialize_questionnaire


def test_contract_variable_approval_writes_only_exact_source_range(tmp_path):
    source=tmp_path/'fictional-contract.docx';d=Document()
    d.add_paragraph('Vendor Quality Agreement')
    sentence='This agreement is entered into between Vendor Name and FICTIONAL BUYER.'
    d.add_paragraph(sentence);d.add_paragraph('The supplier name is mentioned in an ordinary explanatory sentence.')
    d.save(source);original=source.read_bytes()
    result=validate_and_assemble(extract_document(source).model_dump(),[],{'provider':'fixture'})
    app=create_app(tmp_path/'data','test',auth_mode='key');store=app.state.store;headers={'X-QSR-Key':'test'}
    task_id='contract';(store.files/source.name).write_bytes(original)
    with store.connect() as db:
        db.execute('INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,purpose,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)',
            (task_id,source.name,source.name,hashlib.sha256(original).hexdigest(),len(original),'ready','fixture','questionnaire','{}'));db.commit()
    store.save_version(task_id,result,'model')
    with store.connect() as db:materialize_questionnaire(db,store.task(task_id),result,{})
    with TestClient(app) as client:
        snapshot=client.get(f'/api/questionnaires/{task_id}',headers=headers).json()
        q=next(q for q in snapshot['questions'] if q['form_definition'].get('template_placeholder_candidates'))
        assert not q['form_definition']['auto_fill_eligible'] and not q['answer_text']
        saved=client.patch(f"/api/questionnaires/{task_id}/questions/{q['id']}",headers=headers,json={'answer_text':'TEST_VENDOR'})
        assert saved.status_code==200,saved.text
        before=client.post(f'/api/questionnaires/{task_id}/exports',headers=headers,json={'mode':'template'}).json()
        assert before['warnings']
        before_doc=Document(io.BytesIO(client.get(before['download_url'],headers=headers).content))
        assert before_doc.paragraphs[1].text==sentence
        approved=client.patch(f"/api/questionnaires/{task_id}/questions/{q['id']}/fill-position",headers=headers,
            json={'action':'confirm_template_placeholder'})
        assert approved.status_code==200,approved.text
        after=client.post(f'/api/questionnaires/{task_id}/exports',headers=headers,json={'mode':'template'}).json()
        assert not after['warnings'],after
        exported=Document(io.BytesIO(client.get(after['download_url'],headers=headers).content))
        assert exported.paragraphs[1].text==sentence.replace('Vendor Name','TEST_VENDOR')
        assert exported.paragraphs[2].text==d.paragraphs[2].text
        assert source.read_bytes()==original
