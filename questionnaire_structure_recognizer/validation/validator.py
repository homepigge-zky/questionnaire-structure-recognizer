from __future__ import annotations

import re
from collections import defaultdict
from copy import deepcopy
from typing import Any

from ..form_positions import FORM_POSITION_VERSION, build_form_definition
from .field_boundaries import restore_physical_fields, placeholder, UNITS
from .native_choices import normalize_choice_fields
from .selection_evidence import checked_matrix_region, selected_column_caption
from .pdf_source_relations import native_overlay_value_in_region, ocr_value_in_region


SCHEMA_VERSION = "questionnaire-structure-v8-closed-local-grids"
ROLES = {
    "title", "section", "question", "answer", "option", "instruction", "table_header",
    "note", "attachment_requirement", "signature_field", "non_qa", "unresolved",
}
ANSWER_STATUSES = {"filled", "blank", "not_present", "not_located", "unresolved", "unreadable"}


def _valid_ref(ref, lookup):
    block = lookup.get(ref.get("block_id"))
    start, end = ref.get("start"), ref.get("end")
    if block is None or not isinstance(start, int) or not isinstance(end, int):
        return False
    length = len(block.get("text", ""))
    if not 0 <= start <= end <= length: return False
    if start < end or length == 0: return True
    slots = block.get("structure", {}).get("logical_slots", [])
    return any(slot.get("slot_id") == ref.get("slot_id") and slot.get("offset") == start for slot in slots)


def _text(refs, lookup):
    values = []
    for ref in sorted(refs, key=lambda r: (lookup[r["block_id"]]["order"], r["start"])):
        value = lookup[ref["block_id"]].get("text", "")[ref["start"]:ref["end"]]
        if value:
            values.append(value)
    return "\n".join(values).strip()


def _locations(refs, lookup):
    return [deepcopy(lookup[r["block_id"]].get("location", {})) for r in refs]


def _numbered_observation_sections(blocks):
    """Recover printed local headings inside an observation form, not fields.

    The explicit Description/Observations grid, a short numbered caption and
    its empty neighbour jointly establish the scope. An arbitrary Roman
    prefix in a record value or question is not sufficient evidence.
    """
    tables = defaultdict(lambda: defaultdict(list))
    for block in blocks:
        s = block.get('structure', {})
        if s.get('table_id') and not s.get('is_merge_continuation'):
            tables[s['table_id']][s.get('row_start', s.get('row', 0))].append(block)
    headings = {}
    for table_id, rows in tables.items():
        def text(b): return b.get('text', '').strip()
        header_rows = [row for row in rows.values()
            if any(re.fullmatch(r'description|question|描述|题目', text(b), re.I) for b in row)
            and any(re.fullmatch(r'observations?|remarks?|comments?|观察|备注|答复', text(b), re.I) for b in row)]
        if not header_rows:
            continue
        selected = []
        for row_no, row in sorted(rows.items()):
            row = sorted(row, key=lambda b: b.get('structure', {}).get('grid_col_start', 0))
            first = row[0]; value = text(first)
            if (not 0 < len(value) <= 100 or any(text(b) or b.get('controls') for b in row[1:])
                    or first.get('controls') or re.search(r'[?？_]|\b(?:please|provide|describe|specify|explain)\b', value, re.I)):
                continue
            if not re.fullmatch(r'(?:[IVXLCDM]+[.)]?\s+[A-Z][A-Z\s/&(),-]*|第[一二三四五六七八九十]+[章节]\s*[^:：?？]+)', value):
                continue
            if not any(n > row_no for n in rows):
                continue
            first.setdefault('structure', {}).update(resolved_layout_role='section_header',
                local_section_evidence='numbered_caption_in_description_observation_grid')
            selected.append(first)
        if selected:
            headings[table_id] = selected
    return headings


def _placeholder_value(ref, lookup):
    block = lookup.get(ref.get("block_id"), {})
    value = block.get("text", "")[ref.get("start", 0):ref.get("end", 0)].strip()
    return placeholder(block, value)


def _cross_section(question_refs, answer_refs, lookup):
    qparts = {(lookup[r["block_id"]].get("location", {}).get("part"), lookup[r["block_id"]].get("location", {}).get("page"), lookup[r["block_id"]].get("structure", {}).get("table_id")) for r in question_refs}
    aparts = {(lookup[r["block_id"]].get("location", {}).get("part"), lookup[r["block_id"]].get("location", {}).get("page"), lookup[r["block_id"]].get("structure", {}).get("table_id")) for r in answer_refs}
    return bool(qparts and aparts and not any(q[0] == a[0] and (q[2] == a[2] or q[1] == a[1]) for q in qparts for a in aparts))


def _answer_status(answer_refs, option_refs, unresolved_refs, lookup):
    if unresolved_refs:
        return "unresolved"
    if answer_refs:
        if any(lookup[r["block_id"]]["kind"] in {"image", "unsupported"} for r in answer_refs):
            return "unreadable"
        if all(_placeholder_value(ref, lookup) for ref in answer_refs):
            return "blank"
        return "filled" if _text(answer_refs, lookup).strip() else "blank"
    for ref in option_refs:
        block = lookup[ref["block_id"]]
        if any(bool(c.get("selected")) for c in block.get("controls", [])):
            return "filled"
        text = block.get("text", "")[ref["start"]:ref["end"]]
        if re.search(r"[☑☒✓✔]", text):
            return "filled"
    if option_refs and all(
        re.search(r"[☐□]", lookup[ref["block_id"]].get("text", "")[ref["start"]:ref["end"]])
        or any(c.get("selected") is False for c in lookup[ref["block_id"]].get("controls", []))
        for ref in option_refs
    ):
        return "blank"
    return "not_present"


def _repair_table_relations(assignments, source_blocks, lookup):
    """Use native row/column structure to reject incoherent model links."""
    tables = defaultdict(list)
    for block in source_blocks:
        table_id = block.get("structure", {}).get("table_id")
        if table_id:
            tables[table_id].append(block)
    for table_id, blocks in tables.items():
        assignments_by_block = defaultdict(list)
        for assignment in assignments:
            assignments_by_block[assignment["ref"]["block_id"]].append(assignment)

        def force_native(block, role, group_id, reason):
            existing = assignments_by_block.get(block["block_id"], [])
            if existing:
                for assignment in existing:
                    assignment.update(
                        role=role, group_id=group_id, parent_group_id="",
                        relation_status="confirmed", reason=reason,
                        layout_role="field_label" if role == "question" else "answer_region",
                    )
            else:
                ref = {"block_id": block["block_id"], "start": 0, "end": len(block.get("text", "")), "slot_id": ""}
                assignment = {"role": role, "ref": ref, "group_id": group_id, "parent_group_id": "", "section_group_id": "", "relation_status": "confirmed", "reason": reason, "layout_role": "field_label" if role == "question" else "answer_region"}
                assignments.append(assignment)
                assignments_by_block[block["block_id"]].append(assignment)

        # A native row marked as horizontal key/value geometry contains
        # independent local pairs. This repairs a model relation that skipped
        # an empty response and attached the next label as the prior value.
        physical_rows = defaultdict(list)
        for block in blocks:
            if not block.get("structure", {}).get("is_merge_continuation"):
                physical_rows[block.get("structure", {}).get("row")].append(block)
        for row, row_blocks in physical_rows.items():
            ordered = sorted(row_blocks, key=lambda b: b.get("structure", {}).get("column_start", 0))
            if not ordered or ordered[0].get("structure", {}).get("row_layout_hint") != "horizontal_key_value_candidate":
                continue
            for pair_index in range(0, len(ordered), 2):
                label, response = ordered[pair_index:pair_index + 2]
                group_id = f"native:{table_id}:row:{row}:label:{label['structure'].get('cell_id')}"
                force_native(label, "question", group_id, "横向多组表单按物理标签边界建立独立字段")
                force_native(response, "answer", group_id, "横向多组表单按相邻物理答复区域配对")

        # Local choice matrices and record areas take precedence over the
        # table-wide label/response hint. Their blank cells are meaningful
        # option/record positions, while their populated header cells are not
        # answers or standalone questions.
        for row, row_blocks in physical_rows.items():
            ordered = sorted(row_blocks, key=lambda b: b.get("structure", {}).get("column_start", 0))
            if not ordered:
                continue
            layout = ordered[0].get("structure", {}).get("local_layout_type")
            if layout == "choice_matrix_header":
                force_native(ordered[0], "section", "", "选择矩阵的左上标题作为局部章节")
                for header in ordered[1:]:
                    force_native(header, "table_header", "", "选择矩阵选项列标题")
            elif layout == "choice_matrix":
                group_id = f"native:{table_id}:choice-row:{row}"
                force_native(ordered[0], "question", group_id, "选择矩阵按物理行确认问题")
                for response in ordered[1:]:
                    force_native(response, "answer", group_id, "选择矩阵保留空白选项位置")
            elif layout == "record_table_header":
                for header in ordered:
                    force_native(header, "table_header", "", "记录表列标题")
            elif layout == "record_table":
                for region in ordered:
                    force_native(region, "non_qa", "", "记录表单元格由记录字段层关联")

        rows = [b.get("structure", {}).get("row", 0) for b in blocks]
        if not rows:
            continue
        header_row = min(rows)
        headers = {
            b.get("structure", {}).get("column"): b.get("text", "").strip().lower()
            for b in blocks if b.get("structure", {}).get("row") == header_row
        }
        question_header_columns = {
            column for column, text in headers.items()
            if any(token in text for token in ("question", "问题", "题目", "field"))
        }
        answer_header_columns = {
            column for column, text in headers.items()
            if any(token in text for token in ("response", "answer", "回答", "答复"))
        }
        # An explicit Question/Answer header is stronger evidence than a model
        # relationship. Enforce row-local groups so a header or a later field
        # cannot be merged into another row's question and answer.
        table_block_ids = {block["block_id"] for block in blocks}
        has_model_question_evidence = any(
            assignment["role"] == "question" and assignment["ref"]["block_id"] in table_block_ids
            for assignment in assignments
        )
        if question_header_columns and answer_header_columns and has_model_question_evidence:
            assignments_by_block = defaultdict(list)
            for assignment in assignments:
                assignments_by_block[assignment["ref"]["block_id"]].append(assignment)

            def force(block, role, group_id, reason):
                refs = assignments_by_block.get(block["block_id"], [])
                if refs:
                    for assignment in refs:
                        assignment.update(role=role, group_id=group_id, parent_group_id="", relation_status="confirmed", reason=reason)
                else:
                    ref = {"block_id": block["block_id"], "start": 0, "end": len(block.get("text", ""))}
                    assignment = {"role": role, "ref": ref, "group_id": group_id, "parent_group_id": "", "section_group_id": "", "relation_status": "confirmed", "reason": reason}
                    assignments.append(assignment); assignments_by_block[block["block_id"]].append(assignment)

            for block in blocks:
                structure = block.get("structure", {})
                row, column = structure.get("row"), structure.get("column")
                if row == header_row and column in question_header_columns | answer_header_columns:
                    force(block, "table_header", "", "原生问题/回答列标题")
                elif row != header_row and column in question_header_columns and block.get("text", "").strip():
                    force(block, "question", f"native:{table_id}:row:{row}", "原生问题列按物理行建立字段")
                elif row != header_row and column in answer_header_columns and structure.get("blank_state") != "structural_spacer":
                    force(block, "answer", f"native:{table_id}:row:{row}", "原生回答列按物理行关联")

        layout_hint = blocks[0].get("structure", {}).get("table_layout_hint")
        # In a label/response form each physical label row is an independent
        # field.  Model context may span many rows, but it may not merge those
        # rows under a reused semantic group id.  Empty response cells remain
        # answer regions and non-empty right cells become value candidates.
        if layout_hint == "label_response_form_candidate" and not (question_header_columns and answer_header_columns):
            assignments_by_block = defaultdict(list)
            for assignment in assignments:
                assignments_by_block[assignment["ref"]["block_id"]].append(assignment)

            def force_form_role(block, role, group_id, reason):
                existing = assignments_by_block.get(block["block_id"], [])
                if existing:
                    for assignment in existing:
                        assignment.update(role=role, group_id=group_id, parent_group_id="", relation_status="confirmed", reason=reason)
                else:
                    ref = {"block_id": block["block_id"], "start": 0, "end": len(block.get("text", "")), "slot_id": ""}
                    assignment = {"role": role, "ref": ref, "group_id": group_id, "parent_group_id": "", "section_group_id": "", "relation_status": "confirmed", "reason": reason, "layout_role": "field_label" if role == "question" else "answer_region"}
                    assignments.append(assignment); assignments_by_block[block["block_id"]].append(assignment)

            physical_rows = defaultdict(list)
            for block in blocks:
                if not block.get("structure", {}).get("is_merge_continuation"):
                    physical_rows[block.get("structure", {}).get("row")].append(block)
            for row, row_blocks in physical_rows.items():
                ordered = sorted(row_blocks, key=lambda b: b.get("structure", {}).get("column_start", 0))
                if ordered and ordered[0].get("structure", {}).get("row_layout_hint") == "horizontal_key_value_candidate":
                    # Already repaired above as independent local pairs.
                    continue
                if ordered and ordered[0].get("structure", {}).get("local_layout_type") in {"choice_matrix_header", "choice_matrix", "record_table_header", "record_table"}:
                    continue
                label = next((b for b in ordered if b.get("text", "").strip() and b.get("structure", {}).get("column_end", 0) < b.get("structure", {}).get("table_column_count", 0)), None)
                if not label:
                    continue
                group_id = f"native:{table_id}:cell:{label['structure'].get('cell_id')}"
                force_form_role(label, "question", group_id, "标签—答复表单按物理字段边界确认标签")
                label_end = label["structure"].get("column_end", 0)
                for response in ordered:
                    if response["structure"].get("column_start", 0) > label_end:
                        force_form_role(response, "answer", group_id, "标签—答复表单按同一物理行确认答复区域")
        by_row = defaultdict(list)
        for assignment in assignments:
            block = lookup[assignment["ref"]["block_id"]]
            if block.get("structure", {}).get("table_id") == table_id:
                by_row[block.get("structure", {}).get("row")].append(assignment)
        for row, row_assignments in by_row.items():
            if row == header_row:
                continue
            qgroups = list(dict.fromkeys(x["group_id"] for x in row_assignments if x["role"] == "question" and x["group_id"]))
            if len(qgroups) != 1:
                continue
            qgroup = qgroups[0]
            for assignment in row_assignments:
                block = lookup[assignment["ref"]["block_id"]]
                column = block.get("structure", {}).get("column")
                header = headers.get(column, "")
                value = block.get("text", "")[assignment["ref"]["start"]:assignment["ref"]["end"]]
                checkbox_list = len(re.findall(r"[☐□☑☒✓✔]", value)) >= 2
                # A list of checkbox choices remains options even when the
                # model called the whole response cell an answer. Only a
                # native checked mark/control can make it a filled response.
                if checkbox_list and assignment["role"] in {"answer", "unresolved"}:
                    assignment.update(role="option", group_id=qgroup, relation_status="confirmed", reason="复选框列表按原生符号校验为选项")
                if assignment["role"] == "question":
                    assignment["relation_status"] = "confirmed"
                    assignment["reason"] = "模型题目角色与原生表格行列一致"
                if any(token in header for token in ("response", "answer", "回答", "答复")):
                    if assignment["role"] in {"answer", "option", "unresolved"} and not checkbox_list:
                        assignment.update(role="answer", group_id=qgroup, relation_status="confirmed", reason="按原生回答列校验问答关联")
                    elif assignment["role"] == "option":
                        assignment.update(group_id=qgroup, relation_status="confirmed", reason="按原生回答列确认选项关系；选中状态只读来源符号或控件")
            # Preserve numbering and explicit empty response zones even if the model omitted them.
            assigned_ids = {x["ref"]["block_id"] for x in row_assignments}
            question_columns = [
                lookup[x["ref"]["block_id"]].get("structure", {}).get("column", 0)
                for x in row_assignments if x["role"] == "question" and x["group_id"] == qgroup
            ]
            layout_hint = blocks[0].get("structure", {}).get("table_layout_hint")
            for block in blocks:
                if block.get("structure", {}).get("row") != row:
                    continue
                column = block.get("structure", {}).get("column")
                header = headers.get(column, "")
                if block["block_id"] in assigned_ids:
                    continue
                if any(token in header for token in ("no.", "number", "序号", "编号")) and block.get("text", "").strip():
                    ref = {"block_id": block["block_id"], "start": 0, "end": len(block["text"])}
                    assignments.append({"role": "non_qa", "ref": ref, "group_id": "", "parent_group_id": qgroup, "section_group_id": "", "relation_status": "confirmed", "reason": "原生表格题号上下文"})
                elif any(token in header for token in ("response", "answer", "回答", "答复")) and not block.get("text", ""):
                    ref = {"block_id": block["block_id"], "start": 0, "end": 0}
                    assignments.append({"role": "answer", "ref": ref, "group_id": qgroup, "parent_group_id": "", "section_group_id": "", "relation_status": "confirmed", "reason": "原生回答列中的空白答复区域"})
                elif (layout_hint == "label_response_form_candidate" and question_columns
                      and column > max(question_columns) and not block.get("text", "")):
                    ref = {"block_id": block["block_id"], "start": 0, "end": 0}
                    assignments.append({"role": "answer", "ref": ref, "group_id": qgroup, "parent_group_id": "", "section_group_id": "", "relation_status": "confirmed", "reason": "原生标签—答复表格中的空白答复区域"})


