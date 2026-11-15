"""Resolve one physical matrix accidentally interpreted as reserved records.

Column words are not evidence. Every empty record cell must already have one
validated independent choice-row owner using the very same header text range.
"""

VERSION = 'matrix-record-ownership-v2-source-control-uniqueness'


def _walk(fields):
    for field in fields:
        yield field
        yield from _walk(field.get('children', []))


def _key(ref):
    return (ref.get('block_id'), ref.get('start'), ref.get('end'), ref.get('slot_id') or '')


def _controlled(field):
    return (field.get('manual_override') or field.get('review_status') in
            {'human_corrected', 'confirmed', 'approved', 'reviewed'} or
            field.get('answer_origin') in {'manual', 'knowledge', 'reviewed_knowledge'})


def _empty(field):
    return (not _controlled(field) and field.get('answer_status') == 'blank' and
            not any(field.get(k) for k in ('answer_text_original', 'original_answer',
                    'observed_answer_text', 'answer_value_refs', 'answer_refs')))


def _proof(record, choices, lookup):
    if (not record.get('field_id', '').startswith('scan-records:') or
            not record.get('record_columns') or not record.get('record_rows') or
            not _empty(record)):
        return None
    children = record.get('children', [])
    if not children or any(not _empty(c) or c.get('children') or c.get('options') or
            c.get('record_slot_status') != 'reserved' for c in children):
        return None
    child_lookup = {c['field_id']: c for c in children}
    if len(child_lookup) != len(children):
        return None
    headers = [_key(c.get('label_ref', {})) for c in record['record_columns']]
    if len(set(headers)) != len(headers) or any(not h[0] or h[0] not in lookup or
            not isinstance(h[1], int) or not isinstance(h[2], int) or
            not 0 <= h[1] < h[2] <= len(lookup[h[0]].get('text', '')) for h in headers):
        return None
    if set(map(_key, record.get('label_refs', []))) != set(headers):
        return None
    proofs, seen_children, seen_regions, seen_owners = [], set(), set(), set()
    for row in record['record_rows']:
        ids = row.get('field_ids', [])
        if (not row.get('reserved_blank_row') or len(ids) != len(headers) or
                len(set(ids)) != len(ids) or seen_children.intersection(ids) or
                any(i not in child_lookup for i in ids)):
            return None
        cells = [child_lookup[i] for i in ids]
        region_keys = []
        for cell, header in zip(cells, headers):
            refs = cell.get('answer_region_refs', [])
            if len(refs) != 1 or list(map(_key, cell.get('label_refs', []))) != [header]:
                return None
            region = lookup.get(refs[0].get('block_id'), {})
            if (region.get('location', {}).get('type') != 'pdf' or
                    region.get('structure', {}).get('scan_region_kind') != 'text_box' or
                    region.get('structure', {}).get('blank_state') != 'blank'):
                return None
            region_keys.append(_key(refs[0]))
        if len(set(region_keys)) != len(region_keys) or seen_regions.intersection(region_keys):
            return None
        owners = []
        for choice in choices:
            if choice.get('field_id') in seen_owners:
                continue
            if (choice.get('boundary_evidence') != 'closed_scan_column_choice_row' or
                    choice.get('pairing_validation') != 'valid' or
                    choice.get('validation_status') != 'valid' or
                    not choice.get('label_refs') or
                    set(map(_key, choice['label_refs'])).intersection(headers) or
                    set(map(_key, choice.get('answer_region_refs', []))) != set(region_keys)):
                continue
            options = choice.get('options', [])
            if len(options) != len(headers):
                continue
            mapping = {tuple(map(_key, o.get('answer_region_refs', []))):
                       tuple(map(_key, o.get('refs', []))) for o in options}
            if len(mapping) == len(headers) and all(mapping.get((rk,)) == (hk,)
                    for rk, hk in zip(region_keys, headers)):
                owners.append(choice)
        if len(owners) != 1:
            return None
        owner = owners[0]
        seen_children.update(ids)
        seen_regions.update(region_keys)
        seen_owners.add(owner['field_id'])
        proofs.append({'record_row': row.get('row'), 'field_id': owner['field_id'],
                       'answer_region_ids': [r[0] for r in region_keys],
                       'header_refs': [c['label_ref'] for c in record['record_columns']]})
    if seen_children != set(child_lookup):
        return None
    return proofs


def reconcile_matrix_record_ownership(fields, lookup):
    """Remove only an unedited, empty, fully duplicated derived interpretation."""
    choices = [f for f in _walk(fields) if f.get('options')]
    report = {'version': VERSION, 'removed_record_groups': []}

    def visit(items, protected=False):
        retained = []
        for item in items:
            guarded = protected or _controlled(item)
            proof = None if guarded else _proof(item, choices, lookup)
            if proof:
                report['removed_record_groups'].append({
                    'field_id': item['field_id'],
                    'derived_child_ids': [c['field_id'] for c in item['children']],
                    'reason': 'Every reserved cell is the identical physical option of an independently validated choice row.',
                    'rows': proof})
                continue
            if item.get('children') and not guarded:
                visit(item['children'])
            retained.append(item)
        items[:] = retained

    visit(fields)
    from .checkbox_symbol_ownership import reconcile_checkbox_symbol_ownership
    report['checkbox_symbols'] = reconcile_checkbox_symbol_ownership(fields, lookup)
    return report
