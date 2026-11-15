from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from copy import deepcopy
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_BREAK
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from .form_positions import make_writeback_plan, build_form_definition, protected_source_refs, validate_form_definition


class TemplateFillError(ValueError):
    pass


def _save_template_body(source: Path, document, destination: Path) -> None:
    """All supported template targets belong to word/document.xml.

    Preserve the original package's other parts byte-for-byte, including
    parts python-docx does not load. Repacking an entire Document would
    reserialize styles/settings and can discard unreferenced original XML.
    Permission and source-fingerprint checks run before the write plan.
    """
    destination.parent.mkdir(parents=True,exist_ok=True)
    handle,name=tempfile.mkstemp(prefix='.qsr-export-',suffix='.docx',dir=destination.parent)
    os.close(handle);temporary=Path(name)
    try:
        with zipfile.ZipFile(source) as original,zipfile.ZipFile(temporary,'w') as output:
            if original.namelist().count('word/document.xml')!=1:
                raise TemplateFillError('原模板主文档部件不唯一，已停止导出')
            output.comment=original.comment
            for info in original.infolist():
                output.writestr(info,document.part.blob if info.filename=='word/document.xml' else original.read(info.filename))
        Document(temporary)
        os.replace(temporary,destination)
    finally:
        temporary.unlink(missing_ok=True)


def _check_template_permissions(source: Path, destination: Path, *, allow_readonly_ranges=False) -> None:
    if source.resolve() == destination.resolve():
        raise TemplateFillError("原文件只读，导出必须写入独立副本")
    with zipfile.ZipFile(source) as package:
        if any(name.lower().startswith("_xmlsignatures/") for name in package.namelist()):
            raise TemplateFillError("原文件包含数字签名，不能修改已签署的模板；请使用标准导出")
    document = Document(source)
    protection = document.settings._element.find(qn("w:documentProtection"))
    if protection is not None and protection.get(qn("w:enforcement"), "0").lower() in {"1", "true", "on"}:
        from .template_permissions import allowed_range_ids
        if allow_readonly_ranges and protection.get(qn('w:edit'))=='readOnly' and allowed_range_ids(document._element):
            return
        raise TemplateFillError("原文件启用了编辑保护，不能绕过限制回填；请使用标准导出")


def _replace_paragraph_range(paragraph, start: int, end: int, value: str) -> bool:
    """Replace one visible-text range without rebuilding the paragraph.

    Word commonly splits a single label over many runs.  Mutating
    ``cell.text`` destroys those runs and their formatting, so locate the
    affected runs and change only their text.  Offsets are based on
    ``paragraph.text``, matching the parser's visible paragraph text.
    """
    if not 0 <= start <= end <= len(paragraph.text):
        return False
    # Tabs are column positioning, not disposable placeholder text. Insert at
    # the beginning of an empty slot while preserving its native tab stops.
    placeholder = paragraph.text[start:end]
    if '\t' in placeholder and not placeholder.strip():
        end = start
    runs = paragraph.runs
    if not runs:
        if start == end == 0:
            paragraph.add_run(value)
            return True
        return False
    positions = []
    cursor = 0
    for run in runs:
        positions.append((cursor, cursor + len(run.text), run))
        cursor += len(run.text)
    # Zero-width insertion at the end belongs to the final run so its style
    # is inherited from the adjacent prompt punctuation.
    if start == end == cursor:
        runs[-1].text += value
        return True
    start_index = next((i for i, (a, b, _) in enumerate(positions) if a <= start < b or (a == b == start)), None)
    end_index = start_index if start == end else next((i for i, (a, b, _) in enumerate(positions) if a < end <= b), None)
    if start_index is None or end_index is None:
        return False
    sa, _, start_run = positions[start_index]
    ea, _, end_run = positions[end_index]
    prefix = start_run.text[:start - sa]
    suffix = end_run.text[end - ea:]
    start_run.text = prefix + value + suffix
    for index in range(start_index + 1, end_index + 1):
        runs[index].text = ""
    return True


def _append_styled(paragraph,value,permission_guard=None):
    if permission_guard is not None:
        return permission_guard.append(paragraph,value)
    template=next((r for r in reversed(paragraph.runs) if r._r.rPr is not None),None)
    run=paragraph.add_run(value)
    if template is not None:run._r.insert(0,deepcopy(template._r.rPr))
    elif paragraph._p.pPr is not None and paragraph._p.pPr.find(qn('w:rPr')) is not None:
        run._r.insert(0,deepcopy(paragraph._p.pPr.find(qn('w:rPr'))))
    return run


def _replace_container_range(container, container_kind: str, start: int, end: int, value: str) -> bool:
    if container_kind == "paragraph":
        return _replace_paragraph_range(container, start, end, value)
    # The parser joins non-empty cell paragraphs with one newline.  Map its
    # stable source offset back to the exact paragraph and keep every other
    # paragraph, border, width and run untouched.
    paragraphs = [paragraph for paragraph in container.paragraphs if paragraph.text.strip()]
    cursor = 0
    for paragraph in paragraphs:
        visible = paragraph.text.strip()
        paragraph_end = cursor + len(visible)
        if cursor <= start <= end <= paragraph_end:
            leading = len(paragraph.text) - len(paragraph.text.lstrip())
            return _replace_paragraph_range(paragraph, start - cursor + leading, end - cursor + leading, value)
        cursor = paragraph_end + 1
    return False


def knowledge_filled_fields(result: dict) -> list[dict]:
    fields: list[dict] = []

    def visit(field: dict) -> None:
        if field.get("knowledge_source") and field.get("fill_status") == "auto_filled_pending_check":
            fields.append(field)
        for child in field.get("children") or []:
            visit(child)

    for item in result.get("questions") or []:
        visit(item)
    return fields


