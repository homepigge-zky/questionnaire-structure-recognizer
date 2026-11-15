"""Recover local forms from closed raster cells, never from next nonempty text.

This is a deliberately bounded supplement to model output. A numbered form row
must have an actual number cell, a complete adjacent cell chain and no aligned
record-column header. General choice matrices use physical checkboxes and their
separate caption cells. No answer spelling, date, or selection is guessed.
"""
from copy import deepcopy
import re

from .field_boundaries import ref, UNITS
from .form_completion import new_field
from .scanned_cells import _label_inside, _same_row, _replace_owned
from .pdf_source_relations import _inside, validate_ocr_response_values, source_cells_share_row
from .scan_ruled_text_order import cell_reading_order, separate_question_prompts

VERSION = 'scan-local-tables-v10-physical-cell-reading-order'
NUMBER = re.compile(r'^\s*\d+[.)]?\s*$')


def _box(b): return b['location']['bbox']
def _page(b): return b['location']['page']
def _sort(items): return sorted(items, key=lambda b: (_box(b)[1], _box(b)[0]))


def _mark_order(marks):
    rows = []
    for mark in sorted(marks, key=lambda b: (_box(b)[1]+_box(b)[3])/2):
        cy = (_box(mark)[1]+_box(mark)[3])/2
        if not rows or cy-rows[-1][0] > 10: rows.append((cy, []))
        rows[-1][1].append(mark)
    return [m for _, row in rows for m in sorted(row, key=lambda b: _box(b)[0])]


def _contents(cell, texts):
    return cell_reading_order([b for b in texts if _page(b) == _page(cell) and _label_inside(_box(b), _box(cell))], cell)


def _physical_contains(block,cell):
    if _page(block)!=_page(cell) or not _inside(_box(block),_box(cell)):return False
    polygon=cell.get('structure',{}).get('closed_region_evidence',{}).get('polygon',[])
    if len(polygon)!=4:return True
    x,y=(_box(block)[0]+_box(block)[2])/2,(_box(block)[1]+_box(block)[3])/2
    inside=False
    for (ax,ay),(bx,by) in zip(polygon,polygon[1:]+polygon[:1]):
        if (ay>y)!=(by>y) and x<(bx-ax)*(y-ay)/(by-ay)+ax:inside=not inside
    return inside


def _manual(field):
    return field.get('manual_override') or field.get('answer_origin') in {'manual', 'knowledge', 'reviewed_knowledge'}


def _install(fields, field, labels, values, lookup):
    owned = {b['block_id'] for b in labels + values}
    if any(_manual(f) and any(r['block_id'] in owned for r in f.get('label_refs', []) + f.get('answer_value_refs', [])) for f in fields):
        return False
    # The old model can have grouped only a suffix of a wrapped caption, or an
    # OCR source value as a field. Retire only those exact source ranges.
    _replace_owned(fields, owned, lookup)
    for b in labels:
        s = b.setdefault('structure', {})
        s.update(resolved_layout_role='field_label', protected_label_ranges=[{'start': 0, 'end': len(b['text'])}])
    for b in values:
        s = b.setdefault('structure', {})
        prior = s.pop('protected_label_ranges', None)
        if prior: s['superseded_label_ranges'] = prior
        s['resolved_layout_role'] = 'source_answer'
    fields.append(field)
    return True


def _field(left, labels, regions):
    field = new_field(labels[0], ref(labels[0]), [ref(r) for r in regions])
    fid = 'scan-local-cell:' + left['block_id']
    field.update(field_id=fid, question_id=fid, label_refs=[ref(b) for b in labels],
                 question_refs=[ref(b) for b in labels], question_text_original='\n'.join(b['text'] for b in labels),
                 physical_label_region_ref=ref(left), boundary_version=VERSION)
    return field


