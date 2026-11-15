from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree
from questionnaire_structure_recognizer.exporter import _stabilize_empty_signature_frames, _fill_blank_response_lines


def floating_frame(document,label):
    t=document.add_table(rows=2,cols=1);t.cell(0,0).text=label
    p=OxmlElement('w:tblpPr');p.set(qn('w:vertAnchor'),'text');p.set(qn('w:tblpY'),'278');t._tbl.tblPr.append(p)
    return t


def test_empty_seal_frame_stays_whole_without_changing_contents_or_widths():
    d=Document();t=floating_frame(d,'Company Seal')
    grid=etree.tostring(t._tbl.tblGrid);cells=[etree.tostring(c._tc.tcPr) for row in t.rows for c in row.cells]
    changes=_stabilize_empty_signature_frames(d)
    assert len(changes)==1 and changes[0]['table_path']=='1'
    assert t._tbl.tblPr.find(qn('w:tblpPr')) is None
    assert etree.tostring(t._tbl.tblGrid)==grid
    assert [etree.tostring(c._tc.tcPr) for row in t.rows for c in row.cells]==cells
    assert t.cell(0,0).text=='Company Seal' and t.cell(1,0).text==''
    assert all(row._tr.trPr.find(qn('w:cantSplit')) is not None for row in t.rows)
    assert t.cell(0,0).paragraphs[0].paragraph_format.keep_with_next


def test_filled_signing_frame_and_other_floating_tables_are_unchanged():
    d=Document();t=floating_frame(d,'Company Seal');t.cell(1,0).text='Existing authorized content'
    other=floating_frame(d,'Important business note')
    before=[etree.tostring(x._tbl) for x in [t,other]]
    assert not _stabilize_empty_signature_frames(d)
    assert [etree.tostring(x._tbl) for x in [t,other]]==before


def test_blank_response_subrows_keep_distinct_values_and_unfilled_positions():
    d=Document();t=d.add_table(rows=1,cols=2);t.cell(0,0).text='Fictional A\nFictional B\nFictional C'
    assert _fill_blank_response_lines(t.cell(0,1),3,{2:'TEST_C',0:'TEST_A'})
    assert [p.text for p in t.cell(0,1).paragraphs]==['TEST_A','','TEST_C']
    assert t.cell(0,0).text=='Fictional A\nFictional B\nFictional C'
    assert not _fill_blank_response_lines(t.cell(0,1),3,{1:'OVERWRITE'})


def test_native_blank_subrows_export_partial_answers_to_independent_lines(tmp_path):
    import hashlib
    from docx.shared import Inches
    from questionnaire_structure_recognizer.parser_core.router import extract_document
    from questionnaire_structure_recognizer.validation import validate_and_assemble
    from questionnaire_structure_recognizer.exporter import fill_workflow_docx_template
    d=Document();t=d.add_table(rows=1,cols=3)
    for col,width in zip(t.columns,[3,.2,2]):col.width=Inches(width)
    t.cell(0,0).text='Fictional facility name'
    t.cell(0,0).add_paragraph('Fictional capacity')
    t.cell(0,0).add_paragraph('Fictional facility address')
    t.cell(0,1).text=':\n:\n:'
    source=tmp_path/'fictional.docx';d.save(source)
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    r=validate_and_assemble(extract_document(source).model_dump(),[],{'provider':'fixture'})
    fields=[f for f in r['questions'] if f.get('boundary_evidence')=='native_parallel_prompt_lines']
    assert len(fields)==3
    for index,value in [(0,'TEST_NAME'),(2,'TEST_ADDRESS')]:
        fields[index].update(answer_text_original=value,manual_override=True,answer_origin='manual')
    target=tmp_path/'export.docx';updates,warnings=fill_workflow_docx_template(source,r,target)
    assert not warnings and len(updates)==2
    reopened=Document(target).tables[0]
    assert [p.text for p in reopened.cell(0,2).paragraphs]==['TEST_NAME','','TEST_ADDRESS']
    assert reopened.cell(0,0).text==t.cell(0,0).text
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before


