from io import BytesIO
import pytest
from docx import Document
from fastapi.testclient import TestClient
from questionnaire_structure_recognizer.api.app import create_app
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.tests.test_import_source_context import register,materialized


@pytest.fixture
def questionnaire(tmp_path):
    path=tmp_path/'TEST_ONLY_manual_choice.docx';d=Document();d.add_paragraph('To be filled by supplier')
    t=d.add_table(rows=2,cols=4)
    for c,text in zip(t.rows[0].cells,['Question','Answer Yes','Answer No','Remarks\nIf Ans. is No']):c.text=text
    t.cell(1,0).text='Is the fictional audit completed?';d.save(path)
    result=validate_and_assemble(extract_document(path).model_dump(),[],{'provider':'fixture'})
    app=create_app(tmp_path/'isolated','test',auth_mode='key');store=app.state.store
    (store.files/path.name).write_bytes(path.read_bytes());task=register(store,path,result,'TEST_ONLY_manual_choice')
    store.save_version(task['id'],result,'model');materialized(store,task,result)
    with TestClient(app,headers={'X-QSR-Key':'test'})as client:
        route='/api/questionnaires/'+task['id'];snapshot=client.get(route).json()
        parent=next(q for q in snapshot['questions']if q['options']);child=next(q for q in snapshot['questions']if q['parent_id']==parent['id'])
        yield client,route,parent,child,store


def saved(client,route,qid):return next(q for q in client.get(route).json()['questions']if q['id']==qid)


def test_literal_no_maps_to_native_option_and_roundtrips_both_word_modes(questionnaire):
    client,route,parent,child,_=questionnaire
    response=client.patch(route+'/questions/'+parent['id'],json={'answer_text':'No'})
    assert response.status_code==200,response.text
    assert saved(client,route,parent['id'])['selected_options']==['No']
    assert saved(client,route,child['id'])['form_definition']['applicability_status']=='applicable'
    assert client.patch(route+'/questions/'+child['id'],json={'answer_text':'TEST_REASON'}).status_code==200
    for mode in ('standard','template'):
        exp=client.post(route+'/exports',json={'mode':mode});assert exp.status_code==200,exp.text
        doc=Document(BytesIO(client.get(exp.json()['download_url']).content))
        if mode=='template':
            assert doc.tables[0].cell(1,1).text=='☐'
            assert doc.tables[0].cell(1,2).text=='☒'
            assert doc.tables[0].cell(1,3).text=='TEST_REASON'
        else:
            row=next(row for table in doc.tables for row in table.rows if len(row.cells)==4 and row.cells[1].text==parent['question_text'])
            assert row.cells[2].text=='☐ Yes\n☒ No'


@pytest.mark.parametrize('payload',[{'selected_options':['No']},{'answer_text':'no','selected_options':['No']}])
def test_explicit_valid_selection_or_agreeing_text_is_saved(questionnaire,payload):
    client,route,parent,child,_=questionnaire
    r=client.patch(route+'/questions/'+parent['id'],json=payload);assert r.status_code==200,r.text
    assert saved(client,route,parent['id'])['selected_options']==['No']
    assert saved(client,route,child['id'])['form_definition']['applicability_status']=='applicable'


@pytest.mark.parametrize('payload',[{'answer_text':'Maybe'},{'answer_text':'Yes','selected_options':['No']},{'answer_text':'No','selected_options':[]},{'selected_options':['UNLOCATED']}])
def test_unknown_or_conflicting_request_is_atomic_and_cannot_activate_child(questionnaire,payload):
    client,route,parent,child,_=questionnaire
    before=saved(client,route,parent['id']);r=client.patch(route+'/questions/'+parent['id'],json=payload)
    assert r.status_code==422,r.text
    after=saved(client,route,parent['id'])
    assert (after['answer_text'],after['selected_options'])==(before['answer_text'],before['selected_options'])
    assert saved(client,route,child['id'])['form_definition']['applicability_status']=='unresolved'


