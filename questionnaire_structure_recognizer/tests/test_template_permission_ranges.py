import hashlib

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from questionnaire_structure_recognizer.exporter import fill_workflow_docx_template,TemplateFillError
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble


def fixture(tmp_path,group='everyone'):
    path=tmp_path/'FICTIONAL_PROTECTED.docx'; document=Document()
    table=document.add_table(rows=2,cols=2)
    table.cell(0,0).text='Supplier name';table.cell(1,0).text='Customer approval'
    p=table.cell(0,1).paragraphs[0]
    start=OxmlElement('w:permStart');start.set(qn('w:id'),'7');start.set(qn('w:edGrp'),group)
    end=OxmlElement('w:permEnd');end.set(qn('w:id'),'7')
    p._p.append(start);p.add_run(' ');p._p.append(end)
    protection=OxmlElement('w:documentProtection');protection.set(qn('w:edit'),'readOnly');protection.set(qn('w:enforcement'),'1')
    document.settings._element.append(protection);document.save(path)
    native=extract_document(path).model_dump()
    result=validate_and_assemble(native,[],{'provider':'fixture'})
    field=next(f for f in result['questions']if 'Supplier name' in f['question_text_original'])
    field.update(manual_override=True,answer_origin='manual',answer_text_original='TEST_NAME')
    return path,result


def test_explicit_everyone_range_can_be_filled_without_unprotecting(tmp_path):
    source,result=fixture(tmp_path); before=hashlib.sha256(source.read_bytes()).hexdigest()
    out=tmp_path/'out.docx';updates,warnings=fill_workflow_docx_template(source,result,out)
    assert len(updates)==1 and not warnings
    doc=Document(out);p=doc.tables[0].cell(0,1).paragraphs[0]
    assert p.text.strip()=='TEST_NAME'
    assert doc.tables[0].cell(1,1).text==''
    assert doc.settings._element.find(qn('w:documentProtection')).get(qn('w:enforcement'))=='1'
    children=list(p._p);a=next(i for i,n in enumerate(children)if n.tag==qn('w:permStart'))
    z=next(i for i,n in enumerate(children)if n.tag==qn('w:permEnd'))
    assert 'TEST_NAME' in ''.join(''.join(n.itertext())for n in children[a+1:z])
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before


def test_named_editor_is_not_implicitly_authorized(tmp_path):
    source,result=fixture(tmp_path,'editors')
    with pytest.raises(TemplateFillError,match='保护|权限'):
        fill_workflow_docx_template(source,result,tmp_path/'out.docx')


def test_writing_another_region_cannot_borrow_an_everyone_permission(tmp_path):
    source,result=fixture(tmp_path)
    other=next(f for f in result['questions']if 'Customer approval' in f['question_text_original'])
    other.update(manual_override=True,answer_origin='manual',answer_text_original='FORBIDDEN')
    with pytest.raises(TemplateFillError,match='保护|权限'):
        fill_workflow_docx_template(source,result,tmp_path/'out.docx')
    assert not (tmp_path/'out.docx').exists()


def test_permission_preflight_rejects_entire_plan_before_first_mutation(tmp_path, monkeypatch):
    from questionnaire_structure_recognizer import exporter
    source, result = fixture(tmp_path)
    other = next(f for f in result['questions'] if 'Customer approval' in f['question_text_original'])
    other.update(manual_override=True, answer_origin='manual', answer_text_original='FORBIDDEN')
    attempted = []
    original = exporter._append_styled
    def observed(*args, **kwargs):
        attempted.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(exporter, '_append_styled', observed)
    with pytest.raises(TemplateFillError, match='Customer approval'):
        exporter.fill_workflow_docx_template(source, result, tmp_path / 'out.docx')
    assert attempted == []
    assert not (tmp_path / 'out.docx').exists()


@pytest.mark.parametrize('mutation',['paragraph_property','permission_group','permission_position','protected_label'])
def test_xml_guard_rejects_changes_outside_allowed_runs(tmp_path,mutation):
    from questionnaire_structure_recognizer.template_permissions import ReadonlyRangeGuard
    source,_=fixture(tmp_path);doc=Document(source);guard=ReadonlyRangeGuard(doc)
    p=doc.tables[0].cell(0,1).paragraphs[0]
    if mutation=='paragraph_property':p.paragraph_format.keep_with_next=True
    if mutation=='permission_group':p._p.find(qn('w:permStart')).set(qn('w:edGrp'),'editors')
    if mutation=='permission_position':
        end=p._p.find(qn('w:permEnd'));p._p.remove(end);doc.tables[0].cell(1,1).paragraphs[0]._p.append(end)
    if mutation=='protected_label':doc.tables[0].cell(0,0).paragraphs[0].runs[0].text='CHANGED LABEL'
    with pytest.raises(TemplateFillError,match='保护|权限'):guard.verify()


def test_empty_permission_spanning_paragraph_to_cell_can_append_inside(tmp_path):
    from questionnaire_structure_recognizer.template_permissions import ReadonlyRangeGuard
    source,_=fixture(tmp_path);doc=Document(source);cell=doc.tables[0].cell(0,1);p=cell.paragraphs[0]
    end=p._p.find(qn('w:permEnd'));p._p.remove(end);cell._tc.append(end)
    guard=ReadonlyRangeGuard(doc);guard.append(p,'TEST_SPAN');guard.verify()
    assert p.text.strip()=='TEST_SPAN'


def test_column_restricted_permissions_require_specific_column_support(tmp_path):
    source,result=fixture(tmp_path);doc=Document(source)
    start=doc.tables[0].cell(0,1).paragraphs[0]._p.find(qn('w:permStart'))
    start.set(qn('w:colFirst'),'1');start.set(qn('w:colLast'),'1');doc.save(source)
    with pytest.raises(TemplateFillError,match='保护|权限'):
        fill_workflow_docx_template(source,result,tmp_path/'out.docx')
