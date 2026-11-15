"""Independent tiny grids: empty boxes are not failed selections."""
from copy import deepcopy
from questionnaire_structure_recognizer.validation.scanned_cells import _restore_choice_cells, restore_scanned_cell_pairs
from questionnaire_structure_recognizer.parser_core.pdf_caption_fragments import VERSION


def grid():
    blocks=[]
    def b(i,text,box,kind=None):
        item={'block_id':i,'text':text,'order':len(blocks),'kind':'ocr_text',
            'location':{'type':'pdf','page':1,'extraction':'ocr','bbox':box},'controls':[],
            'structure':{'scan_region_kind':kind} if kind else {},'style':{'ocr_confidence':.99}}
        blocks.append(item);return item
    for i,y in enumerate([10,60,150]):b('anchor'+str(i),'',[10,y,100,y+45],'text_box')
    b('top','',[10,108,300,110],'write_line');b('bottom','',[10,138,300,140],'write_line')
    b('label','Temperature bands',[15,117,95,128]);b('response','',[100,110,300,140],'text_box')
    b('captions','CoolWarm',[112,117,260,128])
    for i,(x,a,z) in enumerate([(105,0,4),(190,4,8)]):
        mark=b('mark'+str(i),'',[x,119,x+6,125],'checkbox');mark['controls']=[{'type':'checkbox','source':'scan_rectangle','selected':False}]
        rr={'block_id':'captions','start':a,'end':z,'slot_id':''}
        mark['structure']['scan_option_caption_crop']={'method':VERSION,'container_ref':{'block_id':'response','start':0,'end':0,'slot_id':''},
            'caption_refs':[rr],'page':1,'reading':'CoolWarm'[a:z],'confidence':.99,'patch_sha256':'a'*64,
            'crop_bbox':[x+6.5,116,189 if i==0 else 260,129]}
    return blocks,{b['block_id']:b for b in blocks}


def test_whole_bounded_grid_reads_all_captions_and_empty_boxes_as_blank():
    blocks,lookup=grid();fields=[];_restore_choice_cells(fields,blocks,lookup)
    assert len(fields)==1
    f=fields[0]
    assert [o['text_original'] for o in f['options']]==['Cool','Warm']
    assert f['answer_status']=='blank' and f['review_reasons']==[]
    assert len(f['answer_region_refs'])==2 and f['answer_value_refs']==[]


def test_incomplete_grid_cannot_certify_blank_or_borrow_whole_caption():
    blocks,lookup=grid();lookup['mark1']['structure'].pop('scan_option_caption_crop');fields=[]
    _restore_choice_cells(fields,blocks,lookup)
    assert not fields or fields[0]['answer_status']=='unresolved'


def test_unknown_mark_remains_unresolved():
    blocks,lookup=grid();lookup['mark1']['controls'][0]['selected']=None;fields=[]
    _restore_choice_cells(fields,blocks,lookup)
    assert len(fields)==1 and fields[0]['answer_status']=='unresolved'


def test_manual_grid_relation_preserves_fields_and_native_source_metadata():
    blocks,lookup=grid();fields=[{'field_id':'human','manual_override':True,'label_refs':[],
        'answer_region_refs':[{'block_id':'mark0','start':0,'end':0}],'answer_text_original':'HUMAN'}]
    before=deepcopy((fields,blocks));_restore_choice_cells(fields,blocks,lookup)
    assert (fields,blocks)==before


def test_mark_true_without_native_selection_proof_does_not_create_source_answer():
    blocks,lookup=grid();lookup['mark0']['controls'][0]['selected']=True;fields=[]
    _restore_choice_cells(fields,blocks,lookup)
    assert fields[0]['answer_status']=='unresolved' and fields[0]['answer_value_refs']==[]


def blank_prompt():
    blocks,lookup=grid()
    blocks[:]=[b for b in blocks if b['block_id'].startswith('anchor')]
    left=blocks[1];left['location']['bbox']=[10,60,100,105]
    template=deepcopy(left)
    for i,t,box in [('right','',[100,60,300,105]),('line1','How many sites? List',[15,65,98,75]),('line2','the addresses of each site.',[15,77,98,87])]:
        b=deepcopy(template);b.update(block_id=i,text=t,order=len(blocks));b['location']['bbox']=box
        b['structure']={'scan_region_kind':'text_box','blank_state':'blank','content_state':'empty'} if i=='right' else {}
        blocks.append(b)
    lookup={b['block_id']:b for b in blocks};rr=lambda i,z=0:{'block_id':i,'start':0,'end':z,'slot_id':''}
    f={'field_id':'original','question_id':'original','label_refs':[rr('line1',len(lookup['line1']['text']))],
       'answer_region_refs':[rr('right')],'question_text_original':lookup['line1']['text'],'answer_status':'blank','answer_text_original':'',
       'boundary_evidence':'scan_physical_form'}
    return blocks,lookup,[f]


def test_closed_left_prompt_continuation_kept_with_its_existing_blank_response():
    blocks,lookup,fields=blank_prompt();restore_scanned_cell_pairs(fields,blocks,lookup)
    f=next(f for f in fields if f['field_id']=='original')
    assert f['question_text_original']=='How many sites? List\nthe addresses of each site.'
    assert [r['block_id'] for r in f['answer_region_refs']]==['right']
    assert f['answer_status']=='blank' and f['answer_text_original']==''


def test_blank_prompt_completion_preserves_manual_fields():
    blocks,lookup,fields=blank_prompt();fields[0]['manual_override']=True;before=deepcopy(fields)
    restore_scanned_cell_pairs(fields,blocks,lookup)
    assert fields==before


def record_grid():
    def cell(i,box):
        return {'block_id':i,'text':'','location':{'page':2,'bbox':box},'structure':{'scan_region_kind':'text_box'}}
    left=cell('header-a',[332,307,399,331]);right=cell('header-b',[399,306,459,330])
    cells=[left,right]
    for row,(top,bottom) in enumerate([(329,375),(374,419)]):
        for col,(x0,x1) in enumerate([(332,399),(399,459),(459,544)]):
            cells.append(cell(f'r{row}c{col}',[x0,top-col*.5,x1,bottom-col*.5]))
    values=[{'text':v,'location':{'page':2,'bbox':[402,y,442,y+10]}} for v,y in [('Inspected',308),('on',320)]]
    return left,right,values,cells


def test_skewed_record_columns_do_not_become_question_answer_pairs():
    from questionnaire_structure_recognizer.validation.scanned_cells import _record_heading_pair
    left,right,values,cells=record_grid()
    assert _record_heading_pair(left,right,values,cells,values)


def test_two_columns_and_no_repeated_record_rows_do_not_certify_record_header():
    from questionnaire_structure_recognizer.validation.scanned_cells import _record_heading_pair
    left,right,values,cells=record_grid()
    assert not _record_heading_pair(left,right,values,[c for c in cells if not c['block_id'].endswith('c2')],values)
    assert not _record_heading_pair(left,right,values,[c for c in cells if not c['block_id'].startswith('r1')],values)


def test_closed_whole_panel_preserves_both_rows_of_choices():
    blocks,lookup=grid()
    blocks[:]=[b for b in blocks if b['block_id'] not in {'top','bottom'}]
    panel=deepcopy(lookup['response']);panel['block_id']='whole-panel';panel['location']['bbox']=[10,110,300,155]
    blocks.append(panel);lookup={b['block_id']:b for b in blocks};fields=[]
    _restore_choice_cells(fields,blocks,lookup)
    assert len(fields)==1 and fields[0]['answer_status']=='blank'
    assert [o['text_original'] for o in fields[0]['options']]==['Cool','Warm']
