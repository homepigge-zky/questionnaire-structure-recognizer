"""Interpret only native choice evidence, preserving exact source ranges.

This stage does not choose answers. It repairs model classifications that
mistake printed option text for a filled answer. Uncertain states stay unknown.
"""
from copy import deepcopy
import re

VERSION = "native-choice-ranges-v15-source-verified-table-controls"
MARKER = re.compile(r"[□☐☑☒✓✔\uf031]")
CHECKED = set("☑☒✓✔")
COMMON = re.compile(r"(?<!\w)(?:Not\s+Applicable|N\s*/?\s*A|Yes|No|不适用|是|否)(?!\w)", re.I)
_OPTION_WORD = r"(?:Not\s+Applicable|N\s*/?\s*A|Yes|No|不适用|是|否)"
_MARKER_CHAR = r"[□☐☑☒✓✔\uf031]"
CHOICE_ONLY = re.compile(rf"\s*(?:(?:{_OPTION_WORD}\s*{_MARKER_CHAR}[\s,;，；/|]*){{2,}}|(?:{_MARKER_CHAR}\s*{_OPTION_WORD}[\s,;，；/|]*){{2,}})\s*", re.I)
_PLAIN_SEPARATOR = r'(?:\s*[/|]\s*|\s+)'
PLAIN_CHOICES = re.compile(rf'\s*(?:Yes{_PLAIN_SEPARATOR}No(?:{_PLAIN_SEPARATOR}N\s*/?\s*A)?|是{_PLAIN_SEPARATOR}否(?:{_PLAIN_SEPARATOR}不适用)?)\s*',re.I)


def _ref(block, start, end, slot="", **extra):
    return {"block_id": block["block_id"], "start": start, "end": end, "slot_id": slot, **extra}


def _trim(text, start, end):
    while start < end and (text[start].isspace() or text[start] in ",;，；/|"): start += 1
    while end > start and (text[end - 1].isspace() or text[end - 1] in ",;，；/|"): end -= 1
    return start, end


def _native_state(control):
    state = control.get("selected", control.get("checked"))
    return state if state is True or state is False else None


def _glyph_options(block, start, end):
    text = block.get("text", "")
    cell_id = block.get("structure", {}).get("cell_id") or block["block_id"]
    options = []
    # Newlines are source boundaries: bilingual explanatory lines without
    # markers are not appended to the last English option's value.
    position = start
    for line in text[start:end].splitlines(keepends=True):
        content = line.rstrip("\r\n")
        marks = list(MARKER.finditer(content))
        if not marks:
            position += len(line); continue
        before = content[:marks[0].start()].strip()
        after = content[marks[-1].end():].strip()
        suffix = bool(before and (not after or COMMON.fullmatch(before)))
        for i, marker in enumerate(marks):
            if suffix:
                left = marks[i - 1].end() if i else 0
                right = marker.start()
            else:
                left = marker.end()
                right = marks[i + 1].start() if i + 1 < len(marks) else len(content)
            left, right = _trim(content, left, right)
            if not left < right:
                # A naked checkbox still has a state and a write location,
                # but there is no evidence for an answer label.
                continue
            if suffix:
                # A PDF line may still include the question before "Yes □".
                # Restrict to the explicit trailing choice, not the question.
                common = list(COMMON.finditer(content[left:right]))
                preceding_prompt=any(position+left<=s.get('label_start',-1)<position+right for s in block.get('structure',{}).get('logical_slots',[]) if s.get('kind')=='inline_prompt_candidate')
                if common and (i==0 or preceding_prompt) and not content[left + common[-1].end():right].strip():
                    left += common[-1].start()
            label = _ref(block, position + left, position + right)
            glyph_start, glyph_end = position + marker.start(), position + marker.end()
            target = _ref(block, glyph_start, glyph_end, f"{cell_id}:choice:{glyph_start}")
            options.append({"refs": [label], "text_original": text[label["start"]:label["end"]],
                            "selected": marker.group(0) in CHECKED,
                            "selection_source": "native_symbol",
                            "answer_region_refs": [target], "label_direction": "suffix" if suffix else "prefix"})
        position += len(line)
    return options


