"""Two parser hypotheses for one native label are not two physical fields."""
from copy import deepcopy
from questionnaire_structure_recognizer.validation.field_boundaries import restore_physical_fields


def example(label_text='Fictional check:\nInstruction text', entries=None, aligned=False):
    spans=[];offset=0
    for i,line in enumerate(label_text.splitlines(),1):
        spans.append({'paragraph_id':f'L:p{i}','paragraph_index':i,'start':offset,
                      'end':offset+len(line),'text':line})
        offset+=len(line)+1
    if entries is None:
        entries=[{'label_start':0,'label_end':15,'paragraph_id':'L:p1','paragraph_index':1,
                  'slot_id':'L:value-candidate'},
                 {'label_start':0,'label_end':15,'paragraph_id':'L:p1','paragraph_index':1,
                  'slot_id':'L:prompt-candidate'}]
    common={'table_id':'T','row_start':1,'row_end':1,'local_layout_type':'label_response_form',
            'table_layout_hint':'label_response_form_candidate'}
    label={'block_id':'L','text':label_text,'kind':'table_cell','order':1,
           'location':{'type':'docx','table':1,'row':1,'cell':1},'controls':[],
           'structure':{**common,'grid_col_start':1,'grid_col_end':1,'paragraph_spans':spans,
                        'label_list':deepcopy(entries),'logical_slots':deepcopy(entries)}}
    count=len(spans) if aligned else len(spans)+1
    paragraphs=[{'paragraph_id':f'A:p{i}','paragraph_index':i,'start':0,'end':0,'text':'','blank':True}
                for i in range(1,count+1)]
    answer={'block_id':'A','text':'','kind':'table_cell','order':2,
            'location':{'type':'docx','table':1,'row':1,'cell':2},'controls':[],
            'structure':{**common,'grid_col_start':2,'grid_col_end':2,'blank_state':'blank',
                         'paragraph_spans':paragraphs,
                         'logical_slots':[{'kind':'blank_paragraph_candidate','slot_id':f'A:p{i}:blank',
                             'paragraph_id':f'A:p{i}','paragraph_index':i,'start':0,'end':0}
                             for i in range(1,count+1)]}}
    return [label,answer]


def restore(blocks):
    fields=[]
    restore_physical_fields(fields,blocks,{b['block_id']:b for b in blocks})
    assert len(fields)==1
    return fields[0]


def test_duplicate_slot_hypotheses_have_one_unresolved_child_not_a_writable_whole_cell():
    blocks=example(); parent=restore(blocks)
    assert len(parent['children'])==1
    child=parent['children'][0]
    assert child['field_id']=='physical:L:0:child:15'
    assert child['answer_status']=='not_located' and child['answer_region_refs']==[]
    assert parent['answer_status']=='unresolved' and parent['answer_region_refs']==[]
    assert parent['answer_value_refs']==[] and parent['answer_text_original']==''


def test_aligned_duplicate_hypotheses_preserve_exact_single_original_slot():
    parent=restore(example(aligned=True))
    assert len(parent['children'])==1
    assert parent['children'][0]['answer_region_refs']==[
        {'block_id':'A','start':0,'end':0,'slot_id':'A:p1:blank'}]
    assert parent['answer_region_refs']==[]


def test_same_text_in_separate_paragraph_ranges_remains_two_fields():
    text='Name:\nName:'
    entries=[{'label_start':0,'label_end':4,'paragraph_id':'L:p1','paragraph_index':1},
             {'label_start':6,'label_end':10,'paragraph_id':'L:p2','paragraph_index':2}]
    parent=restore(example(text,entries,aligned=True))
    assert [c['question_text_original'] for c in parent['children']]==['Name','Name']
    assert len({c['field_id'] for c in parent['children']})==2
    assert [c['answer_region_refs'][0]['slot_id'] for c in parent['children']]==['A:p1:blank','A:p2:blank']


def test_distinct_ranges_within_one_paragraph_are_not_deduplicated():
    entries=[{'label_start':0,'label_end':4,'paragraph_id':'L:p1','paragraph_index':1},
             {'label_start':6,'label_end':11,'paragraph_id':'L:p1','paragraph_index':1}]
    parent=restore(example('Name: Title:',entries))
    assert [c['question_text_original'] for c in parent['children']]==['Name','Title']
    assert len({c['field_id'] for c in parent['children']})==2


def test_native_candidates_and_paragraph_evidence_are_not_deleted():
    blocks=example(); before=deepcopy(blocks[0]['structure'])
    restore(blocks)
    for key in ('label_list','logical_slots','paragraph_spans'):
        assert blocks[0]['structure'][key]==before[key]


def test_same_ranges_without_same_paragraph_identity_are_not_silently_merged():
    blocks=example();blocks[0]['structure']['label_list'][1]['paragraph_id']='L:p2'
    blocks[0]['structure']['label_list'][1]['paragraph_index']=2
    parent=restore(blocks)
    # Such contradictory source metadata remains visible for validation;
    # this narrowly scoped fix is not permission to deduplicate by text alone.
    assert len(parent['children'])==2
