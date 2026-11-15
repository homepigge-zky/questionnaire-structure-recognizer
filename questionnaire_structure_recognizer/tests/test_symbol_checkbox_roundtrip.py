"""A choice must update the actual OOXML symbol, leaving its label intact."""
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.form_positions import enrich_result_form_positions
from questionnaire_structure_recognizer.exporter import fill_workflow_docx_template


def test_font_symbol_choices_round_trip_without_replacing_option_labels(tmp_path):
    source = tmp_path/'FICTIONAL_SYMBOLS.docx'
    doc = Document();doc.add_heading('Supplier questionnaire', 1)
    table = doc.add_table(rows=2, cols=3)
    for cell, text in zip(table.rows[0].cells, ['Question', 'Yes', 'No']): cell.text = text
    table.cell(1, 0).text = 'Do you maintain training records?'
    for column in (1, 2):
        symbol = OxmlElement('w:sym');symbol.set(qn('w:font'), 'Wingdings 2');symbol.set(qn('w:char'), 'F035')
        table.cell(1, column).paragraphs[0].add_run()._r.append(symbol)
    doc.save(source)
    native = extract_document(source).model_dump()
    result = validate_and_assemble(native, [], {'provider': 'unit-test-structure-only'})
    question = next(f for f in result['questions'] if f['question_text_original'] == 'Do you maintain training records?')
    assert question['answer_text_original'] == ''
    question.update(manual_override=True, answer_origin='manual', selected_options=['No'])
    enrich_result_form_positions(result)
    target = tmp_path/'FILLED_SYMBOLS.docx'
    writes, warnings = fill_workflow_docx_template(source, result, target)
    assert not warnings
    assert len(writes) == 2
    readback = extract_document(target).model_dump()
    controls = [c for b in readback['source_blocks'] for c in b.get('controls', []) if c.get('source') == 'word_symbol']
    assert [c['selected'] for c in controls] == [False, True]
    reopened = Document(target)
    assert reopened.tables[0].cell(1, 0).text == 'Do you maintain training records?'
    assert [c.text for c in reopened.tables[0].rows[0].cells] == ['Question', 'Yes', 'No']
