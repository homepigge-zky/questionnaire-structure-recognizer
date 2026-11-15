from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .semantic_fields import TOPIC_REGISTRY, classify_topic
from .source_ownership import OWNER_POLICY_VERSION, document_owner_evidence, non_authorizing_statement, pdf_local_review_owner_scope


FORM_POSITION_VERSION = "word-form-position-v22-native-prompt-response-subranges"
PDF_OWNER_SCOPE_VERSION = "pdf-owner-scope-v2-source-assessment-and-completer"
LOCAL_PERSONNEL_SCOPE_VERSION = "local-personnel-scope-v1-verified-caption-groups"


def _ref_key(ref: dict) -> tuple:
    return (ref.get("block_id"), ref.get("start", 0), ref.get("end", 0), ref.get("slot_id", ""))


def _overlaps(left: dict, right: dict) -> bool:
    if left.get("block_id") != right.get("block_id"):
        return False
    a, b = left.get("start", 0), left.get("end", 0)
    c, d = right.get("start", 0), right.get("end", 0)
    if not all(isinstance(v, int) for v in (a, b, c, d)): return False
    if a == b:
        return c < a < d  # An explicit insertion at the label boundary is safe.
    if c == d:
        return a < c < b
    return max(a, c) < min(b, d)


def _native_protected_refs(lookup: dict[str, dict]) -> list[dict]:
    """Protect native labels even when a model omitted their business field.

    These are physical layout clues, not a blacklist of words such as Address.
    Inline prompt labels are protected only over their own character ranges.
    """
    protected = []
    rows: dict[tuple, list[dict]] = {}
    for block in lookup.values():
        s = block.get("structure") or {}
        rows.setdefault((s.get("table_id"), s.get("row_start")), []).append(block)
    for block in lookup.values():
        s, text = block.get("structure") or {}, str(block.get("text") or "")
        if not text or s.get("is_merge_continuation"):
            continue
        whole = {"block_id": block["block_id"], "start": 0, "end": len(text)}
        precise=s.get('protected_label_ranges',[])
        if precise:
            protected.extend({'block_id':block['block_id'],**r} for r in precise)
            # A source-verified in-cell form protects the actual labels, not
            # every checkbox and write-in region in their containing cell.
            continue
        role = s.get("semantic_role_hint") or s.get("semantic_role")
        slots = s.get("logical_slots") or []
        has_inline_labels = any(isinstance(slot.get("label_start"), int) and isinstance(slot.get("label_end"), int) for slot in slots)
        if (role in {"field_label", "question", "section", "table_header"} or s.get("layout_role") in {"question_label", "column_header"}) and not has_inline_labels:
            protected.append(whole)
        question_column = s.get("question_column")
        if question_column is not None and question_column == s.get("grid_col_start"):
            protected.append(whole)
        for slot in slots:
            a, b = slot.get("label_start"), slot.get("label_end")
            if isinstance(a, int) and isinstance(b, int) and 0 <= a < b <= len(text):
                protected.append({"block_id": block["block_id"], "start": a, "end": b})
        # Older parser versions supplied only a trailing slot for short prompt
        # lines, e.g. "Total;\n総数". Protect the prompt, never infer its value.
        first_line = text.splitlines()[0]
        if any(slot.get("kind") == "trailing_fill_candidate" for slot in slots) and re.fullmatch(r"[^:：;；\n]{1,65}[:：;；]\s*", first_line):
            protected.append({"block_id": block["block_id"], "start": 0, "end": len(first_line.rstrip())})
        # Older extracted versions did not tag the question column. A separate
        # question-number cell immediately before a prose cell is strong source
        # evidence. Never apply this to records (numeric values are normal there).
        if s.get("local_layout_type") not in {"record_table", "records", "record"}:
            previous = [b for b in rows.get((s.get("table_id"), s.get("row_start")), [])
                        if (b.get("structure") or {}).get("grid_col_end") == s.get("grid_col_start", 0) - 1]
            if any(re.fullmatch(r"\d+(?:\.\d+)+[.)]?", str(b.get("text") or "").strip()) for b in previous):
                protected.append(whole)
    # Broad native prompt ranges can precede a later physical split into an
    # in-cell label and response. Remove only independently source-verified
    # writable ink from this *native inference*. Explicit field label refs are
    # added by protected_source_refs afterwards and are never waived here.
    writable={}
    by_cell={b.get('structure',{}).get('cell_id'):b for b in lookup.values() if b.get('structure',{}).get('cell_id')}
    for block in lookup.values():
        s=block.get('structure',{});loc=block.get('location',{});text=block.get('text','');spans=[]
        if loc.get('type')=='pdf' and loc.get('extraction')=='native':
            from .validation.pdf_attachment_groups import verified_fill_line
            for slot in s.get('logical_slots') or []:
                if slot.get('kind')=='native_pdf_explicit_fill_line' and verified_fill_line(block,slot,lookup):
                    spans.append((slot['start'],slot['end']))
        if loc.get('type')=='docx' and s.get('local_layout_type') in {'record_table','records','record'}:
            columns=[c for c in s.get('record_columns') or [] if
                     (c.get('column_start'),c.get('column_end'))==(s.get('grid_col_start'),s.get('grid_col_end'))]
            if len(columns)==1:
                col=columns[0];head=by_cell.get(col.get('header_cell_id'),{});hs=head.get('structure',{});hl=head.get('location',{})
                header_ok=(head.get('kind')=='table_cell' and hl.get('type')=='docx' and
                    (hl.get('part'),hl.get('table'))==(loc.get('part'),loc.get('table')) and
                    hs.get('table_id')==s.get('table_id') and head.get('text')==col.get('text') and
                    hs.get('row_start')==hs.get('row_end')==s.get('record_header_row') and
                    type(s.get('row_start')) is int and type(hs.get('row_end')) is int and s['row_start']>hs['row_end'] and
                    (hs.get('grid_col_start'),hs.get('grid_col_end'))==(s.get('grid_col_start'),s.get('grid_col_end')))
                markers=list(re.finditer(r'[□☐☑☒]',text))
                if header_ok and len(markers)>=2 and not text[:markers[0].start()].strip():
                    spans.extend((m.start(),m.end()) for m in markers)
        if spans:writable[block['block_id']]=spans
    narrowed=[]
    for rr in protected:
        pieces=[(rr['start'],rr['end'])]
        for a,z in writable.get(rr['block_id'],[]):
            next_parts=[]
            for x,y in pieces:
                if z<=x or a>=y:next_parts.append((x,y))
                else:
                    if x<a:next_parts.append((x,a))
                    if z<y:next_parts.append((z,y))
            pieces=next_parts
        narrowed.extend({**rr,'start':a,'end':z} for a,z in pieces if a<z)
    return narrowed


def protected_source_refs(result: dict, lookup: dict[str, dict] | None = None) -> list[dict]:
    """All fixed label ranges, including unedited and omitted fields."""
    lookup = lookup or {b["block_id"]: b for b in result.get("source_blocks") or []}
    protected = _native_protected_refs(lookup)
    def visit(field):
        protected.extend(field.get("label_refs") or field.get("question_refs") or [])
        for child in field.get("children") or []: visit(child)
    for field in result.get("questions") or []: visit(field)
    return protected


def _text(refs: list[dict], lookup: dict[str, dict]) -> str:
    values = []
    for ref in refs:
        block = lookup.get(ref.get("block_id")) or {}
        text = str(block.get("text") or "")
        start, end = int(ref.get("start", 0)), int(ref.get("end", 0))
        if 0 <= start < end <= len(text):
            values.append(text[start:end])
    return "\n".join(values).strip()


def confirmed_template_ranges(definition: dict, lookup: dict[str, dict], source_hash: str) -> list[dict]:
    """Explicit approval applies to exact source-proven template words only."""
    if definition.get('mapping_review_status') != 'human_corrected' or definition.get('source_hash') != source_hash:
        return []
    approved = definition.get('confirmed_template_placeholders') or []
    candidates = definition.get('template_placeholder_candidates') or []
    valid = []
    for item in approved:
        if item not in candidates: return []
        block = lookup.get(item.get('block_id'), {})
        text = block.get('text',''); start,end = item.get('start'),item.get('end')
        if not isinstance(start,int) or not isinstance(end,int) or not 0 <= start < end <= len(text): return []
        if text[start:end] != item.get('expected_text'): return []
        slots = block.get('structure',{}).get('logical_slots',[])
        slot = next((s for s in slots if s.get('kind') == 'template_placeholder_candidate' and
            s.get('start') == start and s.get('end') == end and s.get('placeholder_text') == text[start:end]),None)
        if slot is None: return []
        valid.append({'block_id':block['block_id'],'start':start,'end':end,'slot_id':slot['slot_id']})
    return valid


def _without_approved_template_ranges(refs: list[dict], approved: list[dict]) -> list[dict]:
    remaining = deepcopy(refs)
    for area in approved:
        output = []
        for ref in remaining:
            if not _overlaps(ref,area): output.append(ref); continue
            if ref.get('start',0) < area['start']: output.append({**ref,'end':area['start']})
            if ref.get('end',0) > area['end']: output.append({**ref,'start':area['end']})
        remaining=output
    return remaining


def _context(field: dict, lookup: dict[str, dict]) -> str:
    refs = []
    for key in ("section_refs", "instruction_refs", "label_refs", "question_refs"):
        refs.extend(field.get(key) or [])
    return _text(refs, lookup)


def _compact_caption(text: str) -> str:
    # OCR occasionally removes spaces. This form is used only for exact short
    # caption recognition; the source text and offsets remain untouched.
    return re.sub(r'[\W_]+', '', str(text or '').casefold())


def _signature_caption(text: str) -> bool:
    caption = _compact_caption(text)
    if re.fullmatch(
        r'(?:representative)?signature(?:and(?:company)?(?:stamp|seal)|(?:and)?date)?'
        r'|(?:company)?(?:stamp|seal)|sign(?:and)?date|sign|signedby'
        r'|(?:公司|审核人|填表人|填写人)?(?:签字|签名|盖章|签章)(?:日期)?', caption):
        return True
    # Role-qualified signing captions are noun phrases, not audit questions
    # about obtaining/verifying signatures. Keep any explicit handwritten
    # qualifier in the source; strip it only for this caption classification.
    raw = str(text or '').strip()
    if '?' in raw or '？' in raw or re.search(
        r'\b(?:do|does|did|is|are|was|were|can|could|will|would|shall|should|has|have|what|which|who|where|when|how|describe|explain|policy|procedure|system|obtain|verify)\b', raw, re.I):
        return False
    raw = re.sub(r'\s*[:：]?\s*\((?:handwritten|wet[- ]ink|manual)\s+signature[^()]{0,80}\)\s*$', '', raw, flags=re.I).strip(' \t\r\n:：')
    return bool(re.fullmatch(r"(?:[\w’'&./-]+\s+){1,8}signature|signature\s+of\s+(?:[\w’'&./-]+\s*){1,8}", raw, re.I))


