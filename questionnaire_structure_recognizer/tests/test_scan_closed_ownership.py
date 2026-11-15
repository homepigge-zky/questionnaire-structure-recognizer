"""Physical bilingual questions supersede model fragments by source, not text."""
from copy import deepcopy
from questionnaire_structure_recognizer.validation.scanned_context_groups import reconcile_closed_field_ownership


def sample():
    def block(bid,text,box,kind=None):
        return {'block_id':bid,'text':text,'location':{'type':'pdf','page':1,'bbox':box,'extraction':'ocr'},
                'structure':{'scan_region_kind':kind} if kind else {}}
    blocks=[block('left','',[40,100,400,160],'text_box'),block('right','',[400,100,540,160],'text_box'),
            block('zh','虚构问题是否适用？',[45,110,230,124]),block('en','Does the fictional process apply?',[45,130,350,144]),
            block('yes','Yes',[430,110,452,123]),block('mark','',[410,112,420,122],'checkbox'),
            block('other','Unrelated question',[40,180,240,194])]
    def ref(bid):return {'block_id':bid,'start':0,'end':len(next(b for b in blocks if b['block_id']==bid)['text'])}
    native={'field_id':'native','boundary_evidence':'closed_scan_choice_row','physical_label_region_ref':ref('left'),
            'physical_response_region_ref':ref('right'),'label_refs':[ref('zh'),ref('en')],
            'question_refs':[ref('zh'),ref('en')],'question_text_original':'虚构问题是否适用？\nDoes the fictional process apply?',
            'answer_region_refs':[ref('mark')],'answer_value_refs':[ref('yes')],'answer_refs':[ref('yes')],
            'answer_text_original':'Yes','answer_status':'filled','children':[],
            'options':[{'refs':[ref('yes')],'selected':True,'text_original':'Yes','answer_region_refs':[ref('mark')]}]}
    fragment=deepcopy(native);fragment.update(field_id='model-en',boundary_evidence='model',label_refs=[ref('en')],question_refs=[ref('en')],question_text_original=blocks[3]['text'])
    return blocks,native,fragment,ref


def test_closed_bilingual_native_field_removes_nested_model_fragment():
    blocks,native,fragment,ref=sample()
    parent={'field_id':'context','label_refs':[ref('other')],'children':[fragment],'answer_region_refs':[]}
    fields=[parent,native];reconcile_closed_field_ownership(fields,blocks,{b['block_id']:b for b in blocks})
    assert fields==[parent,native] and parent['children']==[]
    assert native['question_text_original'].startswith('虚构问题') and 'Does the fictional' in native['question_text_original']
    assert native['answer_text_original']=='Yes'


def test_unrelated_model_question_cannot_borrow_the_closed_rows_selection():
    blocks,native,fragment,ref=sample();fragment['label_refs']=[ref('other')];fragment['question_refs']=[ref('other')]
    fields=[native,fragment];reconcile_closed_field_ownership(fields,blocks,{b['block_id']:b for b in blocks})
    assert len(fields)==2 and fragment['answer_status']=='unresolved'
    assert not fragment['answer_region_refs'] and not fragment['answer_value_refs'] and not fragment['answer_text_original']
    assert fragment['review_reasons']


def test_manual_fragment_is_preserved_and_the_automatic_duplicate_is_retired():
    blocks,native,fragment,ref=sample();fragment.update(manual_override=True,answer_text_original='MANUAL')
    original=deepcopy(fragment);fields=[fragment,native]
    reconcile_closed_field_ownership(fields,blocks,{b['block_id']:b for b in blocks})
    assert fields==[original]


def test_same_text_at_another_physical_location_is_not_merged():
    blocks,native,fragment,ref=sample();fragment.update(label_refs=[ref('other')],question_refs=[ref('other')],answer_region_refs=[],options=[],answer_value_refs=[])
    fields=[native,fragment];reconcile_closed_field_ownership(fields,blocks,{b['block_id']:b for b in blocks})
    assert len(fields)==2


def test_source_row_number_borrowing_answer_is_context_not_a_second_question():
    blocks,native,fragment,ref=sample()
    blocks.extend([{'block_id':'number-cell','text':'','location':{'type':'pdf','page':1,'bbox':[10,100,40,160]},'structure':{'scan_region_kind':'text_box'}},
                   {'block_id':'number','text':'9.6','location':{'type':'pdf','page':1,'bbox':[15,122,32,135]},'structure':{}}])
    fragment['label_refs']=[ref('number')];fragment['question_refs']=[ref('number')]
    fields=[native,fragment];reconcile_closed_field_ownership(fields,blocks,{b['block_id']:b for b in blocks})
    assert len(fields)==1 and native['question_number_original']=='9.6'
    assert native['number_refs']==[ref('number')]


def test_numeric_value_outside_proved_number_cell_is_not_discarded():
    blocks,native,fragment,ref=sample();blocks[-1]['text']='9.6'
    fragment.update(label_refs=[ref('other')],question_refs=[ref('other')],answer_region_refs=[],options=[])
    fields=[native,fragment];reconcile_closed_field_ownership(fields,blocks,{b['block_id']:b for b in blocks})
    assert len(fields)==2
