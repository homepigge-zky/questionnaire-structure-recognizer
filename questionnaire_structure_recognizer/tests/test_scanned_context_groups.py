from copy import deepcopy
from questionnaire_structure_recognizer.validation.scanned_context_groups import resolve_scanned_context_groups
from questionnaire_structure_recognizer.validation.form_completion import new_field
from questionnaire_structure_recognizer.validation.field_boundaries import ref


def block(bid,text,box,kind=None):
    return {'block_id':bid,'kind':'ocr_text','text':text,'order':len(bid),'location':{'type':'pdf','page':1,'extraction':'ocr','bbox':box},'structure':{'scan_region_kind':kind,'source_method':'raster_geometry'},'controls':[],'style':{}}


def fixture():
    head=block('head','Are records kept for the following production details?',[52,104,540,120]);hc=block('header_cell','',[50,100,560,130],'text_box')
    blocks=[head,hc];children=[]
    for i in range(2):
        y=130+i*40;label=block(f'label{i}',f'Detail {i}',[55,y+10,400,y+25]);left=block(f'left{i}','',[50,y,440,y+40],'text_box');right=block(f'right{i}','',[440,y,560,y+40],'text_box');mark=block(f'mark{i}','',[450,y+8,460,y+18],'checkbox');caption=block(f'caption{i}','Yes',[465,y+8,490,y+20])
        f=new_field(label,ref(label),[ref(mark)]);f.update(field_id=f'child{i}',question_id=f'child{i}',physical_label_region_ref=ref(left),physical_response_region_ref=ref(right),options=[{'refs':[ref(caption)],'text_original':'Yes','selected':True,'answer_region_refs':[ref(mark)]}],answer_value_refs=[ref(caption)],answer_refs=[ref(caption)],answer_text_original='Yes',answer_status='filled',boundary_evidence='closed_scan_choice_row')
        blocks.extend([label,left,right,mark,caption]);children.append(f)
    parent=new_field(head,ref(head),deepcopy(children[0]['answer_region_refs']));parent.update(field_id='wrong_parent',question_id='wrong_parent',options=deepcopy(children[0]['options']),answer_value_refs=deepcopy(children[0]['answer_value_refs']),answer_text_original='Yes',answer_status='filled')
    return [parent]+children,blocks,{b['block_id']:b for b in blocks}


def test_closed_spanning_parent_cannot_borrow_first_childs_choice():
    fields,blocks,lookup=fixture();resolve_scanned_context_groups(fields,blocks,lookup)
    assert len(fields)==1
    parent=fields[0]
    assert parent['answer_text_original']=='' and not parent['answer_region_refs'] and not parent['options']
    assert [c['field_id'] for c in parent['children']]==['child0','child1']
    assert all(c['answer_text_original']=='Yes' for c in parent['children'])
    assert all(c['parent_question_id']==parent['field_id'] for c in parent['children'])


def test_no_closed_header_or_manual_answer_is_never_reclassified():
    fields,blocks,lookup=fixture();fields[0]['manual_override']=True;before=deepcopy(fields)
    resolve_scanned_context_groups(fields,blocks,lookup);assert {f['field_id']:f for f in fields}=={f['field_id']:f for f in before}
    fields,blocks,lookup=fixture();blocks.remove(lookup.pop('header_cell'));before=deepcopy(fields)
    resolve_scanned_context_groups(fields,blocks,lookup)
    # Without the physical spanning header, do not invent a parent/child
    # group. The old model's borrowed child selection still has to be blocked:
    # absence of a header is not permission to reuse another row's answer.
    assert [f['field_id'] for f in fields]==[f['field_id'] for f in before]
    assert fields[1:]==before[1:]
    assert fields[0]['label_refs']==before[0]['label_refs']
    assert fields[0]['question_text_original']==before[0]['question_text_original']
    assert fields[0]['children']==[] and fields[0]['answer_status']=='unresolved'
    assert not fields[0]['answer_region_refs'] and not fields[0]['answer_value_refs']
    assert fields[0]['source_boundary_conflict']['owner_field_ids']


def test_comment_inside_response_cell_is_retained_as_source_note_not_question():
    fields,blocks,lookup=fixture();fields=fields[1:]
    note=block('note','Available during on-site review',[445,152,555,169]);blocks.append(note);lookup[note['block_id']]=note;fields.append(new_field(note,ref(note)))
    result=resolve_scanned_context_groups(fields,blocks,lookup)
    assert len(fields)==2 and fields[0]['answer_text_original']=='Yes'
    assert fields[0]['source_comment_refs']==[ref(note)]
    assert any(x['text_original']==note['text'] for x in result)