def _signatory_source_refs(field: dict, lookup: dict[str, dict]) -> list[dict]:
    """Source evidence for THIS document's signer, never general staff data.

    A model hint, topic classification, or document-wide supplier ownership
    cannot authorize signatures. Short metadata captions require an actual
    local signing caption, or a verified shared-line signing group.
    """
    from .validation.scanned_subfields import verified_scan_signatory_refs
    scan_refs = verified_scan_signatory_refs(field, lookup)
    if scan_refs and all(_signature_caption(_text([ref], lookup)) for ref in scan_refs):
        return scan_refs
    labels = field.get('label_refs') or field.get('question_refs') or []
    text = _text(labels, lookup)
    caption = _compact_caption(text)
    # A combined label may retain separate native "Designation" / "Company
    # Seal" lines. Protect the exact signing caption instead of allowing the
    # non-signing first line to hide the requested seal. Every other line must
    # itself be short signing metadata; a business question mentioning seals
    # is not a signing request.
    signing_lines = []
    metadata_lines = True
    for ref in labels:
        raw = str(lookup.get(ref.get('block_id'), {}).get('text') or '')
        start, end = ref.get('start'), ref.get('end')
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(raw)):
            metadata_lines = False
            break
        offset = start
        for line in raw[start:end].splitlines(keepends=True):
            if line.strip():
                a = offset + len(line) - len(line.lstrip())
                b = offset + len(line.rstrip())
                if _signature_caption(line.strip()):
                    signing_lines.append({**ref, 'start': a, 'end': b})
                elif not re.fullmatch(r'(?:printed)?name|designation|jobtitle|title|position|date|place|姓名|职务|职位|日期|地点', _compact_caption(line)):
                    metadata_lines = False
            offset += len(line)
    if metadata_lines and signing_lines:
        return signing_lines
    # A multiline native signing cell has one real prompt per Name / Title /
    # Signature line. Its signing evidence must identify another actual prompt
    # in that same cell, not a model hint or a document-wide signature mention.
    for candidate in field.get('signatory_context_refs') or []:
        source = lookup.get(candidate.get('block_id'), {})
        if source.get('location', {}).get('type') != 'docx' or source.get('kind') != 'table_cell':
            continue
        prompts = source.get('structure', {}).get('logical_slots') or []
        def prompt_ref(ref):
            return ref.get('block_id') == source.get('block_id') and any(
                slot.get('kind') == 'inline_prompt_candidate'
                and slot.get('label_start') == ref.get('start') and slot.get('label_end') == ref.get('end')
                for slot in prompts)
        if (prompt_ref(candidate) and _signature_caption(_text([candidate], lookup))
                and labels and all(prompt_ref(ref) for ref in labels)):
            return [deepcopy(candidate)]
    if re.fullmatch(r'(?:signee|signatory)(?:s)?(?:name|title|position)',caption):
        return deepcopy(labels)
    if _signature_caption(text) or re.fullmatch(
        r'(?:(?:the|this)?questionnaire(?:hasbeen|was)?)?(?:completed|filled|reviewed)by'
        r'|填表人|审核人', caption):
        return deepcopy(labels)
    # A name explicitly requested with a company stamp is not an ordinary
    # reusable company-name field, even when the provider is known.
    if re.fullmatch(r'(?:suppliers?|company|manufacturers?)namecompany(?:rubber)?(?:stamp|seal)', caption):
        return deepcopy(labels)
    if not re.fullmatch(r'place|date|place(?:and)?date|(?:printed)?name(?:(?:and)?title)?|title(?:department)?|position'
                        r'|functionwithinthecompany|姓名|职位|日期|地点', caption):
        return []
    anchors = [lookup[r['block_id']] for r in labels if r.get('block_id') in lookup]
    if not anchors:
        return []
    # A real, consecutive Name / Signature / Date table is already a signing
    # group. It need not repeat "completed and signed by" above the table.
    # Bound this evidence to its physical label lane and stop on a business
    # caption so a later production date cannot inherit the signing restriction.
    metadata = r'place|date|place(?:and)?date|(?:printed)?name(?:(?:and)?title)?|jobtitle|title|position|姓名|职位|日期|地点'
    for anchor in anchors:
        location, native = anchor.get('location') or {}, anchor.get('structure') or {}
        if location.get('type') != 'docx' or not native.get('table_id'):
            continue
        lane = sorted((block for block in lookup.values()
                       if block.get('location', {}).get('part') == location.get('part')
                       and block.get('structure', {}).get('table_id') == native['table_id']
                       and block.get('structure', {}).get('grid_col_start') == native.get('grid_col_start')
                       and not block.get('structure', {}).get('is_merge_continuation')
                       and str(block.get('text') or '').strip()),
                      key=lambda block: block.get('structure', {}).get('row_start', -1))
        position = next((index for index, block in enumerate(lane) if block['block_id'] == anchor['block_id']), None)
        if position is None:
            continue
        for direction in (-1, 1):
            index = position + direction
            while 0 <= index < len(lane):
                source = lane[index]
                if _signature_caption(source.get('text', '')):
                    return [{'block_id': source['block_id'], 'start': 0, 'end': len(source['text'])}]
                if not re.fullmatch(metadata, _compact_caption(source.get('text', ''))):
                    break
                index += direction
    evidence = []
    for candidate in field.get('signatory_context_refs') or []:
        source = lookup.get(candidate.get('block_id'), {})
        if not source or not _signature_caption(_text([candidate], lookup)):
            continue
        if any(a.get('location', {}).get('page') == source.get('location', {}).get('page')
               and a.get('location', {}).get('type') == source.get('location', {}).get('type') == 'pdf'
               for a in anchors):
            evidence.append(deepcopy(candidate))
    if evidence:
        return evidence
    for source in lookup.values():
        # A PDF's local Filled by / Title / Date group describes this copy's
        # completer. It is not reusable personnel data, even without the word
        # Signature. Only short, explicit captions above the metadata qualify.
        source_loc = source.get('location') or {}
        completion_caption = source_loc.get('type') == 'pdf' and bool(re.fullmatch(
            r'(?:(?:the|this)?questionnaire(?:hasbeen|was)?)?(?:completed|filled|reviewed)by'
            r'|填表人|审核人', _compact_caption(source.get('text', ''))))
        if not _signature_caption(source.get('text', '')) and not completion_caption:
            continue
        loc = source.get('location') or {}; box = loc.get('bbox')
        if loc.get('type') == 'docx':
            native = source.get('structure') or {}
            for anchor in anchors:
                aloc = anchor.get('location') or {}; anative = anchor.get('structure') or {}
                if aloc.get('type') != 'docx' or aloc.get('part') != loc.get('part'):
                    continue
                same_row = bool(native.get('table_id') and native.get('table_id') == anative.get('table_id')
                                and native.get('row_start') == anative.get('row_start'))
                paragraph, other = loc.get('paragraph'), aloc.get('paragraph')
                consecutive_paragraphs = (isinstance(paragraph, int) and isinstance(other, int)
                                          and abs(paragraph-other) <= 3)
                if same_row or consecutive_paragraphs:
                    evidence.append({'block_id': source['block_id'], 'start': 0, 'end': len(source['text'])})
                    break
            continue
        if loc.get('type') != 'pdf' or not isinstance(box, list) or len(box) != 4:
            continue
        for anchor in anchors:
            aloc = anchor.get('location') or {}; abox = aloc.get('bbox')
            if aloc.get('type') != 'pdf' or aloc.get('page') != loc.get('page') or not abox:
                continue
            same_row = abs((abox[1]+abox[3]-box[1]-box[3])/2) <= 15
            same_column = abs(abox[0]-box[0]) <= 35
            if completion_caption:
                if not same_column or not 0 <= abox[1]-box[1] <= 75:
                    continue
            if (same_row and abs(abox[0]-box[0]) <= 320) or (same_column and abs(abox[1]-box[1]) <= 90):
                evidence.append({'block_id': source['block_id'], 'start': 0, 'end': len(source['text'])})
                break
    return evidence


def _current_participant_source_refs(field: dict, lookup: dict[str,dict]) -> list[dict]:
    """This audit's people/dates require confirmation, not generic contact reuse.

    Use explicit short captions or an exact physical row header. Questions
    about auditor qualifications and ordinary company contacts do not qualify.
    """
    from .validation.pdf_submission_groups import current_completer_refs
    completion_refs=current_completer_refs(field,lookup)
    if completion_refs:return completion_refs
    from .source_ownership import responder_metadata_evidence
    responder_refs = responder_metadata_evidence(field, lookup)
    if responder_refs:
        return responder_refs
    from .validation.closing_review import closing_participant_refs
    closing_refs = closing_participant_refs(field, lookup)
    if closing_refs:
        return closing_refs
    labels=field.get('label_refs') or field.get('question_refs') or []
    caption=_compact_caption(_text(labels,lookup))
    completer_refs = _word_completer_group_refs(field, lookup)
    if completer_refs:
        return completer_refs
    if re.fullmatch(r'(?:auditors?|auditees?)(?:name(?:s)?|jobtitle|title|email)?|auditdate(?:s)?|auditedby',caption):
        return deepcopy(labels)
    if caption in {'approved','approvedby','reviewedby'}:
        for ref in labels:
            block=lookup.get(ref.get('block_id'),{})
            if re.search(r'\b(?:signature|signatory|audit report|head[ -]?qa)\b|签署|签字',block.get('text',''),re.I):
                return deepcopy(labels)
    if not re.fullmatch(r'name(?:s)?|jobtitle|title|email|(?:opening|closing)?date',caption):return []
    evidence=[]
    for ref in labels:
        source_block=lookup.get(ref.get('block_id'),{})
        anchor=source_block.get('structure',{})
        # A single physical cell can contain two distinct signing groups.
        # Use the preceding real paragraph caption, not a cell-wide substring.
        for span in reversed(anchor.get('paragraph_spans') or []):
            if not isinstance(span.get('end'),int) or span['end']>ref.get('start',0):continue
            value=_compact_caption(span.get('text',''))
            if re.fullmatch(r'auditors?|auditedby|approved(?:by)?|reviewedby',value):
                raw=str(source_block.get('text') or '')[span['start']:span['end']]
                delimiter=re.search(r'[:：]',raw)
                caption_end=span['start']+(delimiter.end() if delimiter else len(raw.rstrip(' _\t')))
                evidence.append({'block_id':source_block['block_id'],'start':span['start'],'end':caption_end})
                break
            if value and not re.fullmatch(r'name(?:s)?|jobtitle|title|email|date|signature',value):break
        if not anchor.get('table_id') or not isinstance(anchor.get('row_start'),int):continue
        for source in lookup.values():
            native=source.get('structure',{})
            if native.get('table_id')!=anchor['table_id'] or native.get('row_start')!=anchor['row_start']:continue
            if not isinstance(native.get('grid_col_end'),int) or not isinstance(anchor.get('grid_col_start'),int) or native['grid_col_end']>=anchor['grid_col_start']:continue
            if re.fullmatch(r'auditors?(?:team)?|auditees?|auditdates?',_compact_caption(source.get('text',''))):
                evidence.append({'block_id':source['block_id'],'start':0,'end':len(source['text'])})
        prior=sorted((b for b in lookup.values() if b.get('structure',{}).get('table_id')==anchor['table_id']
            and b.get('structure',{}).get('grid_col_start')==anchor.get('grid_col_start')
            and 0<anchor['row_start']-b.get('structure',{}).get('row_start',-1000)<=6
            and not b.get('structure',{}).get('is_merge_continuation')),
            key=lambda b:b['structure']['row_start'],reverse=True)
        for source in prior:
            value=_compact_caption(source.get('text',''))
            if re.fullmatch(r'auditedby|auditors?(?:team)?|auditees?|auditdates?',value):
                evidence.append({'block_id':source['block_id'],'start':0,'end':len(source['text'])});break
            if value and not re.fullmatch(r'name(?:s)?|jobtitle|title|email|date|signature',value):break
    return list({(r['block_id'],r['start'],r['end']):r for r in evidence}.values())


def _word_completer_group_refs(field: dict, lookup: dict[str, dict]) -> list[dict]:
    """An adjacent, bounded signing table identifies this copy's completer.

    A general company contact is not enough: the source table must contain an
    actual Signature caption and only completion metadata in this label lane,
    and its immediately preceding nonempty paragraph must explicitly request
    completion/signing of this questionnaire. Other tables cannot inherit it.
    """
    labels = field.get('label_refs') or field.get('question_refs') or []
    metadata = r'name|jobtitle|title|position|date|contact(?:no|number)?|phone(?:no|number)?|telephone|email|姓名|职位|日期|电话|邮箱'
    if not re.fullmatch(metadata, _compact_caption(_text(labels, lookup))):
        return []
    for ref in labels:
        anchor = lookup.get(ref.get('block_id'), {})
        native, location = anchor.get('structure', {}), anchor.get('location', {})
        if location.get('type') != 'docx' or not native.get('table_id'):
            continue
        table = [b for b in lookup.values() if b.get('structure', {}).get('table_id') == native['table_id']]
        lane = [b for b in table if b.get('structure', {}).get('grid_col_start') == native.get('grid_col_start')
                and b.get('structure', {}).get('grid_col_end') == native.get('grid_col_end')
                and str(b.get('text') or '').strip()]
        signatures = [b for b in lane if _signature_caption(b.get('text', ''))]
        if not signatures or any(not re.fullmatch(metadata, _compact_caption(b.get('text', '')))
                                 and not _signature_caption(b.get('text', '')) for b in lane):
            continue
        first_order = min(b.get('order', 0) for b in table)
        prior = sorted((b for b in lookup.values()
                        if b.get('location', {}).get('type') == 'docx'
                        and b.get('location', {}).get('part') == location.get('part')
                        and b.get('order', 0) < first_order and str(b.get('text') or '').strip()),
                       key=lambda b: b.get('order', 0), reverse=True)
        if not prior or prior[0].get('kind') not in {'paragraph', 'heading', 'title'}:
            continue
        instruction = prior[0]
        text = str(instruction.get('text') or '')
        compact = _compact_caption(text)
        explicit = re.fullmatch(r'(?:(?:the|this)?questionnaire(?:hasbeen|was)?)?(?:completed|filled)by', compact)
        signed = re.fullmatch(r'(?:the|this)?questionnaire(?:shouldbe|mustbe|shallbe|istobe|tobe)?'
                              r'(?:completed|filled)andsignedby[a-z]+', compact)
        if (explicit or signed) and not non_authorizing_statement(text):
            return [{'block_id': b['block_id'], 'start': 0, 'end': len(b['text'])}
                    for b in [instruction, signatures[0]]]
    return []


def _local_personnel_context_refs(field: dict, lookup: dict[str,dict]) -> list[dict]:
    """A consecutive native contact group supplies a role, never an answer.

    Stop at an intervening question or an unrelated label. Empty cells and
    merge continuations do not shift the physical rows used for this check.
    """
    labels=field.get('label_refs') or field.get('question_refs') or []
    prompt=r'name|jobtitle|title|phone(?:no|number)?|telephone(?:no|number)?|fax(?:no|number)?|facsimile(?:no|number)?|email'
    if not re.fullmatch(prompt,_compact_caption(_text(labels,lookup))):return []
    for ref in labels:
        anchor=lookup.get(ref.get('block_id'),{}).get('structure',{})
        if not anchor.get('table_id') or not isinstance(anchor.get('row_start'),int):continue
        prior=sorted((b for b in lookup.values() if b.get('structure',{}).get('table_id')==anchor['table_id']
            and b.get('structure',{}).get('grid_col_start')==anchor.get('grid_col_start')
            and 0<anchor['row_start']-b.get('structure',{}).get('row_start',-1000)<=4
            and not b.get('structure',{}).get('is_merge_continuation')),
            key=lambda b:b['structure']['row_start'],reverse=True)
        for source in prior:
            raw=str(source.get('text') or '');value=_compact_caption(raw)
            if re.fullmatch(r'(?:(?:primary|secondary|main))?(?:qualityassurance|qaoffice)(?:contact(?:person)?|representative)'
                            r'|site(?:operationslead|representative)',value):
                return [{'block_id':source['block_id'],'start':0,'end':len(raw)}]
            if value and not re.fullmatch(prompt,value):break
    return []


