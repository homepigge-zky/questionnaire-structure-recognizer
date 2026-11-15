from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble


def test_bilingual_question_has_one_native_selected_option_lane(tmp_path):
    doc=Document();table=doc.add_table(rows=2,cols=3)
    for c,t in zip(table.rows[0].cells,['No.','Question','Answer']):c.text=t
    row=table.rows[1].cells;row[0].text='1';row[1].text='是否有测试制度？'
    row[1].add_paragraph('Do you have a fictional procedure?')
    row[2].text='';p=row[2].add_paragraph()
    for index,label in enumerate(['Yes','No','N/A']):
        begin=OxmlElement('w:fldChar');begin.set(qn('w:fldCharType'),'begin')
        data=OxmlElement('w:ffData');begin.append(data)
        name=OxmlElement('w:name');name.set(qn('w:val'),f'C{index}');data.append(name)
        box=OxmlElement('w:checkBox');data.append(box)
        state=OxmlElement('w:checked');state.set(qn('w:val'),'1' if index==0 else '0');box.append(state)
        p.add_run()._r.append(begin)
        instruction=OxmlElement('w:instrText');instruction.text=' FORMCHECKBOX ';p.add_run()._r.append(instruction)
        end=OxmlElement('w:fldChar');end.set(qn('w:fldCharType'),'end');p.add_run()._r.append(end)
        p.add_run(label+' ')
    source=tmp_path/'fictional.docx';doc.save(source)
    result=validate_and_assemble(extract_document(source).model_dump(),[],{'provider':'fixture'})
    assert len(result['questions'])==1
    f=result['questions'][0]
    assert not f['children']
    assert f['question_text_original']==row[1].text
    assert f['answer_text_original']=='Yes' and f['pairing_validation']=='valid'
    assert len(f['form_definition']['fill_targets'])==3
