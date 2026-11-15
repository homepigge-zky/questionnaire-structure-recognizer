import pytest
from docx import Document

from questionnaire_structure_recognizer.form_positions import infer_field_type, enrich_result_form_positions
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble


@pytest.mark.parametrize('label', [
    'Supplier’s Quality Representative Signature:',
    'Manufacturer authorized representative signature:',
    'Customer Quality Director Signature:',
    'Supplier’s Quality Representative Signature: (handwritten signature in ink pen only)',
])
def test_explicit_role_signature_caption_is_manual(label):
    source = {'block_id': 'signature-label', 'text': label}
    field = {'question_text_original': label,
             'label_refs': [{'block_id': source['block_id'], 'start': 0, 'end': len(label)}]}
    assert infer_field_type(field, {source['block_id']: source})[0] == 'manual_signature'


@pytest.mark.parametrize('label', [
    'Do you obtain signatures?',
    'Do you obtain signature',
    'Is the document approved by signature?',
    'Describe the electronic signature policy',
    'What is your policy for handwritten signature',
    'Does your system verify the supplier signature',
])
def test_signature_audit_question_is_not_a_signing_caption(label):
    source = {'block_id': 'audit-label', 'text': label}
    field = {'question_text_original': label,
             'label_refs': [{'block_id': source['block_id'], 'start': 0, 'end': len(label)}]}
    assert infer_field_type(field, {source['block_id']: source})[0] != 'manual_signature'


def test_native_docx_role_signature_protects_local_name_and_date_but_keeps_manual_targets(tmp_path):
    doc = Document(); doc.add_paragraph('Supplier questionnaire')
    table = doc.add_table(rows=1, cols=6)
    for index, label in [(0, 'Supplier’s Quality Representative Signature:'),
                         (2, 'Printed Name & Title:'), (4, 'Date:')]:
        table.cell(0, index).text = label
    doc.add_paragraph('(handwritten signature in ink pen only)')
    path = tmp_path/'fictional-signing.docx'; doc.save(path)
    result = validate_and_assemble(extract_document(path).model_dump(), [], {'provider': 'fixture'})
    enrich_result_form_positions(result)
    assert len(result['questions']) == 3
    sign = next(f for f in result['questions'] if 'Signature' in f['question_text_original'])
    assert sign['form_definition']['field_type'] == 'manual_signature'
    assert not sign['form_definition']['auto_fill_eligible']
    for field in result['questions']:
        definition = field['form_definition']
        assert definition['requires_signatory_confirmation']
        assert not definition['auto_fill_eligible']
        assert definition['fill_targets']
        if field is not sign:
            assert definition['field_type'] != 'manual_signature'
            assert definition['fillable_status'] == 'fillable'
            assert all(t['writable'] for t in definition['fill_targets'])
