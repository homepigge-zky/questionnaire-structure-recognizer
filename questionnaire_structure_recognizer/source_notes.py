"""Keep source-only notes together; never use notes as answers or new fields."""
from copy import deepcopy


def _reading_order(notes):
    """Small OCR baseline differences must not reverse one physical line."""
    pending=sorted(notes,key=lambda n:(n['order'],n['block_id']))
    result=[]
    while pending:
        first=pending.pop(0);loc=first.get('location',{});box=loc.get('bbox')
        line=[first]
        if loc.get('type')=='pdf' and box:
            while pending:
                other=pending[0].get('location',{});rect=other.get('bbox')
                if (other.get('type')!='pdf' or other.get('page')!=loc.get('page') or not rect
                    or abs(rect[1]-box[1])>3 or min(rect[3],box[3])-max(rect[1],box[1])<=0):break
                line.append(pending.pop(0))
            line.sort(key=lambda n:n['location']['bbox'][0])
        result.extend(line)
    return result


def _explicit_reviewed_note_groups(notes, questions, blocks):
    """One source-bounded side note remains one ordered read-only paragraph.

    OCR block order can interleave a right-hand note with a left-hand question.
    Use only the locally verified note refs, never a full-page text sort or an
    arbitrary model grouping. Invalid groups leave every source note visible.
    """
    from .validation.pdf_furniture_ownership import _closed
    from .validation.pdf_source_relations import source_text_inside_cell
    from .source_text_review import verified_reviewed_text
    key=lambda r:(r.get('block_id'),r.get('start'),r.get('end'))
    indexed={}
    for i,note in enumerate(notes):
        if note.get('source_ref'):indexed.setdefault(key(note['source_ref']),[]).append((i,note))
    consumed=set();combined=[]
    for question in questions:
        for group in question.get('form_definition',{}).get('source_note_groups') or []:
            refs=group.get('refs') or [];cell=blocks.get(group.get('container_ref',{}).get('block_id'),{})
            if (group.get('kind')!='source_reviewed_same_cell_note' or len(refs)<2 or not _closed(cell)
                or len({key(r) for r in refs})!=len(refs)):continue
            labels=question.get('source_refs') or []
            if not labels or any(not source_text_inside_cell(blocks.get(r.get('block_id'),{}),cell,label=True) for r in labels):continue
            matched=[]
            for ref in refs:
                source=blocks.get(ref.get('block_id'),{});items=indexed.get(key(ref),[])
                if (len(items)!=1 or items[0][0] in consumed or items[0][1].get('anchor_field_id')!=question['field_id']
                    or not verified_reviewed_text(source,ref) or not source_text_inside_cell(source,cell,label=True)):
                    break
                matched.append((items[0][0],items[0][1],source,ref))
            if len(matched)!=len(refs):continue
            # The saved ref order must match the actual top-to-bottom side
            # lane; a same-cell condition on the left is a separate item.
            ordered=sorted(matched,key=lambda x:(x[2]['location']['bbox'][1],x[2]['location']['bbox'][0]))
            boxes=[v[2]['location']['bbox'] for v in matched]
            if [key(v[3]) for v in ordered]!=[key(r) for r in refs] or max(b[0] for b in boxes)-min(b[0] for b in boxes)>15:continue
            item=deepcopy(matched[0][1]);item.pop('source_ref',None)
            item.update(text='\n'.join(v[2]['text'][v[3]['start']:v[3]['end']] for v in matched),
                source_refs=deepcopy(refs),block_ids=[v[2]['block_id'] for v in matched],
                source_locations=[deepcopy(v[2]['location']) for v in matched],verified_source_note_group=True,
                order=min(v[1]['order'] for v in matched))
            item['location']={**item['location'],'bbox':[min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]}
            consumed.update(v[0] for v in matched);combined.append(item)
    return [note for i,note in enumerate(notes) if i not in consumed]+combined