def fill_docx_template(source: str | Path, result: dict, destination: str | Path) -> list[dict]:
    source, destination = Path(source), Path(destination)
    if source.suffix.lower() != ".docx":
        raise TemplateFillError("原模板回填当前只支持 DOCX")
    _check_template_permissions(source, destination)
    document = Document(source)
    source_lookup = {block["block_id"]: block for block in result.get("source_blocks") or []}
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if result.get("source_hash") and result["source_hash"] != source_hash:
        raise TemplateFillError("原模板内容指纹与识别版本不一致，已停止回填")
    protected = protected_source_refs(result, source_lookup)
    updates: list[dict] = []
    occupied: set[tuple[int, int, int]] = set()
    for field in knowledge_filled_fields(result):
        answer = str(field.get("answer_text_original") or "").strip()
        source_answer = str(field.get("knowledge_source", {}).get("answer") or "").strip()
        if not answer or answer != source_answer:
            raise TemplateFillError(f"字段 {field.get('field_id')} 的答案与知识版本快照不一致")
        regions = field.get("answer_region_refs") or []
        if len(regions) != 1:
            raise TemplateFillError(f"字段 {field.get('field_id')} 没有唯一的可定位答复区域")
        definition = build_form_definition(field, source_lookup, source_hash, str(result.get("parser_version") or ""))
        errors = validate_form_definition(definition, source_lookup, source_hash, protected)
        if errors: raise TemplateFillError(f"字段 {field.get('field_id')}: {'；'.join(errors)}")
        ref = regions[0]
        block = source_lookup.get(ref.get("block_id"))
        if not block or block.get("kind") != "table_cell":
            raise TemplateFillError(f"字段 {field.get('field_id')} 的答复区域不是 Word 表格单元格")
        location, structure = block.get("location") or {}, block.get("structure") or {}
        if location.get("part") != "word/document.xml" or structure.get("nested_level", 0):
            raise TemplateFillError("暂不支持在页眉页脚或嵌套表格中自动回填")
        table_no, row_no, column_no = location.get("table"), structure.get("row_start"), structure.get("column_start")
        if not all(isinstance(value, int) and value > 0 for value in (table_no, row_no, column_no)):
            raise TemplateFillError(f"字段 {field.get('field_id')} 暂不支持嵌套或无法定位的表格区域")
        if table_no > len(document.tables):
            raise TemplateFillError(f"Word 中不存在第 {table_no} 个表格")
        cell = document.tables[table_no - 1].cell(row_no - 1, column_no - 1)
        key = (table_no, row_no, column_no)
        slot_id = str(ref.get("slot_id") or "")
        if key in occupied:
            raise TemplateFillError(f"多个答案试图写入同一物理单元格：{key}")
        if slot_id.endswith(":after-content"):
            cell.add_paragraph(answer)
            mode = "append_after_label"
        elif structure.get("blank_state") == "blank" and not block.get("text") and not cell.text.strip():
            cell.paragraphs[0].add_run(answer)
            mode = "fill_blank_cell"
        else:
            raise TemplateFillError(f"字段 {field.get('field_id')} 的目标区域不是已确认空白格或标签后填写槽位")
        occupied.add(key)
        updates.append({
            "field_id": field.get("field_id"), "answer": answer, "table": table_no,
            "row": row_no, "column": column_no, "mode": mode,
            "knowledge_source": deepcopy(field.get("knowledge_source")),
        })
    if not updates:
        raise TemplateFillError("当前结果没有可写入的知识库答案")
    destination.parent.mkdir(parents=True, exist_ok=True)
    document.save(destination)
    # Re-open the generated package. This catches corrupt ZIP/OOXML output
    # before it is offered for download.
    Document(destination)
    return updates


def _display_answer(field: dict) -> str:
    selected = field.get("selected_options") or []
    if selected:
        from .choice_identity import selected_labels
        return "\n".join(f"☒ {value}" for value in selected_labels(field.get('options') or [],selected))
    return str(field.get("answer_text_original") or "")


def _workflow_fields(result: dict) -> list[dict]:
    fields: list[dict] = []
    def visit(field: dict) -> None:
        if field.get("manual_override") or field.get("answer_origin") == "reviewed_knowledge":
            fields.append(field)
        for child in field.get("children") or []:
            visit(child)
    for field in result.get("questions") or []:
        visit(field)
    return fields


def locate_docx_target(document,target):
    """Resolve source-native table paths, including nested physical cells."""
    location,structure=target.get('location') or {},target.get('structure') or {}
    if location.get('part')!='word/document.xml' or location.get('textbox'):return None
    table_path=location.get('table')
    if table_path is not None:
        from docx.table import _Cell
        try:
            path=[int(x) for x in str(table_path).split('.')]
            if len(path)%3!=1 or any(x<1 for x in path):return None
            table=document.tables[path[0]-1]
            for offset in range(1,len(path),3):
                row,physical_cell,nested=path[offset:offset+3]
                parent=_Cell(table.rows[row-1]._tr.tc_lst[physical_cell-1],table)
                table=parent.tables[nested-1]
            row=table.rows[structure['row_start']-1]
            cell=row.cells[structure['grid_col_start']-1-getattr(row,'grid_cols_before',0)]
            if target.get('paragraph_index') is not None:return cell.paragraphs[target['paragraph_index']-1],'paragraph'
            return cell,'cell'
        except (ValueError,IndexError,KeyError):return None
    pn=location.get('paragraph')
    if isinstance(pn,int) and 0<pn<=len(document.paragraphs):return document.paragraphs[pn-1],'paragraph'
    return None


