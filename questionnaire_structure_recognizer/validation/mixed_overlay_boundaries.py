"""Keep native filled overlays inside their physically established scan fields.

Only source-backed continuation lines and repeated record-column lanes are
restored. Native values are never reclassified by their spelling. Conflicting
model remnants are retired only when every source is already accounted for.
"""
from collections import defaultdict
from copy import deepcopy

from .field_boundaries import ref
from .form_completion import new_field
from .pdf_source_relations import native_overlay_value_in_region

VERSION = 'mixed-native-overlay-boundaries-v2-record-lane-provenance'


def _box(b): return b.get('location', {}).get('bbox', [0, 0, 0, 0])
def _cy(b): return (_box(b)[1]+_box(b)[3])/2
def _page(b): return b.get('location', {}).get('page')
def _s(b): return b.setdefault('structure', {})


def _flat(fields):
    for f in fields:
        yield f
        yield from _flat(f.get('children', []))


def _protected(f):
    return f.get('manual_override') or f.get('answer_source') in {'manual', 'knowledge'} or f.get('answer_origin') in {'manual', 'knowledge'}


def _values(region, lookup):
    """Check every native overlay against the same geometry as final validation."""
    ids = _s(region).get('overlay_value_block_ids', [])
    if not ids or _s(region).get('scan_region_kind') != 'write_line': return []
    candidate = deepcopy(region)
    candidate['structure']['scan_association_valid'] = True
    local = dict(lookup)
    local[region['block_id']] = candidate
    values = [lookup[bid] for bid in ids if bid in lookup]
    if len(values) != len(ids) or not all(native_overlay_value_in_region(ref(v), [ref(candidate)], local) for v in values): return []
    return values


def _set_values(field, lookup):
    values = []
    for rr in field.get('answer_region_refs', []):
        region = lookup[rr['block_id']]
        _s(region).update(scan_association_valid=True, scan_field_id=field['field_id'])
        for value in _values(region, lookup):
            if value['block_id'] not in {x['block_id'] for x in values}: values.append(value)
    if values:
        field.update(answer_value_refs=[ref(v) for v in values], answer_refs=[ref(v) for v in values],
                     answer_text_original='\n'.join(v['text'] for v in values), answer_status='filled',
                     answer_pairing_evidence='native_overlay_inside_physical_response')
        for value in values: _s(value)['resolved_layout_role'] = 'answer_value'
        field.setdefault('source_locations', {})['answer'] = [deepcopy(v['location']) for v in values]
    field.setdefault('source_locations', {})['answer_regions'] = [deepcopy(lookup[r['block_id']]['location']) for r in field['answer_region_refs']]
    field['mixed_overlay_boundary_version'] = VERSION


