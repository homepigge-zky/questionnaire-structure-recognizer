from copy import deepcopy
from docx import Document
from docx.shared import Pt


def test_shortened_address_keeps_the_following_answer_beside_its_label():
    from questionnaire_structure_recognizer.validation.rendered_lane_export import spacing_adjustments
    lanes = [
        {'label_paragraph': 'label-address', 'answer_paragraphs': ['address'],
         'page': 1, 'baseline': 100, 'answer_origins': {'address': [0, 200, 100]}},
        {'label_paragraph': 'label-phone', 'answer_paragraphs': ['phone'],
         'page': 1, 'baseline': 124, 'answer_origins': {'phone': [0, 200, 124]}},
        {'label_paragraph': 'label-fax', 'answer_paragraphs': ['fax'],
         'page': 1, 'baseline': 148, 'answer_origins': {'fax': [0, 200, 148]}},
    ]
    current = {'label-address': (0, 10, 100), 'address': (0, 200, 100),
               'label-phone': (0, 10, 124), 'phone': (0, 200, 112),
               'label-fax': (0, 10, 148), 'fax': (0, 200, 136)}
    changes, errors = spacing_adjustments(lanes, current)
    assert not errors
    # Only one native paragraph needs padding; do not cumulatively pad fax too.
    assert changes == [('phone', 12.0)]


def test_long_answer_is_not_squeezed_into_an_overlapping_lane():
    from questionnaire_structure_recognizer.validation.rendered_lane_export import spacing_adjustments
    lanes = [{'label_paragraph': 'label-phone', 'answer_paragraphs': ['phone'],
              'page': 1, 'baseline': 124, 'answer_origins': {'phone': [0, 200, 124]}}]
    changes, errors = spacing_adjustments(lanes, {'label-phone': (0, 10, 124), 'phone': (0, 200, 150)})
    assert not changes and errors


def test_missing_or_cross_page_layout_never_uses_a_guessed_origin():
    from questionnaire_structure_recognizer.validation.rendered_lane_export import spacing_adjustments
    lanes = [{'label_paragraph': 'label', 'answer_paragraphs': ['answer'], 'page': 1,
              'baseline': 124, 'answer_origins': {'answer': [0, 200, 124]}}]
    assert spacing_adjustments(lanes, {'label': (0, 10, 124)})[1]
    assert spacing_adjustments(lanes, {'label': (0, 10, 124), 'answer': (1, 200, 124)})[1]


def test_explicitly_cleared_answer_does_not_require_a_visible_value():
    from questionnaire_structure_recognizer.validation.rendered_lane_export import spacing_adjustments
    lane={'label_paragraph':'label','answer_paragraphs':['answer'],'answer_is_empty':True}
    assert spacing_adjustments([lane],{'label':(0,10,100)})==([],[])


def test_fixed_height_parallel_row_refuses_before_claiming_a_safe_render(tmp_path,monkeypatch):
    import pytest
    from docx.enum.table import WD_ROW_HEIGHT_RULE
    from questionnaire_structure_recognizer.parser_core import extract_document
    from questionnaire_structure_recognizer.validation import rendered_lane_export as module
    from questionnaire_structure_recognizer.exporter import TemplateFillError
    doc=Document();table=doc.add_table(rows=1,cols=2);table.rows[0].height_rule=WD_ROW_HEIGHT_RULE.EXACTLY
    table.rows[0].height=Pt(15);table.cell(0,0).text='Comment:';table.cell(0,1).text='Long response '*40
    path=tmp_path/'fixed.docx';doc.save(path);native=extract_document(path).model_dump()
    left,right=[b for b in native['source_blocks'] if b['kind']=='table_cell']
    lp=left['structure']['paragraph_spans'][0];rp=right['structure']['paragraph_spans'][0]
    right['structure']['filled_lane_alignment']=[{'label_block':left['block_id'],'label_start':0,'label_end':len(left['text']),
        'label_paragraph':lp['paragraph_id'],'answer_paragraphs':[rp['paragraph_id']]}]
    monkeypatch.setattr(module,'_positions',lambda *a:pytest.fail('A fixed-height unsafe region must not be approved by baseline alone'))
    with pytest.raises(TemplateFillError,match='固定行高'):
        module.preserve_rendered_answer_bands(doc,path,native,{right['block_id']})


def test_last_answer_paragraph_must_be_fully_visible_not_only_first_baseline(tmp_path,monkeypatch):
    import pytest
    from questionnaire_structure_recognizer.parser_core import extract_document
    from questionnaire_structure_recognizer.validation import rendered_lane_export as module
    from questionnaire_structure_recognizer.exporter import TemplateFillError
    doc=Document();table=doc.add_table(rows=1,cols=2)
    table.cell(0,0).text='Comment:';table.cell(0,1).text='FIRST_PART';table.cell(0,1).add_paragraph('LAST_PART_MUST_NOT_BE_HIDDEN')
    path=tmp_path/'multiline.docx';doc.save(path);native=extract_document(path).model_dump()
    left,right=[b for b in native['source_blocks'] if b['kind']=='table_cell']
    lp=left['structure']['paragraph_spans'][0]['paragraph_id'];rp=[p['paragraph_id'] for p in right['structure']['paragraph_spans']]
    right['structure']['filled_lane_alignment']=[{'label_block':left['block_id'],'label_start':0,'label_end':len(left['text']),
        'label_paragraph':lp,'answer_paragraphs':rp,'page':1,'baseline':100,
        'answer_origins':{rp[0]:(0,200,100),rp[1]:(0,200,120)},
        'layout_measurement_version':'filled-paragraph-lanes-v7-unmodified-native-render'}]
    monkeypatch.setattr(module,'_positions',lambda *a:{lp:(0,10,100),rp[0]:(0,200,100)})
    with pytest.raises(TemplateFillError) as error:
        module.preserve_rendered_answer_bands(doc,path,native,{right['block_id']})
    assert any(rp[1] in text for text in error.value.layout_diagnostics['errors'])
