from docx import Document
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble


def test_one_physical_long_response_keeps_requirements_with_question(tmp_path):
    doc=Document();table=doc.add_table(rows=2,cols=3)
    for cell,text in zip(table.rows[0].cells,['No.','Question','Answer']):cell.text=text
    row=table.rows[1].cells;row[0].text='1'
    row[1].text=('Please provide a detailed description of the transport process and explain the safeguards used throughout delivery, including all applicable routes and subcontractors.\n'
                 'Specific focus must be given to the following issues:\nTotal delivery time\nIntermediate storage\nAdditional considerations:\nTransport records')
    row[2].text='TEST_TRANSPORT\nTEST_RECORDS'
    path=tmp_path/'fictional.docx';doc.save(path)
    result=validate_and_assemble(extract_document(path).model_dump(),[],{'provider':'test'})
    fields=result['questions']
    assert len(fields)==1
    assert fields[0]['children']==[]
    assert fields[0]['answer_text_original']=='TEST_TRANSPORT\nTEST_RECORDS'
    assert fields[0]['pairing_validation']=='valid'
    assert fields[0]['question_text_original']==row[1].text