def _apply_special_table_fields(questions, source_blocks, lookup):
    """Build option and repeating-record fields from native local layouts."""
    tables = defaultdict(list)
    for block in source_blocks:
        table_id = block.get("structure", {}).get("table_id")
        if block.get("kind") == "table_cell" and table_id and not block.get("structure", {}).get("is_merge_continuation") and block.get("structure", {}).get("blank_state") != 'structural_spacer':
            tables[table_id].append(block)

    def ref(block, empty=False):
        return {"block_id": block["block_id"], "start": 0, "end": 0 if empty else len(block.get("text", "")), "slot_id": ""}

    def first_label(field):
        refs = field.get("label_refs") or field.get("question_refs") or []
        return lookup.get(refs[0].get("block_id")) if refs else None

    for table_id, cells in tables.items():
        by_row = defaultdict(list)
        for cell in cells: by_row[cell["structure"].get("row_start")].append(cell)
        for row in by_row: by_row[row].sort(key=lambda x: x["structure"].get("grid_col_start", 0))
        special_rows = {row for row, items in by_row.items() if items and items[0]["structure"].get("local_layout_type") in {"choice_matrix_header", "choice_matrix", "record_table_header", "record_table"}}
        # Remove model/fallback fields whose labels are actually option/record
        # headers or blank record cells. They are rebuilt below with stable
        # physical identities.
        invalid_label_ids = {
            cell["block_id"] for row, items in by_row.items()
            if items and items[0]["structure"].get("local_layout_type") in {"choice_matrix_header", "record_table_header", "record_table"}
            for cell in items
        }
        questions[:] = [field for field in questions if not ((first_label(field) or {}).get("block_id") in invalid_label_ids)]

        header_rows = [row for row, items in by_row.items() if items and items[0]["structure"].get("local_layout_type") == "choice_matrix_header"]
        if header_rows or any(items and items[0]['structure'].get('choice_options') for items in by_row.values()):
            for row, items in by_row.items():
                if not items or items[0]["structure"].get("local_layout_type") != "choice_matrix": continue
                metadata=items[0]['structure']; option_definitions=metadata.get('choice_options',[])
                label=next((x for x in items if x['structure'].get('grid_col_start')==metadata.get('choice_label_column',items[0]['structure'].get('grid_col_start'))),items[0])
                if not option_definitions:
                    headers=by_row[min(header_rows)][1:]
                    option_definitions=[{'text':h.get('text',''),'column_start':c['structure'].get('grid_col_start'),'header_cell_id':h['structure'].get('cell_id')} for h,c in zip(headers,items[1:])]
                regions=[x for x in items if x['structure'].get('grid_col_start') in {o['column_start'] for o in option_definitions}]
                # Delete independent option labels proposed by the model; the
                # same physical row has one question plus bounded choices.
                option_ids={x['block_id'] for x in regions}
                questions[:]=[f for f in questions if (first_label(f) or {}).get('block_id') not in option_ids]
                field = next((x for x in questions if (first_label(x) or {}).get("block_id") == label["block_id"]), None)
                if field is None:
                    field_id = f"native:{table_id}:choice:{label['structure'].get('cell_id')}"
                    field = {"question_id": field_id, "field_id": field_id, "parent_question_id": None, "section_refs": [], "section_text_original": "", "question_refs": [ref(label)], "label_refs": [ref(label)], "question_text_original": label["text"], "normalized_label": "", "context_label": "", "children": [], "instruction_refs": [], "attachment_refs": [], "attachment_text_original": "", "model_hints": {}}
                    questions.append(field)
                answer_regions, answer_values, options = [], [], []
                for index, region in enumerate(regions):
                    definition=next(o for o in option_definitions if o['column_start']==region['structure'].get('grid_col_start'))
                    option=next((b for b in cells if b['structure'].get('cell_id')==definition.get('header_cell_id')),None)
                    region_ref = ref(region, empty=not bool(region.get("text")))
                    answer_regions.append(region_ref)
                    # A stand-alone U+221A is used as a handwritten tick in
                    # real completed Yes/No column forms. Its meaning comes
                    # from this verified physical option column, not from a
                    # global replacement of every mathematical root symbol.
                    selected = checked_matrix_region(region)
                    # The actual reusable value is the source column caption
                    # (e.g. No), not a context-free tick. Preserve both the
                    # caption reference and the distinct selected region.
                    if selected and option is not None: answer_values.append(ref(option))
                    options.append({"refs": [ref(option)] if option else [], "text_original": definition['text'], "selected": selected, "selection_source": "native_choice_column" if selected else "not_selected", "answer_region_refs": [region_ref]})
                field.update(answer_region_refs=answer_regions, answer_refs=answer_values, answer_value_refs=answer_values, answer_text_original=_text(answer_values, lookup), answer_status="filled" if any(x["selected"] for x in options) else "blank", options=options, review_status="model_proposed", review_reasons=[])

        record_headers = [row for row, items in by_row.items() if items and items[0]["structure"].get("local_layout_type") == "record_table_header"]
        for header_row in record_headers:
            headers = by_row[header_row]
            record_rows = [(row, items) for row, items in sorted(by_row.items()) if items and items[0]["structure"].get("local_layout_type") == "record_table" and items[0]["structure"].get("record_header_row") == header_row]
            if not record_rows: continue
            root = None
            previous_row=by_row.get(header_row-1,[])
            # A caption may occupy the full preceding row, or follow a
            # dedicated number cell. Do not borrow an earlier question across
            # intervening rows or treat a populated response as a caption.
            caption_row = previous_row
            if len(previous_row) == 2 and re.fullmatch(r"\d+(?:\.\d+)*[.)]?", previous_row[0].get('text','').strip()):
                caption_row = previous_row[1:]
            caption = caption_row[0] if len(caption_row) == 1 and previous_row[0]['structure'].get('grid_col_start') == headers[0]['structure'].get('grid_col_start') and caption_row[0]['structure'].get('grid_col_end') == headers[-1]['structure'].get('grid_col_end') and not caption_row[0].get('controls') else None
            if caption:root=next((x for x in questions if (first_label(x) or {}).get('block_id')==caption['block_id']),None)
            if root is None:
                field_id = f"native:{table_id}:records:{header_row}"
                root = {"question_id": field_id, "field_id": field_id, "parent_question_id": None, "section_refs": [], "section_text_original": "", "question_refs": [ref(headers[0])], "label_refs": [ref(headers[0])], "question_text_original": "记录表", "normalized_label": "", "context_label": "", "instruction_refs": [], "attachment_refs": [], "attachment_text_original": "", "options": [], "model_hints": {}}
                root['question_text_original']=headers[0]['text']
                if caption:root.update(label_refs=[ref(caption)],question_refs=[ref(caption)],question_text_original=caption['text'])
                questions.append(root)
            root["record_columns"] = [{"label": x.get("text", ""), "label_ref": ref(x), "grid_col_start": x["structure"].get("grid_col_start"), "grid_col_end": x["structure"].get("grid_col_end")} for x in headers]
            root['boundary_evidence'] = 'native_record_table'
            root["answer_region_refs"] = []; root["answer_value_refs"] = []; root["answer_refs"] = []; root["answer_text_original"] = ""; root["answer_status"] = "blank"; root["review_status"] = "model_proposed"; root["review_reasons"] = []
            root['record_context_values'] = []
            children = []
            for record_index, (row, regions) in enumerate(record_rows, 1):
                row_active = any(not placeholder(region,region.get('text','')) or any(c.get('selected') is True for c in region.get('controls',[])) for region in regions)
                for column_index, region in enumerate(regions):
                    if column_index >= len(headers): continue
                    if region.get('structure', {}).get('fixed_record_context'):
                        root['record_context_values'].append({'record_index': record_index, 'column_index': column_index + 1,
                            'text': region.get('text', ''), 'refs': [ref(region)]})
                        continue
                    header = headers[column_index]; value_refs = [ref(region)] if region.get("text", "").strip() and not region.get('structure', {}).get('is_placeholder') else []
                    field_id = f"native:{table_id}:record:{record_index}:column:{column_index+1}"
                    children.append({"question_id": field_id, "field_id": field_id, "parent_question_id": root["field_id"], "section_refs": root.get("section_refs", []), "section_text_original": root.get("section_text_original", ""), "question_refs": [ref(header)], "label_refs": [ref(header)], "question_text_original": header.get("text", ""), "normalized_label": "", "context_label": f"{root.get('question_text_original','')} · 第{record_index}条记录", "answer_region_refs": [ref(region, empty=not bool(region.get("text")))], "answer_refs": value_refs, "answer_value_refs": value_refs, "answer_text_original": _text(value_refs, lookup), "answer_status": "filled" if value_refs else "blank", "unit_refs": [], "unit_text_original": "", "children": [], "options": [], "instruction_refs": [], "attachment_refs": [], "attachment_text_original": "", "review_status": "model_proposed", "review_reasons": [], "record_index": record_index, "record_column_index": column_index + 1, "record_slot_status": "active" if row_active else "reserved", "model_hints": {}})
            root["children"] = children

        # Some Word forms put every textual option in one populated response
        # cell (for example ``□ Yes  □ No  □ N/A``).  The model can identify
        # it as an option block, but a populated cell is not a text answer and
        # must not be discarded as a fill region.  Split only the physical
        # checkbox glyph ranges; the remaining option wording stays protected.
        option_block_ids = set()
        for field in questions:
            label = first_label(field)
            if not label or label.get("structure", {}).get("table_id") != table_id:
                continue
            label_structure = label.get("structure", {})
            candidates = []
            source_refs = []
            for option in field.get("options") or []:
                source_refs.extend(option.get("refs") or [])
            source_refs.extend(field.get("answer_value_refs") or field.get("answer_refs") or [])
            for option_ref in source_refs:
                    block = lookup.get(option_ref.get("block_id"))
                    structure = (block or {}).get("structure", {})
                    if (
                        block and block.get("kind") == "table_cell"
                        and structure.get("table_id") == table_id
                        and structure.get("row_start") == label_structure.get("row_start")
                        and structure.get("grid_col_start", 0) > label_structure.get("grid_col_end", 0)
                    ):
                        candidates.append(block)
            fragments = []
            for block in {item["block_id"]: item for item in candidates}.values():
                text = str(block.get("text") or "")
                from .native_choices import _glyph_options
                native_options=_glyph_options(block,0,len(text))
                if native_options:
                    fragments.extend(native_options)
                    continue
                # Some legacy Symbol-font checkboxes are converted to the CJK
                # glyph “口”.  Treat it as a box only at the start of a line
                # immediately before a numbered list item; ordinary uses of
                # the character remain text.
                markers = list(re.finditer(r"[☐☑☒□✓✔]|^口(?=\s*\d+[.)])", text, re.M))
                if not markers:
                    continue
                option_block_ids.add(block["block_id"])
                cell_id = block.get("structure", {}).get("cell_id") or block["block_id"]
                for index, marker in enumerate(markers):
                    end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
                    label_start, label_end = marker.end(), end
                    while label_start < label_end and text[label_start].isspace(): label_start += 1
                    while label_end > label_start and text[label_end - 1].isspace(): label_end -= 1
                    option_text = text[label_start:label_end].strip("；;，,")
                    if not option_text:
                        option_text = f"选项{len(fragments) + 1}"
                    selected = marker.group(0) in "☑☒✓✔"
                    region_ref = {
                        "block_id": block["block_id"], "start": marker.start(), "end": marker.end(),
                        "slot_id": f"{cell_id}:choice:{len(fragments) + 1}",
                    }
                    value_ref = {"block_id": block["block_id"], "start": label_start, "end": label_end, "slot_id": ""}
                    fragments.append({
                        "refs": [value_ref], "text_original": option_text, "selected": selected,
                        "selection_source": "native_symbol" if selected else "not_selected",
                        "answer_region_refs": [region_ref],
                    })
            if fragments:
                field["options"] = fragments
                field["answer_region_refs"] = [item["answer_region_refs"][0] for item in fragments]
                selected_refs = [item["refs"][0] for item in fragments if item["selected"]]
                field["answer_value_refs"] = selected_refs
                field["answer_refs"] = selected_refs
                field["answer_text_original"] = _text(selected_refs, lookup)
                field["answer_status"] = "filled" if selected_refs else "blank"
                field["review_status"] = "model_proposed"
                # Keep write-in positions inside the same populated option
                # cell as conditional child fields.  The parent owns the
                # checkboxes; each child owns one exact zero-width insertion
                # point such as Cause, contact person or audit date.  This
                # prevents a whole-cell answer from overwriting fixed labels.
                prompt_children = []
                parent_id = field.get("field_id") or field.get("question_id")
                for block in {item["block_id"]: item for item in candidates}.values():
                    for slot in {s['slot_id']:s for s in block.get("structure", {}).get("logical_slots", [])}.values():
                        if slot.get("kind") != "inline_prompt_candidate":
                            continue
                        start, end = int(slot.get("label_start", 0)), int(slot.get("label_end", 0))
                        if not 0 <= start < end <= len(block.get("text", "")):
                            continue
                        label_ref = {"block_id": block["block_id"], "start": start, "end": end, "slot_id": ""}
                        region_ref = {
                            "block_id": block["block_id"], "start": int(slot.get("start",slot.get("offset", 0))),
                            "end": int(slot.get("end",slot.get("offset", 0))), "slot_id": slot["slot_id"],
                        }
                        child_id = f"{parent_id}:prompt:{len(prompt_children) + 1}"
                        prompt_children.append({
                            "question_id": child_id, "field_id": child_id, "parent_question_id": parent_id,
                            "section_refs": deepcopy(field.get("section_refs") or []),
                            "section_text_original": field.get("section_text_original", ""),
                            "question_refs": [label_ref], "label_refs": [label_ref],
                            "question_text_original": _text([label_ref], lookup),
                            "normalized_label": "", "context_label": field.get("question_text_original", ""),
                            "answer_region_refs": [region_ref], "answer_refs": [], "answer_value_refs": [],
                            "answer_text_original": "", "answer_status": "blank", "unit_refs": [], "unit_text_original": "",
                            "children": [], "options": [], "instruction_refs": [], "attachment_refs": [],
                            "attachment_text_original": "", "review_status": "model_proposed", "review_reasons": [],
                            "model_hints": {"conditional_hint": True},
                            "source_locations": {"question": [block.get("location", {})], "answer_regions": [block.get("location", {})], "answer": []},
                        })
                if prompt_children:
                    prompt_keys={(r['block_id'],r['start']) for child in prompt_children for r in child.get('label_refs',[])}
                    existing_children = [child for child in field.get("children") or [] if ":prompt:" not in str(child.get("field_id")) and not any((r['block_id'],r['start']) in prompt_keys for r in child.get('label_refs',[]))]
                    field["children"] = existing_children + prompt_children

        # A vertically merged label followed by cells containing only ordinal
        # placeholders (1., 2., …) is a repeating blank-entry area.  Those
        # ordinals identify physical slots; they are not pre-filled answers to
        # one another.
        numbered_slot_blocks = set()
        for field in list(questions):
            label = first_label(field)
            structure = (label or {}).get("structure", {})
            if structure.get("table_id") != table_id or int(structure.get("row_span") or 1) <= 1:
                continue
            regions = [
                cell for cell in cells
                if structure.get("row_start") <= cell.get("structure", {}).get("row_start", 0) <= structure.get("row_end")
                and cell.get("structure", {}).get("grid_col_start", 0) > structure.get("grid_col_end", 0)
                and not cell.get("structure", {}).get("is_merge_continuation")
            ]
            if len(regions) < 2 or not all(re.fullmatch(r"\s*\d+[.)、]?\s*", str(cell.get("text") or "")) for cell in regions):
                continue
            children = []
            for index, region in enumerate(sorted(regions, key=lambda x: (x["structure"].get("row_start"), x["structure"].get("grid_col_start"))), 1):
                text = str(region.get("text") or "")
                cell_id = region.get("structure", {}).get("cell_id") or region["block_id"]
                field_id = f"native:{table_id}:numbered-slot:{cell_id}"
                slot_ref = {"block_id": region["block_id"], "start": len(text), "end": len(text), "slot_id": f"{cell_id}:after-content"}
                label_ref = {"block_id": region["block_id"], "start": 0, "end": len(text), "slot_id": ""}
                children.append({
                    "question_id": field_id, "field_id": field_id, "parent_question_id": field.get("field_id"),
                    "section_refs": deepcopy(field.get("section_refs") or []), "section_text_original": field.get("section_text_original", ""),
                    "question_refs": [label_ref], "label_refs": [label_ref],
                    "question_text_original": f"{field.get('question_text_original', '')} · {text.strip()}",
                    "normalized_label": "", "context_label": field.get("context_label", ""),
                    "answer_region_refs": [slot_ref], "answer_refs": [], "answer_value_refs": [],
                    "answer_text_original": "", "answer_status": "blank", "unit_refs": [], "unit_text_original": "",
                    "children": [], "options": [], "instruction_refs": [], "attachment_refs": [], "attachment_text_original": "",
                    "review_status": "model_proposed", "review_reasons": [], "record_index": index,
                    "record_column_index": 1, "record_slot_status": "reserved", "model_hints": {},
                })
                numbered_slot_blocks.add(region["block_id"])
            field["record_columns"] = [{"label": "填写项", "grid_col_start": structure.get("grid_col_end", 0) + 1}]
            field["children"] = children
            field["answer_region_refs"] = []; field["answer_refs"] = []; field["answer_value_refs"] = []
            field["answer_text_original"] = ""; field["answer_status"] = "blank"; field["review_status"] = "model_proposed"
        if numbered_slot_blocks:
            questions[:] = [field for field in questions if (first_label(field) or {}).get("block_id") not in numbered_slot_blocks]

        # A repeated two-column row such as “Evaluation | Completed by
        # Manufacturer” is a section column header, not a question with an
        # existing answer.  Require repetition plus explicit completion/owner
        # wording so repeated business questions are not removed merely for
        # having the same label.
        repeated = defaultdict(list)
        for row, items in by_row.items():
            if len(items) != 2 or not all(item.get("text", "").strip() for item in items):
                continue
            signature = tuple((item["structure"].get("grid_col_start"), item["structure"].get("grid_col_end")) for item in items)
            key = (re.sub(r"\s+", " ", items[0]["text"].strip().lower()), signature)
            repeated[key].append((row, items))
        header_block_ids = set()
        owner_words = ("completed by", "to be completed by", "填写", "manufacturer", "supplier", "vendor")
        for occurrences in repeated.values():
            if len(occurrences) < 2:
                continue
            if all(any(word in pair[1][1].get("text", "").lower() for word in owner_words) for pair in occurrences):
                header_block_ids.update(cell["block_id"] for _, pair in occurrences for cell in pair)
        if header_block_ids:
            questions[:] = [field for field in questions if (first_label(field) or {}).get("block_id") not in header_block_ids]
        if option_block_ids:
            questions[:] = [
                field for field in questions
                if (first_label(field) or {}).get("block_id") not in option_block_ids
                or bool(field.get("answer_region_refs"))
            ]
    # A confirmed field can use a protected trailing slot in the same
    # full-width table cell. Limit this to colon-terminated field labels; a
    # generic title or paragraph is not enough evidence for a fill position.
    def attach_trailing(field):
        if not field.get("answer_region_refs") and not field.get("record_columns"):
            block = first_label(field)
            structure = (block or {}).get("structure", {})
            text = str((block or {}).get("text") or "").rstrip()
            full_width = structure.get("grid_col_start") == 1 and structure.get("grid_col_end") == structure.get("table_column_count")
            slot = next((x for x in structure.get("logical_slots", []) if x.get("kind") == "trailing_fill_candidate"), None)
            if block and block.get("kind") == "table_cell" and full_width and text.endswith((":", "：")) and slot:
                region = {"block_id": block["block_id"], "start": len(block.get("text", "")), "end": len(block.get("text", "")), "slot_id": slot["slot_id"]}
                field["answer_region_refs"] = [region]; field["answer_value_refs"] = []; field["answer_refs"] = []
                field["answer_text_original"] = ""; field["answer_status"] = "blank"; field["review_status"] = "model_proposed"
                field["review_reasons"] = [x for x in field.get("review_reasons", []) if "答复" not in x and "位置" not in x]
        for child in field.get("children") or []: attach_trailing(child)
    for field in questions: attach_trailing(field)

    # Page headers/footers are retained as source blocks for preview and
    # provenance, but their document numbers and page counters are not form
    # questions.  Likewise, a short full-width row without question language,
    # children, or a response region is a structural heading in a form table.
    question_cues = re.compile(r"[?？]|(?:^|\s)(?:please|what|which|how|is|are|does|do|have|has|can|will)\b|请|是否|能否|何时|多少", re.I)
    def structural_heading(field):
        block = first_label(field)
        if not block:
            return False
        part = str(block.get("location", {}).get("part") or "")
        if part.startswith("word/header") or part.startswith("word/footer"):
            return True
        structure = block.get("structure", {})
        table_id = structure.get("table_id")
        effective_width = max(
            (candidate.get("structure", {}).get("grid_col_end", 0) for candidate in source_blocks
             if candidate.get("structure", {}).get("table_id") == table_id),
            default=structure.get("table_column_count", 0),
        )
        full_width = structure.get("grid_col_start") == 1 and structure.get("grid_col_end") == effective_width
        text = str(field.get("question_text_original") or block.get("text") or "").strip()
        return bool(
            full_width and text and len(text) <= 160
            and not field.get("answer_region_refs") and not field.get("children") and not field.get("record_columns")
            and not question_cues.search(text) and not text.endswith((":", "："))
        )
    questions[:] = [field for field in questions if not structural_heading(field)]
    questions.sort(key=lambda x: (first_label(x) or {}).get("order", 10**9))