def infer_field_type(field: dict, lookup: dict[str, dict]) -> tuple[str, list[str]]:
    label = str(field.get("question_text_original") or _text(field.get("label_refs") or [], lookup)).lower()
    unit = str(field.get("unit_text_original") or _text(field.get("unit_refs") or [], lookup)).lower()
    evidence: list[str] = []
    if field.get("record_columns"):
        return "repeating_record", ["存在记录表列定义"]
    if field.get("children") and not field.get('options'):
        return "composite", ["存在子字段"]
    if (field.get('whole_record_response') and field.get('compound_requirement_refs')
            and field.get('local_grid_evidence',{}).get('kind')=='closed_compound_parent_by_rows_and_destination_columns'):
        return 'long_text', ['共享原文标签要求完整记录；实际独立空白格接收多行内容，不虚构格内子位置']
    if _signature_caption(_text(field.get('label_refs') or field.get('question_refs') or [], lookup) or label):
        return 'manual_signature', ['原文短标签明确要求签名或盖章；OCR 空格或括号不改变签署责任']
    if re.search(r'\b(?:signature|stamp|seal)\b', label) and re.fullmatch(r'(?:name|position|date|signature|company|stamp|seal|and|place|[,/&:;\s])+', label):
        return 'manual_signature', ['原文为姓名、职位、日期与签章的组合签署区域，不能生成签名或用当前日期代填']
    if re.fullmatch(r'\s*(?:sign(?:ature)?(?:\s*(?:&|and|/)\s*date)?|signature of .{1,60}|signed by|(?:company\s+)?seal|[^？?\n]{0,15}(?:签字|签名|盖章))\s*[:：]?\s*',label.splitlines()[0] if label else ''):
        return "manual_signature", ["标签明确要求签署或盖章"]
    options = field.get("options") or []
    if options:
        from .validation.word_bullet_answer_groups import verified_independent_bullet_questions
        if verified_independent_bullet_questions(field, lookup):
            return 'multi_select', ['原生同格独立方框逐项对应独立问句，可分别勾选；未见原文只选一项限制']
        from .validation.scanned_option_lists import verified_multiple_selection
        if verified_multiple_selection(field,lookup):
            return 'multi_select', ['原页独立复选框与纵向选项行逐项定位；未出现只选一项的原文限制']
        single = any(token in label for token in ("单选", "选择一项", "choose one", "yes/no", "是/否")) or len(options) <= 3
        return ("single_select" if single else "multi_select"), ["原文存在选项"]
    # Audit observation columns request prose about the left-hand checklist,
    # even if that checklist mentions dates, quantities or production areas.
    for ref in field.get('answer_region_refs') or []:
        target=lookup.get(ref.get('block_id'),{}).get('structure',{})
        if not target.get('table_id'):continue
        headers=[b for b in lookup.values() if b.get('structure',{}).get('table_id')==target['table_id']
            and isinstance(b.get('structure',{}).get('row_start'),int) and b['structure']['row_start']<target.get('row_start',0)
            and b['structure'].get('grid_col_start')==target.get('grid_col_start') and b['structure'].get('grid_col_end')==target.get('grid_col_end')
            and re.fullmatch(r'(?:audit\s+)?observations?(?:\s*/\s*(?:remarks?|comments?))?|审核意见|审计观察',b.get('text','').strip(),re.I)]
        if headers:return 'long_text',['答复列原文表头要求审计观察或说明，不是清单内提及的日期或面积值']
    enumeration_label=re.sub(r'[\s.：:]','',label)
    if enumeration_label in {'srno','sno','序号'} or (field.get('record_index') is not None and enumeration_label=='serialno'):
        return 'integer',['原文为记录行序号列，不是设备或产品序列编号']
    if field.get("attachment_refs") or any(token in label for token in ("附件", "上传", "提供文件", "attachment")):
        return "attachment_reference", ["标签明确要求附件或文件"]
    short_date_label=len(label.split())<=10 and len(label)<90 and not re.search(r'\b(?:records?|procedures?|indicating|contain|describe|explain)\b',label)
    if (short_date_label and (any(token in label for token in ("日期", "年月日", "成立时间", "有效期")) or re.search(r'\bdate\b',label))):
        return "date", ["标签包含日期或时间要求"]
    if any(token in label for token in ("地址", "所在地", "address", "location")):
        return "address", ["标签包含地址含义"]
    if any(token in label for token in ("电话", "传真", "手机", "邮箱", "email", "e-mail", "telephone", "phone", "fax")):
        return "contact", ["标签包含联系方式含义"]
    numeric_label=label.strip(' :：?.')
    short_quantity_label=len(numeric_label.split())<=8 and not re.search(r'\b(?:what|which|how|is|are|do|does|describe|explain|required|procedure)\b',numeric_label)
    area_label=bool(re.fullmatch(r'(?:(?:total|site|land|floor|plot|surface|building|built.?up)\s+){0,3}area(?:\s+in\s+(?:square\s+(?:feet|meters?|metres?)|sq\.?\s*(?:ft|m)|m²))?',numeric_label))
    if unit in {'ppm','ppb','kg','g','mg','ml','l'} or any(token in unit for token in ("%", "m²", "㎡")) or any(token in label for token in ("比例", "百分比", "面积")) or area_label or (short_quantity_label and re.search(r'\b(?:ratio|percentage|quantity|qty)\b',numeric_label)):
        return "decimal", ["原文单位或标签要求小数型数值"]
    if any(token in unit for token in ("人", "名", "个")) or any(token in label for token in ("人数", "数量")) or (short_quantity_label and re.search(r'\b(?:count|number of|no\.?\s+of)\b',label)) or re.search(r'^\s*how many\b',label):
        return "integer", ["原文单位或标签要求整数"]
    regions = field.get("answer_region_refs") or []
    multi_row = any((lookup.get(x.get("block_id")) or {}).get("structure", {}).get("row_span", 1) > 1 for x in regions)
    if multi_row or any(token in label for token in ("请说明", "描述", "流程", "describe", "explain", "details")):
        return "long_text", ["答复区域或问题要求长文本"]
    return "short_text", ["未发现更具体的原文类型约束"]


def _declared_owner(text: str) -> str | None:
    """Explicit instructions only; a company mentioned in an answer is not ownership."""
    text = re.sub(r'\s+', ' ', str(text or '')).strip().lower()
    if non_authorizing_statement(text):
        return None
    # An explicitly named quality-assessment panel is a local review scope,
    # even when OCR joins words or the model omitted the section reference.
    # Accept the entire short noun-phrase heading only; process questions and
    # sentences about assessments cannot establish who fills this document.
    compact = _compact_caption(text)
    if len(text) <= 180 and re.fullmatch(
        r'(?:questionnaire)?assessmentby[a-z0-9]{1,85}'
        r'(?:qualitymanagement|qualityassurance|qualitycontrol)', compact):
        party = compact.partition('assessmentby')[2]
        return 'our_side' if party.startswith(('supplier', 'vendor', 'manufacturer')) else 'reviewer'
    if re.search(r'共同填写|双方填写|jointly completed|both parties', text):
        return 'shared'
    if re.search(r'(?:填表人|供应商填写|供方填写|制造商填写|生产厂家填写)|(?:filled|completed)(?:\s+in)?\s+by\s+(?:the\s+)?(?:supplier|vendor|manufacturer|api manufacturer)|(?:supplier|vendor|manufacturer)\s+(?:to complete|use(?: only)?)|on behalf of supplier',text):
        return 'our_side'
    if re.search(r'审核人员填写|审核意见|审核人|(?:问卷|调查)(?:调查)?(?:情况)?审核记录|reviewer use|auditor use|assessment score|questionnaire was evaluated|reviewed by|evaluation\s+signature|questionnaire\s+evaluation|(?:filled|completed)(?:\s+in)?\s+by\s+(?:the\s+)?(?:cqa|reviewer|auditor)\b',text):
        return 'reviewer'
    if re.search(r'采购方填写|客户填写|客户评分|buyer use|customer use|purchaser use|(?:filled|completed)(?:\s+in)?\s+by\s+(?:the\s+)?(?:customer|buyer|purchaser)',text):
        return 'customer'
    # Named recipient sections such as "Acme use only" carry the same explicit
    # exclusion as "customer use only". This is not a hard-coded organization.
    if re.fullmatch(r'[a-z][a-z0-9 &.,()–—-]{1,80}\s+use\s+only\s*[:;.]?',text):
        return 'customer'
    # A section explicitly assigned to a named organization is not inherited
    # supplier permission. Without a proven party identity retain unknown,
    # along with this exact instruction, rather than guessing its role.
    if re.fullmatch(r'(?:[a-z0-9]+[.)]\s*)?section\s+for\s+[a-z][a-z0-9 &.,()–—-]{1,100}\s*[:;.]?', text):
        return 'unknown'
    return None


def _source_counterparties(lookup: dict[str, dict]) -> dict[str, dict]:
    """Names explicitly receiving products from the supplier in this document.

    Merely mentioning a company or the file's name never establishes owner
    identity. These references describe the actual supplier/recipient relation.
    """
    aliases={}
    pattern=re.compile(r'\b(?:manufactured|supplied|provided|sold)\b[^.;\n]{0,60}\bby\s+(?:the\s+)?(?:vendor|supplier|manufacturer)\s+(?:for|to)\s+([A-Za-z][A-Za-z0-9 &(),\'-]{1,100})(?=[.;\n]|$)',re.I)
    for block in lookup.values():
        if block.get('kind') not in {'paragraph','heading','title'}:continue
        text=str(block.get('text') or '')
        for match in pattern.finditer(text):
            name=match.group(1).strip()
            if len(name.split())>10 or name.lower() in {'customer','buyer','purchaser','vendor','supplier'}:continue
            aliases[name.casefold()]={'block_id':block['block_id'],'start':match.start(1),'end':match.start(1)+len(name)}
    return aliases


def _party_form_heading(text: str, counterparties: dict[str,dict]) -> tuple[str, str] | None:
    """Return ownership only for short physical contact/record headings."""
    text=re.sub(r'\s+',' ',str(text or '')).strip()
    if len(text)>140:return None
    contacts=re.fullmatch(r'(.{1,90}?)\s+(?:key\s+)?contact\s+(?:persons?|details|information|list)(?:\s+and/or\s+affiliates)?\s*[:.]?',text,re.I)
    if contacts:
        party=contacts.group(1).strip().casefold()
        if party in {'supplier','vendor','manufacturer'}:return 'our_side','原文联系人区域明确属于供应商'
        if party in {'customer','buyer','purchaser'} or party in counterparties:return 'customer','原文联系人区域属于文档明确的客户/产品接收方'
        return 'unknown','命名联系人区域的所属方尚未由原文明确，不能沿用供应商文档默认授权'
    # Record-column ownership: retain exact source company identity rather
    # than guessing a role from an arbitrary filled name in a record row.
    if re.fullmatch(r'(?:vendor|supplier|manufacturer)\s+(?:material|product|item)(?:\s+(?:name|code|number))?(?:\s*&\s*(?:material|product|item)?\s*(?:name|code|number))?',text,re.I):
        return 'our_side','原文记录列明确要求供应商自身的产品名称或编号'
    if re.fullmatch(r'(?:vendor|supplier|manufacturer)\s+(?:site\s+)?(?:location|address|name\s*(?:&|and)\s*address)',text,re.I):
        return 'our_side','原文记录列明确要求供应商自身场所或地址'
    for alias, evidence in counterparties.items():
        if re.fullmatch(re.escape(alias)+r'\s+(?:material(?:\s+(?:name|code|number))?|product(?:\s+(?:name|code|number))?|item(?:\s+(?:code|number))?)',text,re.I):
            return 'customer',f"列标题指向原文明确的产品接收方；关系来源 {evidence['block_id']}:{evidence['start']}–{evidence['end']}"
    return None


def _parallel_signing_owner(block: dict, lookup: dict[str,dict], counterparties: dict[str,dict]) -> str | None:
    """Native two-party signature lanes, including unnamed role subheaders."""
    s=block.get('structure',{});table=s.get('table_id');row=s.get('row_start')
    if not table or not isinstance(row,int):return None
    def role(b):
        name=re.sub(r'\s+',' ',str(b.get('text') or '')).strip().casefold()
        if re.fullmatch(r'(?:vendor|supplier|manufacturer)(?:\s+name)?',name):return 'our_side'
        if name in counterparties:return 'customer'
        return None
    if role(block) is None:return None
    peers=[b for b in lookup.values() if b.get('structure',{}).get('table_id')==table and b.get('structure',{}).get('row_start')==row and str(b.get('text') or '').strip()]
    if len(peers)!=2:return None
    roles=[role(b) for b in peers]
    if set(roles)!={'customer','our_side'}:return None
    for peer in peers:
        ps=peer['structure']
        signs=[b for b in lookup.values() if b.get('structure',{}).get('table_id')==table and isinstance(b.get('structure',{}).get('row_start'),int) and row<b['structure']['row_start']<=row+8 and
               b['structure'].get('grid_col_start')==ps.get('grid_col_start') and b['structure'].get('grid_col_end')==ps.get('grid_col_end') and
               re.search(r'\b(?:signee|signatory|signature|sign\s*/\s*date)\b',str(b.get('text') or ''),re.I)]
        if not signs:return None
    return role(block)


