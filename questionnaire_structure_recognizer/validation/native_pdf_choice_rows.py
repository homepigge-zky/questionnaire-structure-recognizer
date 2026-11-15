"""Native PDF response rows inside one physical table cell.

The row's closed cell geometry, actual caption paint runs and separate native
checkboxes establish the boundaries. Text lists and equal counts do not.
"""
from copy import deepcopy
import re

from .field_boundaries import ref
from .form_completion import new_field
from .native_vector_forms import _choice_set, _write_choice, _protected, _overlap
from ..parser_core.pdf_vector_forms import verified_vector_control

VERSION = 'native-pdf-choice-rows-v1-physical-baselines'


def is_native_caption_list(value_ref, lookup):
    """A list of printed captions is never a value, even if pairing failed."""
    block = lookup.get(value_ref.get('block_id'),{})
    a, z = value_ref.get('start'), value_ref.get('end')
    if not isinstance(a,int) or not isinstance(z,int) or not 0 <= a < z <= len(block.get('text','')):return False
    refs = [r for b in lookup.values() if b.get('structure',{}).get('physical_region_kind') == 'native_vector_checkbox'
            for r in b.get('structure',{}).get('native_vector_caption_refs',[])
            if r.get('block_id') == value_ref['block_id'] and a <= r['start'] < r['end'] <= z]
    if len({(r['start'],r['end']) for r in refs}) < 2:return False
    # Additional printed notes (such as a copy-attachment instruction) do
    # not turn the simultaneously included Yes/No captions into a value.
    # Keep the disputed source intact, but require separate bounded refs.
    return True


def _box(block):
    return block.get('location', {}).get('bbox', [])


def _plans(label, response, blocks, lookup):
    ls, rs = label.get('structure', {}), response.get('structure', {})
    lb, rb = _box(label), _box(response)
    if (label.get('kind') != 'table_cell' or response.get('kind') != 'table_cell'
        or label.get('location', {}).get('extraction') != 'native'
        or label['location'].get('type') != 'pdf'
        or label['location'].get('page') != response.get('location', {}).get('page')
        or ls.get('layout_evidence') != 'visible_vector_borders'
        or rs.get('layout_evidence') != 'visible_vector_borders'
        or not ls.get('table_id') or ls['table_id'] != rs.get('table_id')
        or (ls.get('row_start'), ls.get('row_end')) != (rs.get('row_start'), rs.get('row_end'))
        or len(lb) != 4 or len(rb) != 4 or abs(lb[2]-rb[0]) > 1
        or abs(lb[1]-rb[1]) > 1 or abs(lb[3]-rb[3]) > 1):return []
    controls = [b for b in blocks if b.get('location', {}).get('page') == label['location']['page']
                and b.get('structure', {}).get('physical_region_kind') == 'native_vector_checkbox'
                and all(rb[0] <= x <= rb[2] for x in _box(b)[::2])
                and all(rb[1] <= y <= rb[3] for y in _box(b)[1::2])]
    if not controls or not all(verified_vector_control(b) for b in controls):return []
    if any(any(r['block_id'] != response['block_id'] for r in b['structure']['native_vector_caption_refs']) for b in controls):return []
    rows = []
    for control in sorted(controls, key=lambda b: sum(_box(b)[1::2])/2):
        cy = sum(_box(control)[1::2])/2
        if rows and abs(rows[-1][0]-cy) < 2: rows[-1][1].append(control)
        else: rows.append((cy, [control]))
    if len(rows) < 2 or any(not _choice_set(row, lookup) for _, row in rows):return []
    # Every printed character in the response cell must be a native caption.
    # A comment/value in the same cell requires another bounded association.
    covered = set(i for b in controls for r in b['structure']['native_vector_caption_refs'] for i in range(r['start'], r['end']))
    if any(c.strip() and i not in covered for i,c in enumerate(response['text'])):return []
    spans = ls.get('native_text_spans', [])
    if not spans:return []
    # Snap the between-response midpoint only to a nearby actual text gap.
    # A wrapped second question may begin just above that midpoint. Never
    # cut a source line or use counts alone to split bilingual wording.
    ordered = sorted(spans,key=lambda s:s['bbox'][1])
    edges = [lb[1]]
    for previous, following in zip(rows,rows[1:]):
        midpoint = (previous[0]+following[0])/2
        gaps = [(a['bbox'][3]+b['bbox'][1])/2 for a,b in zip(ordered,ordered[1:])
                if b['bbox'][1]-a['bbox'][3] > 1
                and previous[0] < (a['bbox'][3]+b['bbox'][1])/2 < following[0]
                and abs((a['bbox'][3]+b['bbox'][1])/2-midpoint) < .3*(following[0]-previous[0])]
        if not gaps:return []
        gaps.sort(key=lambda y:abs(y-midpoint))
        if len(gaps)>1 and abs(abs(gaps[0]-midpoint)-abs(gaps[1]-midpoint))<1:return []
        edges.append(gaps[0])
    edges.append(lb[3])
    plans = []
    for index, (cy, row) in enumerate(rows):
        local = [s for s in spans if edges[index] <= (s['bbox'][1]+s['bbox'][3])/2 < edges[index+1]]
        if not local or any(s['bbox'][1] < edges[index]-1 or s['bbox'][3] > edges[index+1]+1 for s in local):return []
        start, end = min(s['start'] for s in local), max(s['end'] for s in local)
        text = label['text'][start:end]
        if not re.search(r'[?？]|\b(?:please|describe|provide|state|specify)\b|请', text, re.I):return []
        plans.append({'label_ref': ref(label, start, end), 'controls': row,
                      'local_bbox':[lb[0], edges[index], rb[2], edges[index+1]],
                      'response_cell_id':response['block_id']})
    covered = set(i for plan in plans for i in range(plan['label_ref']['start'], plan['label_ref']['end']))
    if any(c.strip() and i not in covered for i,c in enumerate(label['text'])):return []
    return plans


