from collections import Counter

from docx import Document

from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation.validator import validate_and_assemble


def flat(fields):
    for field in fields:
        yield field
        yield from flat(field.get('children',[]))


def test_wrapped_placeholder_has_one_identity_per_physical_label(tmp_path):
    path=tmp_path/'FICTIONAL_wrapped_writeins.docx'
    doc=Document();table=doc.add_table(rows=2,cols=2)
    for index in range(2):
        table.cell(index,0).text=f'Product class {index+1}'
        cell=table.cell(index,1);cell.text='Tablets'
        p=cell.add_paragraph('Others (please specify):')
        p.add_run().add_break();p.add_run('______________')
    doc.save(path)
    native=extract_document(path).model_dump()
    result=validate_and_assemble(native,[],{'name':'source-only-fixture','metrics':[]})
    fields=list(flat(result['questions']))
    assert all(count==1 for count in Counter(f['field_id']for f in fields).values())
    children=[f for f in fields if f['question_text_original']=='Others (please specify)']
    assert len(children)==2, 'Identical wording in distinct physical rows stays separate'
    lookup={b['block_id']:b for b in result['source_blocks']}
    for field in children:
        assert len(field['answer_region_refs'])==1
        rr=field['answer_region_refs'][0]
        assert lookup[rr['block_id']]['text'][rr['start']:rr['end']]=='______________'
        assert field['original_answer_status']=='blank'
        assert field['form_definition']['validation_status']=='valid'