def group_source_notes(notes, questions, blocks):
    groups=[]
    for item in _reading_order(_explicit_reviewed_note_groups(notes,questions,blocks)):
        note=deepcopy(item);loc=note.get('location',{});box=loc.get('bbox')
        previous=groups[-1] if groups else None
        prior=previous.get('location',{}) if previous else {}
        prior_box=prior.get('bbox')
        last_box=(previous.get('source_locations') or [prior])[-1].get('bbox') if previous else None
        same_line=bool(box and last_box and abs(box[1]-last_box[1])<=3 and -10<=box[0]-last_box[2]<=25)
        continuation=(previous and note['role']=='instruction' and previous['role']=='instruction'
            and not note.get('verified_source_note_group') and not previous.get('verified_source_note_group')
            and not note.get('anchor_validation') and not previous.get('anchor_validation')
            and (not note.get('anchor_field_id') or not previous.get('anchor_field_id') or note.get('anchor_field_id')==previous.get('anchor_field_id'))
            and loc.get('type')=='pdf' and prior.get('page')==loc.get('page') and box and prior_box
            and (same_line or (abs(box[0]-prior_box[0])<=12 and -3<=box[1]-prior_box[3]<=3))
            and not previous['text'].rstrip().endswith((')', '）', '.', '。')))
        if continuation:
            previous['text']+=(' ' if same_line else '\n')+note['text']
            previous['block_ids'].append(note['block_id'])
            previous['source_locations'].append(loc)
            if note.get('anchor_field_id'):previous['anchor_field_id']=note['anchor_field_id']
            previous['location']={**prior,'bbox':[min(prior_box[0],box[0]),min(prior_box[1],box[1]),max(prior_box[2],box[2]),max(prior_box[3],box[3])]}
        else:
            note.setdefault('block_ids',[note['block_id']]);note.setdefault('source_locations',[loc]);groups.append(note)
    for note in groups:
        loc=note.get('location',{});box=loc.get('bbox')
        if note.get('anchor_field_id'):
            matched=next((q for q in questions if q['field_id']==note['anchor_field_id']),None)
            if matched:
                # Native Word order is reliable. An introductory instruction
                # before its record header must not be moved after the whole
                # record table merely because it is associated with it.
                native_precedes = loc.get('type') == 'docx' and note['order'] < matched['source_order']
                if not native_precedes:note['order']=matched['source_order']+(0.75 if note.get('verified_source_note_group') else 0.5)
                elif matched.get('section'):note['section']=matched['section']
            continue
        if loc.get('type')!='pdf' or not box or note['role']!='instruction' or note.get('anchor_validation'):continue
        candidates={}
        for q in questions:
            for ref in q.get('source_refs',[]):
                label=blocks.get(ref.get('block_id'),{}).get('location',{});rect=label.get('bbox')
                if label.get('page')!=loc.get('page') or not rect or rect[2]+5>=box[0]:continue
                if min(rect[3],box[3])-max(rect[1],box[1])>0:
                    candidates[q['id']]=q
        # A right-hand note wholly beside one source question belongs after
        # that question. Multiple possible rows remain unassigned, visible.
        if len(candidates)==1:
            question=next(iter(candidates.values()))
            note['anchor_field_id']=question['field_id']
            note['order']=question['source_order']+0.5
    # Only a validated section reference can place a note under a section.
    # The nearest arbitrary paragraph is not evidence of section ownership.
    headings={}
    for question in questions:
        for ref in question.get('form_definition',{}).get('section_refs',[]):
            source=blocks.get(ref.get('block_id'),{})
            if source and question.get('section'):
                headings.setdefault(source.get('order',0),set()).add(question['section'])
    for note in groups:
        if note.get('location',{}).get('type')!='pdf' and note['role']!='signed_declaration_prose':continue
        preceding=[order for order in headings if order<=note['order']]
        if preceding:
            names=headings[max(preceding)]
            if len(names)==1:note['section']=next(iter(names))
    return sorted(groups,key=lambda n:(n['order'],n['block_id']))


