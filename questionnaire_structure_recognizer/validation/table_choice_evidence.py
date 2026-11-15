"""Associate native PDF boxes with an independently extracted table row.

Separate label/response cells do not share a single enclosing rectangle in
many PDFs. The visible cell borders, grid and exact caption-to-box evidence
provide a different proof; they are not a reason to accept the nearest box.
"""
from __future__ import annotations

import math

VERSION = 'native-table-choice-association-v1'


def _box(value):
    return (isinstance(value, (list, tuple)) and len(value) == 4
            and all(isinstance(n, (int, float)) and math.isfinite(n) for n in value)
            and value[0] < value[2] and value[1] < value[3])


def _inside(inner, outer):
    return (_box(inner) and _box(outer) and outer[0] - .1 <= inner[0]
            and outer[1] - .1 <= inner[1] and inner[2] <= outer[2] + .1
            and inner[3] <= outer[3] + .1)


def native_table_choice_region(field, options, lookup):
    """Return a reproducible source proof, or None without changing content."""
    from ..parser_core.pdf_vector_forms import verified_vector_control

    if field.get('boundary_evidence') != 'native_printed_choice_response_lane':
        return None
    labels = field.get('label_refs') or []
    captions = [r for option in options for r in option.get('refs', [])]
    if (not labels or len(options) < 2 or not captions
            or len({r.get('block_id') for r in labels}) != 1
            or len({r.get('block_id') for r in captions}) != 1):
        return None
    label = lookup.get(labels[0].get('block_id'), {})
    answer = lookup.get(captions[0].get('block_id'), {})
    if not label or not answer or label['block_id'] == answer['block_id']:
        return None
    # If the native reader already supplied an enclosing frame, its separate
    # verifier must succeed. Never use this alternate representation to bypass
    # an inconsistent/missing control association inside that known frame.
    if label.get('structure', {}).get('native_closed_form_regions'):
        return None
    for refs, block in ((labels, label), (captions, answer)):
        for ref in refs:
            a, z = ref.get('start'), ref.get('end')
            if (not isinstance(a, int) or not isinstance(z, int)
                    or not 0 <= a < z <= len(block.get('text', ''))
                    or not block['text'][a:z].strip()):
                return None
    ls, rs = label.get('structure', {}), answer.get('structure', {})
    ll, rl = label.get('location', {}), answer.get('location', {})
    lb, rb = ll.get('bbox'), rl.get('bbox')
    for block, structure, location in ((label, ls, ll), (answer, rs, rl)):
        if (block.get('kind') != 'table_cell' or location.get('type') != 'pdf'
                or location.get('extraction') != 'native'
                or structure.get('layout_evidence') != 'visible_vector_borders'
                or not structure.get('cell_id') or not structure.get('table_id')
                or structure.get('is_merge_continuation')
                or not isinstance(structure.get('row_start'), int)
                or structure.get('row_start') != structure.get('row_end')
                or not _inside(location.get('bbox'), location.get('table_bbox'))):
            return None
    if (ll.get('page') != rl.get('page') or ls['table_id'] != rs['table_id']
            or ls['cell_id'] == rs['cell_id'] or ls['row_start'] != rs['row_start']
            or not isinstance(ls.get('grid_col_end'), int)
            or ls['grid_col_end'] + 1 != rs.get('grid_col_start')
            or not _box(lb) or not _box(rb)
            or max(abs(lb[1] - rb[1]), abs(lb[3] - rb[3]), abs(lb[2] - rb[0])) > .1
            or any(abs(a - b) > .1 for a, b in zip(ll['table_bbox'], rl['table_bbox']))):
        return None
    # The response cell must contain only the source captions/separators.
    # A mixed second question or instruction cannot be swallowed by a row.
    previous = 0
    for ref in sorted(captions, key=lambda r: (r['start'], r['end'])):
        if ref['start'] < previous or answer['text'][previous:ref['start']].strip(' \t\r\n/|'):
            return None
        previous = ref['end']
    if answer['text'][previous:].strip(' \t\r\n/|'):
        return None
    targets, boxes = [], []
    for option in options:
        refs = option.get('answer_region_refs', [])
        if len(refs) != 1:
            return None
        target = lookup.get(refs[0].get('block_id'), {})
        if not verified_vector_control(target) or target['block_id'] in targets:
            return None
        control = target['controls'][0]
        box = target.get('location', {}).get('bbox')
        if (option.get('selection_source') != 'native_pdf_vector'
                or refs[0].get('control_id') != control.get('control_id')
                or refs[0].get('start') != 0 or refs[0].get('end') != 0
                or option.get('selected') is not control.get('selected')
                or option.get('refs') != control.get('caption_refs')
                or option.get('refs') != target.get('structure', {}).get('native_vector_caption_refs')
                or target.get('location', {}).get('page') != ll.get('page')
                or not _inside(box, rb)):
            return None
        if any(min(box[2], old[2]) > max(box[0], old[0])
               and min(box[3], old[3]) > max(box[1], old[1]) for old in boxes):
            return None
        targets.append(target['block_id'])
        boxes.append(box)
    return {'source': 'native_vector_table_row', 'version': VERSION,
            'page': ll['page'], 'table_id': ls['table_id'], 'row': ls['row_start'],
            'label_block_id': label['block_id'], 'answer_block_id': answer['block_id'],
            'label_cell_id': ls['cell_id'], 'answer_cell_id': rs['cell_id'],
            'bbox': [lb[0], lb[1], rb[2], lb[3]], 'response_bbox': list(rb),
            'control_block_ids': targets}


def verified_table_vector_ref(ref, field, lookup):
    if ref not in field.get('answer_region_refs', []):
        return False
    region = native_table_choice_region(field, field.get('options', []), lookup)
    return bool(region and region == field.get('physical_form_region')
                and ref.get('block_id') in region['control_block_ids'])