def restore_native_pdf_choice_rows(fields, blocks, lookup):
    cells = [b for b in blocks if b.get('kind') == 'table_cell' and b.get('location',{}).get('type') == 'pdf']
    for label in cells:
        candidates = [response for response in cells if response is not label
                      and response.get('structure',{}).get('table_id') == label.get('structure',{}).get('table_id')
                      and response.get('structure',{}).get('row_start') == label.get('structure',{}).get('row_start')]
        options = [plans for response in candidates if (plans := _plans(label, response, blocks, lookup))]
        if len(options) != 1:continue
        plans = options[0]
        if any(_protected(old) and any(_overlap(r,p['label_ref']) for r in old.get('label_refs',[]) for p in plans) for old in fields):continue
        recovered = []
        for plan in plans:
            lr = plan['label_ref']; field = new_field(label, lr, [])
            fid = f"pdf-choice-row:{label['block_id']}:{lr['start']}:{lr['end']}"
            field.update(field_id=fid, question_id=fid, boundary_evidence='native_pdf_table_choice_row',
                         boundary_version=VERSION, native_choice_row={k:deepcopy(v) for k,v in plan.items() if k != 'controls'})
            _write_choice(field, plan['controls'], lookup)
            if recovered and re.match(r'^\s*if\s+(?:yes|no)\b', field['question_text_original'], re.I):
                field['context_refs'] = deepcopy(recovered[-1]['label_refs'])
                field['explicit_condition'] = {'status':'unresolved','source_text':field['question_text_original'],
                                                'parent_field_id':recovered[-1]['field_id']}
            recovered.append(field)
        consumed = [p['label_ref'] for p in plans]+[r for p in plans for b in p['controls'] for r in b['structure']['native_vector_caption_refs']]
        kept = []
        for old in fields:
            refs = old.get('label_refs',[])
            covered = refs and all(any(c['block_id']==r['block_id'] and c['start']<=i<c['end'] for c in consumed)
                                  for r in refs for i in range(r['start'], r['end']) if lookup[r['block_id']]['text'][i].strip())
            if covered and not _protected(old) and not old.get('children'):
                label['structure'].setdefault('native_choice_row_replaced_groups',[]).append(deepcopy(old))
            else:kept.append(old)
        fields[:] = kept+recovered
    from .native_table_positions import restore_native_table_positions
    restore_native_table_positions(fields, blocks, lookup)
    fields.sort(key=lambda f:(lookup.get((f.get('label_refs')or[{}])[0].get('block_id'),{}).get('order',0), (f.get('label_refs')or[{}])[0].get('start',0)))
    return fields


def verified_table_choice_ref(region_ref, field, lookup):
    if field.get('boundary_evidence') != 'native_pdf_table_choice_row' or field.get('boundary_version') != VERSION:return False
    evidence = field.get('native_choice_row',{}); lr = evidence.get('label_ref',{})
    label, response = lookup.get(lr.get('block_id'),{}), lookup.get(evidence.get('response_cell_id'),{})
    if not label or not response or field.get('label_refs') != [lr]:return False
    for plan in _plans(label,response,list(lookup.values()),lookup):
        if plan['label_ref'] != lr or plan['local_bbox'] != evidence.get('local_bbox'):continue
        if region_ref not in field.get('answer_region_refs',[]):return False
        control = next((b for b in plan['controls'] if b['block_id'] == region_ref.get('block_id')),None)
        if not control:return False
        return any(region_ref in option.get('answer_region_refs',[]) and
                   option.get('refs') == control['controls'][0]['caption_refs'] and
                   option.get('selected') is control['controls'][0]['selected'] for option in field.get('options',[]))
    return False