def group_readonly_source_tables(blocks, source_hash):
    """Preserve verified physical record rows, without creating answer fields.

    A signed audit trail is original document content, not a new signature to
    fill. Coordinates and original fragments come from the validated native
    grid. Broken mappings retain all text as unpaired read-only prose.
    """
    from collections import defaultdict
    grouped = defaultdict(list)
    for bid, block in blocks.items():
        structure = block.get('structure') or {}
        if structure.get('resolved_layout_role') == 'readonly_record_cell':
            metadata = structure.get('readonly_record') or {}
            grouped[metadata.get('table_id') or ('unlocated:' + bid)].append((bid, block, metadata))
    notes = []
    for table_id, items in grouped.items():
        items.sort(key=lambda item: (item[1]['order'], item[0]))
        reason = None
        cells = {}
        dimensions = set()
        for bid, block, meta in items:
            if not source_hash or meta.get('source_hash') != source_hash:
                reason = 'readonly_record_source_hash_mismatch'; break
            r, c = meta.get('row_index'), meta.get('column_index')
            nr, nc = meta.get('row_count'), meta.get('column_count')
            ref = meta.get('cell_ref') or {}
            if (meta.get('read_only') is not True or any(type(v) is not int for v in (r, c, nr, nc))
                    or not 0 <= r < nr <= 1000 or not 0 <= c < nc <= 64
                    or ref.get('block_id') not in blocks
                    or meta.get('row_span', 1) != 1 or meta.get('column_span', 1) != 1):
                reason = 'readonly_record_invalid_physical_mapping'; break
            dimensions.add((nr, nc))
            target = cells.setdefault((r, c), {'cell_ref': deepcopy(ref), 'fragments': []})
            if target['cell_ref'] != ref:
                reason = 'readonly_record_conflicting_cell_ownership'; break
            target['fragments'].append((bid, block))
        if len(dimensions) != 1:
            reason = reason or 'readonly_record_inconsistent_dimensions'
        if not reason:
            nr, nc = next(iter(dimensions))
            # The source recognizer must explicitly include even empty cells;
            # absent coordinates are not invented as blank source answers.
            if len(cells) != nr * nc:
                reason = 'readonly_record_incomplete_grid'
        if reason:
            notes.extend({'block_id': bid, 'block_ids': [bid], 'order': block['order'],
                          'text': block['text'], 'location': deepcopy(block.get('location') or {}),
                          'role': 'readonly_unresolved_prose', 'diagnostic_reason': reason,
                          'source_hash': source_hash}
                         for bid, block, _ in items)
            continue
        rows = []
        for r in range(nr):
            row = []
            for c in range(nc):
                target = cells[(r, c)]
                fragments = target['fragments']
                row.append({'text': '\n'.join(b['text'] for _, b in fragments),
                            'block_ids': [bid for bid, _ in fragments],
                            'cell_ref': target['cell_ref'], 'row_index': r, 'column_index': c,
                            'source_locations': [deepcopy(b.get('location') or {}) for _, b in fragments]})
            rows.append(row)
        first_id, first, meta = items[0]
        notes.append({'block_id': 'readonly-table:' + str(table_id),
                      'block_ids': [bid for bid, _, _ in items], 'table_id': table_id,
                      'order': first['order'], 'location': deepcopy(first.get('location') or {}),
                      'role': 'readonly_table', 'read_only': True, 'rows': rows,
                      'text': '\n'.join(' | '.join(cell['text'] for cell in row) for row in rows),
                      'source_hash': source_hash, 'header_refs': deepcopy(meta.get('header_refs') or []),
                      'caption_refs': deepcopy(meta.get('caption_refs') or [])})
    return sorted(notes, key=lambda n: (n['order'], n['block_id']))


def is_source_responsibility_notice(text):
    """Keep an explicit original use-only notice, without inferring its owner."""
    import re
    return bool(re.fullmatch(r'\s*[（(]?\s*For\s+[^()（）\n]{1,80}\s+(?:use|purposes?)\s+only\s*[）)]?\s*',
                             str(text or ''), re.I))