def _control_options(block, start, end, controls):
    # Native paragraph/control identity establishes the order. Read every
    # control-bearing paragraph, including repeated Yes/No groups, rather
    # than comparing the first visible line with all controls in the cell.
    paragraphs=block.get('structure',{}).get('paragraph_spans',[])
    ordered=[]
    for p in paragraphs:
        cs=[c for c in controls if c.get('paragraph_index')==p['paragraph_index']]
        if not cs: continue
        line=p.get('text',''); ms=list(COMMON.finditer(line))
        if len(ms)!=len(cs) or COMMON.sub('',line).strip(' \t,;/|'):
            ms=list(re.finditer(r'\S(?:.*?\S)?(?=\s{2,}|$)',line))
        if len(ms)!=len(cs): ordered=[]; break
        for m,c in zip(ms,cs): ordered.append((p['start']+m.start(),p['start']+m.end(),c))
    if ordered and len(ordered)==len(controls):
        selected=[(a,b,c) for a,b,c in ordered if start<=a<b<=end]
        opts=[]
        for a,b,c in selected:
            identity=f"p{c.get('paragraph_index')}:i{c.get('control_index')}"
            opts.append({'refs':[_ref(block,a,b)],'text_original':block['text'][a:b],
                'selected':_native_state(c),'selection_source':'native_control',
                'answer_region_refs':[_ref(block,a,a,f"{block['block_id']}:control:{identity}",control_id=c.get('control_id'),control_index=c.get('control_index'),control_paragraph_index=c.get('paragraph_index'))]})
        return opts,None
    text = block.get("text", "")
    # Multiple paragraphs are safely associated only by native paragraph
    # identity, with exactly one checkbox and one label in each paragraph.
    # A count-only zip across arbitrary lines is not sufficient evidence.
    paragraphs = block.get("structure", {}).get("paragraph_spans") or []
    located = []
    for control in controls:
        pi = control.get("paragraph_index")
        candidates = [p for p in paragraphs if pi is not None and p.get("paragraph_index") == pi and not p.get("blank") and start <= p.get("start", -1) < p.get("end", -1) <= end]
        if len(candidates) != 1 or sum(c.get("paragraph_index") == pi for c in controls) != 1:
            located = []; break
        paragraph = candidates[0]
        located.append((paragraph["start"], paragraph["end"]))
    if len(located) == len(controls) and len(located) > 1:
        labels = located
    else:
        labels = None
    first_line_end = text.find("\n", start, end)
    first_line_end = end if first_line_end < 0 else first_line_end
    line = text[start:first_line_end]
    # The order is used only when the native paragraph has exactly one label
    # for every native checkbox. No fuzzy/nearest label lookup is performed.
    matches = list(COMMON.finditer(line))
    residue = COMMON.sub("", line)
    if len(matches) != len(controls) or residue.strip(" \t\r\n,;，；/|"):
        matches = list(re.finditer(r"\S(?:.*?\S)?(?=\s{2,}|$)", line))
    if labels is None: labels = [(start + m.start(), start + m.end()) for m in matches]
    if len(labels) != len(controls): return [], "控件数量与可定位选项文字不一致，需核对控件标签"
    result = []
    for control_ordinal, ((left, right), control) in enumerate(zip(labels, controls), 1):
        identifier = control.get("control_id")
        label = _ref(block, left, right)
        # This is a control reference, not a zero-length text-value reference.
        # Unsupported legacy controls are rejected later by the write planner.
        physical_identity = f"p{control.get('paragraph_index', 'unknown')}:i{control.get('control_index', control_ordinal)}"
        target = _ref(block, left, left, f"{block['block_id']}:control:{physical_identity}", control_id=identifier,
                      control_index=control.get('control_index'),control_paragraph_index=control.get('paragraph_index'))
        result.append({"refs": [label], "text_original": text[left:right],
                       "selected": _native_state(control), "selection_source": "native_control",
                       "answer_region_refs": [target]})
    return result, None