def _native_owner_scope(field: dict, lookup: dict[str, dict]) -> tuple[str, list[str]] | None:
    """Prefer the write target's column owner to broad question/section text.

    A review column beside supplier Yes/No cells must not own the entire row.
    Conversely, a whole-width client section owns every answer beneath it until
    the next explicit owner boundary in that same physical table.
    """
    field.pop('_native_display_context_refs',None)
    field.pop('_native_owner_evidence_refs',None)
    counterparties=_source_counterparties(lookup)
    targets = [lookup.get(r.get('block_id')) for r in field.get('answer_region_refs', [])]
    explicit_roles=[b.get('structure',{}).get('column_owner_role') for b in targets if b and b.get('structure',{}).get('column_owner_ref') in lookup]
    if explicit_roles and len(explicit_roles)==len(targets) and all(role in {'our_side','customer','reviewer','shared'} for role in explicit_roles):
        field['_native_owner_evidence_refs']=[{'block_id':b['structure']['column_owner_ref'],'start':0,'end':len(lookup[b['structure']['column_owner_ref']].get('text',''))} for b in targets]
        return (explicit_roles[0] if len(set(explicit_roles))==1 else 'shared'),['原生责任矩阵中当前答复列明确属于该填写方']
    owners = []
    party_context_refs=[]
    for target in targets:
        if not target: continue
        s = target.get('structure', {}); table, row = s.get('table_id'), s.get('row_start')
        if not table or not isinstance(row, int): continue
        col0, col1 = s.get('grid_col_start'), s.get('grid_col_end')
        if not isinstance(col0,int) or not isinstance(col1,int): continue
        candidates = []
        for block in lookup.values():
            bs = block.get('structure', {})
            if bs.get('table_id') != table or not isinstance(bs.get('row_start'),int) or bs['row_start'] >= row: continue
            b0,b1=bs.get('grid_col_start'),bs.get('grid_col_end')
            native_heading=_party_form_heading(block.get('text',''),counterparties)
            party_owner=_parallel_signing_owner(block,lookup,counterparties)
            owner = _declared_owner(block.get('text','')) or (native_heading[0] if native_heading else None) or party_owner
            if not owner or not isinstance(b0,int) or not isinstance(b1,int): continue
            same_row=[b for b in lookup.values() if b.get('structure',{}).get('table_id')==table and b.get('structure',{}).get('row_start')==bs['row_start']]
            # A lone preprinted vendor address in a record is still one cell,
            # not an instruction that owns neighboring customer columns.
            whole_row_instruction=bool(_declared_owner(block.get('text',''))) and sum(bool(str(b.get('text','')).strip()) for b in same_row)==1
            if (b0 <= col0 and b1 >= col1) or whole_row_instruction:
                candidates.append((bs['row_start'], int(block.get('order',0)), owner,block['block_id'],bool(party_owner)))
        if candidates:
            selected=max(candidates);owners.append(selected[2])
            heading=lookup[selected[3]]
            field.setdefault('_native_owner_evidence_refs',[]).append({'block_id':heading['block_id'],'start':0,'end':len(heading.get('text',''))})
            if selected[4]:
                heading=lookup[selected[3]]
                party_context_refs.append({'block_id':heading['block_id'],'start':0,'end':len(heading.get('text',''))})
                local_roles=[b for b in lookup.values() if b.get('structure',{}).get('table_id')==table and isinstance(b.get('structure',{}).get('row_start'),int) and selected[0]<b['structure']['row_start']<row and b['structure'].get('grid_col_start')==col0 and b['structure'].get('grid_col_end')==col1 and b['structure'].get('semantic_role_hint')=='table_header' and str(b.get('text','')).strip() and len(b['text'])<100]
                if local_roles:
                    role=max(local_roles,key=lambda b:b.get('order',0))
                    party_context_refs.append({'block_id':role['block_id'],'start':0,'end':len(role['text'])})
    if owners:
        if party_context_refs:
            field['_native_display_context_refs']=list({(r['block_id'],r['start'],r['end']):r for r in party_context_refs}.values())
        owner = owners[0] if len(set(owners)) == 1 else 'shared'
        return owner, ['答复目标所在网格列或跨列表头明确声明填写责任']
    labels = field.get('label_refs') or field.get('question_refs') or []
    anchor = lookup.get(labels[0].get('block_id')) if labels else None
    if not anchor: return None
    order = anchor.get('order',0); part = anchor.get('location',{}).get('part')
    paragraphs = sorted((b for b in lookup.values() if b.get('kind') in {'paragraph','heading','title'} and b.get('location',{}).get('part')==part and b.get('order',0)<order),key=lambda b:b.get('order',0),reverse=True)
    # A native tabbed signing form has several independent party columns.
    # Match the precise source slot within consecutive parallel prompt rows,
    # rather than assigning the entire line to its first supplier mention.
    slots=sorted((s for s in anchor.get('structure',{}).get('logical_slots',[]) if s.get('kind')=='inline_prompt_candidate' and s.get('label_start') is not None),key=lambda s:s['label_start'])
    current=[i for i,s in enumerate(slots) if any(r.get('block_id')==anchor['block_id'] and r.get('start')==s['label_start'] and r.get('end')==s['label_end'] for r in labels)]
    if anchor.get('kind')=='paragraph' and len(slots)>=2 and len(current)==1:
        local=[]
        for b in paragraphs[:6]:
            content=str(b.get('text',''))
            if not content.strip():continue
            parts=[m for m in re.finditer(r'[^\t]+?(?=\t|\s{3,}|$)',content) if m.group().strip()]
            if len(parts)==len(slots) and all(re.fullmatch(r'\s*.{1,70}\b(?:representative\s+)?signature\s*',m.group(),re.I) for m in parts):
                roles=['our_side' if re.search(r'\b(?:supplier|vendor|manufacturer)\b',m.group(),re.I) else 'customer' for m in parts]
                if roles.count('our_side')==1 and 'customer' in roles:
                    evidence=parts[current[0]]
                    field['_native_display_context_refs']=[{'block_id':b['block_id'],'start':evidence.start(),'end':evidence.end()}]
                    return roles[current[0]],[f"原生并列签署栏通过连续同列提示定位；标题来源 {b['block_id']}:{evidence.start()}–{evidence.end()}"]
                break
            other=[s for s in b.get('structure',{}).get('logical_slots',[]) if s.get('kind')=='inline_prompt_candidate' and s.get('label_start') is not None]
            if len(other)!=len(slots):break
    for b in paragraphs:
        content=str(b.get('text','')).strip()
        if not content: continue
        heading_owner=_party_form_heading(content,counterparties)
        owner=_declared_owner(content) or (heading_owner[0] if heading_owner else None)
        if owner:
            field['_native_owner_evidence_refs']=[{'block_id':b['block_id'],'start':0,'end':len(str(b.get('text') or ''))}]
            if heading_owner:
                field['_native_display_context_refs']=[{'block_id':b['block_id'],'start':0,'end':len(str(b.get('text') or ''))}]
                return owner,[heading_owner[1],f"原生区域标题来源 {b['block_id']}"]
            return owner,['当前原生段落区域有明确填写责任说明']
        # A new declaration/heading is a real section boundary. Do not carry a
        # previous buyer-review footer into a later supplier declaration.
        letters=re.sub(r'[^A-Za-z]','',content)
        if b.get('kind') in {'heading','title'} or (len(letters)>8 and letters.isupper() and len(content)<180): break
    return None


def _pdf_assessment_owner_scope(field: dict, lookup: dict[str, dict]) -> tuple[str, list[str]] | None:
    """A real local review caption may govern the following physical page.

    A source reference to a heading is essential: a business question about an
    assessment, a model's responsibility hint, or a caption on a future page
    does not authorize reassignment of the question's owner.
    """
    labels = field.get('label_refs') or field.get('question_refs') or []
    anchor = lookup.get(labels[0].get('block_id'), {}) if labels else {}
    location = anchor.get('location', {})
    page = location.get('page')
    if location.get('type') != 'pdf' or not isinstance(page, int):
        return None
    candidates = []
    for ref in list(field.get('section_refs') or []) + list(field.get('context_refs') or []):
        block = lookup.get(ref.get('block_id'), {})
        source = str(block.get('text') or '')
        start, end = ref.get('start', 0), ref.get('end', len(source))
        loc = block.get('location', {})
        source_page = loc.get('page')
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(source)):
            continue
        if loc.get('type') != 'pdf' or not isinstance(source_page, int) or not 0 <= page-source_page <= 1:
            continue
        # The whole short caption, not a substring lifted from another question.
        text = source[start:end].strip()
        if source[:start].strip() or source[end:].strip() or not text or len(text) > 180 or '?' in text or '？' in text:
            continue
        box, anchor_box = loc.get('bbox'), location.get('bbox')
        if source_page == page and box and anchor_box and box[1] > anchor_box[1]:
            continue
        compact = _compact_caption(text)
        prefix, separator, party = compact.partition('assessmentby')
        if not separator or not party or (prefix and not prefix.endswith('questionnaire')):
            continue
        role = 'our_side' if re.match(r'(?:supplier|vendor|manufacturer)', party) else 'reviewer'
        candidates.append((source_page, (box or [0, 0])[1], role, ref))
    if not candidates:
        return None
    _, _, role, ref = max(candidates, key=lambda value: (value[0], value[1]))
    field['_pdf_owner_evidence_refs'] = [deepcopy(ref)]
    return role, [f"PDF 原文局部评估标题明确声明填写责任；来源 {ref['block_id']}:{ref.get('start', 0)}–{ref.get('end')}"]


def infer_responsibility(field: dict, lookup: dict[str, dict]) -> tuple[str, list[str]]:
    field.pop('_pdf_owner_evidence_refs', None)
    field.pop('_document_owner_evidence_refs', None)
    local_review = pdf_local_review_owner_scope(field, lookup)
    if local_review:
        return local_review
    pdf_scope = _pdf_assessment_owner_scope(field, lookup)
    if pdf_scope:
        return pdf_scope
    native_scope = _native_owner_scope(field, lookup)
    if native_scope: return native_scope
    # A locally attached "if any comments need to be made" makes the response
    # optional; the supplier remains its explicitly named writer. This is not
    # a document-wide authorization or a conditional self-assessment mode.
    for ref in field.get('instruction_refs') or []:
        source = str(lookup.get(ref.get('block_id'), {}).get('text') or '')
        start, end = ref.get('start'), ref.get('end')
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(source)):
            continue
        instruction = source[start:end].strip()
        ordinary_comment_instruction = re.fullmatch(r'(?:to\s+be\s+)?(?:filled(?:\s+(?:in|out))?|completed)\s+by\s+'
                        r'(?:the\s+)?(?:api\s+manufacturer|manufacturer|supplier|vendor)\s+'
                        r'if\s+(?:any\s+)?(?:comments?|remarks?)\s+'
                        r'(?:need\s+to\s+be\s+made|are\s+(?:needed|required))\s*[.!]?', instruction, re.I)
        # Some original forms spell this local caption "FILED IN" or "IF
        # ANY BE COMMENTS". Preserve that source verbatim. Accept it only
        # beside an actual Comments field in the same physical table row;
        # "filed" elsewhere is not permission to fill a business field.
        labels = field.get('label_refs') or field.get('question_refs') or []
        label = lookup.get(labels[0].get('block_id'), {}) if labels else {}
        local = lookup.get(ref.get('block_id'), {})
        a, b = label.get('structure', {}), local.get('structure', {})
        same_row = (a.get('table_id') and a.get('table_id') == b.get('table_id')
                    and isinstance(a.get('row_start'), int) and a.get('row_start') == b.get('row_start')
                    and label.get('location', {}).get('part') == local.get('location', {}).get('part'))
        source_spelling_variant = (same_row and source.strip() == instruction
            and re.fullmatch(r'comments?|remarks?', _text(labels, lookup).strip(), re.I)
            and re.fullmatch(r'to\s+be\s+(?:filed|filled)\s+in\s+by\s+api\s+manufacturer\s+'
                             r'if\s+any\s+(?:be\s+)?comments\s+need\s+to\s+be\s+made\s*[.!]?',
                             instruction, re.I))
        if ordinary_comment_instruction or source_spelling_variant:
            field['_native_owner_evidence_refs'] = [deepcopy(ref)]
            return 'our_side', ['当前填写区的原文说明明确由供应商填写可选备注；不授权其他区域']
    text = _context(field, lookup).lower()
    local_compact = re.sub(r'[^a-z\u4e00-\u9fff]', '', text)
    pdf_field = any(lookup.get(r.get('block_id'), {}).get('location', {}).get('type') == 'pdf'
                    for r in field.get('label_refs') or field.get('question_refs') or [])
    if (not pdf_field and re.search(r'assessmentby(?:dqa|pqa|auditor|reviewer)', local_compact)) or re.search(r'for(?:customer|buyer|purchaser)useonly', local_compact):
        return 'reviewer', ['原文局部区域明确标注为客户或审核部门评估区']
    # Check each source statement independently: a question's punctuation must
    # neither authorize a reviewer nor cancel an explicit reviewer section.
    local_statements = [(ref, _text([ref], lookup))
                        for key in ('section_refs', 'instruction_refs', 'label_refs', 'question_refs')
                        for ref in field.get(key) or []]
    owner_sources = [(ref, _declared_owner(raw)) for ref, raw in local_statements
                     if _declared_owner(raw)]
    explicit_owner = owner_sources[0][1] if owner_sources else None
    if explicit_owner:
        refs=[]
        for key in ('section_refs','instruction_refs','label_refs','question_refs'):
            for ref in field.get(key) or []:
                if _declared_owner(_text([ref],lookup))==explicit_owner:refs.append(deepcopy(ref))
        field['_native_owner_evidence_refs']=refs
        return explicit_owner, ['原文明确声明填写责任']
    if not non_authorizing_statement(text) and any(token in text for token in ("共同填写", "双方填写", "jointly completed", "both parties")):
        return "shared", ["原文明确要求双方或共同填写"]
    if not non_authorizing_statement(text) and any(token in text for token in ("采购方填写", "客户填写", "客户评分", "buyer use", "customer use", "purchaser use", "completed by customer")):
        return "customer", ["原文明确属于客户或采购方"]
    if not non_authorizing_statement(text) and any(token in text for token in ("审核人员填写", "审核意见", "审核人", "reviewer use", "auditor use", "assessment score", "questionnaire was evaluated")):
        return "reviewer", ["原文明确属于审核人员"]
    if not non_authorizing_statement(text) and any(token in text for token in ("供应商填写", "供方填写", "制造商填写", "vendor to complete", "supplier use", "manufacturer to complete", "filled in by api manufacturer")):
        return "our_side", ["原文明确属于供应商或制造商"]
    label_refs = field.get("label_refs") or field.get("question_refs") or []
    label_block = lookup.get(label_refs[0].get("block_id")) if label_refs else None
    structure = (label_block or {}).get("structure", {})
    table_id, row = structure.get("table_id"), structure.get("row_start")
    if table_id and isinstance(row, int):
        row_text = "\n".join(
            str(block.get("text") or "").lower() for block in lookup.values()
            if block.get("structure", {}).get("table_id") == table_id
            and block.get("structure", {}).get("row_start") == row
            and not non_authorizing_statement(block.get("text", ""))
        )
        if any(token in row_text for token in ("审核人", "reviewed by", "审核人员")):
            return "reviewer", ["同一物理行明确标注为审核人填写"]
        if any(token in row_text for token in ("填表人", "completed by supplier", "completed by manufacturer")):
            return "our_side", ["同一物理行明确标注为填表人或签署区域"]
        # Table ownership above is scoped to each physical answer column;
        # never scan an unrelated reviewer column as a row-wide instruction.
    # A document-level supplier/vendor questionnaire title establishes the
    # default owner for ordinary cover-sheet fields. Scope-selection fields
    # remain unknown because they are commonly prepared by the customer.
    # Empty cover-page paragraphs are layout, not twenty pieces of title
    # context. Preserve them in extraction but count meaningful blocks here.
    source_refs = document_owner_evidence(field, lookup)
    if source_refs:
        # A neutral subheading does not prove a return from a customer/review
        # region to supplier ownership. Preserve that exclusion even when the
        # model omitted its section reference. Table-column roles remain local.
        anchor=label_block or {}
        exclusions=[]
        for b in lookup.values():
            loc=b.get('location',{});aloc=anchor.get('location',{})
            if b.get('kind') not in {'paragraph','heading','title','ocr_text'}:continue
            if b.get('structure',{}).get('resolved_layout_role') in {'fixed_template_approval','repeated_header_footer'}:continue
            if loc.get('type')!=aloc.get('type') or loc.get('part')!=aloc.get('part') or b.get('order',0)>=anchor.get('order',0):continue
            raw=str(b.get('text') or '')
            owner=_declared_owner(raw)
            if owner and len(raw)<200 and not non_authorizing_statement(raw):exclusions.append((b.get('order',0),owner,b))
        if exclusions:
            _,owner,b=max(exclusions,key=lambda x:x[0])
            if owner in {'customer','reviewer','shared','unknown'}:
                field['_native_owner_evidence_refs']=[{'block_id':b['block_id'],'start':0,'end':len(b['text'])}]
                return owner,['此前原文已明确限定填写方；普通子标题不解除该限制']
        field['_document_owner_evidence_refs'] = source_refs
        if field.get('_document_owner_evidence_kind')=='company_manufacturing_context':
            return 'our_side', ['公司问卷标题与首表生产场所地址、制造区数量共同确定受访生产企业；局部客户、审核和签署限制优先']
        return 'our_side', ['原文标题或总说明明确为供应商填写；跨普通章节继承，局部客户、审核和签署限制优先']
    cover_blocks = [b for b in lookup.values() if str(b.get('text') or '').strip() and not non_authorizing_statement(b.get('text',''))][:20]
    document_title = "\n".join(str(block.get("text") or "").lower() for block in cover_blocks if block.get("kind") in {"title", "heading", "paragraph"})
    document_title += '\n'+'\n'.join(str(b.get('text','')).lower() for b in lookup.values() if str(b.get('location',{}).get('part','')).startswith('word/header') and (b.get('kind')=='header' or re.match(r'^\s*title\s*:',str(b.get('text','')),re.I)))
    document_title=re.sub(r'(?<![a-z])(?:[a-z] ){2,}[a-z](?![a-z])',lambda m:m[0].replace(' ',''),document_title)
    label = str(field.get("question_text_original") or "").lower()
    if (
        any(token in re.sub(r'\s+', ' ', document_title) for token in ("供应商调查问卷", "supplier questionnaire", "vendor questionnaire", "questionnaire for supplier qualification", "self check sheet for api manufacturer", "manufacturers evaluation questionnaire", "questionnaire for raw material manufacturer qualification"))
        and not any(token in label for token in ("需要完成调查问卷的内容", "contents of questionnaire"))
        and not any(token in label for token in ("审核", "reviewed by", "reviewer"))
    ):
        return "our_side", ["文档标题明确为供应商问卷，该普通填写字段默认由供应商填写"]
    introductory=' '.join(str(b.get('text','')).lower() for b in list(lookup.values())[:50] if not non_authorizing_statement(b.get('text','')))
    if re.search(r'questionnaire should be completed by.{0,180}quality assurance.{0,100}site of manufacturing only',introductory,re.S):
        return 'our_side',['原文总说明明确由生产场所质量保证代表填写；局部客户或审核区优先排除']
    first_page = [b for b in lookup.values() if b.get('location',{}).get('type') == 'pdf' and b.get('location',{}).get('page') == 1 and
                  b.get('structure',{}).get('resolved_layout_role') not in {'repeated_header_footer','fixed_template_approval'} and not non_authorizing_statement(b.get('text',''))]
    compact = re.sub(r'[^a-z]', '', ' '.join(str(b.get('text','')).lower() for b in first_page))
    vendor_form = 'vendorquestionnaire' in compact or 'supplierquestionnaire' in compact
    self_assessment = 'selfassessmentquestionnaire' in compact and 'thisquestionnaireshouldbecompleted' in compact
    producer_sheet = ('productinformationsheet' in compact and 'producer' in compact and 'kindlyreturnfilledindocument' in compact)
    if vendor_form or self_assessment or producer_sheet:
        return 'our_side', ['PDF 原页明确为供应商问卷、自评问卷或由生产者返回的产品资料表；局部客户/审核说明优先']
    return "unknown", ["原文未明确填写责任"]