def _stabilize_empty_signature_frames(document):
    """Keep small empty signing frames out of headers after answer reflow.

    Text-anchored floating tables can split across pages in Word renderers.
    Only clearly labelled, unfilled single-column frames are moved into the
    normal text flow; borders, widths, text, fonts and blank cells stay intact.
    Every intentional layout change is returned for export audit metadata.
    """
    from docx.oxml import OxmlElement
    changes=[]
    for index,table in enumerate(document.tables,1):
        properties=table._tbl.tblPr;floating=properties.find(qn('w:tblpPr'))
        if floating is None or floating.get(qn('w:vertAnchor'))!='text' or len(table.columns)!=1 or not 1<=len(table.rows)<=3:continue
        texts=[p.text.strip() for row in table.rows for cell in row.cells for p in cell.paragraphs if p.text.strip()]
        if not texts or not all(re.fullmatch(r'(?:(?:company|supplier|vendor|official)\s+)?(?:seal|stamp|signature)|(?:公司|单位)?(?:盖章|印章|签章)',t,re.I) for t in texts):continue
        if table._tbl.xpath('.//w:drawing|.//w:pict|.//w:object|.//w:sdt|.//w:fldChar'):continue
        old_align=properties.find(qn('w:jc'))
        change={'kind':'floating_signature_frame_to_flow','table_path':str(index),'removed_tblpPr':dict(floating.attrib),'alignment_before':old_align.get(qn('w:val')) if old_align is not None else None,'alignment_after':'right','row_keep_together':True,'changed_properties':['tblPr/tblpPr','tblPr/jc','trPr/cantSplit','tc/p/pPr/keepLines','tc/p/pPr/keepNext'],'reason':'未填签章框改为正文右对齐表格，避免浮动跨页覆盖页眉；内容、边框、宽度和字体保持原样。'}
        change['table_xml_index']=document._element.xpath('.//w:tbl').index(table._tbl)
        properties.remove(floating);table.alignment=WD_TABLE_ALIGNMENT.RIGHT
        for ri,row in enumerate(table.rows):
            pr=row._tr.get_or_add_trPr()
            if pr.find(qn('w:cantSplit')) is None:pr.append(OxmlElement('w:cantSplit'))
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    paragraph.paragraph_format.keep_together=True
                    if ri<len(table.rows)-1:paragraph.paragraph_format.keep_with_next=True
        changes.append(change)
    return changes


def _relax_excessive_response_indents(document,container,kind,target,field_id):
    """Repair source blank-form padding that leaves no readable text width.

    This applies only inside an explicitly writable response cell. It never
    widens a table or changes fonts, borders, fixed document prose or answers.
    """
    from docx.table import _Cell
    root=container._p if kind=='paragraph' else container._tc
    tc=root if kind=='cell' else next(iter(root.iterancestors(qn('w:tc'))),None)
    if tc is None:return []
    cell=container if kind=='cell' else _Cell(tc,container._parent)
    width=tc.find('./'+qn('w:tcPr')+'/'+qn('w:tcW'))
    if width is None or width.get(qn('w:type'))!='dxa':return []
    try:twips=int(width.get(qn('w:w'),'0'))
    except ValueError:return []
    if twips<1200:return []
    paragraphs=[container] if kind=='paragraph' else ([cell.paragraphs[-1]] if target.get('write_method')=='append_after_label' else cell.paragraphs)
    changes=[]
    for paragraph in paragraphs:
        props=paragraph._p.pPr;indent=props.find(qn('w:ind')) if props is not None else None
        if indent is None:continue
        def val(*names):
            for name in names:
                raw=indent.get(qn('w:'+name))
                if raw is not None:
                    try:return max(0,int(raw))
                    except ValueError:return 0
            return 0
        consumed=val('start','left')+val('end','right')+val('firstLine')
        if consumed<=twips*.65:continue
        before=dict(indent.attrib)
        for name in ('start','end','left','right','firstLine','hanging','startChars','endChars','leftChars','rightChars','firstLineChars','hangingChars'):
            attr=qn('w:'+name)
            if attr in indent.attrib:indent.set(attr,'0')
        changes.append({'kind':'blank_response_indent','field_id':field_id,'target_id':target['target_id'],'cell_xml_index':document._element.xpath('.//w:tc').index(tc),'paragraph_index':cell._tc.xpath('./w:p').index(paragraph._p)+1,'cell_width_twips':twips,'indent_before':before,'indent_after':dict(indent.attrib),'changed_properties':['tc/p/pPr/ind'],'reason':'原答复段落缩进占用超过65%单元格宽度，清除该空白填写段落的过大缩进，保留字号、边框、宽度和其他原文。'})
    return changes


def _fill_blank_response_lines(cell,line_count,values,gaps=None):
    """Fill named logical rows in one evidenced blank response cell."""
    if not isinstance(line_count,int) or not 1<=line_count<=30 or cell.text.strip():return False
    if any(not isinstance(i,int) or not 0<=i<line_count for i in values):return False
    if cell._tc.xpath('.//w:drawing|.//w:pict|.//w:sdt|.//w:fldChar|.//w:tbl'):return False
    if any(b.get(qn('w:type')) in {'page','column'} for b in cell._tc.xpath('.//w:br')):return False
    # The native logical lines replace the evidenced empty response area.
    # Existing line-break runs must not add an extra unlabelled row before the
    # first value. Keep paragraph/run properties; remove only blank content.
    for paragraph in cell.paragraphs:
        for node in paragraph._p.xpath('.//w:t'):
            if not (node.text or '').strip():node.text=''
        for node in paragraph._p.xpath('.//w:br|.//w:cr|.//w:tab'):
            node.getparent().remove(node)
    while len(cell.paragraphs)<line_count:
        new=cell.add_paragraph()
        source=cell.paragraphs[0]._p.pPr
        if source is not None:new._p.insert(0,deepcopy(source))
    for index in range(line_count):
        if index in values:_append_styled(cell.paragraphs[index],values[index])
        for _ in range((gaps or {}).get(index,0)):cell.paragraphs[index].add_run().add_break()
    return True


