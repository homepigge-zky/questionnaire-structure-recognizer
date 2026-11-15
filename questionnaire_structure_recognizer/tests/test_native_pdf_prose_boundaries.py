"""Fictional source coordinates, including negatives; no model inference."""
from copy import deepcopy
import pytest

from questionnaire_structure_recognizer.parser_core.models import make_block
from questionnaire_structure_recognizer.validation.field_boundaries import ref
from questionnaire_structure_recognizer.validation.form_completion import new_field
from questionnaire_structure_recognizer.validation.native_pdf_prose_boundaries import resolve_native_pdf_prose


def block(text,page,box,**structure):
    return make_block('paragraph',text,page*100+int(box[1]),
                      {'type':'pdf','page':page,'bbox':box,'extraction':'native'},structure=structure).model_dump()


def test_contents_needs_heading_page_column_and_repeated_source_rows():
    blocks=[block('INDEX',1,[40,40,80,52]),block('Page',1,[500,40,530,52])]
    for n in range(1,4):
        blocks.extend([block(str(n)+'.',1,[40,70+n*20,50,82+n*20]),
                       block('Fictional section '+str(n),1,[90,70+n*20,290,82+n*20]),
                       block(str(n),1,[520,70+n*20,530,82+n*20])])
    blocks.extend([block('Actual content',p,[40,300,200,315]) for p in (2,3)])
    lookup={b['block_id']:b for b in blocks};fields=[new_field(b,ref(b),[]) for b in blocks]
    manual=deepcopy(fields[3]);manual['manual_override']=True;fields[3]=manual
    notes=resolve_native_pdf_prose(fields,blocks,lookup)
    assert manual in fields and len(fields)==3
    assert len([n for n in notes if n['role']=='table_of_contents'])==11
    assert len(blocks)==13  # Original ledger is never deleted.
    # Without the explicit heading this is not a proved contents layout.
    plain=deepcopy(blocks);plain[0]['text']='Questions'
    untouched=[new_field(b,ref(b),[]) for b in plain]
    resolve_native_pdf_prose(untouched,plain,{b['block_id']:b for b in plain})
    assert len(untouched)==13


def test_repeated_business_text_in_a_closed_form_is_not_margin_furniture():
    blocks=[]
    for page in (1,2,3):
        blocks += [block('FICTIONAL header',page,[30,15,150,25]),
                   block('Company name',page,[30,35,180,50],native_closed_form_regions=[{'bbox':[20,30,200,80]}]),
                   block('Body',page,[30,400,150,415])]
    fields=[new_field(b,ref(b),[]) for b in blocks]
    resolve_native_pdf_prose(fields,blocks,{b['block_id']:b for b in blocks})
    assert [f['question_text_original'] for f in fields].count('Company name')==3
    assert not any(f['question_text_original']=='FICTIONAL header' for f in fields)


def test_numbered_bold_section_requires_matching_child_question():
    blocks=[block('4.',1,[20,50,32,65],native_pdf_font_runs=[{'flags':16}]),
            block('TEST SECTION',1,[70,50,210,65],native_pdf_font_runs=[{'flags':18}]),
            block('4.1. Does this remain a question?',1,[20,100,400,115]),
            block('7.',1,[20,160,35,175],native_pdf_font_runs=[{'flags':16}]),
            block('TEST VALUE',1,[70,160,200,175],native_pdf_font_runs=[{'flags':16}])]
    fields=[new_field(b,ref(b),[]) for b in blocks]
    resolve_native_pdf_prose(fields,blocks,{b['block_id']:b for b in blocks})
    assert [f['question_text_original'] for f in fields]==['4.1. Does this remain a question?','7.','TEST VALUE']


def test_pdf_native_table_target_cannot_be_advertised_as_word_template_target():
    from questionnaire_structure_recognizer.form_positions import build_fill_target
    b=block('TEST_VALUE',1,[100,100,300,140],cell_id='physical-cell',table_id='t1',row_start=1,row_end=1,
            grid_col_start=2,grid_col_end=2,column_start=2,column_end=2,blank_state='filled',content_state='text')
    b['kind']='table_cell';lookup={b['block_id']:b}
    target=build_fill_target(ref(b),lookup,'source-hash','test-parser')
    assert target['writable'] and target['write_method']=='standard_pdf_response'
    assert target['output_modes']==['standard']
    b['location']['type']='docx'
    target=build_fill_target(ref(b),lookup,'source-hash','test-parser')
    assert target['write_method']=='replace_existing_value' and 'template' in target['output_modes']


def _continued_list():
    def cell(text,page,row,col,box,spans=()):
        b=block(text,page,box,table_id=f'p{page}-table',row_start=row,row_end=row,
                grid_col_start=col,grid_col_end=col,table_column_count=2,
                layout_evidence='visible_vector_borders',native_text_spans=list(spans))
        b['kind']='table_cell';return b
    text='7.3.\nDescribe the fictional process.\n[Include the following issues:\na.\nInitial preparation'
    a=text.index('a.\n')
    previous=cell(text,1,5,1,[40,600,400,755],[{'start':a,'end':a+2,'bbox':[90,730,100,740]}])
    answer=cell('Fictional attachment',1,5,2,[400,600,550,755])
    continuation=cell('b.\nTransport conditions\nc.\nStorage conditions]',2,1,1,[40,135,400,240],
                      [{'start':0,'end':2,'bbox':[90,140,100,150]}])
    empty=cell('',2,1,2,[400,135,550,240])
    following=cell('7.4.\nDescribe the next independent procedure.',2,2,1,[40,240,400,350])
    following_answer=cell('',2,2,2,[400,240,550,350])
    blocks=[previous,answer,continuation,empty,following,following_answer]
    fields=[new_field(previous,ref(previous),[ref(answer)]),new_field(continuation,ref(continuation),[ref(empty)]),
            new_field(following,ref(following),[ref(following_answer)])]
    fields[0].update(answer_text_original=answer['text'],answer_value_refs=[ref(answer)],answer_status='filled')
    return blocks,fields


def test_page_split_bracketed_list_is_parent_instruction_not_another_answer():
    blocks,fields=_continued_list();original=deepcopy(fields[0])
    notes=resolve_native_pdf_prose(fields,blocks,{b['block_id']:b for b in blocks})
    assert len(fields)==2
    assert fields[0]['instruction_refs']==[ref(blocks[2])]
    for key in ('label_refs','answer_region_refs','answer_value_refs','answer_text_original'):
        assert fields[0][key]==original[key]
    assert any(n['refs']==[ref(blocks[2])] and n['role']=='instruction' for n in notes)
    assert fields[1]['question_text_original'].startswith('7.4.')
    assert len(blocks)==6


@pytest.mark.parametrize('change',['answered','different_column','new_number','no_bracket','no_marker_geometry','manual_ancestor'])
def test_list_continuation_requires_source_boundaries_and_keeps_manual_content(change):
    blocks,fields=_continued_list()
    if change=='answered':blocks[3]['text']='Separate actual response'
    elif change=='different_column':blocks[2]['location']['bbox'][0]+=20
    elif change=='new_number':blocks[4]['text']='8.1.\nIndependent scope'
    elif change=='no_bracket':blocks[0]['text']=blocks[0]['text'].replace('[','')
    elif change=='no_marker_geometry':blocks[2]['structure']['native_text_spans']=[]
    elif change=='manual_ancestor':fields=[{'manual_override':True,'children':fields}]
    before=deepcopy(fields)
    resolve_native_pdf_prose(fields,blocks,{b['block_id']:b for b in blocks})
    assert fields==before
