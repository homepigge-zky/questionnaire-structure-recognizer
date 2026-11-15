"""Entering one reserved row is a task edit, not a knowledge/identity grant."""
from copy import deepcopy
import hashlib
import io
import json

from docx import Document
from fastapi.testclient import TestClient

from questionnaire_structure_recognizer.api.app import create_app
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.workflow import materialize_questionnaire


def test_manual_record_value_is_counted_saved_exported_and_can_be_cleared(tmp_path):
    source=tmp_path/'FICTIONAL_records.docx'
    doc=Document();doc.add_paragraph('供应商填写：供货品种情况')
    table=doc.add_table(rows=3,cols=4)
    labels=['物料名称','包装规格','年产量','证书情况']
    for cell,label in zip(table.rows[0].cells,labels):cell.text=label
    doc.save(source)
    native=extract_document(source).model_dump()
    classes=[{'block_id':b['block_id'],'start':0,'end':len(b['text']),
              'semantic_role':'table_header' if b['structure']['row']==1 else 'non_qa',
              'group_id':'','relation_status':'confirmed'}
             for b in native['source_blocks'] if b['kind']=='table_cell']
    result=validate_and_assemble(native,[{'classifications':classes}],{'provider':'fictional-test'})
    app=create_app(tmp_path/'data','test',auth_mode='key');store=app.state.store
    identifier='fictional-reserved';headers={'X-QSR-Key':'test'}
    (store.files/source.name).write_bytes(source.read_bytes())
    with store.connect() as db:
        db.execute('INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,purpose,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)',
                   (identifier,source.name,source.name,hashlib.sha256(source.read_bytes()).hexdigest(),source.stat().st_size,'ready','fixture','questionnaire','{}'))
        db.commit()
    store.save_version(identifier,result,'model')
    with store.connect() as db:materialize_questionnaire(db,store.task(identifier),result,{})
    with TestClient(app) as client:
        def get():return client.get(f'/api/questionnaires/{identifier}',headers=headers).json()
        first=get();reserved=[q for q in first['questions'] if q['form_definition'].get('record_slot_status')=='reserved']
        assert len(reserved)==8
        q=next(q for q in reserved if q['form_definition']['record_index']==1 and q['question_text']=='物料名称')
        with store.connect() as db:
            untouched={r['id']:json.loads(r['form_definition_json']) for r in db.execute('SELECT id,form_definition_json FROM questions WHERE parent_id IS NOT NULL')}
        response=client.patch(f'/api/questionnaires/{identifier}/questions/{q["id"]}',headers=headers,json={'answer_text':'TEST_ONLY_MATERIAL','selected_options':[]})
        assert response.status_code==200,response.text
        filled=get();current=next(x for x in filled['questions'] if x['id']==q['id'])
        assert filled['stats']['manual']==first['stats']['manual']+1
        assert filled['stats']['answered']==first['stats']['answered']+1
        assert filled['stats']['reserved_record_slots']==7
        assert current['form_definition']['record_slot_status']=='manual_filled'
        assert current['form_definition']['source_record_slot_status']=='reserved'
        assert not current['form_definition']['auto_fill_eligible']
        assert current['form_definition']['validation_status']==untouched[q['id']]['validation_status']
        client.post(f'/api/questionnaires/{identifier}/rematch',headers=headers)
        rematched=get()
        assert next(x for x in rematched['questions'] if x['id']==q['id'])['answer_text']=='TEST_ONLY_MATERIAL'
        assert all(not x['answer_text'] for x in rematched['questions'] if x['id']!=q['id'])
        with store.connect() as db:
            assert {r['id']:json.loads(r['form_definition_json']) for r in db.execute('SELECT id,form_definition_json FROM questions WHERE parent_id IS NOT NULL')}==untouched
            assert db.execute('SELECT COUNT(*) FROM knowledge_records').fetchone()[0]==0
        exported=client.post(f'/api/questionnaires/{identifier}/exports',headers=headers,json={'mode':'standard'})
        assert exported.status_code==200,exported.text
        word=Document(io.BytesIO(client.get(exported.json()['download_url'],headers=headers).content))
        grid,=[t for t in word.tables if t.rows and [c.text for c in t.rows[0].cells]==['记录 / Record']+labels]
        assert grid.cell(1,1).text=='TEST_ONLY_MATERIAL'
        assert all(not grid.cell(r,c).text for r in (1,2) for c in range(1,5) if (r,c)!=(1,1))
        cleared=client.patch(f'/api/questionnaires/{identifier}/questions/{q["id"]}',headers=headers,json={'answer_text':'','selected_options':[]})
        assert cleared.status_code==200
        last=get();current=next(x for x in last['questions'] if x['id']==q['id'])
        assert current['form_definition']['record_slot_status']=='reserved'
        assert last['stats']['reserved_record_slots']==8
        assert last['stats']['answered']==first['stats']['answered']
        assert not current['form_definition']['auto_fill_eligible']