def test_clear_text_clears_prior_selection_and_choice_only_edit_clears_stale_text(questionnaire):
    client,route,parent,child,_=questionnaire;url=route+'/questions/'+parent['id']
    assert client.patch(url,json={'answer_text':'No'}).status_code==200
    assert client.patch(url,json={'selected_options':['Yes']}).status_code==200
    current=saved(client,route,parent['id']);assert current['answer_text']=='' and current['selected_options']==['Yes']
    assert saved(client,route,child['id'])['form_definition']['applicability_status']=='not_applicable'
    assert client.patch(url,json={'answer_text':''}).status_code==200
    current=saved(client,route,parent['id']);assert current['answer_text']=='' and current['selected_options']==[]


def test_duplicate_visible_labels_require_physical_option_identity(questionnaire):
    client,route,parent,child,store=questionnaire
    with store.connect()as db:
        db.execute("UPDATE answer_options SET text='No',value=id WHERE question_id=?",(parent['id'],));db.commit()
    r=client.patch(route+'/questions/'+parent['id'],json={'answer_text':'No'})
    assert r.status_code==422,r.text
    assert saved(client,route,parent['id'])['selected_options']==[]
    with store.connect()as db:
        value=db.execute('SELECT value FROM answer_options WHERE question_id=? ORDER BY option_order LIMIT 1',(parent['id'],)).fetchone()['value']
    r=client.patch(route+'/questions/'+parent['id'],json={'answer_text':'No','selected_options':[value]})
    assert r.status_code==200,r.text
    assert saved(client,route,parent['id'])['selected_options']==[value]


def test_notes_do_not_rewrite_selection_and_invalid_patch_rolls_back_question_edit(questionnaire):
    client,route,parent,child,_=questionnaire;url=route+'/questions/'+parent['id']
    assert client.patch(url,json={'answer_text':'No'}).status_code==200
    assert client.patch(url,json={'notes':'TEST_NOTE'}).status_code==200
    current=saved(client,route,parent['id'])
    assert current['answer_text']=='No' and current['selected_options']==['No'] and current['notes']=='TEST_NOTE'
    bad=client.patch(url,json={'question_text':'SHOULD_ROLL_BACK','answer_text':'Unknown'})
    assert bad.status_code==422
    assert saved(client,route,parent['id'])['question_text']==parent['question_text']


def test_helper_preserves_multi_select_and_rejects_invalid_single_and_payload_shapes():
    from questionnaire_structure_recognizer.manual_choice_input import reconcile_manual_choice as reconcile
    choices=[{'text':'Yes','value':'y'},{'text':'No','value':'n'}]
    assert reconcile({'answer_text':'Yes; No'},choices,'multi_select','',[])==('Yes; No',['y','n'])
    for payload in ({'selected_options':['y','n']},{'answer_text':'Yes; No'},{'selected_options':'No'},{'selected_options':[None]}):
        with pytest.raises(ValueError):reconcile(payload,choices,'single_select','',[])
    assert reconcile({'answer_text':'TEST_FREE_TEXT'},[],'text','',[])==('TEST_FREE_TEXT',[])
    with pytest.raises(ValueError):reconcile({'selected_options':['No']},[],'text','',[])


@pytest.mark.parametrize('mode',['standard','template'])
def test_legacy_text_without_selected_state_cannot_silently_export_empty_boxes(questionnaire,mode):
    client,route,parent,child,store=questionnaire
    with store.connect()as db:
        db.execute("UPDATE answers SET answer_text='No',selected_options_json='[]',manual_override=1 WHERE question_id=?",(parent['id'],));db.commit()
        old_exports=db.execute('SELECT COUNT(*) FROM exports').fetchone()[0]
    response=client.post(route+'/exports',json={'mode':mode})
    assert response.status_code==422,response.text
    assert '重新选择并保存' in response.json()['detail']
    unchanged=saved(client,route,parent['id']);assert unchanged['answer_text']=='No' and unchanged['selected_options']==[]
    with store.connect()as db:assert db.execute('SELECT COUNT(*) FROM exports').fetchone()[0]==old_exports
    assert client.patch(route+'/questions/'+parent['id'],json={'selected_options':['No']}).status_code==200
    assert client.post(route+'/exports',json={'mode':mode}).status_code==200