def _apply_pairing_validation(questions, lookup):
    """Add independent answer-presence and source-pairing decisions."""
    flat = []
    def collect(field):
        flat.append(field)
        for child in field.get("children") or []:
            collect(child)
    for question in questions:
        collect(question)
    label_owners = defaultdict(set)
    protected_ranges = defaultdict(list)
    value_owners = defaultdict(set)
    for field in flat:
        owner = str(field.get("field_id") or field.get("question_id") or "")
        for ref in field.get("label_refs") or field.get("question_refs") or []:
            label_owners[ref.get("block_id")].add(owner)
            protected_ranges[ref.get("block_id")].append((ref.get("start",0), ref.get("end",0), owner))
        for ref in field.get("answer_value_refs") or field.get("answer_refs") or []:
            value_owners[(ref.get("block_id"), ref.get("start"), ref.get("end"))].add(owner)
    for bid, block in lookup.items():
        for span in block.get('structure', {}).get('protected_label_ranges', []):
            protected_ranges[bid].append((span['start'], span['end'], 'native'))

    for field in flat:
        field_id = str(field.get("field_id") or field.get("question_id") or "")
        labels = field.get("label_refs") or field.get("question_refs") or []
        regions = field.get("answer_region_refs") or []
        values = field.get("answer_value_refs") or field.get("answer_refs") or []
        existing_reasons = list(field.get("review_reasons") or [])
        reasons, hard = [], any("已有答案引用不是有效的非空原文范围" in reason for reason in existing_reasons)
        from .scanned_question_rows import cross_question_boundary_reason
        source_boundary_reason = cross_question_boundary_reason(field, lookup)
        if source_boundary_reason:
            reasons.append(source_boundary_reason)
            hard = True
        if not field.get('question_text_original','').strip():
            reasons.append('字段没有可读的原始问题标签'); hard = True
        for region_ref in regions:
            source=lookup.get(region_ref.get('block_id'),{})
            slot=next((s for s in source.get('structure',{}).get('logical_slots',[])
                       if s.get('slot_id')==region_ref.get('slot_id')),None)
            if slot and slot.get('kind')=='scan_observed_blank_tail':
                from .scan_inline_response_regions import verified_tail_slot
                if not verified_tail_slot(source,slot,lookup):
                    reasons.append('扫描填写片段的原图空白或标签证据无效'); hard=True
        if re.fullmatch(r'\s*\d+(?:\.\d+)*[.)、]?\s*',field.get('question_text_original','')) and not field.get('record_index'):
            reasons.append('题号不能独立作为问题标签'); hard = True
        for ref in values:
            block = lookup.get(ref.get("block_id"))
            start, end = ref.get("start"), ref.get("end")
            from .native_pdf_choice_rows import is_native_caption_list
            if block is None or not isinstance(start, int) or not isinstance(end, int) or not (0 <= start < end <= len(block.get("text", ""))) or not block.get("text", "")[start:end].strip():
                reasons.append("已有答案引用不是有效的非空原文范围")
                hard = True
            elif _placeholder_value(ref, lookup):
                reasons.append('填写占位符或未选项不能作为已有答案'); hard = True
            elif is_native_caption_list(ref, lookup):
                reasons.append('原生复选框的多个选项标签不能作为已填写答案'); hard = True
            elif UNITS.fullmatch(block.get('text','')[start:end].strip()) and re.search(r'how (?:long|many|much)|number of|ppm|ppb|duration|浓度|多少|年限|时长|面积',field.get('question_text_original',''),re.I):
                reasons.append('数值字段仅读取到单位，没有实际数值'); hard = True
                field.setdefault('unit_refs',[]).append(deepcopy(ref))
                field['unit_text_original']=block['text'][start:end]
            elif not any(r.get('block_id')==ref.get('block_id') and r.get('start',0)<=start and end<=r.get('end',0) for r in regions) and not any(option.get('selected') and ref in option.get('refs',[]) for option in field.get('options',[])) and not native_overlay_value_in_region(ref, regions, lookup) and not ocr_value_in_region(ref,regions,lookup):
                reasons.append('答案值不在已定位的答复区域内'); hard = True
        label_blocks = [lookup.get(ref.get("block_id")) for ref in labels if lookup.get(ref.get("block_id"))]
        table_labels = [b for b in label_blocks if b.get("structure", {}).get("table_id")]
        if table_labels:
            table_ids = {b["structure"].get("table_id") for b in table_labels}
            rows = {(b["structure"].get("row_start"), b["structure"].get("row_end")) for b in table_labels}
            cells = {b["structure"].get("cell_id") for b in table_labels}
            if len(table_ids) != 1 or (len(rows) > 1 and len(cells) > 1):
                reasons.append("一道记录包含多个独立物理字段标签")
                hard = True
            else:
                label = table_labels[0]; structure = label["structure"]
                if structure.get("table_id"):
                    for ref in regions + values:
                        if ref in values and selected_column_caption(ref, field, lookup):
                            continue
                        from .selection_evidence import verified_vector_region_ref
                        if ref in regions and verified_vector_region_ref(ref, field, lookup):
                            continue
                        from .native_pdf_choice_rows import verified_table_choice_ref
                        if ref in regions and verified_table_choice_ref(ref, field, lookup):
                            continue
                        from .pdf_attachment_groups import verified_attachment_region
                        if ref in regions and verified_attachment_region(ref, field, lookup):
                            continue
                        block = lookup.get(ref.get("block_id"), {}); bs = block.get("structure", {})
                        record_aligned = (
                            structure.get("local_layout_type") == "record_table_header"
                            and bs.get("local_layout_type") == "record_table"
                            and bs.get("record_header_row") == structure.get("row_start")
                            and bs.get("grid_col_start") == structure.get("grid_col_start")
                        )
                        if bs.get('record_header_block_id')==label['block_id']:
                            lb=label.get('location',{}).get('bbox',[]);rb=block.get('location',{}).get('bbox',[])
                            record_aligned=bool(len(lb)==len(rb)==4 and abs(lb[0]-rb[0])<2 and abs(lb[2]-rb[2])<2 and 0<=block['location'].get('page',0)-label['location'].get('page',0)<=1)
                        comment_aligned = (bs.get('comment_header_block_id')==label['block_id'] and bs.get('table_id')==structure.get('table_id') and bs.get('grid_col_start')==structure.get('grid_col_start') and bs.get('grid_col_end')==structure.get('grid_col_end') and bs.get('row_start',0)>structure.get('row_start',0))
                        owner_aligned = False
                        if field.get('boundary_evidence') == 'physical_responsibility_matrix' and bs.get('column_owner_ref') == label['block_id']:
                            row_contexts = [lookup.get(r.get('block_id'),{}).get('structure',{}) for r in field.get('context_refs',[])]
                            grid_aligned = bs.get('grid_col_start') == structure.get('grid_col_start') and bs.get('grid_col_end') == structure.get('grid_col_end')
                            evidence = bs.get('column_owner_evidence') or {}
                            hx, rx = structure.get('grid_x_twips', []), bs.get('grid_x_twips', [])
                            if (evidence.get('kind') == 'native_grid_interval_alignment' and
                                evidence.get('header_block_id') == label['block_id'] and evidence.get('unique_region') is True and
                                evidence.get('header_x_twips') == hx and evidence.get('region_x_twips') == rx and
                                len(hx) == len(rx) == 2 and hx[1] > hx[0] and rx[1] > rx[0]):
                                overlap = max(0, min(hx[1], rx[1])-max(hx[0], rx[0]))
                                grid_aligned = overlap/(hx[1]-hx[0]) >= .90 and overlap/(rx[1]-rx[0]) >= .95
                            owner_aligned = bool(bs.get('table_id') == structure.get('table_id') and
                                grid_aligned and
                                bs.get('row_start',0) > structure.get('row_end',0) and any(
                                    ctx.get('table_id') == bs.get('table_id') and ctx.get('row_start') == bs.get('row_start') and
                                    ctx.get('grid_col_end',9999) < bs.get('grid_col_start',0) for ctx in row_contexts))
                        choice_aligned = (
                            bs.get("local_layout_type") == "choice_matrix"
                            and bs.get("row_start") == structure.get("row_start")
                            and bs.get("column_start", -1) > structure.get("column_end", -1)
                        )
                        left_choice_aligned = False
                        if field.get('boundary_evidence') == 'physical_left_choice_grid' and bs.get('native_choice_parent_block_id') == label['block_id']:
                            option_id = bs.get('native_choice_label_block_id')
                            option_block = lookup.get(option_id, {})
                            option_structure = option_block.get('structure', {})
                            option_linked = any(any(r.get('block_id') == option_id for r in option.get('refs', [])) for option in field.get('options', []))
                            left_choice_aligned = bool(option_linked and
                                bs.get('table_id') == structure.get('table_id') == option_structure.get('table_id') and
                                bs.get('row_start') == option_structure.get('row_start') and
                                bs.get('grid_col_end', -2)+1 == option_structure.get('grid_col_start', -1) and
                                0 < bs.get('row_start', 0)-structure.get('row_start', 0) <= 8 and
                                (bs.get('blank_state') == 'blank' or block.get('controls')))
                        inline_aligned = (
                            bs.get("cell_id") == structure.get("cell_id")
                            and bool(ref.get("slot_id"))
                            and all(int(label_ref.get('end',0)) <= int(ref.get('start',0)) or int(ref.get('end',0)) <= int(label_ref.get('start',0)) for label_ref in labels if label_ref.get('block_id')==ref.get('block_id'))
                        )
                        stacked_aligned=(field.get('boundary_evidence')=='physical_stacked_form' and bs.get('table_id')==structure.get('table_id') and bs.get('row_start')==structure.get('row_end',structure.get('row_start',0))+1 and bs.get('grid_col_start')==structure.get('grid_col_start') and bs.get('grid_col_end')==structure.get('grid_col_end'))
                        same_vertical_column = (bs.get('table_id') == structure.get('table_id') and
                            bs.get('row_start') == structure.get('row_end',structure.get('row_start',0))+1 and
                            bs.get('grid_col_start') == structure.get('grid_col_start') and
                            bs.get('grid_col_end') == structure.get('grid_col_end'))
                        if same_vertical_column and field.get('boundary_evidence')=='native_numbered_prompt_next_control_row':
                            from .word_text_positions import verified_control_row
                            stacked_aligned=verified_control_row(label,block,ref,lookup)
                        if same_vertical_column and field.get('boundary_evidence')=='native_column_text_controls':
                            from .word_text_positions import verified_column_controls
                            stacked_aligned=verified_column_controls(label,block,ref,lookup)
                        if same_vertical_column and field.get('options') and block.get('controls'):
                            native_control = any(c.get('type') == 'checkbox' and
                                c.get('paragraph_index') == ref.get('control_paragraph_index') and
                                c.get('control_index') == ref.get('control_index') for c in block['controls'])
                            if native_control and any(ref in o.get('answer_region_refs',[]) for o in field['options']):
                                stacked_aligned = True
                        if (same_vertical_column and field.get('boundary_evidence') == 'full_width_prompt_followed_by_bordered_blank_row' and
                            structure.get('column_span') == structure.get('table_column_count') and
                            bs.get('blank_state') == 'blank' and not block.get('text','').strip() and not block.get('controls')):
                            stacked_aligned = True
                        if (field.get('boundary_evidence') == 'native_binary_option_lane' and
                            bs.get('table_id') == structure.get('table_id') and
                            0 <= bs.get('row_start',0)-structure.get('row_end',0) <= 4 and
                            bs.get('grid_col_start',0) > structure.get('grid_col_end',0)):
                            intervening = [b for b in lookup.values() if b.get('structure',{}).get('table_id') == structure.get('table_id') and
                                structure.get('row_end',0) < b.get('structure',{}).get('row_start',0) <= bs.get('row_start',0) and
                                b.get('structure',{}).get('grid_col_start') == structure.get('grid_col_start')]
                            slot = next((s for s in bs.get('logical_slots',[]) if s.get('slot_id') == ref.get('slot_id')),None)
                            if (intervening and all(not b.get('text','').strip() or b.get('structure',{}).get('resolved_layout_role') == 'label_continuation' for b in intervening) and
                                slot and slot.get('kind') == 'printed_option' and any(ref in o.get('answer_region_refs',[]) for o in field.get('options',[]))):
                                stacked_aligned = True
                        if bs.get('native_label_block_id')==label['block_id'] and bs.get('table_id')==structure.get('table_id') and 1<=bs.get('row_start',0)-structure.get('row_start',0)<=3:
                            stacked_aligned=True
                        nested_aligned=bs.get('native_parent_label_cell_id')==structure.get('cell_id') and str(block.get('location',{}).get('table','')).startswith(f"{label['location'].get('table')}.{label['location'].get('row')}.{label['location'].get('cell')}.")
                        if field.get('boundary_evidence')=='native_nested_response_controls':
                            from .nested_response_controls import valid_nested_response
                            nested_aligned=valid_nested_response(label,block,lookup)
                        aligned = owner_aligned or left_choice_aligned or comment_aligned or nested_aligned or record_aligned or choice_aligned or inline_aligned or stacked_aligned or (bs.get("table_id") == structure.get("table_id") and
                                   structure.get("row_start") <= bs.get("row_start", -1) <= structure.get("row_end") and
                                   bs.get("column_start", -1) > structure.get("column_end", -1))
                        if not aligned:
                            reasons.append("答复来源不在当前标签对应的物理答复区域")
                            hard = True
                            break
        for ref in values:
            other_labels = [owner for start,end,owner in protected_ranges.get(ref.get('block_id'),[]) if max(start,ref.get('start',0)) < min(end,ref.get('end',0))]
            from .scan_ruled_choice_groups import verified_self_caption_value
            own_divided_caption = (other_labels and all(owner == field_id for owner in other_labels)
                                   and verified_self_caption_value(ref, field, lookup))
            if other_labels and not own_divided_caption:
                reasons.append("答案引用了另一个字段的标签")
                hard = True
            owners = value_owners.get((ref.get("block_id"), ref.get("start"), ref.get("end")), set()) - {field_id}
            if owners and not selected_column_caption(ref, field, lookup):
                reasons.append("同一答案片段被多个独立字段复用")
                hard = True
        field["answer_presence"] = "present" if field.get("answer_text_original", "").strip() else "blank"
        selection_reasons = set(field.get('selection_review_reasons') or []) if field.get('pairing_evidence') == 'native_choice_regions' else set()
        # Reading accuracy and ownership of a physical response are separate
        # reviews. Recheck every OCR value's bounded source association before
        # treating the transcription warning as non-structural. Import still
        # requires approval and source_observation remains pending_review.
        source_reasons = set(field.get('source_review_reasons') or []) if (
            field.get('answer_pairing_evidence') == 'ocr_text_inside_independent_physical_response'
            and field.get('source_observation',{}).get('status') == 'pending_review'
            and values and all(ocr_value_in_region(v, regions, lookup) for v in values)) else set()
        if field.get('answer_pairing_evidence') == 'ocr_text_inside_verified_source_subfield':
            from .scanned_subfields import SOURCE_REVIEW_REASON, verified_scan_subfield_pairing
            if verified_scan_subfield_pairing(field, lookup):
                # Only this known transcription warning is non-structural.
                # Extra field, condition or ownership warnings remain pending.
                source_reasons = {SOURCE_REVIEW_REASON}.intersection(field.get('source_review_reasons') or [])
        if field.get('boundary_evidence') == 'closed_record_column':
            from .scanned_record_pairing import verified_scan_record_pairing, RECORD_REVIEW_REASON
            if verified_scan_record_pairing(field,lookup):
                source_reasons={RECORD_REVIEW_REASON}.intersection(field.get('source_review_reasons') or [])
        nonstructural_reasons = selection_reasons | source_reasons
        pairing_reasons = [reason for reason in existing_reasons if reason not in nonstructural_reasons]
        structural_review = field.get('review_status') == 'needs_review' and not (nonstructural_reasons and not pairing_reasons)
        # A known control location can be edited explicitly even when its old
        # glyph has an unknown selection state. This does not authorize auto-
        # filling or remove the separate selection warning from the UI.
        field["pairing_validation"] = "invalid" if hard else ("needs_review" if pairing_reasons or structural_review else "valid")
        field["validation_status"] = field["pairing_validation"]
        field["validation_reasons"] = list(dict.fromkeys([*existing_reasons, *reasons]))
        if field["pairing_validation"] != "valid":
            field["review_status"] = "needs_review"
            field["review_reasons"] = list(field["validation_reasons"])
            if hard and values:
                field['observed_answer_text'] = field.get('answer_text_original','')
                field['answer_status'] = 'unresolved'


