from copy import deepcopy
import pytest
from questionnaire_structure_recognizer.parser_core.pdf_caption_fragments import (
    VERSION, _exact_ref, fragment_candidates, verified_caption_crop, enrich_scanned_caption_fragments)
from questionnaire_structure_recognizer.validation.scanned_record_choices import restore_scanned_record_choices


def fixture():
    def block(i,text,box,**structure):
        return {'block_id':i,'kind':'ocr_text','text':text,'order':len(blocks),'location':{'type':'pdf','page':1,'extraction':'ocr','bbox':box},
                'structure':structure,'style':{'ocr_confidence':.99},'controls':[],'warnings':[]}
    blocks=[]
    blocks.append(block('header','Material classification',[120,11,205,19],resolved_layout_role='record_column_header'))
    blocks.append(block('header_cell','',[100,5,300,25],scan_region_kind='text_box'))
    for row,y in enumerate([25,75]):
        cell=f'cell{row}';text=f'text{row}'
        blocks.append(block(cell,'',[100,y,300,y+50],scan_region_kind='text_box'))
        blocks.append(block(text,'AlphaBeta',[103,y+9,260,y+22]))
        for index,(x,value,a,z) in enumerate([(105,'Alpha',0,5),(180,'Beta',5,9)]):
            mark=block(f'mark{row}{index}','',[x,y+11,x+8,y+19],scan_region_kind='checkbox',source_method='raster_geometry')
            chosen=row==0 and index==1
            mark['controls']=[{'type':'checkbox','selected':chosen,'source':'ocr_mark_in_scan_rectangle' if chosen else 'scan_rectangle',
                'mark_evidence':{'text':'✓','confidence':.99} if chosen else None}]
            mark['structure']['scan_option_caption_crop']={'method':VERSION,'container_ref':{'block_id':cell,'start':0,'end':0,'slot_id':''},
                'caption_refs':[{'block_id':text,'start':a,'end':z,'slot_id':''}],'page':1,'reading':value,'confidence':.99,
                'patch_sha256':'a'*64,'crop_bbox':[x+8.5,y+8,179 if index==0 else 260,y+23]}
            blocks.append(mark)
    return blocks,{b['block_id']:b for b in blocks}


def test_source_crop_requires_one_exact_nonempty_original_substring():
    candidate={'source_blocks':[{'block_id':'s','text':'AlphaBeta'}]}
    assert _exact_ref(('Beta',.99),candidate)=={'block_id':'s','start':5,'end':9,'slot_id':''}
    assert _exact_ref(('Gamma',.99),candidate) is None
    assert _exact_ref(('Beta',.6),candidate) is None
    assert _exact_ref(('',.99),candidate) is None
    assert _exact_ref(('Beta',.99),{'source_blocks':[{'block_id':'s','text':'Beta Beta'}]}) is None


def test_crop_typography_alignment_returns_unchanged_source_span_not_rewritten_caption():
    candidate={'source_blocks':[{'block_id':'s','text':'+2to+8°C+8to+15°C+15to+25°C'}]}
    assert _exact_ref(('+8 to +15℃',.99),candidate)=={'block_id':'s','start':8,'end':17,'slot_id':''}
    assert _exact_ref(('+15 to +25°℃',.99),candidate)=={'block_id':'s','start':17,'end':27,'slot_id':''}
    assert _exact_ref(('+8 to +25℃',.99),candidate) is None
    assert _exact_ref(('+8 to +15°F',.99),candidate) is None
    assert _exact_ref(('+8 to +15℃',.99),{'source_blocks':[{'block_id':'s','text':'+8 to +15°C / +8to+15°C'}]}) is None


def test_ocr_disabled_does_not_read_file_or_start_local_recognizer(tmp_path):
    assert enrich_scanned_caption_fragments(tmp_path/'missing.pdf',{},tmp_path,ocr_enabled=False)['status']=='disabled'


def test_local_candidates_include_blank_and_selected_physical_boxes():
    blocks,_=fixture();candidates=fragment_candidates(blocks)
    assert len(candidates)==4
    assert {c['mark_id'] for c in candidates}=={'mark00','mark01','mark10','mark11'}


