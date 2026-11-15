from docx import Document
from questionnaire_structure_recognizer.exporter import _replace_paragraph_range


def test_parallel_names_keep_column_tabs(tmp_path):
    doc = Document()
    paragraph = doc.add_paragraph('Name: \t\t\t\t\t\t\t    \tName:')
    before = paragraph.text
    end = before.rindex('Name:')
    assert _replace_paragraph_range(paragraph, 5, end, 'TEST_NAME')
    assert paragraph.text.count('\t') == before.count('\t') == 8
    assert paragraph.text.count('Name:') == 2
    assert paragraph.text.index('TEST_NAME') < paragraph.text.index('\t')
    target = tmp_path / 'FICTIONAL-parallel-names.docx'
    doc.save(target)
    assert Document(target).paragraphs[0].text == paragraph.text
