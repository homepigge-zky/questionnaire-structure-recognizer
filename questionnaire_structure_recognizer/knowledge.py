from __future__ import annotations

import hashlib
import re
import unicodedata
from copy import deepcopy
from .validation.selection_evidence import selected_column_caption
from .source_pair_guards import (shared_response_targets, REUSE_CONSTRAINT_POLICY,
                                 SHARED_TARGET_RESTRICTION, SHARED_TARGET_REASON)


from .source_pair_guards import reviewed_value_restrictions


def source_reuse_restrictions(field: dict, shared_targets=None, source_lookup=None) -> list[str]:
    definition = field.get('form_definition') or {}
    restrictions = []
    if shared_targets:
        restrictions.append(SHARED_TARGET_RESTRICTION)
    if definition.get('field_type') == 'manual_signature' or definition.get('requires_signatory_confirmation'):
        restrictions.append('source_signatory_requires_current_confirmation')
    if definition.get('requires_current_person_confirmation'):
        restrictions.append('source_person_requires_current_confirmation')
    if definition.get('requires_current_attachment_confirmation'):
        restrictions.append('source_attachment_requires_current_confirmation')
    if definition.get('fill_responsibility') in {'customer', 'reviewer', 'shared'}:
        restrictions.append('source_not_exclusively_our_responsibility')
    if field.get("answer_qualification_refs"):
        restrictions.append("source_choice_requires_qualification")
    from .source_part_constraints import part_exception_refs, CODE
    if part_exception_refs(field, source_lookup or {}):
        restrictions.append(CODE)
    prompts = [field.get("question_text_original", "")]
    for ref in field.get('instruction_refs') or definition.get('instruction_refs') or []:
        raw = (source_lookup or {}).get(ref.get('block_id'), {}).get('text', '')
        a, z = ref.get('start'), ref.get('end')
        if type(a) is int and type(z) is int and 0 <= a < z <= len(raw):
            prompts.append(raw[a:z])
    return reviewed_value_restrictions(field.get("answer_text_original"), restrictions, '\n'.join(prompts))


def answer_value_consistency_reasons(question: str, answer: str) -> list[str]:
    """Flag explicit source-value contradictions without rewriting the source.

    A citation proves where the value came from, not that it answers the field.
    Keep this deliberately narrow: identified registration-number fields with
    full postal addresses, and a direct numeric biological-unit family clash.
    General prose, ordinary numbers and addresses in address fields are not
    rejected. The current reviewed text is checked so corrections can proceed.
    """
    question = unicodedata.normalize("NFKC", question or "").strip()
    answer = unicodedata.normalize("NFKC", answer or "").strip()
    if not question or not answer:
        return []
    reasons = []
    expected_units = {match.group(1).casefold() for match in re.finditer(r"\b(CFU|EU)\s*/\s*(?:mg|g|ml|l)\b", question, re.I)}
    direct_measurement = re.fullmatch(r"\s*[<>=≤≥~≈]*\s*\d+(?:[.,]\d+)?(?:\s*[-–]\s*\d+(?:[.,]\d+)?)?\s*(CFU|EU)\s*/\s*(?:mg|g|ml|l)\s*[.]?\s*", answer, re.I)
    if len(expected_units) == 1 and direct_measurement and direct_measurement.group(1).casefold() not in expected_units:
        expected = next(iter(expected_units)).upper()
        actual = direct_measurement.group(1).upper()
        reasons.append(f"原文答案单位与题目要求的量不一致：题目为 {expected}，答案为 {actual}。保留原值，须核实或修正后才能入库复用。")
    label = re.sub(r"^\s*\d+(?:\.\d+)*[.):、]\s*", "", question).strip().rstrip(":：")
    label = re.sub(r"\s*\((?:if\s+any|如有|如果有)\)\s*$", "", label, flags=re.I).strip().rstrip(":：")
    registration_number = bool(re.fullmatch(
        r"(?:(?:company|site|business|tax|manufacturer|facility)\s*[/&]?\s*)*registration\s*(?:#|no\.?|number)", label, re.I)
        or re.fullmatch(r"(?:企业|公司|场所)?(?:注册号|注册编号|登记编号)", label))
    english_address = bool(re.match(r"^(?:No\.?\s+)?\d+[A-Za-z]?\s+", answer, re.I)
        and re.search(r"\b(?:avenue|ave\.?|road|rd\.?|street|st\.?|boulevard|blvd\.?|lane|drive)\b", answer, re.I)
        and answer.count(",") >= 2)
    chinese_address = bool(len(re.findall(r"[省市县区]", answer)) >= 2
        and re.search(r"(?:路|街|大道).*\d+\s*号", answer))
    if registration_number and (english_address or chinese_address):
        reasons.append("原文答案呈完整邮寄地址，而当前字段明确要求注册或登记编号。保留原值，不自动挪到地址字段；请核实或修正后入库。")
    # Bare booleans cannot provide names, lists or requested explanations.
    binary_answer = answer.casefold().strip(".。 ") in {"yes", "no", "是", "否"}
    direct_detail = re.match(r"^(?:(?:please|kindly)\s*)?(?:name|list|provide|specify|state|explain|describe|mention)\s+", label, re.I)
    asks_boolean = re.search(r"\b(?:whether|yes\s*[/|]\s*no)\b", label, re.I)
    if binary_answer and direct_detail and not asks_boolean:
        reasons.append("原问题要求具体名称、清单或说明，仅是/否不能完整回答，请补充原文证据或人工核对。")
    condition_word = "yes" if answer.casefold().strip(".。 ") in {"yes", "是"} else "no"
    requires_detail = re.search(
        rf"\bif\s*{condition_word}\s*[,;:]?\s*(?:please\s*)?"
        r"(?:provide|specify|state|explain|describe|list|enclose|attach|mention|what|which|how)\b", label, re.I)
    if binary_answer and requires_detail:
        reasons.append("原问题在该选项后还要求补充内容，单独是/否覆盖不完整，需核对完整答复。")
    # Only an explicit continent slot and an agency-only list establish this
    # mismatch. Geographic prose, abbreviations or unknown values are not rewritten.
    continent_slot = re.search(r"(?:\(\s*continents?\s*\)|continent\s+of\s+residence|所在(?:大)?洲)", question, re.I)
    agency_list = re.fullmatch(r"[A-Z][A-Z0-9]{1,9}(?:\s*[,;/、]\s*[A-Z][A-Z0-9]{1,9})*(?:\s*,?\s*(?:etc\.?|等))?\.?", answer)
    known_agency = re.search(r"\b(?:NMPA|FDA|PMDA|MHRA)\b", answer)
    geography = re.search(r"\b(?:EU|US|USA|NA|APAC|EMEA|LATAM|ASEAN)\b", answer)
    if continent_slot and agency_list and known_agency and not geography:
        reasons.append("原文要求填写所在洲，历史回答仅列出机构缩写，请核实地理范围后再复用。")
    return reasons