def test_no_cross_cell_comment_association():
    fields,blocks,lookup=fixture();fields=fields[1:]
    note=block('note','Do not attach this',[570,152,750,169]);blocks.append(note);lookup[note['block_id']]=note;fields.append(new_field(note,ref(note)));before=deepcopy(fields)
    resolve_scanned_context_groups(fields,blocks,lookup);assert {f['field_id']:f for f in fields}=={f['field_id']:f for f in before}


def test_independent_numbered_row_does_not_become_child_of_preceding_group():
    fields,blocks,lookup=fixture()
    left=block('independent_left','',[50,210,440,250],'text_box');right=block('independent_right','',[440,210,560,250],'text_box')
    label=block('independent_label','Independent question?',[55,220,400,235]);mark=block('independent_mark','',[450,218,460,228],'checkbox')
    nc=block('number_cell','',[20,210,50,250],'text_box');number=block('number','9.2',[24,222,45,235])
    f=new_field(label,ref(label),[ref(mark)]);f.update(field_id='independent',question_id='independent',physical_label_region_ref=ref(left),physical_response_region_ref=ref(right),options=[{'text_original':'Yes','refs':[],'answer_region_refs':[ref(mark)]}])
    blocks.extend([left,right,label,mark,nc,number]);lookup.update({b['block_id']:b for b in blocks});fields.append(f)
    resolve_scanned_context_groups(fields,blocks,lookup)
    assert len(fields)==2 and any(x['field_id']=='independent' and not x.get('parent_question_id') for x in fields)
    parent=next(x for x in fields if x.get('children'));assert len(parent['children'])==2


def test_numbered_section_bar_is_preserved_but_cannot_borrow_answer():
    fields,blocks,lookup=fixture();bar=block('bar','',[20,65,560,95],'text_box');heading=block('heading','9. Written procedures',[25,70,320,85]);wrong=new_field(heading,ref(heading),deepcopy(fields[1]['answer_region_refs']));wrong['answer_text_original']='Yes';wrong['answer_status']='filled'
    blocks.extend([bar,heading]);lookup.update({b['block_id']:b for b in blocks});fields.insert(0,wrong)
    notes=resolve_scanned_context_groups(fields,blocks,lookup)
    assert not any(f['question_text_original']=='9. Written procedures' for f in fields)
    assert any(n['role']=='section_header' and n['text_original']=='9. Written procedures' for n in notes)
    assert heading['text']=='9. Written procedures'


def test_conditional_blank_line_keeps_prompt_and_parent_without_creating_answer():
    box=block('cell','',[40,20,560,160],'text_box');q=block('q','Who releases the items?',[45,25,500,45]);cond=block('condition','If not, who is responsible?',[45,100,450,115]);ans=block('ans','Ans:',[45,125,80,140]);line=block('line','',[85,125,350,140],'write_line')
    p=new_field(q,ref(q));p.update(field_id='parent',question_id='parent',options=[{'text_original':'Item','refs':[]}],selection_mode_evidence={'container_ref':ref(box)})
    old=new_field(ans,ref(ans),[ref(line)]);blocks=[box,q,cond,ans,line];lookup={b['block_id']:b for b in blocks};fields=[p,old]
    resolve_scanned_context_groups(fields,blocks,lookup)
    assert len(fields)==1 and len(p['children'])==1
    child=p['children'][0]
    assert child['question_text_original']=='If not, who is responsible?\nAns:'
    assert child['answer_status']=='blank' and not child['answer_text_original'] and child['answer_region_refs']==[ref(line)]
    assert child['explicit_condition']['status']=='unresolved'


def test_page_counter_removal_keeps_other_source_fragments():
    q=block('q','A real source note',[45,500,350,520]);counter=block('counter','1/1',[280,790,305,805]);tail=block('tail','Another source',[500,600,560,620])
    mixed=new_field(q,ref(q));mixed['label_refs']=[ref(q),ref(counter)];mixed['question_refs']=deepcopy(mixed['label_refs']);mixed['question_text_original']=q['text']+'\n'+counter['text']
    blocks=[q,counter,tail];fields=[mixed];notes=resolve_scanned_context_groups(fields,blocks,{b['block_id']:b for b in blocks})
    assert len(fields)==1 and fields[0]['question_text_original']=='A real source note'
    assert any(n['role']=='footer' and n['text_original']=='1/1' for n in notes)
