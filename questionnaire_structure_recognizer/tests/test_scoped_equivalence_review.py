import hashlib
from copy import deepcopy
from docx import Document
from fastapi.testclient import TestClient
from questionnaire_structure_recognizer.api.app import create_app
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.form_positions import enrich_result_form_positions
from questionnaire_structure_recognizer.workflow import materialize_questionnaire

def setup(tmp_path):
    path=tmp_path/'FICTIONAL-scoped-alias.docx';doc=Document()
    doc.add_paragraph('To be filled by supplier representative');doc.add_heading('Personnel',1)
    t=doc.add_table(rows=2,cols=2);t.cell(0,0).text='Question';t.cell(0,1).text='Answer'
    t.cell(1,0).text='Does fictional process alpha comply?';doc.save(path)
    r=validate_and_assemble(extract_document(path).model_dump(),[],{'provider':'fixture'});enrich_result_form_positions(r)
    app=create_app(tmp_path/'data','test',auth_mode='key');store=app.state.store
    (store.files/path.name).write_bytes(path.read_bytes())
    with store.connect()as db:
        db.execute('INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,purpose,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)',('t',path.name,path.name,hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_size,'ready','test','questionnaire','{}'));db.commit()
    store.save_version('t',r,'model')
    with store.connect()as db:materialize_questionnaire(db,store.task('t'),r,{'product':'Fictional A'})
    return app,store

def test_explicit_equivalence_is_scoped_and_versioned_and_preserves_manual(tmp_path):
    app,store=setup(tmp_path);h={'X-QSR-Key':'test'}
    with TestClient(app)as c:
        kid=c.post('/api/knowledge/manual',headers=h,json={'canonical_question':'Doesfictionalprocessalphacomply?', 'section':'Historic personnel training','context':'Fictional training programme', 'answer':'Yes','approved':True,'applicable_scope':{'product':'Fictional A'}}).json()['id']
        snap=c.get('/api/questionnaires/t',headers=h).json();q=next(q for q in snap['questions']if 'Does fictional'in q['question_text']);assert not q['answer_text']
        route=f"/api/questionnaires/t/questions/{q['id']}/confirm-equivalence"
        payload={'knowledge_id':kid,'answer_version':1,'confirmed_equivalent':True,'expected_question_signature':q['equivalence_review_signature'],'reviewer':'FICTIONAL HUMAN REVIEW'}
        payload['expected_knowledge_signature']=next(k for k in c.get('/api/knowledge',headers=h).json()if k['id']==kid)['equivalence_review_signature']
        res=c.post(route,headers=h,json=payload);assert res.status_code==200,res.text
        snap=c.get('/api/questionnaires/t',headers=h).json();saved=next(x for x in snap['questions']if x['id']==q['id']);assert saved['answer_text']=='Yes'
        assert saved['matches'][0]['method']=='confirmed_scoped_alias'
        # Merely changing the target section must not authorize cross-context reuse.
        with store.connect()as db:db.execute('UPDATE questions SET section=? WHERE id=?',('Different independent section',q['id']));db.commit()
        c.post('/api/questionnaires/t/rematch',headers=h)
        assert not next(x for x in c.get('/api/questionnaires/t',headers=h).json()['questions']if x['id']==q['id'])['answer_text']
        with store.connect()as db:db.execute('UPDATE questions SET section=? WHERE id=?',(q['section'],q['id']));db.commit()
        c.post('/api/questionnaires/t/rematch',headers=h)
        assert next(x for x in c.get('/api/questionnaires/t',headers=h).json()['questions']if x['id']==q['id'])['answer_text']=='Yes'
        c.patch('/api/knowledge/'+kid,headers=h,json={'answer':'No'})
        c.post('/api/questionnaires/t/rematch',headers=h)
        assert not next(x for x in c.get('/api/questionnaires/t',headers=h).json()['questions']if x['id']==q['id'])['answer_text']
        assert c.post(route,headers=h,json=payload).status_code==409
        payload['answer_version']=2
        payload['expected_knowledge_signature']=next(k for k in c.get('/api/knowledge',headers=h).json()if k['id']==kid)['equivalence_review_signature']
        c.patch(f"/api/questionnaires/t/questions/{q['id']}",headers=h,json={'answer_text':'KEEP HUMAN'})
        assert c.post(route,headers=h,json=payload).status_code==200
        preserved=next(x for x in c.get('/api/questionnaires/t',headers=h).json()['questions']if x['id']==q['id'])
        assert preserved['answer_text']=='KEEP HUMAN'and preserved['manual_override']

def test_unapproved_or_unconfirmed_equivalence_cannot_be_saved(tmp_path):
    app,store=setup(tmp_path);h={'X-QSR-Key':'test'}
    with TestClient(app)as c:
        kid=c.post('/api/knowledge/manual',headers=h,json={'canonical_question':'Fictional historical question','answer':'Yes','approved':False}).json()['id']
        q=c.get('/api/questionnaires/t',headers=h).json()['questions'][0]
        route=f"/api/questionnaires/t/questions/{q['id']}/confirm-equivalence"
        assert c.post(route,headers=h,json={'knowledge_id':kid,'answer_version':1}).status_code==422
        assert c.post(route,headers=h,json={'knowledge_id':kid,'answer_version':1,'confirmed_equivalent':True,'reviewer':'reviewer'}).status_code==422