def extract_pending_entries(task: dict, version: int, result: dict) -> list[dict]:
    """Create review drafts only from answer text that is cited in the source document."""
    drafts: list[dict] = []
    shared_targets = shared_response_targets(result)
    source_lookup = {block["block_id"]: block for block in result.get("source_blocks") or []}
    table_lookup = {table["table_id"]: table for table in result.get("extraction_metadata", {}).get("tables", [])}
    all_fields: list[dict] = []

    def collect(field: dict) -> None:
        all_fields.append(field)
        for child in field.get("children") or []:
            collect(child)

    for question_item in result.get("questions") or []:
        collect(question_item)
    label_owners: dict[str, set[str]] = {}
    label_ranges = {}
    for item in all_fields:
        owner = str(item.get("field_id") or item.get("question_id") or "")
        for ref in item.get("label_refs") or item.get("question_refs") or []:
            label_owners.setdefault(ref.get("block_id", ""), set()).add(owner)
            label_ranges.setdefault(ref.get('block_id',''),[]).append((ref.get('start',0),ref.get('end',0),owner))

    def valid_text_ref(ref):
        block=source_lookup.get(ref.get('block_id'))
        start,end=ref.get('start'),ref.get('end')
        return bool(block and isinstance(start,int) and isinstance(end,int) and 0<=start<end<=len(block.get('text','')) and block['text'][start:end].strip())

    def overlaps_label(ref, exclude_owner=None):
        return any(owner != exclude_owner and max(ref['start'],left)<min(ref['end'],right) for left,right,owner in label_ranges.get(ref['block_id'],[]))

    def definite_header(ref: dict) -> bool:
        block = source_lookup.get(ref.get("block_id"), {})
        structure = block.get("structure") or {}
        table = table_lookup.get(structure.get("table_id"), {})
        return bool(structure.get("potential_column_header") and table.get("layout_hint") == "data_table_candidate" and table.get("blank_response_row_count", 0) == 0)

    def question_relation_is_usable(field_id: str, question: str, refs: list[dict], field: dict) -> bool:
        if not question.strip() or not refs:
            return False
        if re.fullmatch(r"\s*\d+(?:\.\d+)*[.)]?\s*", question.splitlines()[0]):
            return False
        def verified_shared_choice_label(ref):
            from .validation.scan_ruled_choice_groups import verified_shared_parent_label
            others={owner for left,right,owner in label_ranges.get(ref['block_id'],[])
                    if owner!=field_id and max(ref['start'],left)<min(ref['end'],right)}
            if not others:return False
            for identity in others:
                matching=[f for f in all_fields if f.get('field_id')==identity]
                if len(matching)!=1:return False
                other=matching[0]
                if not (verified_shared_parent_label(field,other,source_lookup)
                        or verified_shared_parent_label(other,field,source_lookup)):return False
            return True
        def verified_record_header(ref):
            source_header=source_lookup.get(ref.get('block_id'),{})
            header=source_header.get('structure',{})
            regions=[source_lookup.get(r.get('block_id'),{}).get('structure',{}) for r in field.get('answer_region_refs',[])]
            if header.get('resolved_layout_role') == 'record_column_header':
                # A scanned record column has one physical printed caption
                # shared by several real response boxes. It lacks Word grid
                # indices; validate its source associations and geometry,
                # without promoting the observed text out of pending review.
                ref_key=lambda r:(r.get('block_id'),r.get('start'),r.get('end'))
                location=source_header.get('location',{}); box=location.get('bbox')
                region_refs=field.get('answer_region_refs',[])
                if location.get('type') == 'pdf' and box and len(region_refs) == 1:
                    rr=region_refs[0]; region=source_lookup.get(rr.get('block_id'),{})
                    relation=region.get('structure',{}); target=region.get('location',{})
                    target_box=target.get('bbox'); column=relation.get('record_column_region_refs') or []
                    matching=(relation.get('source_method')=='raster_geometry'
                        and relation.get('scan_association_valid') is True
                        and relation.get('scan_field_id')==field_id
                        and ref_key(relation.get('record_header_ref') or {})==ref_key(ref)
                        and relation.get('ocr_value_refs')==field.get('answer_value_refs')
                        and len(column)>=2 and sum(ref_key(r)==ref_key(rr) for r in column)==1
                        and target.get('type')=='pdf' and target.get('page')==location.get('page'))
                    if matching and target_box:
                        column_boxes=[source_lookup.get(r.get('block_id'),{}).get('location',{}) for r in column]
                        same_column=all(c.get('page')==location.get('page') and c.get('bbox')
                            and abs(c['bbox'][0]-target_box[0])<=2 and abs(c['bbox'][2]-target_box[2])<=2
                            for c in column_boxes)
                        below_header=(target_box[1]>=box[3]-2 and target_box[0]-2<=box[0]
                                      and box[2]<=target_box[2]+2)
                        values=[source_lookup.get(r.get('block_id'),{}).get('location',{})
                                for r in field.get('answer_value_refs',[])]
                        values_inside=bool(values) and all(v.get('page')==target.get('page') and v.get('bbox')
                            and target_box[0]-2<=v['bbox'][0] and target_box[1]-2<=v['bbox'][1]
                            and v['bbox'][2]<=target_box[2]+2 and v['bbox'][3]<=target_box[3]+2 for v in values)
                        if same_column and below_header and values_inside:return True
            if regions and all(s.get('comment_header_block_id')==ref['block_id'] and s.get('table_id')==header.get('table_id') and s.get('grid_col_start')==header.get('grid_col_start') for s in regions):return True
            if field.get('record_index') is None or header.get('local_layout_type')!='record_table_header':return False
            regions=[source_lookup.get(r.get('block_id'),{}).get('structure',{}) for r in field.get('answer_region_refs',[])]
            return bool(regions) and all(s.get('table_id')==header.get('table_id') and s.get('local_layout_type')=='record_table' and s.get('record_header_row')==header.get('row_start') and s.get('grid_col_start')==header.get('grid_col_start') for s in regions)
        return all(valid_text_ref(ref) and (verified_record_header(ref) or
            (not definite_header(ref) and (not overlaps_label(ref,field_id) or verified_shared_choice_label(ref)))) for ref in refs)

    def selected_column_caption_is_usable(ref: dict, field: dict) -> bool:
        """A caption is a value only with an independently selected native cell.

        A model's ``selected=true`` alone cannot authorize an option caption.
        The actual response cell must belong to the same source table/column
        and contain a checked control or an isolated selection mark.
        """
        return selected_column_caption(ref, field, source_lookup)

    def cited_answer_is_usable(field_id: str, refs: list[dict], field: dict) -> bool:
        for ref in refs:
            if not valid_text_ref(ref):
                return False
            if definite_header(ref) and not selected_column_caption_is_usable(ref, field):
                return False
            if overlaps_label(ref):
                return False
            block = source_lookup.get(ref.get("block_id"), {})
            text = str(block.get("text") or "")[ref.get("start", 0):ref.get("end", 0)]
            if len(re.findall(r"[☐□☑☒✓✔]", text)) >= 2:
                selected = bool(re.search(r"[☑☒✓✔]", text)) or any(control.get("selected") is True for control in block.get("controls") or [])
                if not selected:
                    return False
        return True

    def visit(field: dict, parent_field_id: str | None = None) -> None:
        question = str(field.get("question_text_original") or "").strip()
        answer = str(field.get("answer_text_original") or "").strip()
        value_refs = field.get("answer_value_refs") or field.get("answer_refs") or []
        origin = field.get("answer_origin") or "source_document"
        observation = field.get('source_observation') or {}
        observed_pending_source = (origin == 'none' and not field.get('manual_override')
            and observation.get('origin') == 'source_document'
            and observation.get('status') == 'pending_review'
            and observation.get('answer_value_refs') == value_refs
            and bool(value_refs))
        # Knowledge-filled or manually typed values without a source-document
        # range must never cycle back into the knowledge base.
        field_id = str(field.get("field_id") or field.get("question_id") or "")
        question_refs = field.get("label_refs") or field.get("question_refs") or []
        pairing_validation = str(field.get("pairing_validation") or field.get("validation_status") or "unvalidated")
        shared = shared_targets.get(field_id, [])
        if shared:
            pairing_validation = 'needs_review'
        if observed_pending_source and pairing_validation == 'valid':
            pairing_validation = 'needs_review'
        relation_usable = question_relation_is_usable(field_id, question, question_refs, field)
        answer_usable = cited_answer_is_usable(field_id, value_refs, field)
        if answer_usable:
            cited='\n'.join(source_lookup[r['block_id']]['text'][r['start']:r['end']] for r in value_refs).strip()
            if field.get('native_value_typography'):
                from .validation.native_overlay_value_typography import verified_value
                cited = verified_value(field,source_lookup)
            answer_usable = bool(cited) and cited == answer
        if pairing_validation == "valid" and (not relation_usable or not answer_usable):
            pairing_validation = "invalid"
        # Keep suspect source pairs visible in the review queue.  Their
        # pairing_validation blocks approval; dropping them would hide the
        # parser/model failure from the reviewer.
        if question and answer and field.get("answer_status") == "filled" and value_refs and (origin == "source_document" or observed_pending_source) and relation_usable and answer_usable:
            material = f"{task['id']}|{version}|{field_id}|{answer}".encode("utf-8")
            identifier = "draft-" + hashlib.sha256(material).hexdigest()[:32]
            ambiguity_reasons = list(field.get("validation_reasons") or field.get("review_reasons") or [])
            if shared:
                ambiguity_reasons.append(SHARED_TARGET_REASON)
            if observed_pending_source:
                ambiguity_reasons.append('已读取可定位的原文值，但文字或关系仍待审核；该来源观察不是已确认答案。')
            value_consistency = answer_value_consistency_reasons(question, answer)
            ambiguity_reasons.extend(value_consistency)
            reuse_restrictions = source_reuse_restrictions(field, shared, source_lookup)
            if pairing_validation != "valid":
                ambiguity_reasons.append("问题与答案来源关系未通过校验，修复或重新解析前禁止入库。")
            if not relation_usable:
                ambiguity_reasons.append("问题引用与其他字段边界冲突。")
            if not answer_usable:
                ambiguity_reasons.append("答案引用不是当前字段可用的独立原文范围。")
            drafts.append({
                "id": identifier,
                "question": question,
                "answer": answer,
                "section": str(field.get("section_text_original") or ""),
                "parent_field_id": parent_field_id,
                "review": "pending",
                "active": False,
                "version": 1,
                "general": False,
                "customer": "",
                "aliases": [],
                "ambiguous": pairing_validation != "valid" or bool(ambiguity_reasons),
                "answer_presence": "present",
                "pairing_validation": pairing_validation,
                "source": {
                    "task_id": task["id"],
                    "task_version": version,
                    "file": task["filename"],
                    "field_id": field_id,
                    "question_refs": deepcopy(field.get("label_refs") or field.get("question_refs") or []),
                    "answer_refs": deepcopy(value_refs),
                    "source_locations": deepcopy(field.get("source_locations") or {}),
                    "source_observation": deepcopy(observation) if observed_pending_source else None,
                    "reuse_restrictions": reuse_restrictions,
                    "reuse_constraint_policy": REUSE_CONSTRAINT_POLICY,
                    "shared_response_targets": deepcopy(shared),
                    "value_consistency_policy": "source-value-consistency-v1",
                    "value_consistency_reasons": value_consistency,
                    "whole_record_requirement": deepcopy((field.get('form_definition') or {}).get('whole_record_requirement')),
                },
                "review_reasons": ["由已填写历史问卷提取；必须人工确认问题与答案对应关系、适用范围和可否跨客户使用。", *ambiguity_reasons],
            })
        for child in field.get("children") or []:
            visit(child, str(field.get("field_id") or field.get("question_id") or ""))

    for question in result.get("questions") or []:
        visit(question)
    return drafts
