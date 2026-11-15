from copy import deepcopy

import pytest

from questionnaire_structure_recognizer.validation.field_boundaries import ref
from questionnaire_structure_recognizer.validation.form_completion import new_field
from questionnaire_structure_recognizer.validation.mixed_overlay_boundaries import restore_mixed_overlay_boundaries


def block(key,text,box,extraction='ocr_region',**structure):
    return {'block_id':key,'text':text,'kind':'paragraph' if extraction=='native' else 'ocr_text',
            'location':{'page':1,'type':'pdf','bbox':box,'extraction':extraction},'structure':structure}


def run(fields,blocks):
    return restore_mixed_overlay_boundaries(fields,blocks,{b['block_id']:b for b in blocks})


def conditional_sample():
    parent=block('parent','Fictional process in use?',[40,90,260,102])
    caption=block('caption','Yes No',[435,90,525,102])
    boxes=[block('box'+str(i),'',[460+i*30,90,470+i*30,102],'ocr',scan_region_kind='checkbox') for i in range(2)]
    prompt=block('prompt','If Yes: please describe the process:',[40,111,178,122])
    rows=[block(k,'',coords,'ocr',scan_region_kind='write_line',blank_state=state) for k,coords,state in
          [('first',[180,113,510,124],'filled'),('middle',[40,131,510,142],'filled'),('last',[40,149,510,160],'blank')]]
    vals=[block('v1','FIRST SENTENCE',[182,110,310,123],'native',native_overlay_answer_region='first'),
          block('v2','SECOND SENTENCE WITH REQUIRED QUALIFIER',[42,128,385,140],'native',native_overlay_answer_region='middle')]
    for r,v in zip(rows,vals):r['structure']['overlay_value_block_ids']=[v['block_id']]
    field=new_field(prompt,ref(prompt),[ref(rows[0]),ref(rows[-1])])
    field.update(boundary_evidence='scan_physical_form',label_refs=[ref(parent),ref(prompt)],answer_text_original='FIRST SENTENCE',
                 answer_value_refs=[ref(vals[0])],answer_status='filled')
    return field,[parent,caption,*boxes,prompt,*rows,*vals]


def test_verified_intervening_line_is_preserved_and_conditional_prompt_is_bounded():
    f,blocks=conditional_sample(); run([f],blocks)
    assert [r['block_id'] for r in f['answer_region_refs']]==['first','middle','last']
    assert f['answer_text_original']=='FIRST SENTENCE\nSECOND SENTENCE WITH REQUIRED QUALIFIER'
    assert [r['block_id'] for r in f['label_refs']]==['prompt']
    assert f['explicit_condition']['parent_field_id'] is None
    assert [r['block_id'] for r in f['conditional_parent_label_refs']]==['parent']


@pytest.mark.parametrize('hazard',['neighbor','different_lane','manual_ancestor','other_owner'])
def test_intervening_text_cannot_be_stolen_from_a_different_field(hazard):
    field,blocks=conditional_sample(); fields=[field]
    if hazard=='neighbor':blocks.append(block('next','Another question?',[40,123,200,134]))
    if hazard=='different_lane':next(x for x in blocks if x['block_id']=='middle')['location']['bbox'][0]=65
    if hazard=='manual_ancestor':
        outer=deepcopy(field);outer.update(field_id='outer',children=[field],manual_override=True);fields=[outer]
    if hazard=='other_owner':
        extra=block('other','Another field',[40,164,210,177]);blocks.append(extra)
        fields.append(new_field(extra,ref(extra),[ref(next(x for x in blocks if x['block_id']=='middle'))]))
    run(fields,blocks)
    assert field['answer_text_original']=='FIRST SENTENCE'


def choice_sample():
    prompt=block('prompt','Does QA perform the sampling?',[75,347,426,359])
    options=[block('y','Yes',[433,347,449,359]),block('n','No',[477,347,491,359])]
    regions=[block('cy','',[461,346,472,357],'ocr',scan_region_kind='checkbox'),
             block('cn','',[495,346,505,357],'ocr',scan_region_kind='checkbox')]
    f=new_field(prompt,ref(prompt),[ref(r) for r in regions]);f.update(boundary_evidence='scan_physical_form',
       answer_status='filled',answer_text_original='Yes',answer_value_refs=[ref(options[0])],
       options=[{'text_original':o['text'],'selected':i==0,'refs':[ref(o)],'answer_region_refs':[ref(r)]} for i,(o,r) in enumerate(zip(options,regions))])
    note=block('note','Only raw materials are sampled by QA. Other samples are taken by operators.',[76,360,495,369],'native')
    next_prompt=block('next','5.3 Are retained samples kept?',[75,374,382,386])
    return f,[prompt,*options,*regions,note,next_prompt],note


def test_a_native_qualification_under_a_physical_selected_row_is_exposed():
    field,blocks,note=choice_sample(); run([field],blocks)
    assert field['answer_text_original']=='Yes'
    assert field['answer_qualification_refs']==[ref(note)]
    assert field['source_locations']['answer_qualifications'][0]['text']==note['text']


@pytest.mark.parametrize('hazard',['other_target','different_column','printed_prompt','after_next_question','manual'])
def test_a_separate_native_field_or_manual_answer_is_not_reclassified(hazard):
    field,blocks,note=choice_sample()
    if hazard=='other_target':note['structure']['native_overlay_answer_region']='another-target'
    if hazard=='different_column':note['location']['bbox']=[320,360,495,369]
    if hazard=='printed_prompt':note['text']='A different question?'
    if hazard=='after_next_question':note['location']['bbox']=[75,387,495,398]
    if hazard=='manual':field['answer_origin']='manual'
    run([field],blocks)
    assert not field.get('answer_qualification_refs')