def test_changed_target_or_scope_requires_new_review(tmp_path):
    app,store=setup(tmp_path);h={'X-QSR-Key':'test'}
    with TestClient(app)as c:
        kid=c.post('/api/knowledge/manual',headers=h,json={'canonical_question':'Historical fictional question','answer':'Yes','approved':True}).json()['id']
        q=c.get('/api/questionnaires/t',headers=h).json()['questions'][0]
        route=f"/api/questionnaires/t/questions/{q['id']}/confirm-equivalence"
        payload={'knowledge_id':kid,'answer_version':1,'confirmed_equivalent':True,'reviewer':'reviewer','expected_question_signature':q['equivalence_review_signature']}
        payload['expected_knowledge_signature']=next(k for k in c.get('/api/knowledge',headers=h).json()if k['id']==kid)['equivalence_review_signature']
        with store.connect()as db:db.execute('UPDATE questionnaire_tasks SET product=? WHERE id=?',('Different product','t'));db.commit()
        assert c.post(route,headers=h,json=payload).status_code==409
        with store.connect()as db:db.execute('UPDATE questionnaire_tasks SET product=? WHERE id=?',('Fictional A','t'));db.execute('UPDATE questions SET question_text=? WHERE id=?',('Different question?',q['id']));db.commit()
        assert c.post(route,headers=h,json=payload).status_code==409


def test_explicit_standard_field_and_role_conflicts_are_not_overridable():
    from questionnaire_structure_recognizer.scoped_aliases import semantic_conflict
    for target,source,target_section,source_section in [
        ('Telephone','Fax','',''),('Company Name','Contact person name','',''),
        ('Location','Location','Headquarters','Manufacturing site'),
        ('Name','Name','Quality assurance contact','Contact information')]:
        q={'question_text':target,'section':target_section}
        k={'canonical_question':source,'section':source_section,'context':'','applicable_scope_json':'{}'}
        assert semantic_conflict(k,q,{},{}),(target,source)


def test_source_range_change_invalidates_grant_but_metadata_does_not():
    from questionnaire_structure_recognizer.scoped_aliases import signature
    import json
    k={'id':'known','answer':'Yes','answer_version':1,'source_location_json':json.dumps({'source_hash':'a','field_id':'field','question_refs':[{'block_id':'b','start':0,'end':8}]})}
    before=signature(k);s=json.loads(k['source_location_json']);s['reuse_constraint_policy']='metadata update';k['source_location_json']=json.dumps(s)
    assert signature(k)==before
    s['question_refs'][0]['end']=12;k['source_location_json']=json.dumps(s)
    assert signature(k)!=before


def test_endpoint_rejects_distinct_standard_fields_and_product_scope(tmp_path):
    app,store=setup(tmp_path);h={'X-QSR-Key':'test'}
    with TestClient(app)as c:
        kid=c.post('/api/knowledge/manual',headers=h,json={'canonical_question':'Fax','answer':'12345','approved':True}).json()['id']
        q=c.get('/api/questionnaires/t',headers=h).json()['questions'][0]
        with store.connect()as db:db.execute('UPDATE questions SET question_text=? WHERE id=?',('Telephone',q['id']));db.commit()
        q=c.get('/api/questionnaires/t',headers=h).json()['questions'][0]
        route=f"/api/questionnaires/t/questions/{q['id']}/confirm-equivalence"
        payload={'knowledge_id':kid,'answer_version':1,'confirmed_equivalent':True,'reviewer':'reviewer','expected_question_signature':q['equivalence_review_signature']}
        payload['expected_knowledge_signature']=next(k for k in c.get('/api/knowledge',headers=h).json()if k['id']==kid)['equivalence_review_signature']
        rejected=c.post(route,headers=h,json=payload)
        assert rejected.status_code==422 and '标准字段'in rejected.json()['detail']
        c.patch('/api/knowledge/'+kid,headers=h,json={'canonical_question':'Telephone','applicable_scope':{'product':'Other product'}})
        payload['expected_knowledge_signature']=next(k for k in c.get('/api/knowledge',headers=h).json()if k['id']==kid)['equivalence_review_signature']
        rejected=c.post(route,headers=h,json=payload)
        assert rejected.status_code==422
        with store.connect()as db:assert db.execute('SELECT COUNT(*) FROM knowledge_alias_scopes').fetchone()[0]==0


def test_changed_source_question_without_answer_version_requires_new_review(tmp_path):
    app,store=setup(tmp_path);h={'X-QSR-Key':'test'}
    with TestClient(app)as c:
        kid=c.post('/api/knowledge/manual',headers=h,json={'canonical_question':'Historical wording','answer':'Yes','approved':True}).json()['id']
        k=next(k for k in c.get('/api/knowledge',headers=h).json()if k['id']==kid)
        q=c.get('/api/questionnaires/t',headers=h).json()['questions'][0]
        payload={'knowledge_id':kid,'answer_version':1,'confirmed_equivalent':True,'reviewer':'reviewer','expected_question_signature':q['equivalence_review_signature'],'expected_knowledge_signature':k['equivalence_review_signature']}
        edited=c.patch('/api/knowledge/'+kid,headers=h,json={'canonical_question':'Different historical business question'})
        assert edited.json()['answer_version']==1
        result=c.post(f"/api/questionnaires/t/questions/{q['id']}/confirm-equivalence",headers=h,json=payload)
        assert result.status_code==409 and '历史问题'in result.json()['detail']