def infer_required(field: dict, lookup: dict[str, dict]) -> tuple[str, list[str]]:
    text = _context(field, lookup).lower()
    if re.search(r"选填|非必填|可选填写|not\s+(?:required|mandatory)|\boptional\b", text):
        return "optional", ["原文明确标记为可选"]
    if re.search(r"必填|必须填写|\b(?:required|mandatory)\s+field\b|\bfield\s+(?:is\s+)?(?:required|mandatory)\b|(?:^|[\[(（])\s*(?:required|mandatory)\s*(?:$|[\])）])", text):
        return "required", ["原文明确标记为必填"]
    return "unknown", ["原文未声明是否必填"]


def infer_condition(field: dict, lookup: dict[str, dict]) -> dict | None:
    for ref in field.get('answer_region_refs',[]):
        for slot in lookup.get(ref.get('block_id'),{}).get('structure',{}).get('logical_slots',[]):
            if slot.get('slot_id')==ref.get('slot_id') and slot.get('conditional_choice'):
                return {'status':'unresolved','source_text':slot['conditional_choice'],'parent_field_id':field.get('parent_question_id')}
    # A condition must govern THIS response. A yes/no parent followed by
    # "If yes, complete Part B" remains answerable; the directive governs B.
    # A model boolean with no source cannot create a new applicability rule.
    text = str(field.get('question_text_original') or _text(field.get('label_refs') or [],lookup))
    match = re.match(r"^\s*(?:\d+(?:\.\d+)*[.)]?\s*)?(?:若|如果|当|\bif\b|\bwhen\b)\s*([^。；;\n]{1,100})", text, re.I)
    if not match:
        instructions = _text(field.get('instruction_refs') or [],lookup)
        match = re.search(r'(?:complete|fill|answer|填写|回答)\s+[^。；;\n]{0,35}(?:only\s+if|仅在|仅当)[^。；;\n]{1,100}',instructions,re.I)
    if not match:
        return None
    return {"status": "unresolved", "source_text": match.group(0).strip(), "parent_field_id": field.get("parent_question_id")}


def _target(ref: dict, lookup: dict[str, dict], source_hash: str, parser_version: str) -> dict | None:
    block = lookup.get(ref.get("block_id"))
    if not block:
        return None
    structure, location = block.get("structure", {}), block.get("location", {})
    slot_id = str(ref.get("slot_id") or "")
    slot = next((s for s in structure.get("logical_slots") or [] if s.get("slot_id") == slot_id), None)
    paragraph_slot = slot if slot and slot.get("paragraph_index") else None
    paragraph_info = next((p for p in structure.get('paragraph_spans',[]) if paragraph_slot and p['paragraph_index']==paragraph_slot['paragraph_index']), None)
    controls = [c for c in block.get("controls") or [] if c.get('source')!='native_pdf_glyph']
    if paragraph_slot:
        controls=[c for c in controls if c.get('paragraph_index') in (None,paragraph_slot['paragraph_index'])]
    if slot and slot.get('kind')=='inline_placeholder' and slot.get('placeholder_text') and block.get('text','')[slot['start']:slot['end']]==slot['placeholder_text']:
        controls=[]  # An exact text placeholder, separate from nearby controls.
    control_id = str(ref.get("control_id") or "")
    control = next((x for x in controls if str(x.get("control_id") or "") == control_id), None) if control_id else (controls[0] if len(controls) == 1 else None)
    if ref.get('control_index') is not None:
        control=next((x for x in controls if x.get('control_index')==ref['control_index'] and x.get('paragraph_index')==ref.get('control_paragraph_index')),None)
    method = ""
    writable = True
    reason = ""
    if structure.get('physical_region_kind')=='native_vector_checkbox':
        from .parser_core.pdf_vector_forms import verified_vector_control
        if verified_vector_control(block):
            method='standard_pdf_response'
        else:
            writable,reason=False,'PDF 原生选项框、选项含义或勾选状态尚未完成校验'
    elif slot and slot.get('kind')=='native_pdf_explicit_fill_line':
        from .validation.pdf_attachment_groups import verified_fill_line
        if (ref.get('start')==slot.get('start') and ref.get('end')==slot.get('end') and
                verified_fill_line(block,slot,lookup)):
            method='standard_pdf_response'
        else:
            writable,reason=False,'PDF 填写线缺少原生行范围与完整局部表单关系'
    elif slot and slot.get('kind') in {'native_pdf_boxed_source_value','native_pdf_numbered_paragraph_value'}:
        from .validation.native_vector_forms import verified_boxed_value_slot
        if (ref.get('start')==slot.get('start') and ref.get('end')==slot.get('end') and
                verified_boxed_value_slot(block,slot,lookup)):
            method='standard_pdf_response'
        else:
            writable,reason=False,'PDF 原生答复缺少已验证的题干和位置证据'
    elif slot and slot.get('kind')=='scan_bounded_source_value':
        from .validation.scanned_subfields import verified_scan_value_slot
        if (ref.get('start')==slot.get('start') and ref.get('end')==slot.get('end') and
                verified_scan_value_slot(block,slot,lookup)):
            method='standard_pdf_response'
        else:
            writable,reason=False,'扫描答复片段缺少已验证的标签、封闭区域或原文范围'
    elif slot and slot.get('kind')=='scan_observed_blank_tail':
        from .validation.scan_inline_response_regions import verified_tail_slot
        if (ref.get('start')==slot.get('start')==0 and ref.get('end')==slot.get('end')==0
                and structure.get('scan_association_valid') is True
                and verified_tail_slot(block,slot,lookup,source_hash)):
            method='standard_pdf_response'
        else:
            writable,reason=False,'扫描填写片段缺少同一原件的原图空白、标签和局部边界校验'
    elif structure.get('scan_candidate'):
        from .validation.pdf_source_relations import native_overlay_value_in_region, ocr_value_in_region
        overlay_ids = structure.get('overlay_value_block_ids', [])
        verified_overlay = bool(overlay_ids) and all(native_overlay_value_in_region(
            {'block_id':bid,'start':0,'end':len(lookup.get(bid,{}).get('text',''))}, [ref], lookup) for bid in overlay_ids)
        verified_ocr=bool(structure.get('ocr_value_refs')) and all(ocr_value_in_region(v,[ref],lookup) for v in structure.get('ocr_value_refs',[]))
        from .validation.selection_evidence import verified_raster_tick_control
        from .parser_core.pdf_matrix_marks import verified_matrix_cell_mark
        selected_scan=bool(controls) and all((c.get('source')=='ocr_mark_in_scan_rectangle' and c.get('selected') is True and c.get('mark_evidence')) or
            (structure.get('source_method')=='raster_geometry' and verified_raster_tick_control(c)) or
            (c.get('source')=='raster_tick_in_choice_cell' and verified_matrix_cell_mark(block,lookup)) for c in controls)
        if location.get('type')=='pdf' and structure.get('scan_association_valid') is True and (structure.get('blank_state')=='blank' or verified_overlay or verified_ocr or selected_scan):
            method='standard_pdf_response'
        else:
            writable,reason=False,'扫描区域与字段的关联或原有内容尚未确认，只允许核对，不能作为已验证填写位置'
    elif slot and slot.get('kind') in {'native_table_question_tail','native_table_underlined_gap'} and location.get('type')=='pdf':
        from .validation.native_table_positions import verified_native_table_slot
        if (ref.get('start')==slot.get('start') and ref.get('end')==slot.get('end') and
            verified_native_table_slot(block,slot,lookup,source_hash)):
            method='standard_pdf_response'
            location={**location,'bbox':deepcopy(slot['bbox']),'source_cell_bbox':deepcopy(location.get('bbox'))}
        else:
            writable,reason=False,'PDF 局部填写区缺少原件空白、表格边界或字段关联证据'
    elif slot and slot.get('kind')=='native_matrix_blank_writein' and location.get('type')=='pdf':
        from .validation.native_matrix_writeins import verified_matrix_writein
        if (ref.get('start')==ref.get('end')==slot.get('start')==slot.get('end') and
            verified_matrix_writein(block,slot,lookup,source_hash)):
            method='standard_pdf_response'
            location={**location,'bbox':deepcopy(slot['bbox']),'source_label_bbox':deepcopy(location.get('bbox'))}
        else:
            writable,reason=False,'PDF 矩阵末尾填写区缺少原件空白、完整行组或位置证据'
    elif slot and slot.get('kind')=='native_pdf_blank_tail' and location.get('type')=='pdf':
        start,end=ref.get('start'),ref.get('end');box=slot.get('bbox',[]);cell=location.get('bbox',[])
        native_spans=structure.get('native_text_spans',[])
        if (location.get('extraction')=='native' and slot.get('evidence')=='single_column_repeated_ruled_form_rows' and
            slot.get('blank_verified') is True and slot.get('expected_source_text')==block.get('text','') and
            isinstance(start,int) and start==end==slot.get('start')==slot.get('end') and 0<=start<=len(block.get('text','')) and
            len(box)==len(cell)==4 and cell[0]<=box[0]<box[2]<=cell[2] and cell[1]<=box[1]<box[3]<=cell[3] and
            native_spans and all(len(sp.get('bbox',[]))==4 and
                (sp['bbox'][2]<=box[0] or sp['bbox'][0]>=box[2] or sp['bbox'][3]<=box[1] or sp['bbox'][1]>=box[3]) for sp in native_spans)):
            method='standard_pdf_response'
        else:
            writable,reason=False,'PDF 同格填写区缺少实际空白和原文范围证据'
    elif slot and slot.get('kind')=='native_pdf_source_value' and location.get('type')=='pdf':
        start,end=ref.get('start'),ref.get('end')
        if (location.get('extraction')=='native' and slot.get('evidence')=='repeated_physical_label_value_lanes' and
            isinstance(start,int) and isinstance(end,int) and 0<=start<end<=len(block.get('text','')) and
            slot.get('start')==start and slot.get('end')==end and len(slot.get('bbox',[]))==4 and
            slot.get('label_refs') and all(lookup.get(lr.get('block_id'),{}).get('location',{}).get('page')==location.get('page') and
                not (lr.get('block_id')==ref.get('block_id') and max(start,lr.get('start',0))<min(end,lr.get('end',0))) for lr in slot['label_refs'])):
            method='standard_pdf_response'
        else:
            writable,reason=False,'PDF 原生已有值缺少有效的独立来源范围'
    elif slot and slot.get('kind')=='native_response_paragraph_group':
        from .parser_core.paragraph_groups import verified_paragraph_group
        if verified_paragraph_group(block,slot) and ref.get('start')==slot.get('start') and ref.get('end')==slot.get('end'):
            method='replace_native_paragraph_group'
        else:
            writable,reason=False,'原生连续答复段落组的范围或原文状态未通过校验'
    elif paragraph_slot and slot.get('kind')=='blank_paragraph_candidate' and not controls:
        method = "fill_blank_paragraph"
    elif slot and slot.get('kind') == 'blank_line_slot':
        index, count = slot.get('line_index'), slot.get('line_count')
        if (block.get('kind') == 'table_cell' and not block.get('text','').strip() and not controls and
            structure.get('blank_state') == 'blank' and isinstance(index,int) and isinstance(count,int) and 0 <= index < count <= 20):
            method = 'fill_blank_line_slots'
        else:
            writable, reason = False, '复合答复子行缺少原生空白单元格或合法分组位置'
    elif control:
        ctype = control.get("type")
        if control.get("data_binding"):
            writable, reason = False, "内容控件存在数据绑定，当前不自动写入以避免显示值与绑定值不一致"
        elif control.get("lock") or control.get("locked"):
            writable, reason = False, "内容控件具有锁定限制，当前不自动修改"
        elif control.get('source') == 'word_symbol':
            if ctype == 'checkbox' and control.get('symbol_index') and control.get('paragraph_index'):
                method='set_symbol_checkbox'
            else:
                writable,reason=False,'原生符号的用途或勾选状态尚不明确，保留原符号并要求核对'
        elif control.get("source") == "legacy_form_field":
            if ctype=='checkbox' and control.get('paragraph_index') and control.get('control_index'):
                method='set_legacy_checkbox'
            elif ctype=='text' and control.get('legacy_result_supported') and control.get('paragraph_index') and control.get('control_index'):
                method='set_legacy_text'
            else:
                writable, reason = False, "旧式表单域缺少明确控件位置或类型暂不支持"
        elif ctype == "checkbox":
            method = "set_checkbox"
        elif ctype in {"dropdown", "combo_box"}:
            method = "set_dropdown"
        elif ctype == "date":
            method = "set_date_control"
        else:
            method = "set_text_control"
    elif slot and slot.get('kind')=='trailing_underlined_blank':
        method='fill_underlined_blank'
    elif slot and slot.get('kind')=='printed_option' and slot.get('option_label')==block.get('text','')[slot.get('start',0):slot.get('end',0)].strip():
        method='standard_pdf_response' if location.get('type')=='pdf' else 'replace_text_range'
    elif slot_id and (
        any(slot_id.endswith(suffix) for suffix in (":inline-placeholder", ":underline-slot", ":tab-slot"))
        or ":prompt-slot:" in slot_id
    ):
        method = "replace_text_range"
    elif ":choice:" in slot_id and int(ref.get("start", 0)) < int(ref.get("end", 0)):
        method = "replace_text_range"
    elif slot and slot.get('kind') == 'template_placeholder_candidate' and block.get('text','')[ref.get('start',0):ref.get('end',0)] == slot.get('placeholder_text'):
        method = "replace_text_range"
    elif slot and slot.get('kind') == 'inline_prompt_candidate' and slot.get('start') == slot.get('end') == len(block.get('text','')):
        method = "append_after_label"
    elif slot_id.endswith(":after-content"):
        method = "append_after_label"
    elif slot_id.endswith(":before-content"):
        method = "prepend_before_unit"
    elif block.get("kind") == "table_cell" and (structure.get("blank_state") == "blank" or not block.get("text")):
        method = "set_choice_cell" if structure.get("local_layout_type") == "choice_matrix" else "fill_blank_cell"
    elif block.get("kind") == "table_cell" and int(ref.get("start", 0)) < int(ref.get("end", 0)):
        method = "replace_existing_value"
    else:
        writable, reason = False, "尚无受支持的精确写入方式"
    if controls and control is None:
        writable, reason = False, "同一区域包含多个控件，尚未唯一定位目标控件"
    verified_scan_subregion = bool(slot and slot.get('kind')=='scan_observed_blank_tail' and writable
                                   and method=='standard_pdf_response')
    if structure.get("is_merge_continuation") or structure.get("blank_state") in {"merge_continuation", "missing"} or (not verified_scan_subregion and (structure.get('blank_state')=='unreadable' or structure.get("content_state") in {"unreadable", "image_only"})):
        writable, reason = False, "合并延续、缺省或无法读取的区域不能作为普通空白填写格"
    # A PDF physical cell is not a Word XML cell even when both share the
    # structured table_cell kind. Valid PDF targets only generate standard
    # Word output; never advertise a template mutation method for them.
    if location.get('type') == 'pdf' and writable:
        method = 'standard_pdf_response'
    return {
        "target_id": slot_id or structure.get("cell_id") or ref.get("block_id"),
        "source_hash": source_hash,
        "parser_version": parser_version,
        "block_id": ref.get("block_id"),
        "ref": deepcopy(ref),
        "location": deepcopy(location),
        "structure": {key: structure.get(key) for key in (
            "table_id", "cell_id", "row_start", "row_end", "grid_col_start", "grid_col_end",
            "merge_anchor_id", "blank_state", "content_state", "local_layout_type", "record_header_row", "record_index",
        )},
        "control": deepcopy(control),
        "paragraph_index": paragraph_slot.get("paragraph_index") if paragraph_slot else None,
        "paragraph_id": paragraph_slot.get("paragraph_id") if paragraph_slot else None,
        "expected_paragraph_text": paragraph_info.get('text','') if paragraph_info else ('' if paragraph_slot else None),
        "paragraph_source_start": paragraph_info.get('start',0) if paragraph_info else None,
        "expected_text": str(block.get("text") or ""),
        "expected_state": 'blank' if slot and slot.get('kind') in {'native_table_question_tail','native_table_underlined_gap'} and writable else structure.get("blank_state") or ("filled" if block.get("text") else "blank"),
        "paragraph_group": deepcopy(slot.get('paragraph_group')) if slot and method=='replace_native_paragraph_group' else None,
        "write_method": method,
        "line_index": slot.get('line_index') if slot and method == 'fill_blank_line_slots' else None,
        "line_count": slot.get('line_count') if slot and method == 'fill_blank_line_slots' else None,
        "preserve_option_label": slot.get('option_label') if slot and slot.get('kind')=='printed_option' else None,
        "output_modes": ['standard'] if location.get('type')=='pdf' else ['standard','template'],
        "writable": writable,
        "validation_reason": reason,
    }


