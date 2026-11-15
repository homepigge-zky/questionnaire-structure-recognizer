"""Source numbering and physical merge scopes, not a model accuracy test."""
from copy import deepcopy

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from questionnaire_structure_recognizer.parser_core.router import extract_document
from questionnaire_structure_recognizer.validation.validator import validate_and_assemble
from questionnaire_structure_recognizer.validation.word_table_context import native_word_table_context, apply_word_group_context


def _number(paragraph, level):
    props = paragraph._p.get_or_add_pPr()
    num = OxmlElement('w:numPr')
    for name, value in [('ilvl', str(level)), ('numId', '91')]:
        node = OxmlElement('w:' + name); node.set(qn('w:val'), value); num.append(node)
    props.append(num)


def _document(tmp_path):
    doc = Document(); doc.add_heading('Fictional supplier questionnaire', 0)
    numbering = doc.part.numbering_part.element
    abstract = OxmlElement('w:abstractNum'); abstract.set(qn('w:abstractNumId'), '91')
    for level in (0, 1):
        lvl = OxmlElement('w:lvl'); lvl.set(qn('w:ilvl'), str(level))
        for name, value in [('start', '1'), ('numFmt', 'decimal'), ('lvlText', '%1.' if level == 0 else '%1.%2')]:
            node=OxmlElement('w:'+name); node.set(qn('w:val'),value); lvl.append(node)
        abstract.append(lvl)
    numbering.append(abstract)
    num=OxmlElement('w:num'); num.set(qn('w:numId'),'91')
    node=OxmlElement('w:abstractNumId'); node.set(qn('w:val'),'91'); num.append(node);numbering.append(num)
    t=doc.add_table(rows=8, cols=4)
    for index, text in [(0,'Personnel'),(5,'Buildings')]:
        cell=t.cell(index,0).merge(t.cell(index,3));cell.text=text;_number(cell.paragraphs[0],0)
    anchor=t.cell(1,0).merge(t.cell(3,0)); _number(anchor.paragraphs[0],1)
    t.cell(1,1).merge(t.cell(1,3)).text='Does training cover the following areas?'
    for i,text in [(2,'Personal hygiene'),(3,'Process controls'),(4,'Separate next question?'),(6,'Personal hygiene'),(7,'Does the facility have a written policy?')]:
        t.cell(i,1).merge(t.cell(i,2)).text=text
        t.cell(i,3).text='☐ Yes ☐ No'
    path=tmp_path/'fictional-numbered-groups.docx';doc.save(path)
    return extract_document(path).model_dump()


def _walk(items):
    for item in items:
        yield item
        yield from _walk(item.get('children',[]))


def test_native_numbered_sections_and_merged_parent_scope_reach_fields(tmp_path):
    native=_document(tmp_path)
    result=validate_and_assemble(native,[{'classifications':[],'fields':[]}],{'model':'deterministic-fixture'})
    fields=list(_walk(result['questions']))
    hygiene=[f for f in fields if f['question_text_original']=='Personal hygiene']
    assert len(hygiene)==2
    assert [f['section_text_original'] for f in hygiene]==['Personnel','Buildings']
    assert [f['section_path'][-1]['original_number'] for f in hygiene]==['1','2']
    assert hygiene[0]['form_definition']['semantic_context']=='Does training cover the following areas?'
    assert not hygiene[1]['form_definition']['semantic_context']
    following,=[f for f in fields if f['question_text_original']=='Separate next question?']
    assert not following['form_definition']['semantic_context']
    assert all(f['answer_status']=='blank' for f in hygiene)


def test_full_width_numbered_question_is_not_a_section(tmp_path):
    native=_document(tmp_path)
    headings=[b for b in native['source_blocks'] if b['text']=='Buildings']
    headings[0]['text']='Does the facility have a written policy?'
    sections,_=native_word_table_context(native['source_blocks'])
    assert not sections  # No independently established sequence remains.


def test_broken_merge_continuation_does_not_propagate_parent(tmp_path):
    native=_document(tmp_path)
    continuation=next(b for b in native['source_blocks'] if b.get('structure',{}).get('is_merge_continuation'))
    continuation['structure']['merge_anchor_id']='another-physical-cell'
    _,groups=native_word_table_context(native['source_blocks'])
    assert not groups


def test_manual_ancestor_preserves_descendant_context_and_source_values(tmp_path):
    native=_document(tmp_path);blocks=native['source_blocks'];lookup={b['block_id']:b for b in blocks}
    _,groups=native_word_table_context(blocks)
    label=lookup[groups[0]['label_block_ids'][0]]
    child={'label_refs':[{'block_id':label['block_id'],'start':0,'end':len(label['text'])}],
           'answer_region_refs':[],'context_refs':[],'answer_text_original':'MANUAL_VALUE'}
    parent={'manual_override':True,'children':[child]};before=deepcopy(parent)
    apply_word_group_context([parent],groups,lookup)
    assert parent==before


def test_native_section_caption_does_not_delete_its_record_fields(tmp_path):
    # A source-backed record table group is also a numbered section. The
    # section is not an answer, but its independently located children remain.
    from questionnaire_structure_recognizer.validation.form_completion import complete_local_fields
    from questionnaire_structure_recognizer.validation.validator import _apply_special_table_fields
    doc=Document();t=doc.add_table(rows=4,cols=3)
    for c,text in zip(t.rows[0].cells,['No.','Fictional product','Origin']):c.text=text
    for i in range(1,4):
        t.cell(i,0).text=str(i);t.cell(i,2).text='☐ Plant ☐ Mineral'
    p=tmp_path/'fictional-record.docx';doc.save(p)
    blocks=extract_document(p).model_dump()['source_blocks']
    for b in blocks:
        s=b['structure'];s['local_layout_type']='record_table_header' if s['row_start']==1 else 'record_table'
        if s['row_start']>1:s['record_header_row']=1
    lookup={b['block_id']:b for b in blocks};questions=[]
    _apply_special_table_fields(questions,blocks,lookup)
    assert len(questions)==1 and len(questions[0]['children'])==9
    root=questions[0]
    # This is the source role the independently verified full-width heading
    # resolver attaches to a native record container's caption.
    lookup[root['label_refs'][0]['block_id']]['structure']['resolved_layout_role']='section_header'
    modeled=deepcopy(questions);modeled[0].pop('boundary_evidence')
    complete_local_fields(modeled,blocks,lookup)
    assert not [f for f in _walk(modeled) if f['question_text_original']=='Fictional product']
    fixed=deepcopy(questions);fixed_blocks=deepcopy(blocks)
    fixed_lookup={b['block_id']:b for b in fixed_blocks}
    fixed_lookup[root['label_refs'][0]['block_id']]['structure']['resolved_layout_role']='contract_clause'
    complete_local_fields(fixed,fixed_blocks,fixed_lookup)
    assert not [f for f in _walk(fixed) if f['question_text_original']=='Fictional product']
    complete_local_fields(questions,blocks,lookup)
    products=[f for f in _walk(questions) if f['question_text_original']=='Fictional product']
    assert len(products)==3
    assert len({f['answer_region_refs'][0]['block_id'] for f in products})==3
    assert all(f['answer_status']=='blank' for f in products)