def _record_header(chain, cells, texts, depth=0):
    """An aligned multi-column header is evidence for records, not scalar pairs."""
    if depth > 40: return True  # Long indistinguishable record region needs review.
    first = chain[0]; y = _box(first)[1]
    above = [c for c in cells if _page(c) == _page(first) and 0 <= y-_box(c)[3] < 12]
    aligned = []
    for c in chain:
        options = [p for p in above if abs(_box(p)[0]-_box(c)[0]) < 4 and abs(_box(p)[2]-_box(c)[2]) < 4 and _contents(p, texts)]
        if len(options) == 1: aligned.append(options[0])
    if len(aligned) != len(chain): return False
    # A preceding numbered Q/A row is not a column header.
    numbered = any(NUMBER.fullmatch(t['text']) and _box(t)[2] < _box(first)[0] and
                   abs((_box(t)[1]+_box(t)[3]-_box(aligned[0])[1]-_box(aligned[0])[3])/2) < 12
                   for t in texts if _page(t) == _page(first))
    return _record_header(aligned, cells, texts, depth+1) if numbered else True


def _numbered_rows(fields, blocks, lookup, cells, texts, marks):
    used = set()
    for number_cell in cells:
        number_text = _contents(number_cell, texts)
        if len(number_text) != 1 or not NUMBER.fullmatch(number_text[0]['text']): continue
        chain = []; current = number_cell
        while len(chain) < 12:
            adjacent = [c for c in cells if c is not current and _page(c) == _page(current) and
                        source_cells_share_row(current,c)]
            if len(adjacent) != 1: break
            current = adjacent[0]
            if current in chain: break
            chain.append(current)
        if len(chain) < 2 or len(chain) % 2 or _record_header(chain, cells, texts): continue
        pairs = []
        for i in range(0, len(chain), 2):
            left, right = chain[i:i+2]; labels = _contents(left, texts); values = _contents(right, texts)
            if (not labels or all(NUMBER.fullmatch(b['text']) for b in labels) or
                sum(len(b['text']) for b in labels) > 250 or
                any(_physical_contains(m,right) for m in marks) or
                right.get('structure', {}).get('label_only_region')):
                pairs = []; break
            if not values and right.get('structure', {}).get('blank_state') != 'blank':
                # An unreadable or partly detected physical region is not blank.
                pairs = []; break
            pairs.append((left, right, labels, values))
        for left, right, labels, values in pairs:
            if left['block_id'] in used: continue
            f = _field(left, labels, [right]); f['boundary_evidence'] = 'closed_scan_cell_pair'
            f['local_layout_evidence'] = {'kind': 'numbered_closed_form_row', 'number_cell_ref': ref(number_cell),
                                        'row_cell_refs': [ref(c) for c in chain]}
            if values:
                f.update(answer_value_refs=[ref(b) for b in values], answer_refs=[ref(b) for b in values],
                         answer_text_original='\n'.join(b['text'] for b in values), answer_status='filled')
                owned = {b['block_id'] for b in labels+values}
                remaining = [old for old in fields if not any(r['block_id'] in owned for r in old.get('label_refs', []))]
                if not validate_ocr_response_values(f, lookup, remaining+[f]):
                    f.update(answer_status='unresolved', answer_text_original='', answer_refs=[], answer_value_refs=[],
                             review_status='needs_review', review_reasons=['封闭答复区域存在原文，但 OCR 值或归属尚未通过校验。'],
                             source_observation={'status': 'pending_review', 'answer_value_refs': [ref(b) for b in values]})
            else:
                right['structure'].update(scan_association_valid=True, scan_field_id=f['field_id'])
            if _install(fields, f, labels, values, lookup): used.add(left['block_id'])


def _leaf_marks(marks):
    # Raster segmentation sometimes emits a second outer rectangle around the
    # same small box. Retain the actual inner physical mark, never count both.
    return [m for m in marks if not any(n is not m and _page(n) == _page(m) and
             _inside(_box(n), _box(m)) and
             (_box(m)[2]-_box(m)[0])*(_box(m)[3]-_box(m)[1]) > 1.7*(_box(n)[2]-_box(n)[0])*(_box(n)[3]-_box(n)[1])
             for n in marks)]