def restore_mixed_overlay_boundaries(questions, blocks, lookup):
    pages = defaultdict(list)
    for b in blocks:
        if b.get('location', {}).get('type') == 'pdf' and len(_box(b)) == 4 and _page(b): pages[_page(b)].append(b)
    original_fields = list(_flat(questions))
    protected_ids = set()
    def protect_tree(items, inherited=False):
        for field in items:
            protected = inherited or bool(_protected(field))
            if protected: protected_ids.add(field['field_id'])
            protect_tree(field.get('children', []), protected)
    protect_tree(questions)
    established = [f for f in original_fields if f['field_id'] not in protected_ids and not f.get('options') and
                   f.get('boundary_evidence') in {'scan_physical_form', 'mixed_overlay_record_lane'}
                   and f.get('label_refs') and f.get('answer_region_refs')]
    owned = {r['block_id'] for f in original_fields if f['field_id'] in protected_ids or f in established for r in f.get('answer_region_refs', [])}
    audit = {'version': VERSION, 'continued_fields': [], 'record_children': [], 'retired_model_groups': []}
    # A full-width response BELOW a prompt can contain several written lines.
    # Stop at any intervening printed text or another established target.
    for field in established:
        regions = [lookup[r['block_id']] for r in field['answer_region_refs'] if r['block_id'] in lookup]
        labels = [lookup[r['block_id']] for r in field['label_refs'] if r['block_id'] in lookup]
        if not regions or not labels or any(_s(r).get('scan_region_kind') != 'write_line' for r in regions): continue
        page = _page(regions[0])
        if any(_page(b) != page for b in regions+labels): continue
        first = min(regions, key=lambda b: _box(b)[1])
        if max(_box(b)[3] for b in labels) >= _box(first)[1]-2 or _box(first)[2]-_box(first)[0] < 250: continue
        last = max(regions, key=lambda b: _box(b)[1])
        candidates = sorted([r for r in pages[page] if _s(r).get('scan_region_kind') == 'write_line' and _box(r)[1] > _box(last)[1]], key=lambda b: _box(b)[1])
        added = []
        for region in candidates:
            rb, lb = _box(region), _box(last)
            if rb[1]-lb[3] >= 28: break
            if abs(rb[0]-lb[0]) >= 5 or abs(rb[2]-lb[2]) >= 12: continue
            if region['block_id'] in owned: break
            if not _values(region, lookup): break
            printed = [b for b in pages[page] if b.get('text', '').strip() and b.get('location', {}).get('extraction') in {'ocr', 'ocr_region'} and not _s(b).get('scan_region_kind')]
            if any(lb[3]-2 < _cy(b) < rb[3]+2 and _box(b)[0] < rb[2] and _box(b)[2] > rb[0] for b in printed): break
            field['answer_region_refs'].append(ref(region)); owned.add(region['block_id'])
            added.append(region['block_id']); last = region
        if added:
            _set_values(field, lookup)
            audit['continued_fields'].append({'field_id': field['field_id'], 'added_region_ids': added})

    # An unassigned native-filled right column shares one printed heading
    # across at least two physically aligned record rows. Identity is the row's
    # established parent label, never the repeated word in its filled value.
    proposals = defaultdict(list)
    for page, items in pages.items():
        for region in items:
            if region['block_id'] in owned or not _values(region, lookup): continue
            rb = _box(region)
            parents = []
            for f in established:
                if len(f['answer_region_refs']) != 1: continue
                left = lookup[f['answer_region_refs'][0]['block_id']]
                if _page(left) == page and _box(left)[2] < rb[0]-5 and abs(_cy(left)-_cy(region)) < 5: parents.append(f)
            if len(parents) != 1: continue
            headers = [b for b in items if b.get('text', '').strip() and len(b['text']) < 60 and
                       b.get('location', {}).get('extraction') in {'ocr', 'ocr_region'} and not _s(b).get('scan_region_kind') and
                       rb[0]-8 <= _box(b)[0] < rb[2] and _box(b)[2] <= rb[2]+8 and 0 < rb[1]-_box(b)[3] < 110]
            if not headers: continue
            header = max(headers, key=lambda b: _box(b)[3])
            proposals[(page, header['block_id'])].append((parents[0], region, header))
    for group in proposals.values():
        if len({p['field_id'] for p, _, _ in group}) < 2: continue
        for parent, region, header in group:
            child = new_field(header, ref(header), [ref(region)], parent['field_id'])
            fid = 'scan-field:'+header['block_id']+':'+region['block_id']
            child.update(field_id=fid, question_id=fid, boundary_evidence='mixed_overlay_record_lane', boundary_version=VERSION,
                         context_refs=deepcopy(parent['label_refs']), context_label=parent['question_text_original'],
                         section_refs=deepcopy(parent.get('section_refs', [])), section_text_original=parent.get('section_text_original', ''))
            child['source_locations'] = {'question': [deepcopy(header['location'])]}
            _set_values(child, lookup)
            parent.setdefault('children', []).append(child); established.append(child); owned.add(region['block_id'])
            audit['record_children'].append({'field_id': fid, 'parent_field_id': parent['field_id'], 'region_id': region['block_id']})

    # Reconcile model remnants only using exact source ranges already resolved
    # to physical native values or the owning field's original context spans.
    owners = {}
    for field in established:
        for rr in field.get('answer_value_refs', []):
            owners.setdefault(rr['block_id'], []).append((field, rr))
    def value_owner(rr):
        return [f for f, vr in owners.get(rr.get('block_id'), []) if vr['start'] <= rr.get('start', 0) < rr.get('end', 0) <= vr['end']]
    def clean(items):
        kept = []
        for field in items:
            if _protected(field):
                kept.append(field); continue
            field['children'] = clean(field.get('children', []))
            if field in established or field.get('children'):
                kept.append(field); continue
            labels = field.get('label_refs', [])
            values_only = bool(labels) and all(value_owner(rr) for rr in labels) and all(
                value_owner(rr) for rr in field.get('answer_value_refs', []))
            context_owner = None
            if not values_only:
                for parent in established:
                    contexts = parent.get('context_refs', [])+parent.get('instruction_refs', [])
                    def local(rr):
                        return parent in value_owner(rr) or any(x['block_id'] == rr.get('block_id') and x['start'] <= rr.get('start', 0) < rr.get('end', 0) <= x['end'] for x in contexts)
                    answer_refs = field.get('answer_value_refs', [])
                    if labels and contexts and all(local(rr) for rr in labels) and all(parent in value_owner(rr) for rr in answer_refs):
                        context_owner = parent; break
            if values_only or context_owner:
                record = {'field_id': field['field_id'], 'reason': 'source_value_or_owner_context_reclassified',
                          'label_refs': deepcopy(labels), 'answer_value_refs': deepcopy(field.get('answer_value_refs', [])),
                          'owner_field_id': context_owner['field_id'] if context_owner else None}
                audit['retired_model_groups'].append(record)
                for rr in labels: _s(lookup[rr['block_id']]).setdefault('overlay_reclassification', []).append(deepcopy(record))
                continue
            kept.append(field)
        return kept
    questions[:] = clean(questions)
    from .mixed_response_coverage import reconcile_response_coverage
    audit['response_coverage'] = reconcile_response_coverage(questions, pages, lookup, _values, _set_values)
    return audit
