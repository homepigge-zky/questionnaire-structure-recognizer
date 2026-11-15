"""Knowledge drafts require real, non-overlapping, exact source text."""
from copy import deepcopy
from questionnaire_structure_recognizer.knowledge import extract_pending_entries


def fixture():
    text='Name: TEST_NAME Phone: TEST_PHONE'
    def ref(start,end):return {'block_id':'cell','start':start,'end':end}
    fields=[]
    for ident,label,value in [('name','Name','TEST_NAME'),('phone','Phone','TEST_PHONE')]:
        fields.append({'field_id':ident,'question_text_original':label,
            'label_refs':[ref(text.index(label),text.index(label)+len(label))],
            'answer_value_refs':[ref(text.index(value),text.index(value)+len(value))],
            'answer_text_original':value,'answer_status':'filled',
            'answer_origin':'source_document','pairing_validation':'valid'})
    return {'id':'fictional-only','filename':'fictional.docx'}, {'source_blocks':[{'block_id':'cell','text':text}], 'questions':fields}


def test_independent_slots_in_one_cell_can_be_reviewed():
    task,result=fixture()
    drafts=extract_pending_entries(task,1,result)
    assert [d['answer'] for d in drafts]==['TEST_NAME','TEST_PHONE']


def test_zero_length_and_forged_text_do_not_create_knowledge():
    task,result=fixture()
    for case in ['empty','different_text','missing_block']:
        bad=deepcopy(result); field=bad['questions'][0]
        if case=='empty':field['answer_value_refs'][0]['end']=field['answer_value_refs'][0]['start']
        if case=='different_text':field['answer_text_original']='NOT_IN_SOURCE'
        if case=='missing_block':field['answer_value_refs'][0]['block_id']='nonexistent'
        assert [d['answer'] for d in extract_pending_entries(task,1,bad)]==['TEST_PHONE']


def test_other_field_label_cannot_become_a_source_answer():
    task,result=fixture()
    result['questions'][0]['answer_value_refs']=deepcopy(result['questions'][1]['label_refs'])
    result['questions'][0]['answer_text_original']='Phone'
    assert [d['answer'] for d in extract_pending_entries(task,1,result)]==['TEST_PHONE']