def _caption_cell(mark, cells, texts, marks=()):
    mb = _box(mark); cy = (mb[1]+mb[3])/2
    possible = []
    for c in cells:
        cb = _box(c)
        if _page(c) != _page(mark) or not -2 <= cb[0]-mb[2] < 16 or not cb[1]-2 <= cy <= cb[3]+2: continue
        if any(n is not mark and _page(n) == _page(c) and _inside(_box(n), cb) for n in marks): continue
        captions = _contents(c, texts)
        if captions and not all(NUMBER.fullmatch(b['text']) for b in captions): possible.append((c, captions))
    if len(possible) == 1: return possible[0]
    if possible: return None
    # A clipped raster border can remove the caption's separate rectangle.
    # One source line on the same physical baseline is still unambiguous; a
    # wrapped caption without a cell is deliberately not guessed here.
    inline = [b for b in texts if _page(b) == _page(mark) and
              (-2 <= _box(b)[0]-mb[2] < 20 or abs(_box(b)[0]-mb[0]) < 4 and _box(b)[2] > mb[2]+20) and
              abs((_box(b)[1]+_box(b)[3])/2-cy) < 7 and not re.fullmatch(r'[□☐☑☒√✓✔口D0]+', b['text'].strip())]
    if len(inline) == 1: return None, inline
    return None


def _general_choices(fields, blocks, lookup, cells, texts, marks):
    from .selection_evidence import selected_scan_caption
    marks = _leaf_marks(marks)
    marks = [m for m in marks if not (any(_page(t) == _page(m) and
        _inside(_box(m), _box(t)) and (_box(t)[2]-_box(t)[0]) > 2.5*(_box(m)[2]-_box(m)[0]) and
        not re.search(r'[□☐☑☒√✓✔]', t['text']) for t in texts) and
        any(n is not m and _page(n) == _page(m) and 3 < _box(m)[0]-_box(n)[2] < 30 and
            abs((_box(m)[1]+_box(m)[3]-_box(n)[1]-_box(n)[3])/2) < 7 for n in marks))]
    used = set()
    for left in cells:
        labels = _contents(left, texts); lb = _box(left)
        if not labels or all(NUMBER.fullmatch(b['text']) for b in labels): continue
        if any(b['block_id'] in used for b in labels): continue
        local = [m for m in marks if _page(m) == _page(left) and 0 <= _box(m)[0]-lb[2] < 380 and
                 lb[1]-5 <= (_box(m)[1]+_box(m)[3])/2 <= lb[3]+2]
        if not 4 <= len(local) <= 12: continue
        # The complete label cell must span every option. A caption cell in the
        # option grid cannot become another parent farther along the same row.
        options = []; caption_cells = []; caption_blocks = []
        for mark in _mark_order(local):
            found = _caption_cell(mark, cells, texts, marks)
            if not found: break
            container, captions = found
            if container and container['block_id'] in {c['block_id'] for c in caption_cells if c}: break
            caption_cells.append(container); caption_blocks.extend(captions)
            control = (mark.get('controls') or [{}])[0]
            options.append({'refs': [ref(b) for b in captions], 'text_original': '\n'.join(b['text'] for b in captions),
                            'selected': control.get('selected'), 'selection_source': control.get('source'),
                            'answer_region_refs': [ref(mark)]})
        if len(options) != len(local): continue
        if any(b['block_id'] in {x['block_id'] for x in labels} for b in caption_blocks): continue
        f = _field(left, labels, local)
        f.update(options=options, boundary_evidence='closed_scan_general_choice_row',
                 answer_status='blank' if all(o['selected'] is False for o in options) else 'unresolved')
        for option, container in zip(options, caption_cells):
            mark = lookup[option['answer_region_refs'][0]['block_id']]
            mark['structure'].update(scan_association_valid=True, scan_field_id=f['field_id'],
                scan_option_caption_refs=deepcopy(option['refs']))
            if container: mark['structure']['scan_option_caption_cell_ref'] = ref(container)
        selected = [r for o in options if o['selected'] is True for r in o['refs']]
        if selected and all(isinstance(o['selected'], bool) for o in options) and all(selected_scan_caption(r, f, lookup) for r in selected):
            f.update(answer_status='filled', answer_value_refs=deepcopy(selected), answer_refs=deepcopy(selected),
                     answer_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in selected),
                     answer_pairing_evidence='physical_selected_box_and_source_caption')
        if f['answer_status'] == 'unresolved':
            f.update(review_status='needs_review', review_reasons=['选项位置已确定，部分勾选状态没有可靠原页证据。'])
        if _install(fields, f, labels, caption_blocks, lookup):
            used.update(b['block_id'] for b in labels+caption_blocks)
            for b in caption_blocks: b['structure']['resolved_layout_role'] = 'option'


