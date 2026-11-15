"""Independent fictional analogue of repeated GRM labels in the real F11."""
from copy import deepcopy
import sqlite3

import pytest
from docx import Document
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.exporter import fill_workflow_docx_template


def source(tmp_path):
    path=tmp_path/'FICTIONAL-duplicate-options.docx'
    d=Document();t=d.add_table(rows=1,cols=2)
    t.cell(0,0).text='Facility activities?'
    t.cell(0,1).text='□ Alpha  □ Beta  □ Alpha'
    d.save(path)
    result=validate_and_assemble(extract_document(path).model_dump(),[],{'provider':'fixture'})
    return path,result,next(f for f in result['questions'] if f.get('options'))


def test_repeated_option_text_has_distinct_source_identity_and_exact_writeback(tmp_path):
    path,result,field=source(tmp_path)
    options=field['options']
    assert options[0]['option_id']!=options[2]['option_id']
    assert options[0]['text_original']==options[2]['text_original']=='Alpha'
    assert options[1]['value']=='Beta'
    for index in [0,2]:
        field.update(selected_options=[options[index]['value']],manual_override=True,answer_origin='manual')
        target=tmp_path/f'out-{index}.docx';updates,warnings=fill_workflow_docx_template(path,result,target)
        assert not warnings
        marks=[c for c in Document(target).tables[0].cell(0,1).text if c in '□☐☒']
        assert [i for i,mark in enumerate(marks) if mark=='☒']==[index]
    field['selected_options']=['Alpha']
    _,warnings=fill_workflow_docx_template(path,result,tmp_path/'ambiguous.docx')
    assert warnings and '重名' in ';'.join(warnings)


def test_reviewed_text_cannot_select_two_distinct_repeated_options(tmp_path):
    from questionnaire_structure_recognizer.workflow import knowledge_choice_selection
    _,_,field=source(tmp_path)
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE answer_options(question_id,option_order,text,value)')
    db.execute('CREATE TABLE questions(id,input_type)');db.execute("INSERT INTO questions VALUES('q','multi_select')")
    db.executemany('INSERT INTO answer_options VALUES(?,?,?,?)',[('q',i,o['text_original'],o.get('value','')) for i,o in enumerate(field['options'])])
    assert knowledge_choice_selection(db,'q','Beta')==['Beta']
    with pytest.raises(ValueError):knowledge_choice_selection(db,'q','Alpha')
    with pytest.raises(ValueError):knowledge_choice_selection(db,'q',field['options'][0]['value'])


def test_repeated_choice_api_saves_source_value_but_drafts_and_docx_show_original(tmp_path):
    import hashlib,io
    from fastapi.testclient import TestClient
    from questionnaire_structure_recognizer.api.app import create_app
    from questionnaire_structure_recognizer.workflow import materialize_questionnaire
    path,result,field=source(tmp_path)
    app=create_app(tmp_path/'data','test',auth_mode='key');store=app.state.store;headers={'X-QSR-Key':'test'}
    with TestClient(app) as client:
        identifier='duplicate-choice';(store.files/path.name).write_bytes(path.read_bytes())
        with store.connect() as db:
            db.execute('INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,purpose,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)',
                (identifier,path.name,path.name,hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_size,'ready','test','questionnaire','{}'));db.commit()
        store.save_version(identifier,result,'model')
        with store.connect() as db:materialize_questionnaire(db,store.task(identifier),result,{})
        q=next(q for q in client.get(f'/api/questionnaires/{identifier}',headers=headers).json()['questions'] if q.get('options'))
        url=f"/api/questionnaires/{identifier}/questions/{q['id']}"
        assert client.patch(url,headers=headers,json={'selected_options':['Alpha']}).status_code==422
        value=q['options'][2]['value'];assert value.startswith('option-')
        saved=client.patch(url,headers=headers,json={'selected_options':[value],'answer_text':''});assert saved.status_code==200,saved.text
        again=next(q for q in client.get(f'/api/questionnaires/{identifier}',headers=headers).json()['questions'] if q.get('options'))
        assert again['selected_options']==[value]
        draft=client.post(f'/api/questionnaires/{identifier}/knowledge-drafts',headers=headers,json={'question_ids':[q['id']]}).json()
        review=client.get(f"/api/imports/{draft['batch_id']}",headers=headers).json()
        assert review['items'][0]['answer']=='Alpha'
        for mode in ['standard','template']:
            exported=client.post(f'/api/questionnaires/{identifier}/exports',headers=headers,json={'mode':mode})
            assert exported.status_code==200,exported.text
            details=exported.json();assert not details['warnings']
            doc=Document(io.BytesIO(client.get(details['download_url'],headers=headers).content))
            text='\n'.join(c.text for t in doc.tables for r in t.rows for c in r.cells)
            assert 'option-' not in text and text.count('☒')==1