def _unique_pdf_observations(blocks):
    """Keep one physical source for coincident raster geometry detections.

    The native checkpoint may contain multiple observations of exactly the
    same shape. Preserve their order evidence before resolving model refs.
    Different content or control states cannot share one source identity.
    """
    sources = {}
    observation_orders = {}
    for block in blocks:
        identifier = block["block_id"]
        if identifier not in sources:
            sources[identifier] = block
            continue
        previous = sources[identifier]
        location = block.get('location', {})
        structure = block.get('structure', {})
        is_located_geometry = (location.get('type') == 'pdf' and location.get('page')
                               and len(location.get('bbox') or []) == 4
                               and structure.get('source_method') == 'raster_geometry'
                               and structure.get('scan_region_kind'))
        observed = {key: value for key, value in block.items() if key != 'order'}
        original = {key: value for key, value in previous.items() if key != 'order'}
        if not is_located_geometry or observed != original:
            raise ValueError(f'PDF 第 {location.get("page", "?")} 页的来源标识 {identifier} 冲突，无法唯一定位。')
        observation_orders.setdefault(identifier, [previous['order']]).append(block['order'])
    return list(sources.values()), observation_orders


def validate_and_assemble(extracted: dict, model_batches: list[dict], model_info: dict | None = None):
    source_blocks = extracted["source_blocks"]
    if extracted.get('file_type', '').lower() == 'pdf':
        source_blocks, repeated = _unique_pdf_observations(source_blocks)
        if repeated:
            extracted.setdefault('metadata', {})['coincident_source_observations'] = repeated
    from .form_completion import prepare_form_regions, complete_local_fields
    prepare_form_regions(source_blocks)
    lookup = {b["block_id"]: b for b in source_blocks}
    assignments, model_fields, warnings, invalid = [], [], list(extracted.get("warnings", [])), []
    covered = defaultdict(list)
    for batch_index, output in enumerate(model_batches):
        for raw in output.get("classifications", output.get("segments", [])):
            ref = {"block_id": raw.get("block_id"), "start": raw.get("start"), "end": raw.get("end")}
            role = raw.get("semantic_role", raw.get("role"))
            if role not in ROLES or not _valid_ref(ref, lookup):
                invalid.append({"batch": batch_index + 1, "reason": "无效角色或原文引用", "value": raw})
                continue
            assignment = {
                "role": role, "ref": ref, "group_id": str(raw.get("group_id") or ""),
                "parent_group_id": str(raw.get("parent_group_id") or ""),
                "section_group_id": str(raw.get("section_group_id") or ""),
                "relation_status": str(raw.get("relation_status") or "proposed"),
                "reason": str(raw.get("reason") or "")[:240],
                "layout_role": str(raw.get("layout_role") or "other"),
                "field_type_hint": raw.get("field_type_hint"),
                "responsibility_hint": raw.get("responsibility_hint"),
                "required_hint": raw.get("required_hint"),
                "conditional_hint": bool(raw.get("conditional_hint")),
            }
            signature = (role, ref["block_id"], ref["start"], ref["end"], assignment["group_id"])
            if signature not in {(a["role"], a["ref"]["block_id"], a["ref"]["start"], a["ref"]["end"], a["group_id"]) for a in assignments}:
                assignments.append(assignment)
                covered[ref["block_id"]].append((ref["start"], ref["end"]))
        model_fields.extend(output.get("fields", []))

    _repair_table_relations(assignments, source_blocks, lookup)
    deduplicated = {}
    for assignment in assignments:
        ref = assignment["ref"]
        signature = (assignment["role"], ref["block_id"], ref["start"], ref["end"], assignment["group_id"])
        if signature not in deduplicated or assignment["relation_status"] == "confirmed":
            deduplicated[signature] = assignment
    assignments = list(deduplicated.values())
    for assignment in assignments:
        ref = assignment["ref"]
        covered[ref["block_id"]].append((ref["start"], ref["end"]))

    def field_refs(raw):
        if raw.get("section_ref"): yield raw["section_ref"]
        for key in ("label_refs", "answer_region_refs", "answer_value_refs", "option_refs", "unit_refs"):
            yield from raw.get(key, [])
        for child in raw.get("children", []):
            for key in ("label_refs", "answer_region_refs", "answer_value_refs", "option_refs", "unit_refs"):
                yield from child.get(key, [])
    for raw_field in model_fields:
        for ref in field_refs(raw_field):
            if _valid_ref(ref, lookup) and ref["start"] < ref["end"]:
                covered[ref["block_id"]].append((ref["start"], ref["end"]))

    unresolved_items = []
    for block in source_blocks:
        text = block.get("text", "")
        if not text:
            if block["kind"] in {"image", "unsupported"}:
                unresolved_items.append({"refs": [{"block_id": block["block_id"], "start": 0, "end": 0}], "reason": "发现不可读区域，OCR 已关闭", "kind": "unreadable"})
            continue
        spans = sorted(covered.get(block["block_id"], []))
        cursor = 0
        for start, end in spans:
            if start > cursor and text[cursor:start].strip():
                unresolved_items.append({"refs": [{"block_id": block["block_id"], "start": cursor, "end": start}], "reason": "模型未覆盖该原文范围", "kind": "uncovered"})
            cursor = max(cursor, end)
        if cursor < len(text) and text[cursor:].strip():
            unresolved_items.append({"refs": [{"block_id": block["block_id"], "start": cursor, "end": len(text)}], "reason": "模型未覆盖该原文范围", "kind": "uncovered"})

    by_group = defaultdict(list)
    seen_non_qa = set()
    for item in assignments:
        if item["group_id"]:
            by_group[item["group_id"]].append(item)

    # The model may express an attachment or note as a child group. Preserve
    # that relation instead of treating the child as an orphan, but only when
    # it names one existing parent question group.
    parent_groups = {
        group_id for group_id, items in by_group.items()
        if any(item["role"] == "question" for item in items)
    }
    for group_id, items in list(by_group.items()):
        if group_id in parent_groups or any(item["role"] == "question" for item in items):
            continue
        parents = {item.get("parent_group_id") for item in items if item.get("parent_group_id")}
        if len(parents) == 1 and next(iter(parents)) in parent_groups:
            parent_id = next(iter(parents))
            for item in items:
                item["group_id"] = parent_id
    by_group = defaultdict(list)
    for item in assignments:
        if item["group_id"]:
            by_group[item["group_id"]].append(item)
    sections, questions, non_qa = [], [], []
    section_lookup = {}
    from .form_completion import FIXED_PROSE_ROLES
    explicit_question_blocks = set()
    def section_allowed(block_id):
        return block_id not in explicit_question_blocks and lookup.get(block_id, {}).get('structure', {}).get('resolved_layout_role') not in FIXED_PROSE_ROLES | {'repeated_header_footer', 'fixed_template_approval', 'instruction'}
    for item in assignments:
        if item["role"] == "section" and section_allowed(item['ref']['block_id']):
            value = {"section_id": item["group_id"] or f"section-{len(sections)+1}", "refs": [item["ref"]], "text_original": _text([item["ref"]], lookup)}
            sections.append(value); section_lookup[value["section_id"]] = value
        elif item["role"] in {"title", "table_header", "non_qa"} or not section_allowed(item['ref']['block_id']):
            signature = (item["role"], item["ref"]["block_id"], item["ref"]["start"], item["ref"]["end"])
            if signature not in seen_non_qa:
                seen_non_qa.add(signature)
                non_qa.append({"role": item["role"], "refs": [item["ref"]], "text_original": _text([item["ref"]], lookup), "reason": item["reason"]})

    for group_id, items in by_group.items():
        qrefs = [x["ref"] for x in items if x["role"] == "question"]
        if not qrefs:
            roles = {x["role"] for x in items}
            if roles and roles <= {"instruction", "note"}:
                for item in items:
                    signature = (item["role"], item["ref"]["block_id"], item["ref"]["start"], item["ref"]["end"])
                    if signature not in seen_non_qa:
                        seen_non_qa.add(signature)
                        non_qa.append({"role": item["role"], "refs": [item["ref"]], "text_original": _text([item["ref"]], lookup), "reason": item["reason"]})
            elif any(x["role"] in {"answer", "option", "instruction", "attachment_requirement"} for x in items):
                unresolved_items.append({"refs": [x["ref"] for x in items], "reason": "关联组没有问题原文", "kind": "orphan_relation"})
            continue
        arefs = [x["ref"] for x in items if x["role"] == "answer"]
        region_refs, unit_refs = [], []
        for ref in arefs:
            block = lookup[ref["block_id"]]
            leading = next((slot for slot in block.get("structure", {}).get("logical_slots", []) if slot.get("kind") == "leading_blank_candidate"), None)
            visible = block.get("text", "")[ref["start"]:ref["end"]].strip()
            if leading and re.fullmatch(r"(?:%|m[²2]|㎡|人|名|个|kg|g|mg|ml|l)", visible, re.I):
                region_refs.append({"block_id": block["block_id"], "start": leading["offset"], "end": leading["offset"], "slot_id": leading["slot_id"]})
                unit_refs.append(ref)
            else:
                region_refs.append(ref)
        value_refs = [ref for ref in arefs if ref not in unit_refs and ref["start"] < ref["end"] and lookup[ref["block_id"]].get("text", "")[ref["start"]:ref["end"]].strip() and not _placeholder_value(ref, lookup)]
        orefs = [x["ref"] for x in items if x["role"] == "option"]
        irefs = [x["ref"] for x in items if x["role"] == "instruction"]
        attach = [x["ref"] for x in items if x["role"] == "attachment_requirement"]
        unresolved_refs = [x["ref"] for x in items if x["role"] == "unresolved" or x["relation_status"] == "unresolved"]
        reasons = [x["reason"] for x in items if x["reason"] and (x["role"] == "unresolved" or x["relation_status"] != "confirmed")]
        if _cross_section(qrefs, arefs, lookup):
            reasons.append("问题与答案来源疑似跨页或跨表，需要人工核对")
        options = []
        for ref in orefs:
            block = lookup[ref["block_id"]]
            text = block.get("text", "")[ref["start"]:ref["end"]]
            if not text.strip():
                invalid.append({'reason':'选项文字引用为空；空白答复区域不是选项','value':ref})
                continue
            selected = any(bool(c.get("selected")) for c in block.get("controls", [])) or bool(re.search(r"[☑☒✓✔]", text))
            options.append({"refs": [ref], "text_original": text, "selected": selected, "selection_source": "native_control_or_symbol" if selected else "not_selected"})
        section_refs = []
        for x in items:
            sid = x.get("section_group_id")
            if sid and sid in section_lookup:
                section_refs.extend(section_lookup[sid]["refs"])
        if not section_refs and sections:
            first_order = min(lookup[r["block_id"]]["order"] for r in qrefs)
            preceding = [s for s in sections if lookup[s["refs"][0]["block_id"]]["order"] < first_order]
            if preceding:
                section_refs = preceding[-1]["refs"]
        questions.append({
            "question_id": group_id, "field_id": group_id,
            "parent_question_id": next((x["parent_group_id"] for x in items if x["parent_group_id"]), None),
            "section_refs": section_refs,
            "question_refs": qrefs, "label_refs": qrefs,
            "question_text_original": _text(qrefs, lookup),
            "context_label": "",
            "answer_region_refs": region_refs, "answer_refs": value_refs, "answer_value_refs": value_refs,
            "answer_text_original": _text(value_refs, lookup),
            "answer_status": _answer_status(region_refs, orefs, unresolved_refs, lookup),
            "options": options,
            "unit_refs": unit_refs, "unit_text_original": _text(unit_refs, lookup),
            "instruction_refs": irefs,
            "attachment_refs": attach,
            "attachment_text_original": _text(attach, lookup),
            "review_status": "needs_review" if reasons or unresolved_refs else "model_proposed",
            "review_reasons": list(dict.fromkeys(reasons)),
            "model_hints": next(({key: x.get(key) for key in ("field_type_hint", "responsibility_hint", "required_hint", "conditional_hint")} for x in items if x["role"] == "question"), {}),
            "source_locations": {"question": _locations(qrefs, lookup), "answer_regions": _locations(region_refs, lookup), "answer": _locations(value_refs, lookup)},
        })

    if model_fields:
        direct_questions = []

        def checked_refs(values, field_id, role):
            refs = []
            for ref in values or []:
                if _valid_ref(ref, lookup): refs.append(ref)
                else: invalid.append({"field_id": field_id, "reason": f"{role} 包含无效原文引用", "value": ref})
            return refs

        def build_field(raw, parent_id=None, inherited_section=None):
            field_id = str(raw.get("field_id") or f"field-{len(direct_questions)+1}")
            labels = checked_refs(raw.get("label_refs"), field_id, "label_refs")
            if not labels:
                invalid.append({"field_id": field_id, "reason": "字段没有有效标签引用"}); return None
            regions = checked_refs(raw.get("answer_region_refs"), field_id, "answer_region_refs")
            values = checked_refs(raw.get("answer_value_refs"), field_id, "answer_value_refs")
            option_refs = checked_refs(raw.get("option_refs"), field_id, "option_refs")
            units = checked_refs(raw.get("unit_refs"), field_id, "unit_refs")
            excluded = {(x["block_id"], x["start"], x["end"]) for x in labels + units}
            values = [x for x in values if (x["block_id"], x["start"], x["end"]) not in excluded and not _placeholder_value(x, lookup)]
            section = inherited_section
            if raw.get("section_ref") is not None:
                candidate = checked_refs([raw.get("section_ref")], field_id, "section_ref")
                if candidate: section = candidate[0]
            reasons = [str(x)[:240] for x in raw.get("review_reasons", []) if str(x).strip()]
            if any(ref.get("start") == ref.get("end") for ref in raw.get("answer_value_refs") or []):
                reasons.append("已有答案引用不是有效的非空原文范围")
            option_items = []
            for option_ref in option_refs:
                block = lookup[option_ref["block_id"]]
                text = block.get("text", "")[option_ref["start"]:option_ref["end"]]
                if not text.strip():
                    invalid.append({'field_id':field_id,'reason':'选项文字引用为空；空白答复区域不是选项','value':option_ref})
                    continue
                selected = any(bool(control.get("selected")) for control in block.get("controls", [])) or bool(re.search(r"[☑☒✓✔]", text))
                option_items.append({"refs": [option_ref], "text_original": text, "selected": selected, "selection_source": "native_control_or_symbol" if selected else "not_selected"})
            if values and _text(values, lookup).strip(): status = "filled"
            elif any(item["selected"] for item in option_items): status = "filled"
            elif any(lookup[x["block_id"]]["kind"] in {"image", "unsupported"} for x in regions): status = "unreadable"
            elif regions or option_items: status = "blank"
            elif raw.get("answer_status") == "unresolved" or reasons: status = "unresolved"
            else: status = "not_located"
            children = [x for child in raw.get("children", []) if (x := build_field(child, field_id, section))]
            return {
                "question_id": field_id, "field_id": field_id, "parent_question_id": parent_id,
                "section_refs": [section] if section else [], "section_text_original": _text([section], lookup) if section else "",
                "question_refs": labels, "label_refs": labels, "question_text_original": _text(labels, lookup), "normalized_label": "", "context_label": _text([section], lookup) if section else "",
                "answer_region_refs": regions, "answer_refs": values, "answer_value_refs": values,
                "answer_text_original": _text(values, lookup), "answer_status": status,
                "unit_refs": units, "unit_text_original": _text(units, lookup), "children": children,
                "options": option_items, "instruction_refs": [], "attachment_refs": [], "attachment_text_original": "",
                "review_status": "needs_review" if status in {"unresolved", "unreadable", "not_located"} or reasons else "model_proposed",
                "review_reasons": reasons, "source_locations": {"question": _locations(labels, lookup), "answer_regions": _locations(regions, lookup), "answer": _locations(values, lookup)},
            }

        for raw in model_fields:
            item = build_field(raw)
            if item: direct_questions.append(item)

        def first_label_block(field):
            refs = field.get("label_refs", [])
            return lookup.get(refs[0]["block_id"]) if refs else None

        def slot_ref(block, kind="trailing_fill_candidate"):
            slot = next((x for x in block.get("structure", {}).get("logical_slots", []) if x.get("kind") == kind), None)
            return {"block_id": block["block_id"], "start": slot["offset"], "end": slot["offset"], "slot_id": slot["slot_id"]} if slot else None

        # A section context accidentally emitted as a field is rejected. Its
        # children remain candidates and inherit the valid section reference.
        promoted = []
        for field in direct_questions:
            label_ids = {x["block_id"] for x in field.get("label_refs", [])}
            section_ids = {x["block_id"] for x in field.get("section_refs", [])}
            if label_ids and label_ids == section_ids and first_label_block(field).get("kind") != "table_cell":
                for child in field.get("children", []):
                    child["section_refs"] = field["section_refs"]; child["section_text_original"] = field["section_text_original"]; child["context_label"] = field["section_text_original"]
                    promoted.append(child)
            else: promoted.append(field)
        direct_questions = promoted

        table_cells = defaultdict(list)
        for block in source_blocks:
            table_id = block.get("structure", {}).get("table_id")
            if block.get("kind") == "table_cell" and table_id: table_cells[table_id].append(block)

        def fallback_field(block, section_refs, parent_id=None, reason="模型未返回该物理表单字段，按表格候选保留"):
            ref = {"block_id": block["block_id"], "start": 0, "end": len(block.get("text", "")), "slot_id": ""}
            return {"question_id": "physical-" + block["structure"]["cell_id"], "field_id": "physical-" + block["structure"]["cell_id"], "parent_question_id": parent_id, "section_refs": section_refs, "section_text_original": _text(section_refs, lookup), "question_refs": [ref], "label_refs": [ref], "question_text_original": block["text"], "normalized_label": "", "context_label": _text(section_refs, lookup), "answer_region_refs": [], "answer_refs": [], "answer_value_refs": [], "answer_text_original": "", "answer_status": "not_located", "unit_refs": [], "unit_text_original": "", "children": [], "options": [], "instruction_refs": [], "attachment_refs": [], "attachment_text_original": "", "review_status": "needs_review", "review_reasons": [reason], "source_locations": {"question": [block.get("location", {})], "answer_regions": [], "answer": []}}

        # Geometry produces candidates, never answers: a non-empty leftmost
        # cell in a label-response table must not disappear when the model
        # omits it. The candidate stays reviewable until relations validate.
        existing_label_ids = {first_label_block(x)["block_id"] for x in direct_questions if first_label_block(x)}
        for table_id, cells in table_cells.items():
            if not cells or cells[0].get("structure", {}).get("table_layout_hint") != "label_response_form_candidate": continue
            left = min(x["structure"]["column_start"] for x in cells)
            context_ids = cells[0].get("structure", {}).get("preceding_block_ids", [])
            section_refs = [{"block_id": x, "start": 0, "end": len(lookup[x].get("text", "")), "slot_id": ""} for x in context_ids if x in lookup and lookup[x].get("text")]
            for cell in cells:
                s = cell["structure"]
                # A full-width merged row has no separate response region. It
                # is commonly a note or instruction and must not become a
                # field solely because its physical column starts at the left.
                # It remains covered by the model classification/unresolved
                # tracking instead of silently disappearing.
                has_distinct_response_columns = s["column_end"] < s.get("table_column_count", s["column_end"])
                if s["column_start"] == left and has_distinct_response_columns and cell.get("text") and not s.get("is_merge_continuation") and cell["block_id"] not in existing_label_ids:
                    direct_questions.append(fallback_field(cell, section_refs)); existing_label_ids.add(cell["block_id"])

        removed = set()
        for table_id, cells in table_cells.items():
            fields = [x for x in direct_questions if (first_label_block(x) or {}).get("structure", {}).get("table_id") == table_id]
            layout_hint = cells[0].get("structure", {}).get("table_layout_hint")
            # Once the model has returned a field in a physical table, native
            # geometry may validate its adjacent response region regardless of
            # the preliminary layout hint. A populated first label/value row is
            # not sufficient evidence that the row is a column header.
            if not fields: continue
            context_ids = cells[0].get("structure", {}).get("preceding_block_ids", [])
            context_refs = [{"block_id": x, "start": 0, "end": len(lookup[x].get("text", "")), "slot_id": ""} for x in context_ids if x in lookup and lookup[x].get("text")]
            if context_refs:
                for field in fields:
                    field["section_refs"] = context_refs; field["section_text_original"] = _text(context_refs, lookup); field["context_label"] = field["section_text_original"]
            fields.sort(key=lambda x: (first_label_block(x)["structure"]["row_start"], first_label_block(x)["structure"]["column_start"]))
            # A vertically merged left label owns the labels to its right in
            # the covered rows; this is a geometric composite-field relation.
            for parent in fields:
                pb = first_label_block(parent); ps = pb["structure"]
                if ps.get("row_span", 1) <= 1: continue
                existing = {x["field_id"] for x in parent.get("children", [])}
                existing_blocks = {first_label_block(x)["block_id"] for x in parent.get("children", []) if first_label_block(x)}
                for cell in cells:
                    cs = cell["structure"]
                    label_like = "\n" in cell.get("text", "") or cell.get("text", "").rstrip().endswith((";", ":"))
                    if ps["row_start"] <= cs["row_start"] <= ps["row_end"] and cs["column_start"] > ps["column_end"] and cell.get("text") and label_like and cell["block_id"] not in existing_blocks:
                        child = fallback_field(cell, parent.get("section_refs", []), parent["field_id"], "复合字段右侧存在标签候选，模型未完整建立子字段关系")
                        parent.setdefault("children", []).append(child); existing.add(child["field_id"]); existing_blocks.add(cell["block_id"])
                for candidate in fields:
                    if candidate is parent or candidate["field_id"] in existing: continue
                    cb = first_label_block(candidate); cs = cb["structure"]
                    if ps["row_start"] <= cs["row_start"] <= ps["row_end"] and cs["column_start"] > ps["column_end"]:
                        candidate["parent_question_id"] = parent["field_id"]; parent.setdefault("children", []).append(candidate); removed.add(candidate["field_id"])
                parent["children"].sort(key=lambda x: (first_label_block(x)["structure"]["row_start"], first_label_block(x)["structure"]["column_start"]))

            for field in fields:
                if field["field_id"] in removed: continue
                label = first_label_block(field); structure = label["structure"]
                row_start, row_end, col_end = structure["row_start"], structure["row_end"], structure["column_end"]
                child_blocks = {first_label_block(x)["block_id"] for x in field.get("children", []) if first_label_block(x)}
                # The next independent label on the same row is a hard local
                # boundary. Never search through it for a later non-empty
                # value, which previously produced “企业名称 → 企业地址”.
                next_label_columns = [
                    first_label_block(candidate)["structure"]["column_start"]
                    for candidate in fields
                    if candidate is not field and candidate["field_id"] not in removed
                    and first_label_block(candidate)["structure"]["row_start"] == row_start
                    and first_label_block(candidate)["structure"]["column_start"] > col_end
                ]
                next_label_column = min(next_label_columns) if next_label_columns else None
                right = [x for x in cells if row_start <= x["structure"]["row_start"] <= row_end and x["structure"]["column_start"] > col_end and (next_label_column is None or x["structure"]["column_start"] < next_label_column) and not x["structure"].get("is_merge_continuation")]
                regions, values, units = [], [], []
                for cell in right:
                    if cell["block_id"] in child_blocks: continue
                    if not cell.get("text"):
                        regions.append({"block_id": cell["block_id"], "start": 0, "end": 0, "slot_id": ""})
                    elif any(control.get("placeholder") for control in cell.get("controls", [])):
                        regions.append({"block_id": cell["block_id"], "start": 0, "end": len(cell.get("text", "")), "slot_id": ""})
                    elif (leading := slot_ref(cell, "leading_blank_candidate")):
                        regions.append(leading); units.append({"block_id": cell["block_id"], "start": 0, "end": len(cell["text"]), "slot_id": ""})
                    else:
                        values.append({"block_id": cell["block_id"], "start": 0, "end": len(cell["text"]), "slot_id": ""}); regions.append(values[-1])
                def unique_refs(refs):
                    seen = set(); result = []
                    for ref in refs:
                        key = (ref["block_id"], ref["start"], ref["end"], ref.get("slot_id", ""))
                        if key not in seen: seen.add(key); result.append(ref)
                    return result
                regions, values, units = unique_refs(regions), unique_refs(values), unique_refs(units)
                field["answer_region_refs"] = regions; field["answer_value_refs"] = values; field["answer_refs"] = values
                field["answer_text_original"] = _text(values, lookup); field["unit_refs"] = units; field["unit_text_original"] = _text(units, lookup)
                field["answer_status"] = "filled" if values else ("blank" if regions or field.get("children") else "not_located")
                field["review_status"] = "model_proposed" if field["answer_status"] in {"filled", "blank"} else "needs_review"
                for child in field.get("children", []):
                    cb = first_label_block(child); trailing = slot_ref(cb)
                    child["answer_region_refs"] = [trailing] if trailing else []; child["answer_value_refs"] = []; child["answer_refs"] = []
                    child["answer_text_original"] = ""; child["answer_status"] = "blank" if trailing else "not_located"; child["review_status"] = "model_proposed" if trailing else "needs_review"
        direct_questions = [x for x in direct_questions if x["field_id"] not in removed]
        direct_questions.sort(key=lambda x: first_label_block(x).get("order", 10**9))
        questions = direct_questions

    # Native inline placeholders are physical fill positions. Create one field
    # per label/slot range before section assignment. This does not depend on
    # the model recognizing several roles inside a single source block.
    existing_label_ranges = {
        (ref.get("block_id"), ref.get("start"), ref.get("end"))
        for field in questions for ref in (field.get("label_refs") or [])
    }
    for block in source_blocks if model_info is not None else []:
        for slot in block.get("structure", {}).get("logical_slots", []):
            if slot.get("kind") != "inline_placeholder" or slot.get("label_start") is None:
                continue
            label_ref = {"block_id": block["block_id"], "start": slot["label_start"], "end": slot["label_end"], "slot_id": ""}
            if (label_ref["block_id"], label_ref["start"], label_ref["end"]) in existing_label_ranges:
                continue
            region_ref = {"block_id": block["block_id"], "start": slot["start"], "end": slot["end"], "slot_id": slot["slot_id"]}
            context_ids = block.get("structure", {}).get("preceding_block_ids", [])
            section_refs = [{"block_id": x, "start": 0, "end": len(lookup[x].get("text", "")), "slot_id": ""} for x in context_ids if x in lookup and lookup[x].get("text")]
            field_id = f"inline:{block['block_id']}:{slot['slot_id']}"
            questions.append({
                "question_id": field_id, "field_id": field_id, "parent_question_id": None,
                "section_refs": section_refs, "section_text_original": _text(section_refs, lookup),
                "question_refs": [label_ref], "label_refs": [label_ref], "question_text_original": _text([label_ref], lookup),
                "normalized_label": "", "context_label": _text(section_refs, lookup),
                "answer_region_refs": [region_ref], "answer_refs": [], "answer_value_refs": [],
                "answer_text_original": "", "answer_status": "blank", "unit_refs": [], "unit_text_original": "",
                "children": [], "options": [], "instruction_refs": [], "attachment_refs": [], "attachment_text_original": "",
                "review_status": "model_proposed", "review_reasons": [],
                "source_locations": {"question": [block.get("location", {})], "answer_regions": [block.get("location", {})], "answer": []},
            })
            existing_label_ranges.add((label_ref["block_id"], label_ref["start"], label_ref["end"]))
    if model_info is not None:
        restore_physical_fields(questions, source_blocks, lookup)
    questions.sort(key=lambda x: lookup[(x.get("label_refs") or x.get("question_refs"))[0]["block_id"]].get("order", 10**9))

    def full_ref(block_id):
        block = lookup.get(block_id)
        return {"block_id": block_id, "start": 0, "end": len(block.get("text", "")), "slot_id": ""} if block else None

    def section_node(refs, level):
        if len(refs) == 1:
            marker = lookup.get(refs[0]['block_id'], {}).get('structure', {}).get('native_section_marker_ref')
            if marker and _valid_ref(marker, lookup): refs = [deepcopy(marker)] + refs
        text = _text(refs, lookup)
        first_line = text.splitlines()[0] if text else ""
        number = re.match(r"^\s*(\d+(?:\.\d+)*|[IVXLCDM]+)[.)]?\s+", first_line)
        stable = "|".join(ref["block_id"] for ref in refs) or f"unknown-{level}"
        native_number = ''
        if len(refs) == 1:
            spans = lookup[refs[0]['block_id']].get('structure', {}).get('paragraph_spans', [])
            numbered = [p.get('style', {}).get('numbering', {}) for p in spans if p.get('style', {}).get('numbering')]
            if len(numbered) == 1 and numbered[0].get('format') != 'bullet':
                native_number = str(numbered[0].get('marker', '')).rstrip('.)')
        return {"section_id": f"section:{stable}", "level": level, "refs": refs,
                "text_original": text, "original_number": number.group(1) if number else native_number}

    # Local completion creates new children after the model pass. Apply the
    # same source section path both before and after that completion so these
    # fields cannot fall into a spurious "unassigned" section.
    native_table_sections = _numbered_observation_sections(source_blocks)
    from .word_table_context import native_word_table_context, apply_word_group_context
    word_table_sections, word_table_groups = native_word_table_context(source_blocks)
    for table_id, headings in word_table_sections.items():
        native_table_sections.setdefault(table_id, []).extend(headings)

    def attach_sections(field, inherited=None):
        label_refs = field.get("label_refs") or field.get("question_refs") or []
        label = lookup.get(label_refs[0]["block_id"]) if label_refs else None
        raw_path = label.get("structure", {}).get("section_path_block_ids", []) if label else []
        path = deepcopy(inherited) if inherited else []
        if not path:
            for level, block_ids in enumerate(raw_path):
                refs = [r for block_id in block_ids if section_allowed(block_id) and (r := full_ref(block_id)) and lookup[block_id].get("text")]
                if refs: path.append(section_node(refs, level))
        if not path and field.get("section_refs"):
            refs = [r for r in field['section_refs'] if section_allowed(r['block_id'])]
            if refs: path = [section_node(refs, 0)]
        rejected_question_context = any(bid in explicit_question_blocks for group in raw_path for bid in group)
        if not path and label and (rejected_question_context or label.get('kind') in {'paragraph','heading','title'}):
            # Native proximity can contain the immediately preceding questions.
            # Recover only an earlier, independently classified standalone
            # heading, never the rejected question text as a section.
            heading_ids = {a['ref']['block_id'] for a in assignments if a['role'] in {'section', 'title', 'table_header'}}
            headings = [b for b in source_blocks if section_allowed(b['block_id'])
                and b.get('order', 0) < label.get('order', 0) and b.get('kind') in {'paragraph','heading','title'}
                and not b.get('structure', {}).get('logical_slots') and '?' not in b.get('text', '')
                and 0 < len(b.get('text', '')) <= 160
                and (b.get('kind') in {'heading','title'} or (b.get('text','').isupper()
                    and (b['block_id'] in heading_ids or len(b.get('text','').split())>=3)))]
            if headings: path = [section_node([full_ref(max(headings,key=lambda b:b.get('order',0))['block_id'])],0)]
        if label:
            structure = label.get('structure', {})
            # Only a source-proven consecutive alphabetic heading series can
            # cross physical table boundaries; adjacent prose cannot.
            peers = [b for b in source_blocks if (scope := b.get('structure', {}).get('native_peer_section_range'))
                and scope.get('kind') == 'consecutive_native_alphabetic_peer_rows'
                and scope.get('part') == label.get('location', {}).get('part')
                and scope['start_order'] < label.get('order', -1)
                and (scope.get('end_order') is None or label.get('order', -1) < scope['end_order'])]
            peer = max(peers, key=lambda b: b.get('order', -1)) if peers else None
            if peer:
                # A peer replaces stale sibling scope from an earlier table.
                # Real document-title ancestry remains distinct.
                path = [node for node in path if node.get('refs') and all(
                    lookup.get(r['block_id'], {}).get('kind') == 'title' for r in node['refs'])]
                path.append(section_node([full_ref(peer['block_id'])], len(path)))
            candidates = [b for b in native_table_sections.get(structure.get('table_id'), [])
                if b.get('structure', {}).get('row_start', 0) < structure.get('row_start', 0)
                and (not peer or b.get('order', -1) > peer.get('order', -1))]
            if candidates:
                heading = max(candidates, key=lambda b: b.get('structure', {}).get('row_start', 0))
                local_ids = {b['block_id'] for b in native_table_sections[structure['table_id']]}
                # Replace an inherited sibling scope rather than accumulating
                # every preceding section or duplicating the printed number.
                path = [node for node in path if not any(r['block_id'] in local_ids for r in node['refs'])]
                path.append(section_node([full_ref(heading['block_id'])], len(path)))
        field["section_path"] = path
        if path:
            field["section_refs"] = path[-1]["refs"]
            field["section_text_original"] = path[-1]["text_original"]
            # Previously inferred headings may have included definition prose.
            # Rebuild this derived context from the verified heading path.
            if not field.get('record_index'): field["context_label"] = field["section_text_original"]
        else:
            field['section_refs'] = []
            field['section_text_original'] = ''
            # Once a proximity-derived heading is disproved, its old text must
            # not survive as a hidden matching scope. Preserve source-backed
            # contexts and explicit human mappings.
            if rejected_question_context and not field.get('record_index') and not field.get('context_refs'):
                from .word_form_ownership import _protected
                if not _protected(field): field['context_label'] = ''
        for child in field.get("children", []): attach_sections(child,path)
    for field in questions: attach_sections(field)

    def make_document_tree(fields):
        roots, nodes = [], {}
        for field in fields:
            container = roots
            for section in field.get("section_path", []):
                key = section["section_id"]
                node = nodes.get(key)
                if node is None:
                    node = {"node_type": "section", **deepcopy(section), "children": []}
                    nodes[key] = node
                    container.append(node)
                container = node["children"]
            container.append({"node_type": "field", "field_id": field.get("field_id", field["question_id"])})
        return roots

    _apply_special_table_fields(questions, source_blocks, lookup)
    if model_info is not None:
        complete_local_fields(questions, source_blocks, lookup)
        from .word_control_groups import restore_word_control_groups
        restore_word_control_groups(questions, source_blocks, lookup)
        from .word_bullet_answer_groups import restore_word_bullet_answer_groups
        restore_word_bullet_answer_groups(questions, source_blocks, lookup)
        from .nested_response_controls import restore_nested_response_controls
        restore_nested_response_controls(questions, source_blocks, lookup)
        from .word_compound_choices import restore_word_compound_choices, restore_word_floating_choices
        restore_word_compound_choices(questions, source_blocks, lookup)
        restore_word_floating_choices(questions, source_blocks, lookup)
        from .response_prompt_groups import restore_response_prompt_groups
        restore_response_prompt_groups(questions, source_blocks, lookup)
        from .word_label_slots import restore_word_label_slots
        restore_word_label_slots(questions, source_blocks, lookup)
        from .word_form_ownership import restore_word_form_ownership
        non_qa.extend(restore_word_form_ownership(questions, source_blocks, lookup))
        from .word_text_positions import restore_word_text_positions
        restore_word_text_positions(questions, source_blocks, lookup)
        from .word_readonly_context import resolve_word_readonly_context
        non_qa.extend(resolve_word_readonly_context(questions,source_blocks,lookup))
        from .word_ordinal_responses import restore_ordinal_responses
        non_qa.extend(restore_ordinal_responses(questions, source_blocks, lookup))
        from .numbered_condition_routes import restore_numbered_condition_routes
        restore_numbered_condition_routes(questions, source_blocks, lookup)
        for source_block in source_blocks:
            structure = source_block.get('structure', {})
            if structure.get('native_section_marker_ref'):
                native_table_sections.setdefault(structure.get('table_id'), []).append(source_block)
        def mark_question_blocks(items):
            for item in items:
                if item.get('boundary_evidence') == 'native_paragraph_option_list':
                    explicit_question_blocks.update(ref['block_id'] for ref in item.get('label_refs',[]))
                if item.get('answer_region_refs'):
                    explicit_question_blocks.update(ref['block_id'] for ref in item.get('label_refs',[])
                        if lookup.get(ref['block_id'],{}).get('kind') == 'paragraph')
                mark_question_blocks(item.get('children', []))
        mark_question_blocks(questions)
        from .scanned_forms import restore_scanned_fields
        restore_scanned_fields(questions, source_blocks, lookup)
        from .scanned_cells import restore_scanned_cell_pairs
        restore_scanned_cell_pairs(questions, source_blocks, lookup)
        from .scanned_local_tables import restore_scanned_local_tables
        extracted.setdefault('metadata', {})['scanned_local_tables'] = restore_scanned_local_tables(questions, source_blocks, lookup)
        from .native_pdf_forms import restore_native_pdf_lanes
        restore_native_pdf_lanes(questions,source_blocks,lookup)
        from .native_pdf_blank_tails import restore_native_pdf_blank_tails
        restore_native_pdf_blank_tails(questions,source_blocks,lookup)
        from .ocr_inline_fields import restore_ocr_inline_fields
        restore_ocr_inline_fields(questions,source_blocks,lookup)
        from .pdf_section_banners import resolve_pdf_section_banners
        resolve_pdf_section_banners(questions,source_blocks,lookup)
        from .native_vector_forms import restore_native_vector_fields
        restore_native_vector_fields(questions,source_blocks,lookup)
        from .native_pdf_choice_rows import restore_native_pdf_choice_rows
        restore_native_pdf_choice_rows(questions,source_blocks,lookup)
        from .pdf_attachment_groups import restore_pdf_attachment_groups
        restore_pdf_attachment_groups(questions,source_blocks,lookup)
        from .native_pdf_prose_boundaries import resolve_native_pdf_prose
        non_qa.extend(resolve_native_pdf_prose(questions,source_blocks,lookup))
        from .mixed_overlay_boundaries import restore_mixed_overlay_boundaries
        extracted.setdefault('metadata', {})['mixed_overlay_boundaries'] = restore_mixed_overlay_boundaries(questions,source_blocks,lookup)
        from .scanned_context_groups import resolve_scanned_context_groups
        non_qa.extend(resolve_scanned_context_groups(questions,source_blocks,lookup))
        from .scanned_fixed_prose import resolve_scanned_fixed_prose
        non_qa.extend(resolve_scanned_fixed_prose(questions,source_blocks,lookup,extracted.get('source_hash','')))
        from .scanned_choice_boundaries import restore_scanned_choice_boundaries
        non_qa.extend(restore_scanned_choice_boundaries(questions,source_blocks,lookup))
        from .scanned_ruled_partitions import restore_scanned_ruled_partitions
        restore_scanned_ruled_partitions(questions,source_blocks,lookup)
        from .scan_matrix_sections import resolve_matrix_section_bands
        extracted.setdefault('metadata', {})['scan_matrix_sections'] = resolve_matrix_section_bands(questions,source_blocks,lookup)
        if any(f.get('boundary_evidence')=='native_source_record_cell' for f in questions):
            questions.sort(key=lambda f: lookup[(f.get('answer_region_refs') if f.get('boundary_evidence')=='native_source_record_cell' else
                                                 f.get('label_refs') or f.get('question_refs'))[0]['block_id']].get('order',10**9))
    for field in questions: attach_sections(field)
    apply_word_group_context(questions, word_table_groups, lookup)
    normalize_choice_fields(questions, lookup)
    from ..local_condition_scopes import group_local_conditions
    group_local_conditions(questions,source_blocks,extracted.get('source_hash',''))
    from .choice_label_boundaries import separate_choice_label_responses
    non_qa.extend(separate_choice_label_responses(questions,lookup))
    if extracted.get('file_type','').lower() == 'pdf':
        from .native_overlay_value_typography import restore_native_value_typography
        extracted.setdefault('metadata',{})['native_value_typography'] = restore_native_value_typography(questions,lookup)
    _apply_pairing_validation(questions, lookup)
    if extracted.get('file_type','').lower() == 'pdf':
        from .matrix_record_ownership import reconcile_matrix_record_ownership
        extracted.setdefault('metadata', {})['matrix_record_ownership'] = reconcile_matrix_record_ownership(questions, lookup)
        from .pdf_furniture_ownership import reconcile_pdf_furniture_ownership
        furniture = reconcile_pdf_furniture_ownership(questions, lookup)
        non_qa.extend(furniture.pop('non_qa_items'))
        extracted.setdefault('metadata', {})['pdf_furniture_ownership'] = furniture
        from .scan_closed_local_grids import reconcile_scanned_local_grids
        extracted.setdefault('metadata', {})['scan_closed_local_grids'] = reconcile_scanned_local_grids(questions, source_blocks, lookup)
        from .scan_section_boundaries import reconcile_scan_section_boundaries
        non_qa.extend(reconcile_scan_section_boundaries(questions,source_blocks,lookup))
        from .scan_business_grids import reconcile_business_grids
        extracted.setdefault('metadata', {})['scan_business_grids'] = reconcile_business_grids(questions,source_blocks,lookup)
        from .scan_inline_response_regions import reconcile_inline_response_regions
        extracted.setdefault('metadata', {})['scan_inline_response_regions'] = reconcile_inline_response_regions(questions,source_blocks,lookup,non_qa)
        from .scan_parallel_forms import reconcile_parallel_forms
        extracted.setdefault('metadata', {})['scan_parallel_forms'] = reconcile_parallel_forms(questions,source_blocks,lookup,non_qa)
        from .pdf_submission_groups import reconcile_completion_positions, reconcile_inline_source_options
        extracted.setdefault('metadata',{})['completion_line_positions']=reconcile_completion_positions(questions,source_blocks,lookup,non_qa=non_qa)
        extracted.setdefault('metadata',{})['inline_source_options']=reconcile_inline_source_options(questions,source_blocks,lookup)
        # Local builders above create new child/record fields after the first
        # chapter pass. Fill only missing source-backed scopes now; customer
        # sections and explicit/manual section choices retain precedence.
        reconcile_scan_section_boundaries(questions,source_blocks,lookup,missing_only=True)
        from .native_table_sections import inherit_native_table_sections
        extracted.setdefault('metadata',{})['native_table_sections']=inherit_native_table_sections(questions,source_blocks,lookup)
        from .scanned_table_sections import reconcile_scanned_table_sections
        extracted.setdefault('metadata',{})['scanned_table_sections']=reconcile_scanned_table_sections(
            questions, source_blocks, lookup, sections=sections, non_qa=non_qa)
        _apply_pairing_validation(questions, lookup)
    from .section_ownership import reconcile_response_sections
    extracted.setdefault('metadata', {})['section_response_ownership'] = reconcile_response_sections(questions,sections,lookup)
    from .nonanswerable_source_roles import reconcile_nonanswerable_source_roles
    extracted.setdefault('metadata', {})['nonanswerable_source_roles'] = reconcile_nonanswerable_source_roles(questions,sections,lookup,non_qa)
    from .title_contour_relations import reconcile_title_contour_relations
    extracted.setdefault('metadata', {})['title_contour_relations'] = reconcile_title_contour_relations(questions,sections,lookup,non_qa)
    from .word_paragraph_sections import reconcile_word_paragraph_sections
    extracted.setdefault('metadata', {})['word_paragraph_sections'] = reconcile_word_paragraph_sections(
        questions, source_blocks, lookup, assignments, sections, non_qa)
    for field in questions:
        stack = [field]
        while stack:
            item = stack.pop()
            original_status = item.get("answer_status", "not_located")
            item["original_answer_status"] = original_status
            item["original_answer"] = item.get("answer_text_original") if original_status == "filled" else None
            item["answer_origin"] = "source_document" if original_status == "filled" and item.get("pairing_validation") == "valid" and item.get("answer_value_refs") else "none"
            definition = build_form_definition(item, lookup, extracted.get("source_hash", ""), extracted.get("parser_version", ""))
            item["form_definition"] = definition
            for key in ("field_type", "fill_responsibility", "required_status", "applicability_condition", "fillable_status", "auto_fill_eligible", "allowed_write_methods", "location_evidence"):
                item[key] = deepcopy(definition[key])
            stack.extend(item.get("children") or [])
    fill_position_inventory = []
    for field in questions:
        fill_position_inventory.append(deepcopy(field["form_definition"]))
        fill_position_inventory.extend(deepcopy(child["form_definition"]) for child in field.get("children") or [] if child.get("form_definition"))
    document_tree = make_document_tree(questions)

    for item in assignments:
        if item["role"] in {"note", "signature_field"} and not item["group_id"]:
            non_qa.append({"role": item["role"], "refs": [item["ref"]], "text_original": _text([item["ref"]], lookup), "reason": item["reason"]})
        if item["role"] == "unresolved" and not item["group_id"]:
            unresolved_items.append({"refs": [item["ref"]], "reason": item["reason"] or "内容角色不确定", "kind": "semantic"})

    if invalid:
        warnings.append({"code": "invalid_model_references", "message": "模型返回了无效原文引用，已拒绝。", "details": invalid})
    if unresolved_items:
        warnings.append({"code": "unresolved_content", "message": f"存在 {len(unresolved_items)} 个未覆盖或不确定区域。"})
    status = extracted.get("extraction_status", "complete")
    if model_info is None:
        status = "original_only"
    elif warnings or unresolved_items or any(q["review_status"] == "needs_review" for q in questions):
        status = "partial"
    diagnostic_trace = []
    for block in source_blocks:
        roles = [{"semantic_role": x["role"], "layout_role": x.get("layout_role"), "relation_status": x["relation_status"], "reason": x["reason"]} for x in assignments if x["ref"]["block_id"] == block["block_id"]]
        links = []
        for field in questions:
            for relation, key in (("label", "label_refs"), ("answer_region", "answer_region_refs"), ("answer_value", "answer_value_refs"), ("unit", "unit_refs"), ("section", "section_refs")):
                if any(x["block_id"] == block["block_id"] for x in field.get(key, [])): links.append({"field_id": field["field_id"], "relation": relation})
            for child in field.get("children", []):
                for relation, key in (("label", "label_refs"), ("answer_region", "answer_region_refs"), ("answer_value", "answer_value_refs"), ("unit", "unit_refs")):
                    if any(x["block_id"] == block["block_id"] for x in child.get(key, [])): links.append({"field_id": child["field_id"], "relation": relation})
        unresolved_for_block = [x.get("reason") for x in unresolved_items if any(r["block_id"] == block["block_id"] for r in x.get("refs", []))]
        structure = block.get("structure", {})
        diagnostic_trace.append({
            "block_id": block["block_id"],
            "physical_cell": {
                "cell_id": structure.get("cell_id"), "table_id": structure.get("table_id"),
                "row_start": structure.get("row_start"), "row_end": structure.get("row_end"),
                "grid_col_start": structure.get("grid_col_start", structure.get("column_start")),
                "grid_col_end": structure.get("grid_col_end", structure.get("column_end")),
                "merge_anchor_id": structure.get("merge_anchor_id", structure.get("anchor_cell_id")),
                "blank_state": structure.get("blank_state"), "content_state": structure.get("content_state"),
                "text": block.get("text", ""),
            } if structure.get("cell_id") else None,
            "extraction": "preserved", "classifications": roles, "field_links": links,
            "unresolved_reasons": unresolved_for_block,
            "final_state": "linked" if links else ("unresolved" if unresolved_for_block else "classified_non_field"),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "document_id": extracted["document_id"], "source_hash": extracted["source_hash"],
        "file_type": extracted.get("file_type"), "parser": extracted.get("parser"),
        "parser_version": extracted.get("parser_version"), "extraction_metadata": extracted.get("metadata", {}),
        "extraction_status": status, "sections": sections, "source_blocks": source_blocks,
        "questions": questions, "document_tree": document_tree, "fill_position_inventory": fill_position_inventory,
        "form_position_version": FORM_POSITION_VERSION, "non_qa_items": non_qa, "unresolved_items": unresolved_items,
        "warnings": warnings, "model": model_info, "validation": {
            "invalid_references": len(invalid), "unresolved_ranges": len(unresolved_items),
            "input_blocks": len(source_blocks), "classified_assignments": len(assignments),
            "coverage_complete": not any(x.get("kind") == "uncovered" for x in unresolved_items),
        }, "diagnostic_trace": diagnostic_trace,
    }


def validate_correction(result: dict, corrected: dict):
    lookup = {b["block_id"]: b for b in result["source_blocks"]}
    for question in corrected.get("questions", []):
        if question.get("answer_status") not in ANSWER_STATUSES:
            raise ValueError("答案状态不合法")
        for key in ("question_refs", "answer_refs", "section_refs", "instruction_refs", "attachment_refs"):
            if any(not _valid_ref(ref, lookup) for ref in question.get(key, [])):
                raise ValueError(f"{key} 包含无效原文引用")
        question["question_text_original"] = _text(question.get("question_refs", []), lookup)
        question["answer_text_original"] = _text(question.get("answer_refs", []), lookup)
        question["review_status"] = "human_corrected"
    corrected["source_blocks"] = result["source_blocks"]
    corrected["source_hash"] = result["source_hash"]
    corrected["document_id"] = result["document_id"]
    corrected["schema_version"] = SCHEMA_VERSION
    return corrected
