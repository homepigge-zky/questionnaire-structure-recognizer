from docx import Document
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble

def parse(path):
    return validate_and_assemble(extract_document(path).model_dump(),[],{'provider':'fixture'})

def flat(fields):
    for field in fields:
        yield field
        yield from flat(field.get('children',[]))

def test_numbered_matrix_excludes_client_review_column(tmp_path):
    p=tmp_path/'fixture.docx';d=Document();d.add_paragraph('To be filled by supplier representative')
    t=d.add_table(rows=3,cols=6)
    for c,s in zip(t.rows[0].cells,['S. No','Check points','Yes','No','NA','Example Evaluation Signature']):c.text=s
    for n in range(1,3):
        for c,s in zip(t.rows[n].cells,[str(n),'Does fictional process '+str(n)+' comply?','','','','□ Ok □ Not Ok □ NA']):c.text=s
    d.save(p);r=parse(p);fs=[f for f in flat(r['questions']) if f['question_text_original'].startswith('Does fictional')]
    assert len(fs)==2
    for f in fs:
        assert f['answer_status']=='blank' and not f['answer_text_original']
        assert [o['text_original'] for o in f['options']]==['Yes','No','NA']
        lookup={b['block_id']:b for b in r['source_blocks']}
        assert {lookup[x['block_id']]['location']['cell'] for x in f['answer_region_refs']}=={3,4,5}

def test_each_plain_yes_no_row_is_a_question_not_header(tmp_path):
    p=tmp_path/'fixture.docx';d=Document();t=d.add_table(rows=4,cols=3)
    for n,row in enumerate(t.rows):
        for c,s in zip(row.cells,[f'Do you perform fictional activity {n}?','Yes','No']):c.text=s
    d.save(p);r=parse(p);fs=r['questions']
    assert len(fs)==4 and all(f['answer_status']=='blank' and not f['answer_text_original'] for f in fs)
    assert all([o['text_original'] for o in f['options']]==['Yes','No'] for f in fs)

def test_answer_yes_no_header_and_remark_are_distinct_regions(tmp_path):
    p=tmp_path/'fixture.docx';d=Document();t=d.add_table(rows=3,cols=4)
    for c,s in zip(t.rows[0].cells,['Question','Answer\nYes','Answer\nNo','Remarks if answer is No']):c.text=s
    for n in range(1,3):
        for c,s in zip(t.rows[n].cells,[f'Do you check fictional item {n}?','Yes','No','']):c.text=s
    d.save(p);r=parse(p)
    fs=[f for f in r['questions'] if f['question_text_original'].startswith('Do you')]
    assert len(fs)==2
    assert all(f['answer_status']=='blank' and len(f['options'])==2 for f in fs)
    assert not any(f['question_text_original']=='No' for f in flat(r['questions']))

def test_two_column_nested_reserved_product_record_is_preserved(tmp_path):
    p=tmp_path/'fixture.docx';d=Document();parent=d.add_table(rows=1,cols=1).cell(0,0)
    t=parent.add_table(rows=4,cols=2)
    t.cell(0,0).text='Products';t.cell(0,1).text='Manufacturer name & Address'
    d.save(p);r=parse(p);roots=[f for f in r['questions'] if f.get('record_columns')]
    assert len(roots)==1 and len(roots[0]['children'])==6
    assert all(not f['answer_text_original'] for f in roots[0]['children'])

def test_plain_slash_option_list_is_not_original_answer(tmp_path):
    p=tmp_path/'fixture.docx';d=Document();t=d.add_table(rows=2,cols=2)
    t.cell(0,0).text='Is the fictional activity performed?';t.cell(0,1).text='Yes / No'
    t.cell(1,0).text='Fictional contact';d.save(p);r=parse(p)
    f=next(x for x in r['questions'] if x['question_text_original'].startswith('Is the'))
    assert f['answer_status']=='blank' and not f['answer_text_original']
    assert [o['text_original'] for o in f['options']]==['Yes','No']

def test_responsibility_matrix_has_separate_vendor_and_customer_targets(tmp_path):
    p=tmp_path/'fixture.docx';d=Document();t=d.add_table(rows=3,cols=4)
    for c,s in zip(t.rows[0].cells,['','Fictional Customer','VENDOR','']):c.text=s
    t.cell(1,0).text='Fictional batch release';t.cell(2,0).text='Fictional contract'
    d.save(p);r=parse(p)
    roots=[f for f in r['questions'] if f.get('boundary_evidence')=='physical_responsibility_matrix']
    assert len(roots)==2
    for f in roots:
        assert not f['answer_text_original'] and len(f['children'])==2
        assert {c['fill_responsibility'] for c in f['children']}=={'our_side','customer'}
        assert all(len(c['answer_region_refs'])==1 and not c['answer_text_original'] for c in f['children'])