def build_fill_target(ref: dict, lookup: dict[str, dict], source_hash: str, parser_version: str, approved_definition: dict | None = None) -> dict | None:
    """Build one validated write target from a physical source reference.

    The API uses this for a human-corrected mapping.  Keeping target creation
    here ensures automatic and manual mappings obey the same write whitelist.
    """
    target = _target(ref, lookup, source_hash, parser_version)
    if target:
        reasons = validate_form_definition({**(approved_definition or {}), "source_hash": source_hash, "parser_version": parser_version, "fill_targets": [target]}, lookup, source_hash)
        if reasons:
            target["writable"] = False
            target["validation_reason"] = "；".join(reasons)
    return target


def build_form_definition(field: dict, lookup: dict[str, dict], source_hash: str, parser_version: str) -> dict:
    local_personnel_refs=_local_personnel_context_refs(field,lookup)
    if local_personnel_refs:
        refs=list(field.get('context_refs') or [])+local_personnel_refs
        field['context_refs']=list({(r['block_id'],r.get('start',0),r.get('end',0)):r for r in refs}.values())
    field_type, type_evidence = infer_field_type(field, lookup)
    responsibility, responsibility_evidence = infer_responsibility(field, lookup)
    required, required_evidence = infer_required(field, lookup)
    model_hints = field.get("model_hints") or {}
    topic = classify_topic(str(field.get("question_text_original") or ""), str(field.get("section_text_original") or ""), _context(field, lookup))
    if topic["topic_key"] == "unclassified" and model_hints.get("topic_hint") not in {None, "", "unclassified"}:
        hinted = next((item for item in TOPIC_REGISTRY if item.key == model_hints["topic_hint"]), None)
        if hinted:
            topic = {"topic_key": hinted.key, "topic_label": hinted.label, "topic_parent": hinted.parent, "topic_confidence": "model_proposed", "topic_evidence": ["local_qwen_field_hint"]}
    if field_type == "short_text" and model_hints.get("field_type_hint"):
        # A category code is not source evidence for a date, choice list or
        # numeric restriction. Keep such proposals visible without changing
        # the editor into an empty option group or a restrictive date input.
        if model_hints["field_type_hint"] in {"short_text", "long_text"}:
            field_type = model_hints["field_type_hint"]
            type_evidence.append("本地 Qwen 根据字段上下文给出的文本长度提示")
        else:
            type_evidence.append("模型提出其他字段类型，未获原文或控件佐证，保留可编辑文本")
    if responsibility == "unknown" and model_hints.get("responsibility_hint") in {"customer", "reviewer", "shared"}:
        responsibility_evidence.append("模型提示可能属于其他填写方，但缺少可核验的原文责任声明；保留责任待确认并停用自动复用")
    if required == "unknown" and model_hints.get("required_hint") in {"required", "optional"}:
        required_evidence.append("模型提出必填性提示，但没有可核验的原文依据，保留未知")
    targets = [target for ref in (field.get("answer_region_refs") or []) if (target := _target(ref, lookup, source_hash, parser_version))]
    pairing_valid = field.get("pairing_validation") == "valid"
    is_signature = field_type == "manual_signature"
    reserved_record_slot = field.get("record_slot_status") == "reserved"
    ambiguous_record = field.get('record_identity_status') == 'ambiguous'
    condition=field.get('explicit_condition') or infer_condition(field,lookup)
    writable = bool(targets) and all(x["writable"] for x in targets) and not is_signature
    fillable = "fillable" if writable else ("review_required" if targets else "not_located")
    from .validation.word_table_context import verified_group_container
    source_group_container = bool(field_type == 'composite' and pairing_valid and verified_group_container(field,lookup))
    if source_group_container:
        fillable = 'container'
    # An unknown owner may still fill the field manually, but knowledge must
    # never be written automatically until the document or a human confirms
    # that the region belongs to our side.
    original_state = field.get('original_answer_status') or field.get('answer_status')
    auto_eligible = bool(writable and pairing_valid and responsibility == "our_side" and not reserved_record_slot and not ambiguous_record and not condition and original_state not in {'unresolved','unreadable'})
    signatory_refs = _signatory_source_refs(field, lookup)
    signing_metadata = bool(signatory_refs)
    participant_refs=_current_participant_source_refs(field,lookup)
    current_participant=bool(participant_refs)
    from .validation.pdf_attachment_groups import current_attachment_evidence
    attachment_confirmation_refs=current_attachment_evidence(field,lookup)
    if attachment_confirmation_refs:
        auto_eligible=False
    if current_participant:
        auto_eligible=False
        type_evidence.append('原文要求本次审计人员或审计日期，需确认当前人员与事件，不能复用通用历史联系人')
        refs=list(field.get('_native_display_context_refs') or [])+participant_refs
        field['_native_display_context_refs']=list({(r['block_id'],r['start'],r['end']):r for r in refs}.values())
    if signing_metadata:
        auto_eligible = False
        type_evidence.append('当前原文签署栏的姓名、地点、日期或职位须由本次授权签署人确认，不能从历史资料自动代填')
    if re.fullmatch(r'name|title(?:\s*/\s*department)?|position|姓名|职位', str(field.get('question_text_original','')).strip(), re.I):
        for label_ref in field.get('label_refs', []):
            label_block = lookup.get(label_ref.get('block_id'), {})
            native = label_block.get('structure', {})
            neighbors = [label_block]
            if native.get('table_id'):
                neighbors += [b for b in lookup.values() if b.get('structure',{}).get('table_id') == native['table_id'] and b.get('structure',{}).get('row_start') == native.get('row_start')]
                local_table = [b for b in lookup.values() if b.get('structure',{}).get('table_id') == native['table_id']]
                if len(local_table) <= 10:
                    neighbors += local_table
            elif label_block.get('kind') == 'paragraph':
                location = label_block.get('location', {})
                number = location.get('paragraph')
                if isinstance(number,int):
                    neighbors += [b for b in lookup.values() if b.get('kind') == 'paragraph' and
                        b.get('location',{}).get('part') == location.get('part') and
                        isinstance(b.get('location',{}).get('paragraph'),int) and
                        0 < b['location']['paragraph']-number <= 2 and
                        re.match(r'^\s*(?:name|title|position|sign(?:ature)?)\b',b.get('text',''),re.I)]
            if re.search(r'\bsign(?:ature)?\b|\bcompany\s+seal\b|questionnaire\s+(?:filled|completed)\s+by|签字|签名', ' '.join(b.get('text','') for b in neighbors), re.I):
                signing_metadata = True
        if signing_metadata:
            auto_eligible = False
            type_evidence.append('同一原生签署栏的姓名或职位需由本次授权签署人确认，不从通用人员知识自动代填')
    response_format_conflict=None
    option_names=[str(o.get('text_original') or '').strip() for o in field.get('options',[])]
    only_boolean=bool(option_names) and all(re.fullmatch(r'yes|no|n\s*/?\s*a|not applicable|是|否|不适用',name,re.I) for name in option_names)
    label=str(field.get('question_text_original') or '')
    # Explicit requests for details need a text response. "Please state if"
    # and "indicate whether" are boolean prompts, so don't infer a missing
    # text component from the verb alone.
    detail_request = (r'(?:please\s+)?(?:give|describe|explain|specify|mention|list|state|provide|indicate|write)'
                      r'\b(?!\s+(?:if|whether)\b)')
    open_requirement=bool(re.search(r'^\s*(?:\d+(?:\.\d+)*[.)]?\s*)?(?:(?:(?:since|approximately|about)\s+)?how\s+(?:many|much|long)|what\b|which\b|'+detail_request+r'|请(?:说明|描述)|多少|如何)',label,re.I))
    # An explicit conditional request in the same printed prompt still needs
    # a text/number response. A Yes tick cannot supply a person's identity or
    # duration; ordinary procedural mentions of "who" do not trigger this.
    open_requirement = open_requirement or bool(re.search(
        r'\bif\s+(?:so|yes)\b\s*[,，:：]?\s*(?:(?:for\s+)?how\s+(?:long|many|much)\b|who\b|what\b|which\b|'+detail_request+r')'
        r'|(?:若是|如果是|如是)\s*[,，:：]?\s*请(?:说明|提供|描述)', label, re.I))
    text_child_located = only_boolean and open_requirement and any(
        c.get('pairing_validation') == 'valid' and c.get('answer_region_refs') and not c.get('options')
        and all((target := _target(r, lookup, source_hash, parser_version)) and target.get('writable')
                for r in c['answer_region_refs']) for c in field.get('children', []))
    # "List of ..." beneath a physically verified Attached header asks
    # whether the list is supplied on this occasion. The choice is complete
    # for that question, but remains a protected current-submission fact.
    # Other attachment instructions do not waive missing-detail checks.
    from .validation.pdf_submission_groups import attached_matrix_refs
    attached_status = bool(only_boolean and open_requirement and attached_matrix_refs(field, lookup))
    if only_boolean and open_requirement and not text_child_located and not attached_status:
        response_format_conflict={'status':'needs_review','reason':'原问题要求数量、明细或说明，但原模板仅提供 Yes / No / N/A；需人工确认答复方式，不能把选择标记当作完整业务答案。','provided_options':option_names,'label_refs':deepcopy(field.get('label_refs') or [])}
        auto_eligible=False
    reasons = []
    if not pairing_valid: reasons.append("字段与填写区域配对未通过校验")
    if responsibility != "our_side": reasons.append("填写责任尚未确认属于我方，知识库不会自动写入")
    if is_signature: reasons.append("签名或盖章必须人工完成")
    elif signing_metadata: reasons.append('本次签署信息需要签署人确认，不能自动复用历史人员、地点或日期')
    if current_participant: reasons.append('本次审计人员、联系人或日期须人工确认；填写位置仍可编辑')
    if attachment_confirmation_refs: reasons.append('原文询问本次是否已附复印件；历史答案不能证明本次附件已提交，请确认实际附件')
    if reserved_record_slot: reasons.append("空白预留记录尚未启用，不参与自动填报或未回答统计")
    if ambiguous_record: reasons.append(field.get('record_identity_reason') or '重复记录缺少唯一业务对象，不能按行号套用历史答案')
    if condition:reasons.append('条件填写项需要确认适用性；保留候选，不自动决定是否适用')
    if response_format_conflict:reasons.append(response_format_conflict['reason'])
    reasons.extend(x["validation_reason"] for x in targets if x.get("validation_reason"))
    from .source_section_scope import source_section_scope
    return {
        "schema_version": FORM_POSITION_VERSION,
        "field_id": field.get("field_id") or field.get("question_id"),
        "source_hash": source_hash,
        "parser_version": parser_version,
        "label_refs": deepcopy(field.get("label_refs") or field.get("question_refs") or []),
        "template_placeholder_candidates": deepcopy(field.get('template_placeholder_candidates') or []),
        "section_refs": deepcopy(field.get("section_refs") or []),
        "source_section_scope": source_section_scope(field, lookup, source_hash),
        "instruction_refs": deepcopy(field.get('instruction_refs') or []),
        "source_note_groups": deepcopy(field.get('source_note_groups') or []),
        "field_type": field_type,
        "field_type_evidence": type_evidence,
        "model_hints": deepcopy(model_hints),
        "topic_classification": topic,
        "fill_responsibility": responsibility,
        "responsibility_evidence": responsibility_evidence,
        "responsibility_policy_version": OWNER_POLICY_VERSION,
        "responsibility_source_refs": deepcopy(field.get('_document_owner_evidence_refs') or field.get('_native_owner_evidence_refs') or field.get('_pdf_owner_evidence_refs') or []),
        **({"pdf_owner_scope_version": PDF_OWNER_SCOPE_VERSION}
           if any(lookup.get(r.get('block_id'), {}).get('location', {}).get('type') == 'pdf' for r in field.get('label_refs') or []) else {}),
        "display_context_refs": deepcopy(field.get('_native_display_context_refs') or []),
        "display_context": _text(field.get('_native_display_context_refs') or [],lookup),
        "required_status": required,
        "required_evidence": required_evidence,
        "applicability_condition": condition,
        "requires_signatory_confirmation": signing_metadata,
        "signatory_evidence_refs": deepcopy(signatory_refs),
        "requires_current_person_confirmation": current_participant,
        "current_person_evidence_refs": deepcopy(participant_refs),
        "requires_current_attachment_confirmation": bool(attachment_confirmation_refs),
        "current_attachment_evidence_refs": deepcopy(attachment_confirmation_refs),
        **({"local_personnel_scope_version": LOCAL_PERSONNEL_SCOPE_VERSION,
            "personnel_role_source_refs": deepcopy(local_personnel_refs)} if local_personnel_refs or participant_refs else {}),
        "response_format_conflict": response_format_conflict,
        "fill_targets": targets,
        "existing_value_refs": deepcopy(field.get("answer_value_refs") or []),
        "existing_value_status": field.get("original_answer_status") or field.get("answer_status") or "not_located",
        "protected_refs": deepcopy(field.get("label_refs") or field.get("question_refs") or []),
        "allowed_write_methods": list(dict.fromkeys(x["write_method"] for x in targets if x.get("writable") and x.get("write_method"))),
        "location_evidence": "validated_answer_region_refs" if targets else "none",
        "fillable_status": fillable,
        "auto_fill_eligible": auto_eligible,
        "auto_fill_eligible_if_condition_met": bool(condition and pairing_valid and writable and responsibility == 'our_side' and not is_signature and not reserved_record_slot and not ambiguous_record and not signing_metadata and not current_participant and not attachment_confirmation_refs and not response_format_conflict),
        "validation_status": "valid" if pairing_valid and (writable or source_group_container) else "needs_review",
        **({'source_group_container': True} if source_group_container else {}),
        "validation_reasons": list(dict.fromkeys(reasons)),
        "mapping_review_status": "model_or_native",
        "record_slot_status": field.get("record_slot_status"),
        "record_identity_status": field.get('record_identity_status'),
        "record_identity_reason": field.get('record_identity_reason'),
        "record_columns": deepcopy(field.get('record_columns') or []),
        "record_context_values": deepcopy(field.get('record_context_values') or []),
        "unit_text_original": field.get('unit_text_original') or _text(field.get('unit_refs') or [],lookup),
        "semantic_context": _text(field.get('context_refs') or [],lookup),
        "record_index": field.get('record_index'),
        "record_column_index": field.get('record_column_index'),
        "record_context_refs": deepcopy(field.get('record_context_refs') if 'record_context_refs' in field else field.get('context_refs') or []),
        "record_context": _text(field.get('record_context_refs') if 'record_context_refs' in field else field.get('context_refs') or [],lookup),
        **({'whole_record_requirement': {
            'label_refs':deepcopy(field.get('label_refs') or []),
            'context_refs':deepcopy(field.get('context_refs') or []),
            'requirement_text':_text(field.get('label_refs') or [],lookup),
            'object_context':_text(field.get('context_refs') or [],lookup),
        }} if field.get('whole_record_response') and field.get('compound_requirement_refs') and
            field.get('local_grid_evidence',{}).get('kind')=='closed_compound_parent_by_rows_and_destination_columns' else {}),
    }


