from copy import deepcopy

from questionnaire_structure_recognizer.validation.pdf_section_banners import resolve_pdf_section_banners


def source(bid, text, box, kind=None):
    return {'block_id':bid,'text':text,'order':int(bid[1:]),'kind':'ocr_text',
        'location':{'type':'pdf','page':1,'bbox':box,'extraction':'ocr_region'},
        'structure':{'scan_region_kind':kind,'scan_candidate':bool(kind)},'controls':[]}


def ref(block):
    return {'block_id':block['block_id'],'start':0,'end':len(block['text'])}


def fixture():
    banner=source('b1','',[50,100,550,111],'text_box')
    heading=source('b2','Inspection',[235,102,280,109])
    yes=source('b3','Yes',[300,117,317,125])
    no=source('b4','No',[325,117,340,125])
    blocks=[banner,heading,yes,no]
    fields=[{'field_id':'model-heading','label_refs':[ref(heading)],'question_text_original':heading['text'],
             'answer_region_refs':[ref(yes)],'answer_value_refs':[ref(yes)]}]
    for i in range(2):
        label=source(f'b{5+i*3}',f'Genuine question {i}?',[55,130+i*22,250,139+i*22])
        y=source(f'b{6+i*3}','',[298,130+i*22,308,139+i*22],'checkbox')
        n=source(f'b{7+i*3}','',[323,130+i*22,333,139+i*22],'checkbox')
        blocks.extend([label,y,n])
        fields.append({'field_id':f'field-{i}','label_refs':[ref(label)],'question_text_original':label['text'],
            'boundary_evidence':'scan_physical_form','answer_region_refs':[ref(y),ref(n)],'answer_value_refs':[],
            'options':[{'text_original':'Yes','refs':[ref(yes)],'answer_region_refs':[ref(y)]},
                       {'text_original':'No','refs':[ref(no)],'answer_region_refs':[ref(n)]}]})
    return blocks,fields


def test_shallow_banner_is_section_not_question_with_borrowed_option_header():
    blocks,fields=fixture();original=deepcopy(blocks)
    result=resolve_pdf_section_banners(fields,blocks,{b['block_id']:b for b in blocks})
    assert [f['field_id'] for f in fields]==['field-0','field-1']
    assert len(result)==1 and result[0]['removed_model_field_ids']==['model-heading']
    assert all(f['section_refs']==[ref(blocks[1])] for f in fields)
    assert all(b['text']==old['text'] and b['location']==old['location'] for b,old in zip(blocks,original))


def test_wide_empty_box_or_question_text_is_not_a_banner():
    for heading in ['', 'Inspection?', 'Please provide details:']:
        blocks,fields=fixture();blocks[1]['text']=heading
        before=deepcopy(fields)
        resolve_pdf_section_banners(fields,blocks,{b['block_id']:b for b in blocks})
        assert fields==before


def test_manual_or_independent_response_field_is_not_removed():
    for override in [True,False]:
        blocks,fields=fixture()
        if override:fields[0]['manual_override']=True
        else:fields[0]['answer_region_refs']=[ref(blocks[4])]
        resolve_pdf_section_banners(fields,blocks,{b['block_id']:b for b in blocks})
        assert any(f['field_id']=='model-heading' for f in fields)


def test_no_shared_physical_choice_columns_means_no_heading_inference():
    blocks,fields=fixture();fields[2]['options']=[]
    before=deepcopy(fields)
    assert resolve_pdf_section_banners(fields,blocks,{b['block_id']:b for b in blocks})==[]
    assert fields==before


def test_inherited_column_caption_requires_a_local_header_below_the_bar():
    blocks,fields=fixture();blocks[3]['location']['bbox']=[325,20,340,28]
    assert resolve_pdf_section_banners(fields,blocks,{b['block_id']:b for b in blocks})
    blocks,fields=fixture()
    blocks[2]['location']['bbox']=[300,20,317,28]
    blocks[3]['location']['bbox']=[325,20,340,28]
    before=deepcopy(fields)
    assert not resolve_pdf_section_banners(fields,blocks,{b['block_id']:b for b in blocks})
    assert fields==before