def _independent_matrix_prompt(labels,excluded):
    """One complete, contiguous question in an already proved mixed cell.

    Existing inline prompts/values remain separate. A conditional prompt is
    not evidence of the subject of the shared Yes/No/NA columns. Ambiguous
    multiple questions or an unterminated fragment cannot take those columns.
    """
    candidates=[b for b in _sort(labels) if b['block_id'] not in excluded and not UNITS.fullmatch(b['text'].strip())]
    start=re.compile(r'^\s*(?:(?:is|are|has|have|does|do|can|could|did|was|were|will|would)\b|是否|有无|有沒有|有没有)',re.I)
    starts=[b for b in candidates if start.match(b['text'])]
    if len(starts)!=1:return []
    first=starts[0];selected=[first]
    if re.search(r'[?？]\s*$',first['text']):return selected
    for block in candidates[candidates.index(first)+1:]:
        previous=selected[-1]
        if (abs(_box(block)[0]-_box(first)[0])>15 or _box(block)[1]-_box(previous)[3]>10
                or _box(block)[1]<=_box(previous)[1] or re.match(r'^\s*(?:if\b|please\b|若|如果)',block['text'],re.I)):return []
        selected.append(block)
        if re.search(r'[?？]\s*$',block['text']):return selected
    return []


def _column_choice_rows(fields,blocks,lookup,cells,texts):
    """Keep the full closed question cell and separately bounded Yes/No columns.

    These forms use the whole response cell for a mark rather than a small
    checkbox. Unknown ink remains unknown; missing cell contours stay missing.
    A header caption alone never proves a selected answer.
    """
    from .pdf_source_relations import source_text_inside_cell
    from ..parser_core.pdf_matrix_marks import verified_matrix_cell_mark
    from .scan_matrix_structure import matrix_groups, is_physical_checkbox_text
    groups=matrix_groups(cells,texts)
    marks=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='checkbox']
    for row in groups:
        page=_page(row[0][0]); header_y=max(_box(c)[3] for c,t in row)
        for left in cells:
            lb=_box(left)
            if _page(left)!=page or lb[1]<header_y-8 or not 80<lb[2]-lb[0]<500:continue
            if left.get('structure',{}).get('scan_header_evidence'):continue
            if abs(lb[2]-_box(row[0][0])[0])>20:continue
            labels=[b for b in _contents(left,texts) if not re.fullmatch(r'\s*\d+(?:\.\d+)*[.)]?\s*',b['text'])
                    and source_text_inside_cell(b,left,label=True)]
            if not labels:continue
            label_ids={b['block_id'] for b in labels}
            # A matrix question can also contain an independent written value
            # (e.g. a duration and a Yes/No/NA column). Preserve both locations;
            # the written value must never be folded back into the question.
            subfields=[f for f in fields if f.get('boundary_evidence')=='closed_scan_source_subfield' and
                       any(r['block_id'] in label_ids for r in f.get('label_refs',[]))]
            attached_subfields=[]
            if subfields:
                # A shared physical row is not proof that its first inline
                # prompt is the question answered by every Yes/No/NA column.
                # Only promote one fully accounted question/value pair;
                # additional business text keeps the existing local split.
                # Conditional continuations can refer to the previous row.
                own_labels={r['block_id'] for f in subfields for r in f['label_refs']}
                own_values={r['block_id'] for f in subfields for r in (
                    f.get('answer_value_refs',[])+f.get('source_observation',{}).get('answer_value_refs',[]))}
                if any(_manual(f) or not f.get('answer_region_refs') or
                       not (rule:=lookup.get(f.get('inline_rule_evidence_ref',{}).get('block_id'))) or
                       not _physical_contains(rule,left) or
                       any(not (b:=lookup.get(r['block_id'])) or not _physical_contains(b,left) for r in f['answer_region_refs'])
                       for f in subfields):continue
                independent=any(b['block_id'] not in own_labels|own_values and not UNITS.fullmatch(b['text'].strip())
                                for b in labels)
                conditional=any(re.match(r'^\s*(?:if\s*(?:yes|no)\b|若是|若否|如果是|如果否)',lookup[bid]['text'],re.I)
                                for bid in own_labels)
                if len(subfields)!=1 or independent or conditional:
                    labels=_independent_matrix_prompt(labels,own_labels|own_values)
                else:
                    labels=[b for b in labels if b['block_id'] in own_labels]
                    attached_subfields=subfields
                if not labels:continue
            if separate_question_prompts(labels):continue
            connected=set();frontier=[left]
            for _ in range(3):
                adjacent=[c for c in cells if c['block_id'] not in connected and
                          any(source_cells_share_row(a,c) for a in frontier)]
                if len(adjacent)!=1:break
                connected.add(adjacent[0]['block_id']);frontier=adjacent
            options=[];targets=[]
            for header,caption in row:
                hx=(_box(header)[0]+_box(header)[2])/2
                possible=[c for c in cells if c is not left and _page(c)==page and
                          _box(c)[0]-3<=hx<=_box(c)[2]+3 and (c['block_id'] in connected or _same_row(lb,_box(c))) and
                          abs((_box(c)[2]-_box(c)[0])-(_box(header)[2]-_box(header)[0]))<12]
                if len(possible)>1:break
                region=possible[0] if possible else None
                observed=_contents(region,texts) if region else []
                local_marks=[m for m in marks if region and _physical_contains(m,region)
                             and not any(_physical_contains(m,t) for t in observed if not is_physical_checkbox_text(t,marks))]
                if observed and not all(is_physical_checkbox_text(t,local_marks) for t in observed):break
                if len(local_marks)>1:break
                control=local_marks[0] if local_marks else None
                from ..source_matrix_choices import reviewed_matrix_state
                reviewed_state=reviewed_matrix_state(region,lookup) if region else None
                state=(reviewed_state if isinstance(reviewed_state,bool) else (control.get('controls') or [{}])[0].get('selected') if control else
                       True if region and verified_matrix_cell_mark(region,lookup) else
                       False if region and region.get('structure',{}).get('blank_state')=='blank' else None)
                refs=[ref(control or region)] if region else []
                options.append({'refs':[ref(caption)],'text_original':caption['text'],'selected':state,
                                'selection_source':'raster_tick_in_choice_cell' if state is True else 'bounded_scan_column_empty' if state is False else 'bounded_scan_column_unreadable',
                                'answer_region_refs':refs})
                if region:targets.append(control or region)
            complete_row = (len(targets)>=2 and any(source_cells_share_row(left,t) for t in targets) or
                            len(targets)==len(row) and len(connected)>=len(row))
            # One surviving contour still establishes which matrix column it
            # occupies, not that its siblings are empty. Preserve the complete
            # closed question cell and expose missing options as unresolved.
            # Require actual four-sided source contours for this partial case.
            incomplete_row = (len(targets)==1 and _same_row(lb,_box(targets[0])) and
                all(len(c.get('structure',{}).get('closed_region_evidence',{}).get('polygon',[]))==4 and
                    min(c.get('structure',{}).get('closed_region_evidence',{}).get('edge_ink_coverage') or [0])>=.8
                    for c in [left,targets[0],*[h for h,t in row]]))
            if len(options)!=len(row) or not (complete_row or incomplete_row):continue
            f=_field(left,labels,targets)
            if attached_subfields:
                f['children']=deepcopy(attached_subfields)
                for child in f['children']:
                    child['parent_question_id']=f['field_id']
                    child['matrix_parent_region_ref']=ref(left)
                f['local_relationship_evidence']={'type':'independent_inline_and_matrix_responses',
                    'question_cell_ref':ref(left),'inline_field_ids':[child['field_id'] for child in attached_subfields]}
            f.update(options=options,boundary_evidence='closed_scan_column_choice_row',
                     answer_status='blank' if all(o['selected'] is False for o in options) else 'unresolved',
                     scan_column_header_refs=[ref(c) for c,t in row])
            # A condition printed in a single wide band immediately above the
            # column headings governs these rows. Preserve its source even
            # when no parent yes/no question exists; that unknown activation
            # must not become permission to auto-fill the conditional matrix.
            conditional=[]
            for t in texts:
                if _page(t)!=page or not re.match(r'^\s*(?:if\b|when\b|若|如果)',t['text'],re.I):continue
                if not 0<=header_y-_box(t)[3]<30:continue
                bands=[c for c in cells if _page(c)==page and _physical_contains(t,c)
                       and _box(c)[0]<=lb[0]+5 and _box(c)[2]>=_box(row[-1][0])[2]-5
                       and _box(c)[3]-_box(c)[1]<40]
                if len(bands)==1:conditional.append(t)
            if len(conditional)==1:
                f['explicit_condition']={'status':'unresolved','source_text':conditional[0]['text'],
                    'source_refs':[ref(conditional[0])],'parent_field_id':None,
                    'reason':'原文条件限定整组选择区域；没有已回答的父题证明条件成立。'}
                f['context_refs']=[ref(conditional[0])]
            if incomplete_row:
                f['incomplete_matrix_region_evidence']={'version':VERSION,'label_region_ref':ref(left),
                    'located_region_refs':[ref(t) for t in targets],
                    'missing_option_refs':[ref(caption) for option,(header,caption) in zip(options,row)
                                           if not option['answer_region_refs']]}
            selected_count=sum(o['selected'] is True for o in options)
            if all(isinstance(o['selected'],bool) for o in options) and selected_count==1:
                selected=[r for o in options if o['selected'] for r in o['refs']]
                f.update(answer_status='filled',answer_refs=deepcopy(selected),answer_value_refs=deepcopy(selected),
                         answer_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in selected))
            if f['answer_status']=='unresolved':
                reason=('原页互斥的 Yes / No / NA 列出现多个明确勾选，不能组合为一个有效答案。' if selected_count>1 else
                        '原页选择列及完整题干已定位；部分选择状态或单元格边界尚无可靠证据。')
                f.update(review_status='needs_review',review_reasons=[reason])
            for option,(header,caption) in zip(options,row):
                for rr in option['answer_region_refs']:
                    region=lookup[rr['block_id']]
                    region['structure'].update(scan_association_valid=True,scan_field_id=f['field_id'],
                        scan_column_header_ref=ref(header),scan_option_caption_refs=deepcopy(option['refs']),
                        scan_matrix_label_region_ref=ref(left))
            _install(fields,f,labels,[],lookup)


def restore_scanned_local_tables(fields, blocks, lookup):
    from .scan_local_prompt_regions import restore_conditional_rule_fields
    cells = [b for b in blocks if b.get('location', {}).get('type') == 'pdf' and
             b.get('structure', {}).get('scan_region_kind') == 'text_box' and len(b['location'].get('bbox', [])) == 4]
    texts = [b for b in blocks if b.get('text', '').strip() and b.get('location', {}).get('extraction') == 'ocr' and len(b['location'].get('bbox', [])) == 4]
    marks = [b for b in blocks if b.get('structure', {}).get('scan_region_kind') == 'checkbox' and len(b.get('location', {}).get('bbox', [])) == 4]
    _general_choices(fields, blocks, lookup, cells, texts, marks)
    _numbered_rows(fields, blocks, lookup, cells, texts, marks)
    restore_conditional_rule_fields(fields,blocks,lookup)
    _column_choice_rows(fields, blocks, lookup, cells, texts)
    return {'version': VERSION, 'fields': sum(f.get('boundary_version') == VERSION for f in fields)}
