from copy import deepcopy
import json

from questionnaire_structure_recognizer.understanding.local_pairs import refine_stacked_pairs
from questionnaire_structure_recognizer.validation import validate_and_assemble


def source():
    return {'file_type':'PDF','document_id':'fictional','source_hash':'fictional-hash','parser_version':'fictional-v1','extraction_status':'complete',
            'source_blocks':[{'block_id':str(i),'kind':'table_cell','text':text,'order':i,'controls':[],
                'location':{'type':'pdf','page':1},'structure':{'table_id':'t1','cell_id':str(i),'row_start':i,
                'row_end':i,'grid_col_start':1,'grid_col_end':1,'table_column_count':1}}
                for i,text in enumerate(('Instructions '*40,'Supplied product','TEST_PRODUCT'),1)]}


def test_local_retry_accepts_only_exact_source_ids_and_caches(tmp_path,monkeypatch):
    calls=[]
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'message':{'content':json.dumps({'relation':'label_then_existing_value','label_id':'C1','value_id':'C2'})}}
    monkeypatch.setattr('questionnaire_structure_recognizer.understanding.local_pairs.httpx.post',lambda *a,**k:(calls.append(k) or Response()))
    native=source(); metrics=refine_stacked_pairs(native,tmp_path)
    assert metrics[0]['accepted'] and native['source_blocks'][1]['structure']['stacked_pair_candidate_block_id']=='3'
    assert metrics[0]['automatic_pairing_authorized'] is False
    assert 'TEST_PRODUCT' in calls[0]['json']['messages'][1]['content']
    again=source();assert refine_stacked_pairs(again,tmp_path)[0]['cache_hit']
    assert len(calls)==1
    result=validate_and_assemble(native,[{'classifications':[]}],{'model':'fixture'})
    field=next(f for f in result['questions'] if f['question_text_original']=='Supplied product')
    assert field['pairing_validation']=='needs_review'
    assert field['answer_status']=='unresolved' and field['answer_origin']=='none'
    assert not field['form_definition']['auto_fill_eligible']


def test_reversed_or_unresolved_local_relation_never_creates_target(tmp_path,monkeypatch):
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'message':{'content':json.dumps({'relation':'label_then_existing_value','label_id':'C2','value_id':'C1'})}}
    monkeypatch.setattr('questionnaire_structure_recognizer.understanding.local_pairs.httpx.post',lambda *a,**k:Response())
    native=source();assert not refine_stacked_pairs(native,tmp_path)[0]['accepted']
    assert all('stacked_pair_candidate_block_id' not in b['structure'] for b in native['source_blocks'])


def test_local_retry_never_sends_to_external_service(tmp_path,monkeypatch):
    monkeypatch.setenv('QSR_OLLAMA_URL','https://example.invalid')
    monkeypatch.setattr('questionnaire_structure_recognizer.understanding.local_pairs.httpx.post',lambda *a,**k:(_ for _ in ()).throw(AssertionError('network must not be used')))
    assert refine_stacked_pairs(source(),tmp_path)==[{'failed':'non_local_model_blocked'}]
