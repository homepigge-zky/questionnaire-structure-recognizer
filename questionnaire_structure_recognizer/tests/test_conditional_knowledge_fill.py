import hashlib
import io
from docx import Document
from fastapi.testclient import TestClient
from questionnaire_structure_recognizer.api.app import create_app
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.workflow import materialize_questionnaire


def test_no_condition_uses_reviewed_answer_only_while_parent_is_no(tmp_path):
    source=tmp_path/'fictional.docx';doc=Document();doc.add_paragraph('To be filled by supplier')
    table=doc.add_table(rows=2,cols=4)
    for c,t in zip(table.rows[0].cells,['Question','Answer Yes','Answer No','Remarks\nIf Ans. is No']):c.text=t
    table.cell(1,0).text='Is the fictional audit completed?';doc.save(source)
    result=validate_and_assemble(extract_document(source).model_dump(),[],{'provider':'fixture'})
    parent=next(f for f in result['questions'] if f.get('options'));child=parent['children'][0]
    assert child['explicit_condition']['equals_option']=='No'
    app=create_app(tmp_path/'data','test',auth_mode='key');store=app.state.store;headers={'X-QSR-Key':'test'}
    identifier='conditional';(store.files/source.name).write_bytes(source.read_bytes())
    with store.connect() as db:
        db.execute('INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,purpose,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)',
            (identifier,source.name,source.name,hashlib.sha256(source.read_bytes()).hexdigest(),source.stat().st_size,'ready','fixture','questionnaire','{}'));db.commit()
    store.save_version(identifier,result,'model')
    with TestClient(app) as client:
        for f,answer in [(parent,'No'),(child,'FICTIONAL reason: audit planned')]:
            response=client.post('/api/knowledge/manual',headers=headers,json={'canonical_question':f['question_text_original'],
                'answer':answer,'section':f.get('section_text_original',''),'context':f['form_definition'].get('semantic_context',''),'approved':True})
            assert response.status_code==200,response.text
        with store.connect() as db:materialize_questionnaire(db,store.task(identifier),result,{})
        initial=client.get(f'/api/questionnaires/{identifier}',headers=headers).json()
        byfield={q['field_id']:q for q in initial['questions']};p=byfield[parent['field_id']];c=byfield[child['field_id']]
        assert p['selected_options']==['No']
        assert c['origin']=='reviewed_knowledge' and c['answer_text']=='FICTIONAL reason: audit planned'
        assert c['form_definition']['applicability_status']=='applicable'
        saved=client.patch(f"/api/questionnaires/{identifier}/questions/{p['id']}",headers=headers,json={'answer_text':'','selected_options':['Yes']})
        assert saved.status_code==200,saved.text
        final=client.get(f'/api/questionnaires/{identifier}',headers=headers).json()
        after=next(q for q in final['questions'] if q['id']==c['id'])
        assert after['answer_text']=='' and after['form_definition']['applicability_status']=='not_applicable'
        meta=client.post(f'/api/questionnaires/{identifier}/exports',headers=headers,json={'mode':'template'}).json()
        assert not meta['warnings']
        exported=Document(io.BytesIO(client.get(meta['download_url'],headers=headers).content))
        assert exported.tables[0].cell(1,1).text=='☒'
        assert exported.tables[0].cell(1,3).text==''
