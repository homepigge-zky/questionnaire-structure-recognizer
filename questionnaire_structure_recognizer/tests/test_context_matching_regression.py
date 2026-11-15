"""Real q4 failure shapes, with fictional values and an isolated database."""
import hashlib
from docx import Document
from fastapi.testclient import TestClient
from questionnaire_structure_recognizer.api.app import create_app
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.semantic_fields import classify_field
from questionnaire_structure_recognizer.workflow import materialize_questionnaire

CONTACT = 'Part II: GENERAL COMPANY INFORMATION\nD. Contact Information (Please provide details of an available contact person)'
OPERATIONS = 'Part VI: COMPLETION\nSite Operations Lead Representative:'
QUALITY = 'Part VI: COMPLETION\nHead of Quality Assurance or representative; person who completed the questionnaire:'


def test_specific_contact_section_beats_generic_company_heading():
    result = classify_field('1. Name:', CONTACT)
    assert result['canonical_field_key'] == 'person.name'
    assert result['qualifiers']['person_role'] == 'contact_person'
    assert classify_field('Position:', OPERATIONS)['qualifiers']['person_role'] == 'site_representative'
    assert classify_field('Position:', QUALITY)['qualifiers']['person_role'] == 'quality_contact'
    assert classify_field('Telephone:', CONTACT)['qualifiers']['contact_role'] == 'contact_person'
    assert classify_field('Phone No.', 'General information', 'Details of the manufacturer')['canonical_field_key'] == 'contact.phone'


def test_product_name_uses_explicit_product_details_section():
    assert classify_field('Name:', 'A -1Details on theproduct:')['canonical_field_key']=='product.name'
    assert classify_field('Name:', 'Product information')['canonical_field_key']=='product.name'
    assert classify_field('Name:', 'General information')['canonical_field_key']==''
    assert classify_field('Name:', 'Product Quality Assurance contact details')['canonical_field_key']=='person.name'
    assert classify_field('Name:', 'A -2Details on themanufacturer (if not identical with supplier)')['canonical_field_key']=='organization.name'


def test_exact_generic_labels_cannot_cross_person_or_section_context(tmp_path):
    labels = [('company','Company Name:','Company information'),
              ('contact-name','1. Name:',CONTACT),
              ('contact-position','2. Position:',CONTACT),
              ('operations-position','Position:',OPERATIONS),
              ('quality-position','Position:',QUALITY),
              ('contact-phone','Telephone:',CONTACT),
              ('comment-a','Comment','Manufacturing controls'),
              ('comment-b','Comment','Laboratory controls')]
    source=tmp_path/'fictional.docx';doc=Document();t=doc.add_table(rows=len(labels),cols=2)
    for i,(_,label,_) in enumerate(labels):t.cell(i,0).text=label
    doc.save(source);native=extract_document(source).model_dump()
    cells={(b['structure']['row_start'],b['structure']['column_start']):b for b in native['source_blocks'] if b['kind']=='table_cell'}
    ref=lambda b:{'block_id':b['block_id'],'start':0,'end':len(b['text'])}
    result={**native,'questions':[]}
    for i,(fid,label,section) in enumerate(labels,1):
        result['questions'].append({'field_id':fid,'question_id':fid,'question_text_original':label,'section_text_original':section,'label_refs':[ref(cells[i,1])],'question_refs':[ref(cells[i,1])],'answer_region_refs':[ref(cells[i,2])],'answer_value_refs':[],'answer_status':'blank','answer_text_original':'','children':[],'options':[]})
    app=create_app(tmp_path/'data','key');store=app.state.store;headers={'X-QSR-Key':'key'}
    with TestClient(app) as client:
        for question,answer,section in [('Company Name','TEST_CORPORATION','Company information'),('Position','TEST_CONTACT_POSITION',CONTACT),('Telephone','TEST_MAIN_PHONE','Company information'),('Comment','TEST_MANUFACTURING_NOTE','Manufacturing controls')]:
            response=client.post('/api/knowledge/manual',headers=headers,json={'canonical_question':question,'answer':answer,'section':section,'approved':True})
            assert response.status_code==200
        task_id='context-task';(store.files/'fixture.docx').write_bytes(source.read_bytes())
        with store.connect() as db:
            db.execute('INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,purpose,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)',(task_id,source.name,'fixture.docx',hashlib.sha256(source.read_bytes()).hexdigest(),source.stat().st_size,'ready','fixture','questionnaire','{}'));db.commit()
        store.save_version(task_id,result,'model')
        with store.connect() as db:materialize_questionnaire(db,store.task(task_id),result,{})
        fields={q['field_id']:q for q in client.get(f'/api/questionnaires/{task_id}',headers=headers).json()['questions']}
        assert fields['company']['answer_text']=='TEST_CORPORATION'
        assert fields['contact-position']['answer_text']=='TEST_CONTACT_POSITION'
        assert fields['comment-a']['answer_text']=='TEST_MANUFACTURING_NOTE'
        for fid in ('contact-name','operations-position','quality-position','contact-phone','comment-b'):
            assert fields[fid]['answer_text']=='', (fid,fields[fid]['answer_text'])
        # A group heading has no single response position: matching belongs
        # to its actual child fields, not a concatenated Name/Website prompt.
        group={**result['questions'][0],'field_id':'container','question_id':'container',
               'question_text_original':'Company Name: Website', 'answer_region_refs':[],
               'children':result['questions']}
        grouped={**result,'questions':[group]}
        store.save_version(task_id,grouped,'model')
        with store.connect() as db:materialize_questionnaire(db,store.task(task_id),grouped,{})
        fields={q['field_id']:q for q in client.get(f'/api/questionnaires/{task_id}',headers=headers).json()['questions']}
        assert not fields['container']['matches']
        assert fields['company']['answer_text']=='TEST_CORPORATION'
