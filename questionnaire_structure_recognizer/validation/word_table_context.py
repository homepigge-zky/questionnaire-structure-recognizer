"""Word list numbering and merged grid boundaries establish local context.

Native numbering and physical row groups distinguish a lead-in container from
its independently answered descendants; proximity alone is not an inheritance
rule. Original text and existing values remain immutable.
"""
from collections import defaultdict
from copy import deepcopy
import re


VERSION = 'word-table-context-v2-native-group-containers-and-bullet-continuations'


def _s(block):
    return block.get('structure', {})


def _number(block):
    spans = [p for p in _s(block).get('paragraph_spans', []) if p.get('style', {}).get('numbering')]
    if len(spans) != 1:
        return {}
    return spans[0]['style']['numbering']


def _ref(block):
    return {'block_id': block['block_id'], 'start': 0, 'end': len(block.get('text', '')), 'slot_id': ''}


def native_word_table_context(blocks):
    tables = defaultdict(lambda: defaultdict(list))
    for b in blocks:
        s = _s(b)
        if b.get('location', {}).get('type') == 'docx' and b.get('kind') == 'table_cell' and s.get('table_id'):
            tables[s['table_id']][s.get('row_start', 0)].append(b)
    headings, groups = {}, []
    for table_id, rows in tables.items():
        physical = {r: [b for b in row if not _s(b).get('is_merge_continuation')] for r, row in rows.items()}
        candidates = defaultdict(list)
        for row, cells in physical.items():
            if len(cells) != 1:
                continue
            b = cells[0]; s = _s(b); n = _number(b); text = b.get('text', '').strip()
            if (s.get('grid_col_start') != 1 or s.get('grid_col_end', 0) != s.get('table_column_count')
                    or s.get('row_end') != row or not n.get('num_id') or n.get('format') == 'bullet'
                    or not isinstance(n.get('value'), int) or b.get('controls')
                    or not 0 < len(text) <= 160 or re.search(r'[?？]|_{2,}|\b(?:please|provide|describe|specify)\b', text, re.I)):
                continue
            candidates[(n['num_id'], n.get('level'))].append(b)
        selected = []
        for values in candidates.values():
            values.sort(key=lambda b: _s(b)['row_start'])
            # An isolated full-width item can still be a question. Require
            # the native list's ordered section sequence and subordinate rows.
            if len(values) < 2 or any(_number(b)['value'] >= _number(c)['value'] for b, c in zip(values, values[1:])):
                continue
            for b in values:
                if not any(r > _s(b)['row_start'] and len(row) >= 2 for r, row in physical.items()):
                    continue
                _s(b).update(resolved_layout_role='section_header',
                             local_section_evidence='native_numbered_full_width_table_sequence')
                selected.append(b)
        if selected:
            headings[table_id] = sorted(selected, key=lambda b: _s(b)['row_start'])

        for start, cells in physical.items():
            for anchor in cells:
                s = _s(anchor); n = _number(anchor); end = s.get('row_end', start)
                if (s.get('vertical_merge') != 'restart' or end <= start
                        or not s.get('merge_anchor_id') or not n.get('num_id') or n.get('format') == 'bullet'
                        or anchor.get('controls') or anchor.get('text', '').strip()):
                    continue
                peers = [b for b in cells if b is not anchor]
                if len(peers) != 1:
                    continue
                prompt = peers[0]; ps = _s(prompt)
                if (not prompt.get('text', '').strip() or prompt.get('controls')
                        or ps.get('grid_col_start') != s.get('grid_col_end', 0) + 1
                        or ps.get('grid_col_end') != s.get('table_column_count') or ps.get('row_end') != start):
                    continue
                labels = []
                for row in range(start + 1, end + 1):
                    continuation = [b for b in rows.get(row, []) if _s(b).get('is_merge_continuation')
                                    and _s(b).get('merge_anchor_id') == s['merge_anchor_id']]
                    children = sorted(physical.get(row, []), key=lambda b: _s(b).get('grid_col_start', 0))
                    if (len(continuation) != 1 or len(children) != 2
                            or _s(children[0]).get('grid_col_start') != ps['grid_col_start']
                            or _s(children[0]).get('grid_col_end', 0) + 1 != _s(children[1]).get('grid_col_start')
                            or _s(children[1]).get('grid_col_end') != ps['grid_col_end']
                            or not children[0].get('text', '').strip()):
                        labels = []; break
                    labels.append(children[0]['block_id'])
                if labels:
                    # Word can restart an empty merged gutter halfway through
                    # a continued source list. Continue only the same native
                    # bullet sequence/grid, stopping at any numbered item.
                    continuation_refs=[]
                    last=next(b for b in rows[end] if b['block_id']==labels[-1])
                    bullet=_number(last)
                    current=end+1
                    while bullet.get('format')=='bullet' and isinstance(bullet.get('value'),int) and current in rows:
                        full=sorted(rows[current],key=lambda b:_s(b).get('grid_col_start',0))
                        if len(full)!=3:break
                        gutter,label,response=full;gs,ls,rs=map(_s,full);next_bullet=_number(label)
                        if (gutter.get('text','').strip() or gutter.get('controls') or _number(gutter)
                            or gs.get('grid_col_start')!=s['grid_col_start'] or gs.get('grid_col_end')!=s['grid_col_end']
                            or ls.get('grid_col_start')!=ps['grid_col_start'] or ls.get('grid_col_end',0)+1!=rs.get('grid_col_start')
                            or rs.get('grid_col_end')!=ps['grid_col_end'] or not label.get('text','').strip()
                            or any(next_bullet.get(k)!=bullet.get(k) for k in ('num_id','level','format','marker'))
                            or not isinstance(next_bullet.get('value'),int) or next_bullet['value']!=bullet['value']+1):break
                        labels.append(label['block_id']);continuation_refs.append(_ref(gutter));bullet=next_bullet
                        end=current;current+=1
                    groups.append({'table_id': table_id, 'row_start': start, 'row_end': end,
                                   'anchor_ref': {'cell_id': s['merge_anchor_id'], 'block_id': anchor['block_id']},
                                   'prompt': prompt, 'label_block_ids': labels,'continued_gutter_refs':continuation_refs})
    return headings, groups