def enrich_result_form_positions(result: dict) -> dict:
    lookup = {x["block_id"]: x for x in result.get("source_blocks") or []}
    source_hash, parser_version = str(result.get("source_hash") or ""), str(result.get("parser_version") or "")
    inventory = []

    visited: list[dict] = []

    def visit(field: dict, parent_topic: dict | None = None) -> None:
        definition = build_form_definition(field, lookup, source_hash, parser_version)
        topic = definition.get("topic_classification") or {}
        if topic.get("topic_key") == "unclassified" and parent_topic and parent_topic.get("topic_key") != "unclassified":
            definition["topic_classification"] = {**deepcopy(parent_topic), "topic_confidence": "inherited_from_parent", "topic_evidence": ["parent_composite_or_record"]}
        field["form_definition"] = definition
        for key in ("field_type", "fill_responsibility", "required_status", "applicability_condition", "fillable_status", "auto_fill_eligible", "allowed_write_methods", "location_evidence"):
            field[key] = deepcopy(definition[key])
        inventory.append(deepcopy(definition))
        visited.append(field)
        for child in field.get("children") or []:
            visit(child, definition.get("topic_classification"))

    for field in result.get("questions") or []:
        visit(field)
    protected = protected_source_refs(result, lookup)
    for field in visited:
        definition = field["form_definition"]
        errors = validate_form_definition(definition, lookup, source_hash, protected)
        if errors:
            definition["validation_status"] = "needs_review"
            definition["validation_reasons"] = list(dict.fromkeys(definition["validation_reasons"] + errors))
            definition["auto_fill_eligible"] = False
            definition["auto_fill_eligible_if_condition_met"] = False
            definition["fillable_status"] = "review_required" if definition.get("fill_targets") else "not_located"
            field["auto_fill_eligible"] = False
            field["fillable_status"] = definition["fillable_status"]
    # Short labels such as Position/Date are ambiguous alone.  They are only
    # classified as approval/signature metadata when their immediate physical
    # sequence contains an explicit Completed by, Reviewed by or Signature
    # label.  This uses local document order and does not generalize all dates.
    for index, field in enumerate(visited):
        definition = field.get("form_definition") or {}
        topic = definition.get("topic_classification") or {}
        label = re.sub(r"\s+", " ", str(field.get("question_text_original") or "").strip().lower())
        if topic.get("topic_key") != "unclassified" or not re.fullmatch(r"(?:职\s*位(?:\s*position)?|position|日\s*期(?:\s*date)?|date)", label, re.I):
            continue
        nearby = "\n".join(str(item.get("question_text_original") or "") for item in visited[max(0, index-2):min(len(visited), index+3)]).lower()
        if re.search(r"填表人|审核人|completed by|reviewed by|signature|签名", nearby, re.I):
            definition["topic_classification"] = {"topic_key": "review.signature", "topic_label": "审核与签署", "topic_parent": "审批", "topic_confidence": "high", "topic_evidence": ["adjacent_signature_sequence"]}
            field["form_definition"] = definition
    from .local_condition_scopes import attach_local_conditions
    attach_local_conditions(result.get("source_blocks") or [], visited, source_hash)
    for field in visited:
        _attach_source_value_validation(field)
    inventory = [deepcopy(field["form_definition"]) for field in visited]
    result["fill_position_inventory"] = inventory
    result["form_position_version"] = FORM_POSITION_VERSION
    return result


def _attach_source_value_validation(field: dict) -> None:
    """Location validity and observed-answer validity are separate evidence."""
    from .knowledge import answer_value_consistency_reasons
    reasons = answer_value_consistency_reasons(
        field.get('question_text_original') or '', field.get('answer_text_original') or '')
    status = 'conflict' if reasons else 'no_detected_conflict'
    field['source_value_validation'] = status
    field['source_value_reasons'] = reasons
    definition = field.get('form_definition') or {}
    definition['source_value_validation'] = status
    definition['source_value_reasons'] = reasons
    if reasons:
        definition['auto_fill_eligible'] = False
        definition['auto_fill_eligible_if_condition_met'] = False
        field['auto_fill_eligible'] = False
    # Keep physical validation, original text and write targets intact. A
    # correction remains possible; a historical contradiction cannot grant reuse.


def upgrade_responsibility_definition(field: dict, definition: dict, lookup: dict[str, dict], source_hash: str) -> dict:
    """Update responsibility from saved source, never answer text or targets.

    Manual mapping decisions, invalid locations and document-version changes
    are not repaired or reauthorized here. Snapshots retain the prior policy.
    """
    updated=deepcopy(definition)
    if not definition or definition.get('responsibility_policy_version')==OWNER_POLICY_VERSION:
        return updated
    if definition.get('mapping_review_status')=='human_corrected':
        return updated
    if not source_hash or definition.get('source_hash')!=source_hash:
        return updated
    labels=field.get('label_refs') or field.get('question_refs') or []
    if not labels or any(not isinstance(r.get('start'),int) or not isinstance(r.get('end'),int) or
        not 0<=r['start']<r['end']<=len(str(lookup.get(r.get('block_id'),{}).get('text',''))) for r in labels):
        return updated
    role,evidence=infer_responsibility(field,lookup)
    updated.update(responsibility_policy_version=OWNER_POLICY_VERSION,
        fill_responsibility=role,responsibility_evidence=evidence,
        responsibility_source_refs=deepcopy(field.get('_document_owner_evidence_refs') or field.get('_native_owner_evidence_refs') or field.get('_pdf_owner_evidence_refs') or []))
    participant_refs=_current_participant_source_refs(field,lookup)
    if participant_refs:
        updated['requires_current_person_confirmation']=True
        updated['current_person_evidence_refs']=deepcopy(participant_refs)
    signatory_refs = _signatory_source_refs(field, lookup)
    if signatory_refs:
        updated['requires_signatory_confirmation'] = True
        updated['signatory_evidence_refs'] = deepcopy(signatory_refs)
    from .validation.pdf_attachment_groups import current_attachment_evidence
    attachment_confirmation_refs=current_attachment_evidence(field,lookup)
    if attachment_confirmation_refs:
        updated['requires_current_attachment_confirmation']=True
        updated['current_attachment_evidence_refs']=deepcopy(attachment_confirmation_refs)
    safe=bool(updated.get('validation_status')=='valid' and updated.get('fillable_status')=='fillable' and
        updated.get('fill_targets') and all(t.get('writable') and (t.get('ref') or {}).get('block_id') in lookup for t in updated['fill_targets']) and
        role=='our_side' and updated.get('field_type') not in {'manual_signature','composite','repeating_record'} and
        not updated.get('requires_current_person_confirmation') and not updated.get('requires_signatory_confirmation') and
        not updated.get('requires_current_attachment_confirmation') and
        not updated.get('response_format_conflict') and updated.get('record_slot_status')!='reserved' and
        updated.get('record_identity_status')!='ambiguous' and updated.get('existing_value_status') not in {'unresolved','unreadable','not_located'})
    updated['auto_fill_eligible']=safe and not bool(updated.get('applicability_condition'))
    updated['auto_fill_eligible_if_condition_met']=safe and bool(updated.get('applicability_condition'))
    old_reason='填写责任尚未确认属于我方，知识库不会自动写入'
    reasons=[r for r in updated.get('validation_reasons',[]) if r!=old_reason]
    if role!='our_side':reasons.append(old_reason)
    if participant_refs:reasons.append('本次审计人员、联系人或日期须人工确认；填写位置仍可编辑')
    if signatory_refs:reasons.append('本次签署信息需要签署人确认，不能自动复用历史人员、地点或日期')
    if attachment_confirmation_refs:reasons.append('原文询问本次是否已附复印件；历史答案不能证明本次附件已提交，请确认实际附件')
    updated['validation_reasons']=list(dict.fromkeys(reasons))
    return updated


