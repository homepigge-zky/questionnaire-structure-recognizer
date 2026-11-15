"""Reviewed knowledge must populate real choice state, not hidden text only."""
import hashlib
import io
from docx import Document
from fastapi.testclient import TestClient
from questionnaire_structure_recognizer.api.app import create_app
from questionnaire_structure_recognizer.form_positions import enrich_result_form_positions
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.workflow import materialize_questionnaire


def test_reviewed_choice_answers_match_save_and_export(tmp_path):
    source=tmp_path/'FICTIONAL-choice.docx'
    doc=Document();doc.add_paragraph('To be filled by supplier representative')
    table=doc.add_table(rows=6,cols=4)
    for c,s in zip(table.rows[0].cells,['Question','Yes','No','N/A']):c.text=s
    labels=['Does fictional process alpha comply?','Does fictional process beta comply?',
        'Does fictional process gamma comply?','Does fictional process delta comply?',
        'Does fictional process alpha NOT comply?']
    for i,label in enumerate(labels,1):table.cell(i,0).text=label
    doc.save(source)
    result=validate_and_assemble(extract_document(source).model_dump(),[],{'provider':'fixture'})
    enrich_result_form_positions(result)
    app=create_app(tmp_path/'data','test',auth_mode='key');store=app.state.store;headers={'X-QSR-Key':'test'}
    with TestClient(app) as client:
        for label,answer in zip(labels,['Yes','No','N/A','B']):
            f=next(f for f in result['questions'] if f['question_text_original']==label)
            response=client.post('/api/knowledge/manual',headers=headers,json={'canonical_question':label,
                'answer':answer,'section':f.get('section_text_original',''),'context':f['form_definition'].get('semantic_context',''),'approved':True})
            assert response.status_code==200,response.text
        task_id='choices';(store.files/source.name).write_bytes(source.read_bytes())
        with store.connect() as db:
            db.execute('INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,purpose,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)',
                (task_id,source.name,source.name,hashlib.sha256(source.read_bytes()).hexdigest(),source.stat().st_size,'ready','test','questionnaire','{}'));db.commit()
        store.save_version(task_id,result,'model')
        with store.connect() as db:materialize_questionnaire(db,store.task(task_id),result,{})
        snapshot=client.get(f'/api/questionnaires/{task_id}',headers=headers).json()
        bylabel={q['question_text']:q for q in snapshot['questions']}
        for label,answer in zip(labels,['Yes','No','N/A']):
            assert bylabel[label]['selected_options']==[answer]
            assert bylabel[label]['origin']=='reviewed_knowledge'
        for label in labels[3:]:
            assert not bylabel[label]['answer_text'] and not bylabel[label]['selected_options']
        meta=client.post(f'/api/questionnaires/{task_id}/exports',headers=headers,json={'mode':'template'}).json()
        assert not meta['warnings']
        output=Document(io.BytesIO(client.get(meta['download_url'],headers=headers).content))
        for row,col in [(1,1),(2,2),(3,3)]:assert output.tables[0].cell(row,col).text=='☒'
        assert output.tables[0].cell(4,1).text==''
        manual=bylabel[labels[3]]
        saved=client.patch(f"/api/questionnaires/{task_id}/questions/{manual['id']}",headers=headers,
            json={'answer_text':'','selected_options':['No']})
        assert saved.status_code==200,saved.text
        draft=client.post(f'/api/questionnaires/{task_id}/knowledge-drafts',headers=headers,
            json={'question_ids':[manual['id']]}).json()
        assert draft['created']==1 and draft['review_status']=='pending'
        review=client.get(f"/api/imports/{draft['batch_id']}",headers=headers).json()
        assert review['items'][0]['answer']=='No'
        assert review['items'][0]['review_status']=='pending'