def apply_word_group_context(fields, groups, lookup):
    by_label = defaultdict(list)
    for group in groups:
        for bid in group['label_block_ids']:
            by_label[bid].append(group)

    def visit(items, protected=False):
        for field in items:
            guarded = protected or bool(field.get('manual_override') or field.get('answer_origin') in {'manual', 'knowledge'}
                                       or field.get('answer_source') in {'manual', 'knowledge'})
            refs = field.get('label_refs') or field.get('question_refs') or []
            candidates = by_label.get(refs[0]['block_id'], []) if refs else []
            if not guarded and len(candidates) == 1:
                group = candidates[0]
                if all(r['block_id'] in group['label_block_ids'] for r in refs):
                    region_blocks = [lookup.get(r['block_id'], {}) for r in field.get('answer_region_refs', [])]
                    if all(_s(b).get('table_id') == group['table_id'] and
                           group['row_start'] < _s(b).get('row_start', 0) <= group['row_end'] for b in region_blocks):
                        refs = deepcopy(field.get('context_refs') or []) + [_ref(group['prompt'])]
                        field['context_refs'] = list({(r['block_id'], r['start'], r['end']): r for r in refs}.values())
                        field['native_parent_context'] = {
                            'version': VERSION, 'prompt_refs': [_ref(group['prompt'])],
                            'merge_anchor_ref': deepcopy(group['anchor_ref']),
                            'row_start': group['row_start'], 'row_end': group['row_end']}
            visit(field.get('children', []), guarded)
    visit(fields)
    _restore_group_containers(fields,groups,lookup)


