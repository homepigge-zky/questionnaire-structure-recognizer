"""Recover source-defined labels inside response cells without treating them as values.

This is deliberately bounded to explicit coordinate notation and contact
subforms. Native physical rows, paragraph ranges and local parent labels are
required. No nearby nonempty text is borrowed as an answer.
"""
from collections import defaultdict
from copy import deepcopy
import re
from .field_boundaries import ref
from .form_completion import new_field

VERSION = 'native-inline-label-slots-v1'


def _walk(fields):
    for field in fields:
        yield field
        yield from _walk(field.get('children', []))


def _paragraph(block):
    ps = block.get('structure', {}).get('paragraph_spans', [])
    active = [p for p in ps if p.get('text', '').strip()]
    return active[0] if len(active) == 1 and not block.get('controls') else None


def _children(parent, block, spans, context):
    p = _paragraph(block)
    children = []
    for a, z in spans:
        a += p['start']; z += p['start']
        slot_id = p['paragraph_id'] + f':prompt-slot:native-label-insert:{z}'
        slot = {'slot_id': slot_id, 'kind': 'inline_placeholder', 'start': z, 'end': z,
                'paragraph_index': p['paragraph_index'], 'paragraph_id': p['paragraph_id'],
                'label_start': a, 'label_end': z, 'evidence': VERSION}
        slots = block['structure'].setdefault('logical_slots', [])
        slots[:] = [s for s in slots if s.get('slot_id') != slot_id]
        slots.append(slot)
        child = new_field(block, ref(block, a, z), [ref(block, z, z, slot_id)], parent['field_id'])
        child.update(context_refs=[ref(context)], boundary_evidence='native_inline_label_group', boundary_version=VERSION)
        children.append(child)
    return children


def restore_word_label_slots(fields, blocks, lookup):
    rows = defaultdict(list)
    for block in blocks:
        s = block.get('structure', {})
        if (block.get('kind') == 'table_cell' and block.get('location', {}).get('type') == 'docx'
                and not s.get('is_merge_continuation') and s.get('blank_state') not in {'missing', 'unreadable'}):
            rows[(s.get('table_id'), s.get('row_start'), s.get('row_end'))].append(block)
    for cells in rows.values():
        row = sorted(cells, key=lambda b: b['structure'].get('grid_col_start', 0))
        if len(row) not in (2, 3) or any(a['structure']['grid_col_end'] + 1 != b['structure']['grid_col_start'] for a, b in zip(row, row[1:])):
            continue
        label = row[0]; plans = []
        if len(row) == 2 and re.search(r'GPS|坐标|经纬度|coordinates?', label['text'], re.I):
            p = _paragraph(row[1])
            if p:
                match = re.fullmatch(r'([NS])\s{2,}([EW])', p['text'])
                # Both directional labels must have real native whitespace.
                raw = p.get('raw_text', '')
                if match and re.fullmatch(r'\s*[NS]\s{2,}[EW]\s{2,}', raw):
                    plans = [(row[1], [match.span(1), match.span(2)])]
        elif len(row) == 3 and re.search(r'联系人|contact\s*(?:person|details)', label['text'], re.I):
            p, email = _paragraph(row[1]), _paragraph(row[2])
            if p and email:
                pattern = r'(姓名)\s+(职务|职位|职称)\s+(电话|联系电话)|(Name)\s+(Title|Position)\s+(Telephone|Phone)'
                match = re.fullmatch(pattern, p['text'], re.I)
                if match and re.fullmatch(r'E[ -]?mail\s*[:：]?|电子邮箱\s*[:：]?', email['text'], re.I):
                    plans = [(row[1], [match.span(i) for i in range(1, 7) if match.group(i)]),
                             (row[2], [(0, len(email['text']))])]
        if not plans:
            continue
        ids = {b['block_id'] for b in row}
        touching = [f for f in _walk(fields) if any(r.get('block_id') in ids for r in f.get('label_refs', []) + f.get('answer_region_refs', []))]
        if any(f.get('manual_override') or f.get('human_corrected')
               or f.get('review_status') == 'human_corrected'
               or (f.get('form_definition') or {}).get('mapping_review_status') == 'human_corrected'
               or f.get('answer_origin') in {'manual', 'knowledge', 'reviewed_knowledge'} for f in touching):
            continue
        # Reuse a stable parent only when its label belongs to this row.
        parent = next((f for f in touching if f.get('label_refs') == [ref(label)]), None)
        parent = parent or new_field(label, ref(label), [])
        children = [child for block, spans in plans for child in _children(parent, block, spans, label)]
        parent.update(children=children, answer_region_refs=[], answer_value_refs=[], answer_refs=[],
                      answer_text_original='', answer_status='blank', options=[], review_reasons=[],
                      boundary_evidence='native_inline_label_group', boundary_version=VERSION)
        def keep(items):
            kept = []
            for f in items:
                if f is parent:
                    continue
                refs = f.get('label_refs') or f.get('question_refs') or []
                if refs and all(r.get('block_id') in ids for r in refs):
                    continue
                f['children'] = keep(f.get('children', [])); kept.append(f)
            return kept
        fields[:] = keep(fields) + [parent]
        for block, _ in plans:
            ranges = block['structure'].setdefault('protected_label_ranges', [])
            for child in children:
                for r in child['label_refs']:
                    if r['block_id'] == block['block_id']:
                        entry = {'start': r['start'], 'end': r['end']}
                        if entry not in ranges: ranges.append(entry)


def pad_inline_label_value(target, value, lookup):
    """Spacing for a validated zero-width insertion, preserving native labels."""
    r = target.get('ref') or {}; block = lookup.get(r.get('block_id'), {})
    slot = next((s for s in block.get('structure', {}).get('logical_slots', [])
                 if s.get('slot_id') == r.get('slot_id') and s.get('evidence') == VERSION), None)
    if not slot or not value or not (r.get('start') == r.get('end') == slot.get('start') == slot.get('end')):
        return value
    text = block.get('text', ''); offset = r['start']
    prefix = ' ' if offset and not text[offset-1].isspace() else ''
    suffix = ' ' if offset < len(text) and not text[offset].isspace() else ''
    return prefix + value + suffix