def refresh_result_responsibility(result: dict) -> dict:
    """Derive a versionable copy without OCR/model calls or source changes."""
    refreshed=deepcopy(result)
    lookup={b['block_id']:b for b in refreshed.get('source_blocks',[])}
    inventory=[]
    def visit(field):
        field['form_definition']=upgrade_responsibility_definition(field,field.get('form_definition',{}),lookup,str(refreshed.get('source_hash') or ''))
        _attach_source_value_validation(field)
        for key in ('fill_responsibility','auto_fill_eligible'):
            if key in field['form_definition']:field[key]=field['form_definition'][key]
        inventory.append(deepcopy(field['form_definition']))
        for child in field.get('children',[]):visit(child)
    for field in refreshed.get('questions',[]):visit(field)
    refreshed['fill_position_inventory']=inventory
    refreshed['responsibility_policy_version']=OWNER_POLICY_VERSION
    return refreshed


def validate_form_definition(definition: dict, lookup: dict[str, dict], source_hash: str, protected_refs: list[dict] | None = None) -> list[str]:
    reasons = []
    if definition.get("source_hash") != source_hash:
        reasons.append("填写位置不属于当前原文件版本")
    labels = list(definition.get("label_refs") or []) + list(protected_refs or _native_protected_refs(lookup))
    approved = confirmed_template_ranges(definition,lookup,source_hash)
    labels = _without_approved_template_ranges(labels,approved)
    for target in definition.get("fill_targets") or []:
        ref = target.get("ref") or {}
        block = lookup.get(ref.get("block_id"))
        if not block:
            reasons.append("填写目标引用不存在")
            continue
        native_slot = next((s for s in block.get('structure',{}).get('logical_slots',[]) if s.get('slot_id') == ref.get('slot_id')),None)
        if native_slot and native_slot.get('kind') == 'template_placeholder_candidate' and _ref_key(ref) not in {_ref_key(r) for r in approved}:
            reasons.append('模板文字占位尚未由用户确认允许替换')
        if any(_overlaps(ref, label) for label in labels):
            reasons.append("填写目标与标签引用重叠")
        text = str(block.get("text") or "")
        start, end = ref.get("start", 0), ref.get("end", 0)
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end <= len(text):
            reasons.append("填写目标文字范围无效")
            continue
        if target.get("source_hash") != source_hash or target.get("block_id") != block["block_id"]:
            reasons.append("填写目标与原文件版本或来源 ID 不一致")
        fresh = _target(ref, lookup, source_hash, str(definition.get("parser_version") or ""))
        expected_location = block.get("location") or {}
        if (native_slot and native_slot.get('kind') in {'native_matrix_blank_writein','native_table_question_tail','native_table_underlined_gap'} and fresh and fresh.get('writable')):
            # A verified logical PDF subregion is below its label block. Its
            # precise bbox must equal the independently rebuilt source slot;
            # no generic location mismatch exemption is granted.
            expected_location = fresh['location']
        if target.get("location") != expected_location:
            reasons.append("填写目标的物理位置与来源引用不一致")
        for key, value in (target.get("structure") or {}).items():
            if value != (block.get("structure") or {}).get(key):
                reasons.append("填写目标的网格或合并关系与来源不一致")
                break
        if not fresh or not fresh.get("writable") or not target.get("writable"):
            reasons.append((fresh or {}).get("validation_reason") or "填写目标不可写")
        elif target.get("write_method") != fresh.get("write_method") or target.get("control") != fresh.get("control"):
            reasons.append("填写方式或控件与原生来源不一致")
        if fresh and any(target.get(key) != fresh.get(key) for key in ("paragraph_index", "paragraph_id", "expected_paragraph_text", "line_index", "line_count", "paragraph_group")):
            reasons.append("填写段落位置与原生来源不一致")
        slot_id = str(ref.get("slot_id") or "")
        slots = (block.get("structure") or {}).get("logical_slots") or []
        if slot_id and ":choice:" not in slot_id and ':control:' not in slot_id:
            slot = next((s for s in slots if s.get("slot_id") == slot_id), None)
            if slot is None or (start, end) != (slot.get("start", slot.get("offset")), slot.get("end", slot.get("offset"))):
                reasons.append("填写槽位不存在或范围与原生槽位不一致")
        if ":choice:" in slot_id and (end - start != 1 or text[start:end] not in "□☐☑☒✓✔"):
            reasons.append("选择控件的符号范围无效")
        if start == end and text and not slot_id and not target.get("control"):
            reasons.append("非空区域中的零长度引用不是已验证的填写槽位")
        if target.get("expected_text") != str(block.get("text") or ""):
            reasons.append("填写目标原文状态已经变化")
        if target.get("write_method") not in {
            "fill_blank_cell", "replace_existing_value", "replace_text_range", "replace_native_paragraph_group", "append_after_label", "prepend_before_unit",
            "set_text_control", "set_checkbox", "set_dropdown", "set_date_control", "set_choice_cell",
            "fill_blank_paragraph", "fill_blank_line_slots", "set_legacy_checkbox", "set_legacy_text", "set_symbol_checkbox", "fill_underlined_blank", "standard_pdf_response",
        }:
            reasons.append("填写目标写入方式不受支持")
    return list(dict.fromkeys(reasons))


def make_writeback_plan(result: dict) -> tuple[list[dict], list[str]]:
    lookup = {x["block_id"]: x for x in result.get("source_blocks") or []}
    source_hash = str(result.get("source_hash") or "")
    plan, warnings = [], []
    protected = protected_source_refs(result, lookup)

    def visit(field: dict) -> None:
        answer = str(field.get("answer_text_original") or "")
        selected = field.get("selected_options") or []
        if field.get("manual_override") or field.get("answer_origin") == "reviewed_knowledge":
            definition = field.get("form_definition") or build_form_definition(field, lookup, source_hash, str(result.get("parser_version") or ""))
            errors = validate_form_definition(definition, lookup, source_hash, protected)
            if definition.get("validation_status") != "valid": errors.append("填写位置尚未通过校验")
            if field.get("pairing_validation") != "valid" and not confirmed_template_ranges(definition,lookup,source_hash): errors.append("字段与答复区域的配对未通过校验")
            if definition.get("field_type") == "manual_signature" or infer_field_type(field, lookup)[0] == "manual_signature":
                errors.append("签字盖章区域必须人工签署，不能写入生成文本")
            owner, _ = infer_responsibility(field, lookup)
            if definition.get("fill_responsibility") in {"customer", "reviewer", "shared"} or (owner in {"customer", "reviewer", "shared"} and definition.get("mapping_review_status") != "human_corrected"):
                errors.append("该填写区属于客户、审核人或共同填写，不能代填")
            if field.get("answer_origin") == "reviewed_knowledge" and definition.get("fill_responsibility") != "our_side":
                errors.append("填写责任尚未确认属于我方，历史答案不能自动写回")
            if field.get('answer_origin') == 'reviewed_knowledge' and definition.get('applicability_condition') and definition.get('applicability_status') != 'applicable':
                errors.append('父题尚未满足此填写位置的条件，不能自动写回历史答案')
            targets = definition.get("fill_targets") or []
            if not field.get('options') and any(t.get('write_method') in {'set_checkbox','set_legacy_checkbox','set_symbol_checkbox'} for t in targets):
                errors.append('复选框缺少已验证的选项关联，不能把普通答案文本转换成选中或未选中；请重新识别此位置')
            allowed_refs = {_ref_key(r) for r in field.get("answer_region_refs") or []}
            if any(_ref_key(t.get("ref") or {}) not in allowed_refs for t in targets):
                errors.append("写入目标不属于该字段已验证的答复区域")
            if not targets: errors.append("未定位可写填写区域")
            selected_choice_indices=set()
            if field.get('options') and selected:
                from .choice_identity import selected_indices
                try:selected_choice_indices=selected_indices(field['options'],selected)
                except ValueError as exc:errors.append(str(exc))
            if errors:
                warnings.append(f"{field.get('question_text_original')}: {'；'.join(dict.fromkeys(errors))}")
            else:
                if definition.get("field_type") in {"single_select", "multi_select"} and targets:
                    values = ["☒" if index in selected_choice_indices else "☐" for index in range(len(targets))]
                    # A marker and its printed option form one visual choice;
                    # a normal space could strand "No" on the next line.
                    # Restore a separator when the writable slot consumed the
                    # only space immediately following the fixed question.
                    def printed_choice(value,target):
                        if not target.get('preserve_option_label'):return value
                        start=target.get('ref',{}).get('start',0);original=target.get('expected_text','')
                        separator=' ' if start>0 and original[start:start+1].isspace() and not original[start-1].isspace() else ''
                        return separator+value+'\u00a0'+target['preserve_option_label']
                    values = [printed_choice(value,target) for value,target in zip(values,targets)]
                else:
                    values = list(selected) if selected else [answer]
                # A single label spanning consecutive blank response rows is
                # a continuing answer area. Write in its first physical cell;
                # do not repeat the answer or require one answer per empty line.
                ordered = sorted(targets, key=lambda t: t['structure'].get('row_start') or 0)
                continuous = len(ordered)>1 and not selected and all(t['write_method']=='fill_blank_cell' for t in ordered)
                if continuous:
                    structures=[t['structure'] for t in ordered]
                    continuous=all((s['table_id'],s['grid_col_start'],s['grid_col_end'])==(structures[0]['table_id'],structures[0]['grid_col_start'],structures[0]['grid_col_end']) for s in structures) and all(a['row_end']+1==b['row_start'] for a,b in zip(structures,structures[1:]))
                if continuous:
                    targets=ordered[:1]
                if field.get('continuous_response_lines') and len(targets)>1 and not selected:
                    line_targets=sorted(targets,key=lambda t:t['ref']['start'])
                    if len({t['ref']['block_id'] for t in line_targets})==1 and all(t['ref'].get('slot_id') and re.fullmatch(r'[_＿]+',t.get('expected_text','')[t['ref']['start']:t['ref']['end']]) for t in line_targets):
                        targets=line_targets[:1]
                if len(targets) != 1 and len(values) != len(targets):
                    warnings.append(f"{field.get('question_text_original')}: 多个填写位置与答案数量不一致")
                else:
                    if len(targets) == 1:
                        values = ["\n".join(values) if selected else answer]
                    for target, value in zip(targets, values):
                        plan.append({
                            "field_id": field.get("field_id"), "question": field.get("question_text_original"),
                            "answer_source": field.get("answer_origin"), "answer_version": (field.get("knowledge_source") or {}).get("version"),
                            "value": value, "target": deepcopy(target), "preserve_refs": deepcopy(definition.get("protected_refs") or []),
                        })
        for child in field.get("children") or []:
            visit(child)

    for field in result.get("questions") or []:
        visit(field)
    # Reject both sides of an overlapping write, not just whichever appeared
    # second. Distinct, non-overlapping inline slots in one cell remain valid.
    conflicts = set()
    for i, left in enumerate(plan):
        for j in range(i + 1, len(plan)):
            a, b = left["target"], plan[j]["target"]
            if (a.get('write_method') in {'set_legacy_checkbox','set_legacy_text'} and b.get('write_method') in {'set_legacy_checkbox','set_legacy_text'} and
                a.get('block_id') == b.get('block_id') and a.get('control') and b.get('control')):
                control_a,control_b=a['control'],b['control']
                identity_a=(control_a.get('paragraph_index'),control_a.get('control_index'))
                identity_b=(control_b.get('paragraph_index'),control_b.get('control_index'))
                if all(isinstance(x,int) and x>0 for x in identity_a+identity_b) and identity_a!=identity_b:
                    continue
            if (a.get('write_method') == b.get('write_method') == 'fill_blank_line_slots' and
                a.get('block_id') == b.get('block_id') and a.get('line_count') == b.get('line_count') and
                a.get('line_index') != b.get('line_index') and a.get('target_id') != b.get('target_id')):
                continue
            if a.get("block_id") == b.get("block_id") and a.get("paragraph_index") and b.get("paragraph_index") and a["paragraph_index"] != b["paragraph_index"]:
                continue
            same = a.get("target_id") == b.get("target_id") or _overlaps(a["ref"], b["ref"]) or _ref_key(a["ref"])[:3] == _ref_key(b["ref"])[:3]
            if same:
                conflicts.update((i, j))
    for i in sorted(conflicts): warnings.append(f"{plan[i]['question']}: 多个字段的写入目标重叠，已停止相关字段")
    return [item for i, item in enumerate(plan) if i not in conflicts], list(dict.fromkeys(warnings))