def _restore_group_containers(fields,groups,lookup):
    """A full-width lead-in owns its separately answered rows, not a gutter."""
    from .form_completion import new_field
    from .word_bullet_answer_groups import _protected
    def walk(items):
        for field in items:
            yield field
            yield from walk(field.get('children',[]))
    def key(rr):return rr.get('block_id'),rr.get('start',0),rr.get('end',0),rr.get('slot_id','')
    for group in groups:
        all_fields=list(walk(fields));prompt=group['prompt'];pid=prompt['block_id']
        guarded_ids=set()
        def guarded(items, inherited=False):
            for item in items:
                protect=inherited or _protected(item)
                if protect:guarded_ids.add(id(item))
                guarded(item.get('children',[]),protect)
        guarded(fields)
        parents=[f for f in all_fields if f.get('label_refs') and all(r.get('block_id')==pid for r in f['label_refs'])]
        selected=[]
        for bid in group['label_block_ids']:
            found=[f for f in all_fields if f.get('label_refs') and all(r.get('block_id')==bid for r in f['label_refs'])
                and f.get('answer_region_refs')]
            if len(found)!=1:break
            child=found[0];label=lookup[bid];ls=_s(label)
            if any((_s(lookup.get(r['block_id'],{})).get('table_id')!=group['table_id']
                    or _s(lookup.get(r['block_id'],{})).get('row_start')!=ls.get('row_start')
                    or _s(lookup.get(r['block_id'],{})).get('grid_col_start',0)<=ls.get('grid_col_end',0))
                   for r in child['answer_region_refs']):break
            selected.append(child)
        if len(selected)!=len(group['label_block_ids']) or any(id(f) in guarded_ids for f in parents+selected):continue
        child_targets={key(r) for f in selected for r in f['answer_region_refs']}
        anchor_id=group['anchor_ref']['block_id']
        ghosts=[f for f in parents if f.get('parent_question_id') and f.get('answer_region_refs')
                and all(r['block_id']==anchor_id for r in f['answer_region_refs'])
                and not f.get('answer_text_original') and not f.get('answer_value_refs')]
        real_parents=[f for f in parents if f not in ghosts]
        if len(real_parents)>1:continue
        parent=real_parents[0] if real_parents else new_field(prompt,_ref(prompt))
        if (parent.get('answer_text_original') or parent.get('answer_value_refs')
            or any(key(r) not in child_targets and r['block_id']!=anchor_id for r in parent.get('answer_region_refs',[]))
            or any(c not in ghosts and c not in selected for c in parent.get('children',[]))):continue
        retired=[f.get('field_id') for f in ghosts]
        parent.update(answer_region_refs=[],answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='blank',
            options=[],children=selected,review_status='model_proposed',review_reasons=[],
            boundary_evidence='native_numbered_row_group_container',boundary_version=VERSION,
            native_group_evidence={'version':VERSION,'prompt_refs':[_ref(prompt)],
                'anchor_ref':deepcopy(group['anchor_ref']),'row_start':group['row_start'],'row_end':group['row_end'],
                'label_block_ids':deepcopy(group['label_block_ids']),'continued_gutter_refs':deepcopy(group.get('continued_gutter_refs',[])),
                'retired_number_gutter_fields':retired})
        parent.pop('form_definition',None)
        for child in selected:child['parent_question_id']=parent['field_id']
        remove={id(f) for f in selected+ghosts}
        def retain(items):
            kept=[]
            for f in items:
                if id(f) in remove:continue
                if f is not parent:f['children']=retain(f.get('children',[]))
                kept.append(f)
            return kept
        fields[:]=retain(fields)
        if not real_parents:fields.append(parent)
        prompt.setdefault('structure',{})['native_group_container']=deepcopy(parent['native_group_evidence'])
    fields.sort(key=lambda f:lookup.get((f.get('label_refs') or [{}])[0].get('block_id'),{}).get('order',10**9))


def verified_group_container(field,lookup):
    """Replay physical grouping, never trust a saved container flag alone."""
    proof=field.get('native_group_evidence') or {}
    if proof.get('version')!=VERSION or field.get('answer_region_refs') or field.get('options'):
        return False
    _,groups=native_word_table_context(list(lookup.values()))
    groups=[g for g in groups if proof.get('prompt_refs')==[_ref(g['prompt'])]
        and proof.get('anchor_ref')==g['anchor_ref'] and proof.get('label_block_ids')==g['label_block_ids']
        and (proof.get('row_start'),proof.get('row_end'))==(g['row_start'],g['row_end'])]
    if len(groups)!=1:return False
    g=groups[0];children=field.get('children') or []
    labels=field.get('label_refs') or []
    expected=_ref(g['prompt'])
    if (len(labels)!=1 or any(labels[0].get(k,0 if k in {'start','end'} else '')!=expected[k]
            for k in ('block_id','start','end','slot_id'))
        or len(children)!=len(g['label_block_ids'])):return False
    for child,bid in zip(children,g['label_block_ids']):
        if (child.get('parent_question_id')!=field.get('field_id') or not child.get('answer_region_refs')
            or child.get('pairing_validation')!='valid' or not child.get('label_refs')
            or any(r['block_id']!=bid for r in child['label_refs'])):return False
        ls=_s(lookup[bid])
        for r in child['answer_region_refs']:
            target=_s(lookup.get(r['block_id'],{}))
            if (target.get('table_id')!=g['table_id'] or target.get('row_start')!=ls['row_start']
                or target.get('grid_col_start',0)<=ls['grid_col_end']):return False
    return True