def test_layout_adjustment_sidecar_is_bound_to_unchanged_source_and_export(tmp_path):
    import hashlib,json
    from questionnaire_structure_recognizer.parser_core.router import extract_document
    from questionnaire_structure_recognizer.exporter import fill_workflow_docx_template
    d=Document();floating_frame(d,'Company Seal')
    source=tmp_path/'fictional-signing.docx';d.save(source)
    r=extract_document(source).model_dump();r['questions']=[]
    target=tmp_path/'export-signing.docx'
    updates,warnings=fill_workflow_docx_template(source,r,target)
    assert not updates and not warnings
    meta=json.loads(target.with_suffix('.metadata.json').read_text(encoding='utf8'))
    assert meta['source_sha256']==hashlib.sha256(source.read_bytes()).hexdigest()
    assert meta['export_sha256']==hashlib.sha256(target.read_bytes()).hexdigest()
    assert meta['layout_adjustments'][0]['table_xml_index']==0
    assert Document(source).tables[0]._tbl.tblPr.find(qn('w:tblpPr')) is not None
    assert Document(target).tables[0]._tbl.tblPr.find(qn('w:tblpPr')) is None


def test_verified_blank_response_indent_is_relaxed_without_widening_or_shrinking(tmp_path):
    import json
    from docx.shared import Inches,Pt
    from questionnaire_structure_recognizer.parser_core import extract_document
    from questionnaire_structure_recognizer.validation import validate_and_assemble
    from questionnaire_structure_recognizer.exporter import fill_workflow_docx_template
    d=Document();t=d.add_table(rows=1,cols=2);t.cell(0,0).text='What is the fictional reference?'
    c=t.cell(0,1);c.width=Inches(2);p=c.paragraphs[0]
    p.paragraph_format.left_indent=Pt(90);p.paragraph_format.right_indent=Pt(45)
    p.add_run().font.size=Pt(12)
    source=tmp_path/'indented.docx';d.save(source)
    from questionnaire_structure_recognizer.validation.form_completion import new_field
    from questionnaire_structure_recognizer.validation.field_boundaries import ref
    from questionnaire_structure_recognizer.form_positions import enrich_result_form_positions
    r=extract_document(source).model_dump()
    cells=[b for b in r['source_blocks'] if b['kind']=='table_cell']
    # The fixture independently labels this exact physical pairing; this
    # regression checks writeback layout, not a simulated model inference.
    f=new_field(cells[0],ref(cells[0]),[ref(cells[1])])
    f.update(pairing_validation='valid',validation_status='valid')
    r['questions']=[f]
    enrich_result_form_positions(r)
    f.update(manual_override=True,answer_origin='manual',answer_text_original='TEST_REFERENCE')
    dest=tmp_path/'indented-filled.docx';updates,warnings=fill_workflow_docx_template(source,r,dest)
    assert not warnings and len(updates)==1
    out=Document(dest).tables[0].cell(0,1)
    assert out.width==c.width and out.text=='TEST_REFERENCE'
    assert out.paragraphs[0].paragraph_format.left_indent==0 and out.paragraphs[0].paragraph_format.right_indent==0
    assert out.paragraphs[0].runs[-1].font.size==Pt(12)
    meta=json.loads(dest.with_suffix('.metadata.json').read_text(encoding='utf8'))
    assert meta['layout_adjustments'][0]['kind']=='blank_response_indent'
    assert Document(source).tables[0].cell(0,1).paragraphs[0].paragraph_format.left_indent==Pt(90)


def test_logical_response_lines_preserve_native_separator_gaps():
    d=Document();c=d.add_table(rows=1,cols=1).cell(0,0)
    assert _fill_blank_response_lines(c,3,{0:'TEST_A',1:'TEST_B',2:'TEST_C'},{0:3,1:2})
    assert [p.text for p in c.paragraphs]==['TEST_A\n\n\n','TEST_B\n\n','TEST_C']
