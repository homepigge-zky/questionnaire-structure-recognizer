from copy import deepcopy
from docx import Document
from questionnaire_structure_recognizer.exporter import standard_questionnaire_docx


def row(path):
    return next(r for t in Document(path).tables if len(t.columns)==4 for r in t.rows[1:])


def test_unverified_options_stay_in_diagnostics_not_fabricated_response_boxes(tmp_path):
    q={'id':'q','question_text':'Fictional question?','section':'','answer_text':'',
       'options':[{'text':'sNo','value':'guess'}],
       'form_definition':{'validation_status':'needs_review','validation_reasons':['No verified option ownership']},
       'answer_region_details':[{'block_id':'b','text':'No□'}]}
    before=deepcopy(q);out=tmp_path/'standard.docx'
    standard_questionnaire_docx({'task':{},'questions':[q]},out)
    r=row(out)
    assert r.cells[2].text==''
    assert r.cells[1].text=='Fictional question?'
    assert 'No verified option ownership' not in '\n'.join(c.text for c in r.cells)
    assert q==before


def test_empty_caption_model_refs_do_not_create_eleven_blank_checkboxes(tmp_path):
    q={'id':'q','question_text':'Traceability?','section':'','answer_text':'',
       'options':[{'text':'','value':str(i)} for i in range(11)],
       'form_definition':{'validation_status':'needs_review'},'answer_region_details':[{'text':'No口'}]}
    out=tmp_path/'empty.docx';standard_questionnaire_docx({'task':{},'questions':[q]},out)
    r=row(out)
    assert r.cells[2].text=='' and r.cells[1].text=='Traceability?'
    assert '☐' not in '\n'.join(c.text for c in r.cells)


def test_valid_choices_keep_exact_labels_and_manual_answers_are_never_dropped(tmp_path):
    q={'id':'q','question_text':'Fictional valid question?','section':'','answer_text':'No',
       'options':[{'text':'Yes'},{'text':'No'}],'selected_options':['No'],
       'form_definition':{'validation_status':'valid'}}
    out=tmp_path/'valid.docx';standard_questionnaire_docx({'task':{},'questions':[q]},out)
    assert row(out).cells[2].text=='☐ Yes\n☒ No'
    q.update(manual_override=True,origin='manual',answer_text='TEST_MANUAL_TEXT')
    q['form_definition']['validation_status']='needs_review'
    out=tmp_path/'manual.docx';standard_questionnaire_docx({'task':{},'questions':[q]},out)
    assert row(out).cells[2].text=='TEST_MANUAL_TEXT'


def test_option_target_count_and_readonly_targets_cannot_authorize_checkbox_output(tmp_path):
    q={'id':'q','question_text':'Fictional two choices?','section':'','answer_text':'',
       'options':[{'text':'Yes'},{'text':'No'}],
       'form_definition':{'validation_status':'valid','fill_targets':[{'writable':True}]}}
    out=tmp_path/'mismatch.docx';standard_questionnaire_docx({'task':{},'questions':[q]},out)
    assert row(out).cells[2].text==''
    q['form_definition']['fill_targets']=[{'writable':True},{'writable':False}]
    out=tmp_path/'readonly.docx';standard_questionnaire_docx({'task':{},'questions':[q]},out)
    assert row(out).cells[2].text==''
