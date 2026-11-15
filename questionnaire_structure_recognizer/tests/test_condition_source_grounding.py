from docx import Document
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.form_positions import enrich_result_form_positions, infer_condition


def test_model_flag_cannot_invent_condition_for_supplier_name(tmp_path):
    path=tmp_path/'fictional.docx';d=Document();d.add_paragraph('To be filled by supplier')
    t=d.add_table(rows=1,cols=2);t.cell(0,0).text='Supplier name:';d.save(path)
    result=validate_and_assemble(extract_document(path).model_dump(),[],{'provider':'fixture'})
    f=next(f for f in result['questions'] if f['question_text_original'].startswith('Supplier name'))
    f['model_hints']={'conditional_hint':True,'field_type_hint':'date'}
    enrich_result_form_positions(result)
    assert f['form_definition']['applicability_condition'] is None
    assert f['form_definition']['field_type'] in {'short_text','long_text'}
    assert f['form_definition']['auto_fill_eligible']


def test_followup_directive_does_not_block_the_parent_boolean():
    parent={'question_text_original':'Do you supply fictional solvent in tankers? If Yes, follow Part C',
        'options':[{'text_original':'Yes'},{'text_original':'No'}]}
    assert infer_condition(parent,{}) is None
    child={'question_text_original':'If answer is No, please explain.','parent_question_id':'p'}
    assert infer_condition(child,{})['parent_field_id']=='p'


def test_unproven_customer_hint_remains_unknown_and_does_not_block_manual_fill(tmp_path):
    path=tmp_path/'fictional.docx';d=Document()
    table=d.add_table(rows=1,cols=2);table.cell(0,0).text='Fictional observation:';d.save(path)
    result=validate_and_assemble(extract_document(path).model_dump(),[],{'provider':'fixture'})
    field=result['questions'][0];field['model_hints']={'responsibility_hint':'customer'}
    enrich_result_form_positions(result)
    definition=field['form_definition']
    assert definition['fill_responsibility']=='unknown'
    assert definition['fillable_status']=='fillable'
    assert not definition['auto_fill_eligible']