def _scopes(field, lookup):
    refs = list(field.get("answer_region_refs") or [])
    # The earlier physical assembly may already replace a whole option area
    # by exact glyph slots. Retain its separate label refs for revalidation.
    for option in field.get("options") or []:
        refs.extend(option.get("refs") or [])
        refs.extend(option.get("answer_region_refs") or [])
    grouped = {}
    for ref in refs:
        block = lookup.get(ref.get("block_id"))
        if not block: continue
        start, end = ref.get("start"), ref.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(block.get("text", "")): continue
        grouped.setdefault(block["block_id"], []).append((start, end))
    for bid, ranges in grouped.items():
        block = lookup[bid]
        text = block.get("text", "")
        own_label = any(r.get("block_id") == bid for r in field.get("label_refs") or field.get("question_refs") or [])
        if block.get("kind") == "table_cell" and not own_label and CHOICE_ONLY.fullmatch(text):
            # Earlier prefix-only code can clip a suffix cell to just "□ No".
            # Restore the full physical area only when the *entire* cell is a
            # validated plain choice list, with no field label/other prose.
            # Merely seeing a marker never justifies a whole-block fallback.
            yield block, [(0, len(text))]
            continue
        merged = []
        for start, end in sorted(ranges):
            if merged and (start <= merged[-1][1] or not text[merged[-1][1]:start].strip()):
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else: merged.append((start, end))
        yield lookup[bid], merged


def _verified_pdf_vector_options(field, lookup):
    """Do not flatten physically measured PDF boxes back into printed words."""
    from ..parser_core.pdf_vector_forms import verified_vector_control
    options=field.get('options',[])
    if not options:return False
    used=set()
    for option in options:
        targets=option.get('answer_region_refs',[]);refs=option.get('refs',[])
        if len(targets)!=1 or not refs:return False
        rr=targets[0];block=lookup.get(rr.get('block_id'),{})
        if rr not in field.get('answer_region_refs',[]) or not verified_vector_control(block):return False
        if block['block_id'] in used:return False
        used.add(block['block_id']);control=block['controls'][0]
        if option.get('selected') is not control['selected']:return False
        def identity(r):return (r.get('block_id'),r.get('start'),r.get('end'))
        if {identity(r)for r in refs}!={identity(r)for r in control.get('caption_refs',[])}:return False
        for r in refs:
            text=lookup.get(r.get('block_id'),{}).get('text','');a,z=r.get('start'),r.get('end')
            if not isinstance(a,int) or not isinstance(z,int) or not 0<=a<z<=len(text):return False
    return used=={r.get('block_id')for r in field.get('answer_region_refs',[])}


def _bind_printed_native_vectors(field, options, lookup):
    """Bind exact native caption ranges to already measured PDF controls.

    No text similarity, nearest-box lookup or selection inference is used.
    This also works for an unnumbered table question that the numbered-form
    pass correctly left alone. Ambiguous existing marks remain unresolved.
    """
    from ..parser_core.pdf_vector_forms import verified_vector_control
    if not options or any(o.get('selection_source')!='printed_choice_list'for o in options):return options, None
    def keys(refs):return {(r.get('block_id'),r.get('start'),r.get('end'))for r in refs}
    candidates=[b for b in lookup.values()if b.get('structure',{}).get('physical_region_kind')=='native_vector_checkbox']
    selected=[];used=set();any_mark=False
    for option in options:
        exact=[b for b in candidates if keys(b.get('structure',{}).get('native_vector_caption_refs',[]))==keys(option.get('refs',[]))]
        any_mark=any_mark or bool(exact)
        if len(exact)!=1 or not verified_vector_control(exact[0])or exact[0]['block_id']in used:
            selected.append(None);continue
        b=exact[0];c=b['controls'][0];used.add(b['block_id'])
        selected.append({**deepcopy(option),'option_id':c['control_id'],'selected':c['selected'],
            'selection_source':'native_pdf_vector','answer_region_refs':[{'block_id':b['block_id'],
                'start':0,'end':0,'slot_id':'','control_id':c['control_id']}]})
    if not any_mark:return options,None
    if any(o is None for o in selected):return options,'已读取原生选项笔画，但选项与控件无法唯一对应；需核对原文件。'
    sources=[lookup[r['block_id']]for r in field.get('label_refs',[])]+[lookup[r['block_id']]for o in selected for r in o['answer_region_refs']]
    if not sources:return options,'原生选项尚未与问题位置对应；需核对。'
    regions=sources[0].get('structure',{}).get('native_closed_form_regions',[])
    common=[r for r in regions if r.get('source')=='native_vector_closed_edges'and all(r in b.get('structure',{}).get('native_closed_form_regions',[])for b in sources)]
    if len(common)==1:
        field['physical_form_region']=deepcopy(common[0])
    else:
        from .table_choice_evidence import native_table_choice_region
        table_region=native_table_choice_region(field,selected,lookup)
        if not table_region:return options,'原生选项与问题未处于唯一的同一闭合表单区域；需核对。'
        field['physical_form_region']=table_region
    if sum(o['selected']is True for o in selected)>1:return selected,'原生互斥选项同时选中；需核对原文件。'
    return selected,None


