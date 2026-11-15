from docx import Document
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble


def test_answer_section_caption_before_question_table_is_not_write_in(tmp_path):
    source = tmp_path / 'fictional.docx'
    doc = Document()
    doc.add_paragraph('ANSWERS TO QUESTIONS:')
    doc.add_paragraph('This questionnaire applies to all manufacturers. Please add comments where needed.')
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = 'Question'; table.cell(0, 1).text = 'Answer'
    table.cell(1, 0).text = 'Describe the fictional process.'
    doc.save(source)
    result = validate_and_assemble(extract_document(source).model_dump(), [], {'provider': 'fixture'})
    assert all(q['question_text_original'] != 'ANSWERS TO QUESTIONS' for q in result['questions'])
    assert any(b['text'] == 'ANSWERS TO QUESTIONS:' for b in result['source_blocks'])
    assert any(q['question_text_original'] == 'Describe the fictional process.' for q in result['questions'])


def test_explicit_answer_write_in_remains_available(tmp_path):
    source = tmp_path / 'fictional.docx'
    doc = Document(); doc.add_paragraph('Answer to the additional question: __________')
    doc.save(source)
    result = validate_and_assemble(extract_document(source).model_dump(), [], {'provider': 'fixture'})
    assert result['questions'] and result['questions'][0]['answer_region_refs']
