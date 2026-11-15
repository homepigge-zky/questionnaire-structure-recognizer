from docx import Document
from questionnaire_structure_recognizer.exporter import standard_questionnaire_docx


def test_readonly_source_instructions_survive_in_order_without_answer_rows(tmp_path):
    question={'id':'q','ordinal':1,'source_order':5,'parent_id':None,'question_text':'Fictional company name',
              'section':'Supplier information','answer_text':'TEST_NAME','options':[],'selected_options':[],'notes':''}
    snapshot={'task':{'questionnaire_name':'FICTIONAL'},'questions':[question],
        'document_content':[{'block_id':'before','order':3,'role':'attachment_requirement','text':'Enclose a copy of the fictional policy.'},
            {'block_id':'after','order':7,'role':'group_context','text':'Observations'},
            {'block_id':'final','order':8,'role':'instruction','text':'Retain a copy for your own records.'}]}
    target=tmp_path/'standard.docx';standard_questionnaire_docx(snapshot,target)
    doc=Document(target)
    texts='\n'.join(doc.element.body.itertext())
    # XML body order includes both paragraphs and table cells.
    assert texts.index('Enclose a copy')<texts.index('Fictional company name')<texts.index('Observations')<texts.index('Retain a copy')
    rows=[row for table in doc.tables for row in table.rows if any(c.text=='TEST_NAME' for c in row.cells)]
    assert len(rows)==1
    assert not any('Enclose a copy' in c.text for table in doc.tables for row in table.rows for c in row.cells)
    assert len(snapshot['questions'])==1