def test_bounded_caption_budget_prioritizes_merged_multibox_source_lines():
    blocks,_=fixture()
    # These captions already have independent OCR blocks. Their small cells
    # must not exhaust the local crop budget before a real multi-box line.
    for index in range(50):
        y=100+index*40
        cell={'block_id':f'closed{index}','text':'','location':{'type':'pdf','page':2,'extraction':'ocr','bbox':[10,y,70,y+35]},
              'structure':{'scan_region_kind':'text_box'},'controls':[]}
        blocks.append(cell)
        for j,label in enumerate(['Yes','No']):
            b={'block_id':f'ordinary{index}:{j}','text':'','location':{'type':'pdf','page':2,'extraction':'ocr','bbox':[15,y+j*15+2,23,y+j*15+10]},
               'structure':{'scan_region_kind':'checkbox'},'controls':[{'type':'checkbox','selected':False}]}
            blocks.append(b)
            blocks.append({'block_id':f'caption{index}:{j}','text':'Z'+label,
                'location':{'type':'pdf','page':2,'extraction':'ocr','bbox':[15,y+j*15,50,y+j*15+13]},'structure':{},'controls':[]})
    candidates=fragment_candidates(blocks)
    assert len(candidates)>96
    assert {'mark00','mark01','mark10','mark11'} <= {c['mark_id'] for c in candidates[:96]}


@pytest.mark.parametrize('corruption',['wrong_page','empty_range','cross_box','changed_source','wrong_container','missing_hash'])
def test_invalid_crop_relationship_is_never_evidence(corruption):
    blocks,lookup=fixture();mark=lookup['mark00'];rr=deepcopy(mark['structure']['scan_option_caption_crop']['caption_refs'][0])
    assert verified_caption_crop(mark,rr,lookup)
    if corruption=='wrong_page':lookup['text0']['location']['page']=2
    elif corruption=='empty_range':rr['end']=rr['start']
    elif corruption=='cross_box':mark['structure']['scan_option_caption_crop']['crop_bbox'][2]=260
    elif corruption=='changed_source':lookup['text0']['text']='OtherData'
    elif corruption=='wrong_container':mark['structure']['scan_option_caption_crop']['container_ref']['block_id']='cell1'
    else:mark['structure']['scan_option_caption_crop'].pop('patch_sha256')
    assert not verified_caption_crop(mark,rr,lookup)


def test_record_choices_keep_rows_separate_and_use_source_selected_caption():
    blocks,lookup=fixture();before={b['block_id']:b['text'] for b in blocks};fields=[]
    restore_scanned_record_choices(fields,blocks,lookup)
    assert len(fields)==2 and fields[0]['answer_text_original']=='Beta' and fields[1]['answer_text_original']==''
    assert fields[0]['record_instance_id']!=fields[1]['record_instance_id']
    assert fields[0]['answer_status']=='filled' and fields[1]['answer_status']=='blank'
    assert all(len(f['answer_region_refs'])==2 for f in fields)
    assert before=={b['block_id']:b['text'] for b in blocks}


def test_record_unknown_companion_keeps_options_but_not_partial_value():
    blocks,lookup=fixture();lookup['mark00']['controls'][0]['selected']=None;fields=[]
    restore_scanned_record_choices(fields,blocks,lookup)
    assert fields[0]['answer_status']=='unresolved'
    assert fields[0]['answer_text_original']=='' and fields[0]['answer_value_refs']==[]
    assert [o['selected'] for o in fields[0]['options']]==[None,True]


def test_incomplete_caption_set_and_human_edits_never_forced_into_record():
    blocks,lookup=fixture();lookup['mark00']['structure'].pop('scan_option_caption_crop');fields=[]
    restore_scanned_record_choices(fields,blocks,lookup)
    assert len(fields)==1 and fields[0]['record_instance_id']=='cell1'
    blocks,lookup=fixture();manual={'field_id':'human','manual_override':True,'answer_text_original':'MANUAL',
        'answer_region_refs':[{'block_id':'mark00','start':0,'end':0}],'label_refs':[]};fields=[deepcopy(manual)]
    restore_scanned_record_choices(fields,blocks,lookup)
    assert fields[0]==manual and not any(f.get('record_instance_id')=='cell0' for f in fields)