def fill_workflow_docx_template(source: str | Path, result: dict, destination: str | Path) -> tuple[list[dict], list[str]]:
    """Write saved workflow answers only to validated physical answer regions."""
    source, destination = Path(source), Path(destination)
    if source.suffix.lower() != ".docx":
        raise TemplateFillError("原模板回填只适用于 DOCX；PDF 任务请使用标准 Word 导出")
    _check_template_permissions(source, destination, allow_readonly_ranges=True)
    expected_hash = str((result.get("extraction_metadata") or {}).get("converted_template_hash") if result.get("file_type") == "DOC" else result.get("source_hash") or "")
    actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if expected_hash and actual_hash != expected_hash:
        raise TemplateFillError("原模板内容指纹与识别版本不一致，已停止回填")
    document = Document(source)
    from .template_permissions import readonly_guard
    permission_guard=readonly_guard(document)
    plan, warnings = make_writeback_plan(result)

    def target_container(target):
        location, structure = target.get("location") or {}, target.get("structure") or {}
        if location.get("part") != "word/document.xml" or location.get("textbox") or structure.get("nested_level", 0):
            return None
        table_no, row_no, column_no = location.get("table"), structure.get("row_start"), structure.get("grid_col_start")
        if all(isinstance(value, int) and value > 0 for value in (table_no, row_no, column_no)) and table_no <= len(document.tables):
            table = document.tables[table_no - 1]
            if row_no > len(table.rows): return None
            row = table.rows[row_no - 1]
            before = getattr(row, "grid_cols_before", 0)
            index = column_no - 1 - before
            if index < 0 or index >= len(row.cells): return None
            cell = row.cells[index]
            paragraph_index = target.get("paragraph_index")
            if paragraph_index is not None:
                if not isinstance(paragraph_index, int) or not 0 < paragraph_index <= len(cell.paragraphs): return None
                return cell.paragraphs[paragraph_index - 1], "paragraph"
            return cell, "cell"
        paragraph_no = location.get("paragraph")
        if location.get("part") == "word/document.xml" and isinstance(paragraph_no, int) and 0 < paragraph_no <= len(document.paragraphs):
            return document.paragraphs[paragraph_no - 1], "paragraph"
        return None

    # Validate every target against the untouched template before performing
    # any mutation. This prevents a partially written file when one target is stale.
    executable = []
    permission_preflight = []
    for item in plan:
        target, value = item["target"], str(item.get("value") or "")
        container = locate_docx_target(document,target)
        if container is None:
            warnings.append(f"{item['question']}: Word 物理位置无效或暂不支持")
            continue
        cell, container_kind = container
        expected = str(target.get("expected_paragraph_text") if target.get("paragraph_index") is not None else target.get("expected_text") or "")
        from lxml import etree
        from .parser_core.docx_parser import _visible_text, NS
        root = etree.fromstring(etree.tostring(cell._p if container_kind == 'paragraph' else cell._tc))
        # Check against exactly the parser's source representation, including
        # superscript units and per-paragraph trimming. The content fingerprint
        # is checked separately; this is not a fuzzy comparison.
        parts = [_visible_text(p)[0] for p in (root.findall('w:p', NS) if container_kind == 'cell' else [root])]
        current_text = '\n'.join(text for text in parts if text)
        if current_text.strip() != expected.strip():
            warnings.append(f"{item['question']}: 原模板目标状态已变化，已跳过")
            continue
        if permission_guard is not None:
            method = target['write_method']
            if method in {'fill_blank_cell', 'set_choice_cell', 'fill_blank_paragraph', 'append_after_label'}:
                paragraphs = [cell] if container_kind == 'paragraph' else cell.paragraphs
                if not paragraphs:
                    raise TemplateFillError(f"{item['question']}: 受保护目标不存在可授权的答复段落，已停止导出")
                paragraph = paragraphs[-1 if method == 'append_after_label' else 0]
                try:
                    authorization = permission_guard.authorize_append(paragraph)
                except TemplateFillError as exc:
                    raise TemplateFillError(f"{item['question']}: {exc}") from exc
                permission_preflight.append({'field_id': item['field_id'],
                    'target_id': target.get('target_id'), 'write_method': method,
                    'authorization': authorization})
            else:
                # Exact run replacements keep the existing interval handling;
                # the complete protected-XML comparison below remains mandatory.
                permission_preflight.append({'field_id': item['field_id'],
                    'target_id': target.get('target_id'), 'write_method': method,
                    'authorization': {'kind': 'protected_xml_diff_required'}})
        executable.append((item, cell, container_kind, value))

    # Multiple inline slots in one cell are replaced from right to left so
    # earlier source offsets remain stable.
    executable.sort(key=lambda entry: (entry[0]["target"].get("block_id", ""), -int(entry[0]["target"].get("ref", {}).get("start", 0))))
    updates = []
    # Multiple named blank lines share a cell but never a writing slot. The
    # full native slot declaration is checked before creating any paragraphs.
    layout_adjustments=[]
    source_blocks={b['block_id']:b for b in result.get('source_blocks',[])}
    field_lookup={}
    def collect_fields(fields):
        for f in fields:
            field_lookup[f['field_id']]=f;collect_fields(f.get('children',[]))
    collect_fields(result.get('questions',[]))
    line_groups={}
    for entry in executable:
        if entry[0]['target']['write_method']=='fill_blank_line_slots':line_groups.setdefault(entry[0]['target']['block_id'],[]).append(entry)
    completed_line_groups=set()
    for item, cell, container_kind, value in executable:
        target, method = item["target"], item["target"]["write_method"]
        ref = target.get("ref") or {}
        if method=='fill_blank_line_slots':
            bid=target['block_id']
            if bid in completed_line_groups:continue
            completed_line_groups.add(bid);entries=line_groups[bid]
            slots=[s for s in source_blocks.get(bid,{}).get('structure',{}).get('logical_slots',[]) if s.get('kind')=='blank_line_slot']
            count=target.get('line_count');indices=[e[0]['target'].get('line_index') for e in entries]
            declarations={s.get('line_index'):s for s in slots}
            legal=isinstance(count,int) and 1<=count<=30 and len(slots)==count and set(declarations)==set(range(count)) and len({s.get('slot_id') for s in slots})==count
            legal=legal and all(isinstance(s.get('slot_id'),str) and s['slot_id'] and s.get('line_count')==count for s in slots)
            legal=legal and len(set(indices))==len(indices) and all(isinstance(i,int) and 0<=i<count for i in indices)
            legal=legal and all(e[0]['target'].get('line_count')==count and declarations.get(e[0]['target'].get('line_index'),{}).get('slot_id')==e[0]['target'].get('ref',{}).get('slot_id') for e in entries)
            legal=legal and not any(e[0]['target'].get('block_id')==bid and e[0]['target']['write_method']!=method for e in executable)
            gaps={}
            layout_indices=[declarations.get(i,{}).get('layout_paragraph_index') for i in range(count)] if isinstance(count,int) else []
            separators={s.get('separator_block_id') for s in slots}
            if legal and all(isinstance(i,int) and i>0 for i in layout_indices) and layout_indices==sorted(set(layout_indices)) and len(separators)==1 and next(iter(separators)) in source_blocks:
                separator=source_blocks[next(iter(separators))]
                spans=separator.get('structure',{}).get('paragraph_spans',[])
                if all(i<=len(spans) and spans[i-1].get('text','').strip() in {':','：'} for i in layout_indices):
                    gaps={i:layout_indices[i+1]-layout_indices[i]-1 for i in range(count-1)}
            if not legal or container_kind!='cell' or not _fill_blank_response_lines(cell,count,{e[0]['target']['line_index']:e[3] for e in entries},gaps):
                warnings.append(f"{item['question']}: 空白子行声明或目标状态不一致，已停止该单元格写入")
                continue
            if gaps:
                old=cell._tc.tcPr.find(qn('w:vAlign'));before=old.get(qn('w:val')) if old is not None else None
                cell.vertical_alignment=WD_CELL_VERTICAL_ALIGNMENT.TOP
                layout_adjustments.append({'kind':'logical_response_line_alignment','field_ids':[e[0]['field_id'] for e in entries],'target_id':bid,'cell_xml_index':document._element.xpath('.//w:tc').index(cell._tc),'vertical_alignment_before':before,'vertical_alignment_after':'top','source_separator_block_id':separator['block_id'],'source_separator_paragraph_indices':layout_indices,'paragraph_gap_lines':gaps,'changed_properties':['tcPr/vAlign','tc/p/r/br'],'reason':'按原生冒号栏的段落空行对齐独立子字段；原答复格从底部对齐改为顶部对齐，标签及表格结构保留。'})
            layout_adjustments.extend(_relax_excessive_response_indents(document,cell,container_kind,target,item['field_id']))
            for child,_,_,_ in entries:updates.append({'field_id':child['field_id'],'target_id':child['target'].get('target_id'),'mode':method,'answer_source':child.get('answer_source')})
            continue
        if target.get('block_id') in line_groups:
            warnings.append(f"{item['question']}: 同一答复格存在互相冲突的整格和子行写入，已跳过")
            continue
        original=field_lookup.get(item['field_id'],{})
        if permission_guard is None and (original.get('original_answer_status') or original.get('answer_status'))=='blank' and method in {'fill_blank_cell','fill_blank_paragraph','append_after_label','replace_text_range','fill_underlined_blank'}:
            layout_adjustments.extend(_relax_excessive_response_indents(document,cell,container_kind,target,item['field_id']))
        if method == "fill_blank_paragraph":
            _append_styled(cell,value,permission_guard)
        elif method in {"fill_blank_cell", "set_choice_cell"}:
            paragraph = cell.paragraphs[0] if cell.paragraphs else cell.add_paragraph()
            _append_styled(paragraph,value,permission_guard)
        elif method == "append_after_label":
            paragraph = cell if container_kind == "paragraph" else (cell.paragraphs[-1] if cell.paragraphs else cell.add_paragraph())
            _append_styled(paragraph,(' ' if paragraph.text and not paragraph.text[-1].isspace() and paragraph.text[-1] not in ':：;；' else '')+value,permission_guard)
        elif method == 'fill_underlined_blank':
            paragraphs=[cell] if container_kind=='paragraph' else cell.paragraphs
            candidates=[(run,re.search(r'[ \u3000]{2,}$',run.text)) for p in paragraphs for run in p.runs if run.underline]
            candidates=[(run,m) for run,m in candidates if m is not None]
            if len(candidates)!=1:
                warnings.append(f"{item['question']}: 下划线填写运行未唯一定位，已跳过")
                continue
            run,span=candidates[0]
            run.text=run.text[:span.start()]+value+run.text[span.end():]
        elif method == 'replace_native_paragraph_group':
            from .parser_core.paragraph_groups import write_paragraph_group
            if permission_guard is not None or container_kind!='cell' or not write_paragraph_group(cell,target,value):
                warnings.append(f"{item['question']}: 连续答复段落状态或编辑权限不符，已停止该位置写入")
                continue
        elif method in {"replace_text_range", "replace_existing_value", "prepend_before_unit"}:
            start, end = int(ref.get("start", 0)), int(ref.get("end", 0))
            if target.get('paragraph_source_start') is not None:
                leading=len(cell.text)-len(cell.text.lstrip())
                start=start-target['paragraph_source_start']+leading
                end=end-target['paragraph_source_start']+leading
            from .validation.word_label_slots import pad_inline_label_value
            inserted = pad_inline_label_value(target, value, source_blocks)
            if method=='replace_text_range' and ':prompt-slot:' in str(ref.get('slot_id','')) and value and ref.get('end',0)>ref.get('start',0) and ref['end']<len(target.get('expected_text','')):
                inserted=value+' '
            if method == "prepend_before_unit" and value:
                inserted += " "
                paragraph = cell if container_kind == 'paragraph' else next((p for p in cell.paragraphs if p.text.strip()), None)
                if paragraph is None:
                    warnings.append(f"{item['question']}: 单位所在段落不存在，已跳过")
                    continue
                current_visible = paragraph.text
                leading = len(current_visible) - len(current_visible.lstrip())
                if not _replace_paragraph_range(paragraph, 0, leading, inserted):
                    warnings.append(f"{item['question']}: 单位前的填写片段无法定位，已跳过")
                    continue
                updates.append({"field_id":item['field_id'],"target_id":target.get('target_id'),"mode":method,"answer_source":item.get('answer_source')})
                continue
            if not _replace_container_range(cell, container_kind, start, end, inserted):
                warnings.append(f"{item['question']}: 无法把原文范围定位到 Word 文字运行，已跳过")
                continue
            if target.get('preserve_option_label'):
                from .validation.printed_choice_layout import suppress_duplicate_choice_bullet
                change=suppress_duplicate_choice_bullet(document,cell,container_kind,target,source_blocks.get(target.get('block_id'),{}))
                if change:layout_adjustments.append(change)
        elif method == 'set_symbol_checkbox':
            control = target['control']
            root = cell._p if container_kind == 'paragraph' else cell._tc
            paragraphs = [root] if container_kind == 'paragraph' else root.xpath('./w:p')
            pi = 0 if container_kind == 'paragraph' else control.get('paragraph_index', 0) - 1
            if not 0 <= pi < len(paragraphs):
                warnings.append(f"{item['question']}: 符号控件段落不存在"); continue
            symbols = paragraphs[pi].xpath('.//w:sym')
            si = control.get('symbol_index', 0) - 1
            if not 0 <= si < len(symbols):
                warnings.append(f"{item['question']}: 符号控件位置不存在"); continue
            symbol = symbols[si]
            # ST_ShortHexNumber is case-insensitive; LibreOffice-converted
            # legacy DOC uses lowercase hex while native extraction canonicalizes it.
            if symbol.get(qn('w:font')) != control.get('symbol_font') or str(symbol.get(qn('w:char')) or '').upper() != str(control.get('symbol_char') or '').upper():
                warnings.append(f"{item['question']}: 符号控件已变化，已跳过"); continue
            symbol.set(qn('w:font'), 'Segoe UI Symbol')
            symbol.set(qn('w:char'), '2611' if value in {'☒', '☑'} else '2610')
        elif method == 'set_legacy_text':
            from .parser_core.legacy_fields import set_legacy_text
            control=target['control']; root=cell._p if container_kind=='paragraph' else cell._tc
            paragraphs=[root] if container_kind=='paragraph' else root.xpath('./w:p')
            pi=0 if container_kind=='paragraph' else control.get('paragraph_index',0)-1
            nodes=paragraphs[pi].xpath('.//w:fldChar[w:ffData]') if 0<=pi<len(paragraphs) else []
            ci=control.get('control_index',0)-1
            if not 0<=ci<len(nodes) or not set_legacy_text(nodes[ci],value,control):
                raise TemplateFillError(f"{item['question']}: 旧式文字域状态、格式要求、长度限制或位置不一致，未生成不完整回填文件；请重新保存符合原控件要求的值或使用标准 Word 导出")
        elif method == 'set_legacy_checkbox':
            control=target['control']; root=cell._p if container_kind=='paragraph' else cell._tc
            paragraphs=[root] if container_kind=='paragraph' else root.xpath('./w:p')
            pi=control.get('paragraph_index',0)-1
            if not 0<=pi<len(paragraphs):
                warnings.append(f"{item['question']}: 旧式控件段落不存在"); continue
            controls=paragraphs[pi].xpath('.//w:fldChar[w:ffData]')
            ci=control.get('control_index',0)-1
            if not 0<=ci<len(controls):
                warnings.append(f"{item['question']}: 旧式控件位置不存在"); continue
            cb=controls[ci].find('.//'+qn('w:checkBox'))
            if cb is None:
                warnings.append(f"{item['question']}: 目标不是复选框");continue
            checked=cb.find(qn('w:checked'))
            if checked is None:
                from docx.oxml import OxmlElement
                checked=OxmlElement('w:checked'); cb.append(checked)
            checked.set(qn('w:val'),'1' if value in {'☒','☑'} else '0')
        elif method.startswith("set_"):
            control = target.get("control") or {}
            control_id = str(control.get("control_id") or "")
            root = cell._p if container_kind == "paragraph" else cell._tc
            candidates = root.xpath(".//w:sdt")
            def content_control_id(node):
                props = node.find(qn("w:sdtPr"))
                identifier = props.find(qn("w:id")) if props is not None else None
                return identifier.get(qn("w:val"), "") if identifier is not None else ""
            sdt = next((node for node in candidates if control_id and content_control_id(node) == control_id), candidates[0] if not control_id and len(candidates) == 1 else None)
            if sdt is None:
                warnings.append(f"{item['question']}: 内容控件已变化，已跳过")
                continue
            props = sdt.find(qn("w:sdtPr"))
            if props is None or props.find(qn("w:dataBinding")) is not None or props.find(qn("w:lock")) is not None:
                warnings.append(f"{item['question']}: 内容控件存在绑定或锁定限制，已跳过")
                continue
            if method == "set_checkbox":
                selected = value.strip().lower() in {"1", "true", "yes", "是", "选中", "☑", "☒"}
                checked = sdt.findall(".//{http://schemas.microsoft.com/office/word/2010/wordml}checked")
                if checked: checked[0].set("{http://schemas.microsoft.com/office/word/2010/wordml}val", "1" if selected else "0")
                texts = sdt.findall(".//" + qn("w:t"))
                if texts: texts[0].text = "☑" if selected else "☐"
            else:
                if method == "set_dropdown":
                    options = props.findall(".//" + qn("w:listItem"))
                    allowed = {node.get(qn("w:value")) for node in options} | {node.get(qn("w:displayText")) for node in options}
                    if value not in allowed:
                        warnings.append(f"{item['question']}: 值不属于原控件可选项，已跳过")
                        continue
                if method == "set_date_control":
                    # Display text alone would leave stale fullDate metadata.
                    from datetime import date
                    try: canonical_date = date.fromisoformat(value).isoformat()
                    except ValueError:
                        warnings.append(f"{item['question']}: 日期控件仅接受明确的 YYYY-MM-DD 值，已跳过")
                        continue
                    date_node = props.find(qn("w:date"))
                    if date_node is not None: date_node.set(qn("w:fullDate"), canonical_date + "T00:00:00Z")
                texts = sdt.findall(".//" + qn("w:t"))
                if texts:
                    texts[0].text = value
                    for node in texts[1:]: node.text = ""
                else:
                    warnings.append(f"{item['question']}: 内容控件没有可写文字节点")
                    continue
            for placeholder in list(props.findall(qn("w:showingPlcHdr"))): props.remove(placeholder)
        updates.append({"field_id": item["field_id"], "target_id": target.get("target_id"), "mode": method, "answer_source": item.get("answer_source")})
    from .validation.rendered_lane_export import preserve_rendered_answer_bands
    changed_bands={item['target']['block_id'] for item, _, _, value in executable
        if any(u['target_id']==item['target'].get('target_id') for u in updates)
        and value.strip()!=str(item['target'].get('expected_paragraph_text') or item['target'].get('expected_text') or '').strip()}
    if permission_guard is None:
        layout_adjustments.extend(preserve_rendered_answer_bands(document, source, result, changed_bands))
        layout_adjustments.extend(_stabilize_empty_signature_frames(document))
    else:
        permission_guard.verify()
    destination.parent.mkdir(parents=True, exist_ok=True)
    _save_template_body(source,document,destination)
    Document(destination)
    metadata={'schema_version':'template-export-v3-body-part-whitelist','source_sha256':actual_hash,'export_sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),'layout_adjustments':layout_adjustments,'package_write_parts':['word/document.xml'],'other_package_parts_preserved_exactly':True}
    if permission_guard is not None:
        metadata['permission_verification'] = {**permission_guard.evidence(),
            'protected_xml_unchanged': True, 'settings_unchanged': True,
            'target_preflight': permission_preflight}
    destination.with_suffix('.metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf8')
    return updates, warnings


def _native_choice_companion_notes(snapshot: dict) -> tuple[dict, list]:
    """Move only exact source-backed native choice companions into their row.

    This changes standard Word presentation, never saved questions, option
    identities, conditional values or original-template write targets.
    Ambiguous/edited notes stay visible in the ordinary document flow.
    """
    owners = {}
    for question in snapshot.get('questions', []):
        definition = question.get('form_definition') or {}
        targets = definition.get('fill_targets') or []
        if not question.get('options') or len(targets) != len(question['options']) or not targets or definition.get('validation_status') != 'valid':
            continue
        sources = {}
        for target in targets:
            control = target.get('control') or {}
            evidence = control.get('mark_evidence') or {}
            cell = evidence.get('cell_evidence') or {}
            refs = control.get('caption_refs') or []
            if (control.get('source') != 'native_pdf_vector' or not evidence.get('caption_after_outline')
                    or target.get('location', {}).get('type') != 'pdf' or not target.get('writable')
                    or len(str(target.get('source_hash') or '')) != 64
                    or cell.get('source_hash') != target.get('source_hash') or target.get('source_hash') != definition.get('source_hash') or len(refs) != 1
                    or not isinstance(cell.get('source_text'), str)):
                sources = {}; break
            bid = refs[0].get('block_id')
            if bid in sources and sources[bid] != cell['source_text']:
                sources = {}; break
            sources[bid] = cell['source_text']
        if sources and question.get('field_id'):
            owners[question['field_id']] = (definition.get('instruction_refs') or [], sources)
    inline, remaining = {}, []
    for note in snapshot.get('document_content') or []:
        owner = owners.get(note.get('anchor_field_id'))
        rr = note.get('source_ref') or {}
        a, z = rr.get('start'), rr.get('end')
        text = owner[1].get(rr.get('block_id'), '') if owner else ''
        if (owner and note.get('role') == 'instruction' and not note.get('anchor_validation')
                and rr in owner[0] and type(a) is int and type(z) is int and 0 <= a < z <= len(text)
                and note.get('text') == text[a:z]):
            inline.setdefault(note['anchor_field_id'], []).append(note)
        else:
            remaining.append(note)
    return inline, remaining


def standard_questionnaire_docx(snapshot: dict, destination: str | Path) -> None:
    destination = Path(destination)
    task, questions = snapshot["task"], snapshot["questions"]
    document = Document()
    section = document.sections[0]
    section.top_margin = section.bottom_margin = Cm(2)
    section.left_margin = section.right_margin = Cm(2.2)
    section.page_width=Cm(21);section.page_height=Cm(29.7)
    styles = document.styles
    styles["Normal"].font.name = "Arial"
    styles["Normal"]._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    styles["Normal"].font.size = Pt(10.5)
    title = document.add_heading(task.get("questionnaire_name") or "问卷", 0)
    title.alignment = 1
    metadata = [("客户", task.get("customer_name")), ("产品", task.get("product")), ("业务范围", task.get("business_scope")), ("备注", task.get("notes"))]
    meta_table = document.add_table(rows=0, cols=2)
    meta_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    meta_table.style = "Table Grid"
    for label, value in metadata:
        if value:
            cells = meta_table.add_row().cells; cells[0].text = label; cells[1].text = str(value)
    by_parent = {q["id"]: q for q in questions}
    children={}
    for q in questions:children.setdefault(q.get('parent_id'),[]).append(q)
    consumed=set()
    last_section = None
    number = 0
    table = None
    companion_notes, ordinary_notes = _native_choice_companion_notes(snapshot)
    source_notes=sorted(ordinary_notes,key=lambda block:(block['order'],block['block_id']))
    note_index=0
    def emit_source_notes(before_order=float('inf')):
        nonlocal note_index,table,last_section
        while note_index<len(source_notes) and source_notes[note_index]['order']<=before_order:
            note=source_notes[note_index]
            if note.get('section') and note['section']!=last_section:
                document.add_heading(note['section'],level=2).paragraph_format.keep_with_next=True
                last_section=note['section']
            if note['role'] == 'readonly_table':
                rows = note.get('rows') or []
                width = len(rows[0]) if rows else 0
                if not width or any(len(row) != width for row in rows):
                    raise TemplateFillError('只读原文表格结构无效，停止标准导出')
                separate_tables()
                original_grid = document.add_table(rows=len(rows), cols=width)
                original_grid.style = 'Table Grid'
                original_grid.alignment = WD_TABLE_ALIGNMENT.CENTER
                from docx.oxml import OxmlElement
                original_grid.rows[0]._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
                for original_row, source_row in zip(original_grid.rows, rows):
                    keep_response_row(original_row)
                    for cell, source_cell in zip(original_row.cells, source_row):
                        cell.text = source_cell.get('text') or ''
                        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                separate_tables()
            elif note['role'] in {'group_context','section_header'}:
                paragraph=document.add_heading(note['text'],level=2)
                paragraph.paragraph_format.keep_with_next=True
            else:
                paragraph=document.add_paragraph(note['text'])
                if note.get('location',{}).get('type')=='pdf':paragraph.paragraph_format.keep_together=True
                if len(note['text'])<140:paragraph.paragraph_format.keep_with_next=True
            table=None
            note_index+=1
    def separate_tables():
        # Adjacent w:tbl elements can be joined by Word/LibreOffice, causing
        # the preceding record table's header to repeat over unrelated QA.
        p=document.add_paragraph();p.paragraph_format.space_after=Pt(0)
        p.paragraph_format.space_before=Pt(0);p.paragraph_format.line_spacing=Pt(2)
        p.paragraph_format.keep_with_next=True
    def keep_response_row(row):
        # Keep one short question and its Yes/No/NA group together across
        # pages. Word still paginates a row longer than the printable page.
        from docx.oxml import OxmlElement
        row._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
    def answer_text(q):
        options=q.get('options') or []
        if options:
            # A model option array is not evidence of a native response group.
            # Keep malformed/unaligned options in the saved source diagnostics;
            # do not invent empty checkboxes or bind neighbouring prose to this
            # question in a customer document. Explicit manual text still wins.
            definition=q.get('form_definition') or {}
            valid=definition.get('validation_status')=='valid' and all(str(o.get('text') or '').strip() for o in options)
            targets=definition.get('fill_targets')
            if targets is not None:
                valid=valid and len(targets)==len(options) and all(t.get('writable') is True for t in targets)
            if valid:
                from .choice_identity import selected_indices
                chosen=selected_indices(options,q.get('selected_options') or [])
                return '\n'.join(('☒ ' if i in chosen else '☐ ')+o['text'] for i,o in enumerate(options))
            if not q.get('answer_text') and q.get('manual_override') and q.get('selected_options'):
                from .choice_identity import selected_labels
                return '\n'.join(selected_labels(options,q['selected_options']))
        return q.get('answer_text') or ''
    for question in questions:
        if question['id'] in consumed:continue
        emit_source_notes(question.get('source_order',question.get('ordinal',0)))
        definition=question.get('form_definition') or {}
        columns=definition.get('record_columns') or []
        records=children.get(question['id'],[])
        if columns and records and all((q.get('form_definition') or {}).get('record_index') is not None for q in records):
            # A record group has the same source scope as other questions.
            # Its early table-rendering branch must not omit that scope.
            record_section = question.get('section') or '未分章节'
            if record_section != last_section:
                document.add_heading(record_section, level=1).paragraph_format.keep_with_next=True
                last_section = record_section
                table = None
            separate_tables()
            document.add_heading(question['question_text'],level=2)
            grid=document.add_table(rows=1,cols=len(columns)+1);grid.style='Table Grid'
            grid.rows[0].cells[0].text='记录 / Record'
            for i,col in enumerate(columns,1):grid.rows[0].cells[i].text=col['label']
            from docx.oxml import OxmlElement
            repeat=OxmlElement('w:tblHeader');grid.rows[0]._tr.get_or_add_trPr().append(repeat)
            row_groups={}
            for q in records:row_groups.setdefault(q['form_definition']['record_index'],[]).append(q)
            for index,items in sorted(row_groups.items()):
                row=grid.add_row();keep_response_row(row);cells=row.cells
                for context in definition.get('record_context_values', []):
                    column = context.get('column_index', 0)
                    if context.get('record_index') == index and 0 < column < len(cells):
                        cells[column].text = context.get('text', '')
                cells[0].text=items[0]['form_definition'].get('record_context') or str(index)
                for q in items:
                    column=q['form_definition']['record_column_index']
                    if not isinstance(column,int) or not 1<=column<=len(columns):raise TemplateFillError('记录表列定位无效，停止标准导出')
                    cells[column].text=answer_text(q)
                    consumed.add(q['id'])
            for row in grid.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        for run in paragraph.runs:run.font.size=Pt(8 if len(columns)>5 else 10)
            table=None
            continue
        section_text = question.get("section") or "未分章节"
        if section_text != last_section:
            document.add_heading(section_text, level=1)
            table=None
        if table is None:
            separate_tables()
            table = document.add_table(rows=1, cols=4); table.style = "Table Grid"; table.alignment = WD_TABLE_ALIGNMENT.CENTER
            table.autofit = False
            for column,width in zip(table.columns,(Cm(1.2),Cm(7.4),Cm(5.4),Cm(2.6))):column.width=width
            headers = table.rows[0].cells
            from docx.oxml import OxmlElement
            repeat=OxmlElement('w:tblHeader');table.rows[0]._tr.get_or_add_trPr().append(repeat)
            for cell, text in zip(headers, ("序号", "问题", "答案", "备注")):
                cell.text = text; cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            for cell, width in zip(headers, (Cm(1.2), Cm(7.4), Cm(5.4), Cm(2.6))):
                cell.width = width
            last_section = section_text
        number += 1
        row=table.add_row();keep_response_row(row);cells=row.cells
        parent = by_parent.get(question.get("parent_id"))
        label = question["question_text"]
        unit=definition.get('unit_text_original')
        if unit and unit not in label:label += f' [{unit}]'
        context_parts=[definition.get('display_context'),definition.get('record_context')]
        context='\n'.join(dict.fromkeys(str(part).strip() for part in context_parts if part and str(part).strip()))
        # Independent short fields otherwise become indistinguishable once
        # the original parallel signing columns are rendered as standard QA.
        # This is source text, not an inferred party name or a translation.
        if (not parent or parent['id'] in consumed) and context and len(question['question_text'])<=80 and len(question['question_text'].split())<=6 and context.casefold() not in label.casefold():
            label=context+'\n'+label
        if parent:
            # The full parent prompt is already printed immediately above.
            # Keep child labels readable instead of duplicating it per row.
            label = "  · " + label
        cells[0].text = str(number); cells[1].text = label
        if companions := companion_notes.get(question.get('field_id')):
            heading = cells[1].add_paragraph('原文说明 / Original wording')
            heading.paragraph_format.space_before = Pt(5)
            heading.paragraph_format.space_after = Pt(2)
            heading.runs[0].bold = True
            for note in companions:
                paragraph = cells[1].add_paragraph(note['text'])
                paragraph.paragraph_format.space_before = Pt(0)
                paragraph.paragraph_format.space_after = Pt(2)
        cells[2].text = answer_text(question)
        cells[3].text = question.get("notes") or ""
        for cell, width in zip(cells, (Cm(1.2), Cm(7.4), Cm(5.4), Cm(2.6))):
            cell.width = width
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    emit_source_notes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    document.save(destination)
    Document(destination)