def normalize_choice_fields(questions, lookup):
    """Mutate native-derived choice fields in place and return the same list.

    Existing manual/knowledge answers are protected if called on saved data.
    No source block or source text is modified by this function.
    """
    for field in questions:
        normalize_choice_fields(field.get("children") or [], lookup)
        # A record cell containing one native checkbox uses its own column
        # heading as the option label. It is not a free-text response slot.
        regions=field.get('answer_region_refs') or []
        labels=field.get('label_refs') or []
        if field.get('record_index') is not None and len(regions)==1 and len(labels)==1:
            block=lookup.get(regions[0].get('block_id'),{});header=lookup.get(labels[0].get('block_id'),{})
            controls=[c for c in block.get('controls') or [] if c.get('type') in {'checkbox','legacy_checkbox'}]
            bs=block.get('structure',{});hs=header.get('structure',{})
            if (len(controls)==1 and not block.get('text','').strip() and bs.get('record_header_row')==hs.get('row_start')
                and bs.get('table_id')==hs.get('table_id') and bs.get('grid_col_start')==hs.get('grid_col_start')
                and not field.get('manual_override')):
                c=controls[0];identity=f"p{c.get('paragraph_index')}:i{c.get('control_index')}"
                target=_ref(block,0,0,f"{block['block_id']}:control:{identity}",control_id=c.get('control_id'),control_index=c.get('control_index'),control_paragraph_index=c.get('paragraph_index'))
                selected=_native_state(c);option={'refs':deepcopy(labels),'answer_region_refs':[target],
                    'text_original':header['text'][labels[0]['start']:labels[0]['end']],'selected':selected,'selection_source':'native_record_checkbox'}
                field.update(options=[option],answer_region_refs=[target],answer_value_refs=deepcopy(labels) if selected is True else [],
                             answer_refs=deepcopy(labels) if selected is True else [],answer_text_original=option['text_original'] if selected is True else '',
                             answer_status='unresolved' if selected is None else 'filled' if selected else 'blank',pairing_evidence='native_choice_regions')
                if selected is None:field.setdefault('selection_review_reasons',[]).append('记录单元格控件原始选中状态尚不明确')
                continue
        if field.get('boundary_evidence') in {'native_diagram_options','native_paragraph_option_list','physical_left_choice_grid','native_parallel_control_lanes','native_response_control_group','native_same_cell_bullet_answers','native_same_cell_bullet_writein','native_control_writein','native_nested_response_controls','rendered_native_compound_choice','rendered_native_compound_choice_parent','rendered_native_floating_choice','native_pdf_table_choice_row','native_pdf_attachment_group','closed_scan_choice_groups'}:continue
        if field.get("manual_override") or field.get("answer_origin") in {"manual", "knowledge"}:
            continue
        if _verified_pdf_vector_options(field,lookup):
            continue
        from .joined_option_peer_rows import verified_peer_choice
        if verified_peer_choice(field,lookup):
            continue
        from .question_response_groups import verified_bounded_response
        if verified_bounded_response(field,lookup):
            continue
        options, evidence_blocks, issues = [], set(), []
        for block, scopes in _scopes(field, lookup):
            controls = [c for c in block.get("controls") or [] if c.get("type", c.get("control_type")) in {"checkbox", "legacy_checkbox"} and c.get('source')!='native_pdf_glyph']
            block_options = []
            for start, end in scopes:
                current = _glyph_options(block, start, end)
                if current:
                    block_options.extend(current)
                elif controls:
                    current, issue = _control_options(block, start, end, controls)
                    if issue: issues.append(issue)
                    block_options.extend(current)
                elif PLAIN_CHOICES.fullmatch(block.get('text','')[start:end]):
                    # Printed "Yes / No" is an unselected option list. It is
                    # not evidence that both mutually exclusive answers hold.
                    for match in re.finditer(r'Yes|No|N\s*/?\s*A|不适用|是|否',block['text'][start:end],re.I):
                        a,b=start+match.start(),start+match.end()
                        slot_id=f"{block['block_id']}:printed-option:{a}"
                        slots=block.setdefault('structure',{}).setdefault('logical_slots',[])
                        if not any(s.get('slot_id')==slot_id for s in slots):slots.append({'slot_id':slot_id,'kind':'printed_option','start':a,'end':b,'option_label':block['text'][a:b]})
                        block_options.append({'refs':[_ref(block,a,b)],'text_original':match.group(),'selected':False,'selection_source':'printed_choice_list','answer_region_refs':[_ref(block,a,b,slot_id)]})
            if not block_options and not issues: continue
            evidence_blocks.add(block["block_id"])
            if controls and block_options and all(x["selection_source"] == "native_symbol" for x in block_options):
                if len(controls) == len(block_options):
                    for option, control in zip(block_options, controls):
                        state = _native_state(control)
                        if state is not None and state != option["selected"]:
                            option["selected"] = None
                            issues.append("控件状态与显示勾选符号不一致，需核对原控件")
                else: issues.append("控件与显示选项数量不一致，需核对原控件")
            options.extend(block_options)
        if not evidence_blocks: continue
        unique = {}
        for option in options:
            target = option["answer_region_refs"][0]
            unique[(target["block_id"], target["start"], target["end"], target.get("control_id"))] = option
        options = list(unique.values())
        options, vector_issue = _bind_printed_native_vectors(field,options,lookup)
        if vector_issue:issues.append(vector_issue)
        unknown = bool(issues) or any(x["selected"] is None for x in options)
        selected_refs = [deepcopy(r) for o in options if o["selected"] is True for r in o["refs"]]
        # Preserve a separately located free-text answer in another block;
        # never keep the whole source block containing unchecked choices.
        remaining_values = []
        for r in field.get("answer_value_refs") or []:
            if r.get("block_id") in evidence_blocks: continue
            source = lookup.get(r.get("block_id"))
            start, end = r.get("start"), r.get("end")
            if not source or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(source.get("text", "")):
                issues.append("已有值含无效来源范围，需重新核对")
                unknown = True
                continue
            remaining_values.append(deepcopy(r))
        values = remaining_values + selected_refs
        if unknown: values = remaining_values
        targets = [deepcopy(r) for r in field.get("answer_region_refs") or [] if r.get("block_id") not in evidence_blocks]
        targets.extend(deepcopy(r) for o in options for r in o["answer_region_refs"])
        field.update(options=options, answer_region_refs=targets,
                     answer_value_refs=values, answer_refs=deepcopy(values),
                     answer_text_original="\n".join(lookup[r["block_id"]]["text"][r["start"]:r["end"]] for r in values).strip(),
                     answer_status="unresolved" if unknown else "filled" if values else "blank",
                     native_choice_version=VERSION)
        if unknown:
            review=list(dict.fromkeys(issues or ["部分原生控件的选中状态无法确定"]))
            field.setdefault("review_reasons", []).extend(review)
            if not issues and options and all(o.get('refs') and o.get('answer_region_refs') for o in options):
                field['selection_review_reasons']=review
                field['pairing_evidence']='native_choice_regions'
            field["review_status"] = "needs_review"
    from ..choice_identity import assign_option_identities
    assign_option_identities(questions)
    return questions
