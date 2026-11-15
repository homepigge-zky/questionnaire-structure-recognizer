"""Retain printed form furniture as source content, never as fill targets.

This pass needs repeated occupied source cells or shared matrix-header geometry.
It never classifies by a customer/company name or deletes source blocks.
"""
from collections import defaultdict
from copy import deepcopy
import math
import re

from .field_boundaries import ref
from .pdf_source_relations import source_cells_share_row, source_text_inside_cell

VERSION = 'pdf-furniture-ownership-v3-verified-outer-banner-print'
HUMAN = {'confirmed', 'approved', 'reviewed', 'human_corrected', 'corrected'}


def _box(b):
    v = b.get('location', {}).get('bbox')
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        return None
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in v):
        return None
    return v if v[0] < v[2] and v[1] < v[3] else None


def _page(b): return b.get('location', {}).get('page')
def _norm(b): return re.sub(r'\s+', '', b.get('text', '')).casefold()
def _center(b):
    x = _box(b)
    return ((x[0]+x[2])/2, (x[1]+x[3])/2)


def _walk(fields):
    for f in fields:
        yield f
        yield from _walk(f.get('children') or [])


def _protected(f):
    d = f.get('form_definition') or {}
    return bool(f.get('manual_override') or f.get('human_mapping') or
                f.get('review_status') in HUMAN or f.get('mapping_review_status') in HUMAN or
                d.get('mapping_review_status') in HUMAN or
                f.get('answer_origin') in {'manual', 'knowledge', 'reviewed_knowledge'})


def _unused(f):
    return not (f.get('children') or f.get('record_columns') or
                f.get('answer_status') == 'filled' or f.get('original_answer_status') == 'filled' or
                any(f.get(k) for k in ('answer_text_original', 'original_answer',
                    'answer_value_refs', 'answer_refs')))


def _retirable(f, marked, lookup):
    if not _unused(f):
        # A derived logo -> issuer pairing can have a nonempty 'answer'. It
        # is fixed print only when BOTH sides have independent repeated-page
        # source proofs. Unreferenced values and every human edit stay intact.
        labels=f.get('label_refs') or f.get('question_refs') or []
        values=f.get('answer_value_refs') or f.get('answer_refs') or []
        refs=labels+values
        if (f.get('children') or f.get('record_columns') or f.get('options') or not labels or not values
            or any(r.get('block_id') not in marked or marked[r['block_id']][0]!='repeated_header_footer'
                or marked[r['block_id']][1].get('kind') not in {'repeated_occupied_document_title_band','repeated_print_with_closed_outer_banner'}
                or type(r.get('start')) is not int or type(r.get('end')) is not int
                or not 0<=r['start']<r['end']<=len(lookup[r['block_id']].get('text','')) for r in refs)
            or len({_page(lookup[r['block_id']]) for r in refs})!=1):return False
        value='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in values)
        if any(f.get(key) and f[key]!=value for key in ('answer_text_original','original_answer')):return False
    if f.get('observed_answer_text'):
        refs = f.get('unvalidated_answer_refs') or []
        if not refs or f.get('answer_status') != 'unresolved':
            return False
        for r in refs:
            b = lookup.get(r.get('block_id'), {})
            a, z = r.get('start'), r.get('end')
            if (b.get('block_id') not in marked or not isinstance(a, int) or not isinstance(z, int)
                    or not 0 <= a < z <= len(b.get('text', ''))):
                return False
        if '\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in refs) != f['observed_answer_text']:
            return False
    for option in f.get('options') or []:
        if option.get('selected') is True or option.get('text_original') or option.get('value'):
            return False
        if not option.get('refs'):
            return False
        for r in option['refs']:
            control = lookup.get(r.get('block_id'), {})
            if (control.get('text') or control.get('structure', {}).get('scan_region_kind') != 'checkbox'
                    or not _box(control) or any(c.get('selected') is True for c in control.get('controls') or [])):
                return False
            # Letter contours can look like empty controls. Only the contour
            # wholly inside already proved printed glyphs is non-business.
            if not any(source_text_inside_cell(control, lookup[bid]) for bid in marked):
                return False
    return True


def _closed(b, *, check_box=True):
    s = b.get('structure', {})
    p = s.get('closed_region_evidence') or {}
    edges = p.get('edge_ink_coverage') or []
    polygon = p.get('polygon') or []
    number = lambda x: type(x) in (int, float) and math.isfinite(x)
    if not (s.get('scan_region_kind') == 'text_box' and s.get('source_method') == 'raster_geometry'
            and p.get('method') == 'observed_four_sided_polygon_v1' and _box(b)
            and len(polygon) == 4 and all(isinstance(v, (list, tuple)) and len(v) == 2
                and all(number(x) for x in v) for v in polygon)
            and len(edges) == 4 and all(number(x) and .8 <= x <= 1 for x in edges)):
        return False
    measured = [min(v[0] for v in polygon), min(v[1] for v in polygon),
                max(v[0] for v in polygon), max(v[1] for v in polygon)]
    if check_box and any(abs(a-z) > 3 for a, z in zip(_box(b), measured)):
        return False
    turns = []
    for i in range(4):
        a, z, c = polygon[i], polygon[(i+1) % 4], polygon[(i+2) % 4]
        turns.append((z[0]-a[0])*(c[1]-z[1])-(z[1]-a[1])*(c[0]-z[0]))
    return all(t > 0 for t in turns) or all(t < 0 for t in turns)


def _contents(cell, texts):
    return sorted([b for b in texts if source_text_inside_cell(b, cell, label=True)],
                  key=lambda b: (_box(b)[1], _box(b)[0]))


def _above(upper, lower):
    a, b = _box(upper), _box(lower)
    return (abs(a[0]-b[0]) < 4 and abs(a[2]-b[2]) < 4 and
            a[1] < b[1] and abs(a[3]-b[1]) < 9)


def _title_bands(pages, cells):
    bands = []
    for page, texts in pages.items():
        local = [c for c in cells if _page(c) == page]
        height = max((_box(b)[3] for b in texts), default=0)
        for title in texts:
            if not (re.search(r'questionnaire|问卷|問卷', title['text'], re.I) and
                    len(_norm(title)) >= 20 and not re.search(r'[?？]', title['text']) and
                    _box(title)[3] < height*.23):
                continue
            owners = [c for c in local if source_text_inside_cell(title, c, label=True)]
            if not owners:
                continue
            right = min(owners, key=lambda c: (_box(c)[2]-_box(c)[0])*(_box(c)[3]-_box(c)[1]))
            lefts = [c for c in local if c is not right and source_cells_share_row(c, right)]
            for left in lefts:
                top_left = [c for c in local if _above(c, left)]
                top_right = [c for c in local if _above(c, right)]
                for a in top_left:
                    for b in top_right:
                        if not source_cells_share_row(a, b):
                            continue
                        group = [a, b, left, right]
                        contents = [_contents(c, texts) for c in group]
                        # Every cell is occupied fixed print; a missing logo,
                        # title label, organization, or title is no evidence.
                        if any(not part for part in contents):
                            continue
                        if any(re.search(r'[_□☐☑☒]|\.{3,}|[?？]', x['text'])
                               for part in contents for x in part):
                            continue
                        signature = tuple(''.join(_norm(x) for x in part) for part in contents[:3]) + (_norm(title),)
                        bands.append({'page': page, 'title': title, 'cells': group,
                                      'contents': contents, 'signature': signature})
    groups = defaultdict(list)
    for band in bands:
        groups[band['signature']].append(band)
    return [b for group in groups.values() if len({x['page'] for x in group}) >= 3 for b in group]


def _band_marks(bands, pages):
    marked = {}
    for band in bands:
        peers = [x for x in bands if x['signature'] == band['signature']]
        mismatches = []
        for c in band['cells']:
            if _closed(c):
                continue
            poly = c['structure']['closed_region_evidence']['polygon']
            measured = [min(p[0] for p in poly), min(p[1] for p in poly),
                        max(p[0] for p in poly), max(p[1] for p in poly)]
            mismatches.append({'cell_id': c['block_id'], 'bbox': deepcopy(_box(c)),
                'polygon_bounds': measured, 'maximum_difference_points': max(abs(x-y) for x,y in zip(_box(c),measured))})
        evidence = {'kind': 'repeated_occupied_document_title_band',
            'cell_ids': [c['block_id'] for c in band['cells']],
            'peer_title_ids': [p['title']['block_id'] for p in peers], 'geometry_mismatches': mismatches}
        for item in [x for part in band['contents'] for x in part]:
            marked[item['block_id']] = ('repeated_header_footer', deepcopy(evidence))
        for b in pages[band['page']]:
            if not (_box(b)[3] <= min(_box(c)[1] for c in band['cells'])+4 and
                    len(_norm(b)) <= 40 and re.fullmatch(r'[\w./–—-]+', _norm(b)) and
                    any(ch.isdigit() for ch in b['text'])):
                continue
            bx, by = _center(b); tx, ty = _center(band['title'])
            matches = []
            for peer in peers:
                px, py = _center(peer['title'])
                for other in pages[peer['page']]:
                    ox, oy = _center(other)
                    if (_norm(other) == _norm(b) and abs((bx-tx)-(ox-px)) <= 14 and
                            abs((by-ty)-(oy-py)) <= 14):
                        matches.append(other)
            if len({_page(x) for x in matches}) >= 3:
                marked[b['block_id']] = ('repeated_header_footer', {
                    'kind': 'identifier_above_repeated_document_title_band',
                    'anchor_title_id': band['title']['block_id'],
                    'peer_block_ids': [x['block_id'] for x in matches], 'geometry_mismatches': deepcopy(mismatches)})
    return marked


def _whole_banner_marks(bands,pages,cells,marked):
    """Use a separate outer contour if one inner header cell is malformed.

    A questionable inner bbox is never relaxed. Each printed source fragment
    must also match >=3 independently valid bands at the same relative source
    position; a closed outer banner must contain ALL its occupied fragments.
    Repeated business captions below a banner are outside this proof.
    """
    found={}
    for p,texts in pages.items():
        for title in texts:
            peers=[b for b in bands if _norm(b['title'])==_norm(title)]
            if len({b['page'] for b in peers})<3:continue
            tx,ty=_center(title)
            matches={}
            for text in texts:
                x,y=_center(text);support=[]
                for peer in peers:
                    px,py=_center(peer['title'])
                    for candidate in pages[peer['page']]:
                        if candidate['block_id'] not in marked or _norm(candidate)!=_norm(text):continue
                        cx,cy=_center(candidate)
                        if abs((x-tx)-(cx-px))<=14 and abs((y-ty)-(cy-py))<=14:support.append(candidate)
                if len({_page(b) for b in support})>=3:matches[text['block_id']]=support
            outers=[]
            for cell in cells:
                cb=_box(cell)
                if (_page(cell)!=p or cb[2]-cb[0]<300 or not 40<cb[3]-cb[1]<120
                    or not source_text_inside_cell(title,cell,label=True)):continue
                content=_contents(cell,texts)
                if len(content)<4 or any(b['block_id'] not in matches for b in content):continue
                outers.append((cell,content))
            if len(outers)!=1:continue
            outer,content=outers[0]
            selected=list(content)
            # Exact repeated fixed codes above the independently verified
            # banner retain the same relative title coordinates as peers.
            selected.extend(b for b in texts if b['block_id'] in matches and _box(b)[3]<_box(outer)[1]+4
                and len(_norm(b))<=40 and re.fullmatch(r'[\w./–—-]+',_norm(b)) and any(c.isdigit() for c in b['text']))
            for b in selected:
                found[b['block_id']]=('repeated_header_footer',{
                    'kind':'repeated_print_with_closed_outer_banner','outer_cell_id':outer['block_id'],
                    'anchor_title_id':title['block_id'],'peer_block_ids':[v['block_id'] for v in matches[b['block_id']]],
                    'occupied_source_refs':[ref(t) for t in content]})
    return found


def _matrix_superheaders(fields, cells, texts, lookup):
    groups = defaultdict(list)
    for f in _walk(fields):
        if f.get('pairing_validation') != 'valid' or f.get('validation_status') != 'valid':
            continue
        if len(f.get('options') or []) < 2 or not f.get('label_refs'):
            continue
        headers = [lookup.get(r.get('block_id'), {}) for r in f.get('scan_column_header_refs') or []]
        if not headers:
            headers = [lookup.get(lookup.get(r.get('block_id'), {}).get('structure', {}).get(
                'scan_column_header_ref', {}).get('block_id'), {}) for r in f.get('answer_region_refs') or []]
        if len(headers) != len(f['options']) or not all(_closed(c) for c in headers):
            continue
        headers.sort(key=lambda c: _box(c)[0])
        if len({c['block_id'] for c in headers}) != len(headers) or len({_page(c) for c in headers}) != 1 or any(not source_cells_share_row(a, b) for a, b in zip(headers, headers[1:])):
            continue
        if any(not _contents(c, texts) for c in headers):
            continue
        regions = [lookup.get(r.get('block_id'), {}) for r in f.get('answer_region_refs') or []]
        if not regions or any(not _box(r) or _page(r) != _page(headers[0]) or
                _box(r)[1] < max(_box(c)[1] for c in headers) for r in regions):
            continue
        key = lambda r: (r.get('block_id'), r.get('start'), r.get('end'), r.get('slot_id') or '')
        owned = {key(r) for r in f.get('answer_region_refs') or []}
        if len(owned) != len(regions):
            continue
        checked, used_headers = set(), set()
        for option in f['options']:
            target_refs = option.get('answer_region_refs') or []
            if len(target_refs) != 1 or key(target_refs[0]) not in owned or key(target_refs[0]) in checked:
                break
            target = lookup.get(target_refs[0].get('block_id'), {})
            tb = _box(target)
            corresponding = [h for h in headers if tb and _box(h)[0]-3 <= tb[0] and tb[2] <= _box(h)[2]+3]
            if len(corresponding) != 1 or corresponding[0]['block_id'] in used_headers:
                break
            header = corresponding[0]
            caption_refs = option.get('refs') or []
            if not caption_refs:
                break
            valid_captions = True
            for r in caption_refs:
                caption = lookup.get(r.get('block_id'), {})
                a, z = r.get('start'), r.get('end')
                if not (isinstance(a, int) and isinstance(z, int) and
                        0 <= a < z <= len(caption.get('text', '')) and _box(caption) and
                        source_text_inside_cell(caption, header, label=True)):
                    valid_captions = False
                    break
            if not valid_captions:
                break
            checked.add(key(target_refs[0]));used_headers.add(header['block_id'])
        if checked != owned or len(used_headers) != len(headers):
            continue
        groups[tuple(c['block_id'] for c in headers)].append(f)
    proofs = []
    for ids, owners in groups.items():
        if len({f['field_id'] for f in owners}) < 2:
            continue
        headers = [lookup[x] for x in ids]
        x0, x1 = _box(headers[0])[0], _box(headers[-1])[2]
        candidates = [c for c in cells if _page(c) == _page(headers[0]) and
                      abs(_box(c)[0]-x0) < 4 and abs(_box(c)[2]-x1) < 4 and
                      _box(c)[1] < min(_box(h)[1] for h in headers) and
                      all(abs(_box(c)[3]-_box(h)[1]) < 9 for h in headers) and
                      _box(c)[3]-_box(c)[1] < 40]
        if len(candidates) != 1:
            continue
        cell = candidates[0]
        values = _contents(cell, texts)
        if not values:
            continue
        proofs.append((values, {'kind': 'closed_superheader_above_shared_matrix_columns',
            'parent_cell_id': cell['block_id'], 'column_cell_ids': list(ids),
            'owner_field_ids': [f['field_id'] for f in owners]}))
    return proofs


def reconcile_pdf_furniture_ownership(fields, lookup):
    """Return audit and non-QA source rows; mutate only proved derived fields."""
    report = {'version': VERSION, 'removed_fields': [], 'unresolved_fields': [], 'non_qa_items': []}
    blocks = [b for b in lookup.values() if b.get('location', {}).get('type') == 'pdf' and _box(b)]
    texts = [b for b in blocks if b.get('text', '').strip() and not b.get('structure', {}).get('scan_region_kind')]
    pages = defaultdict(list)
    for b in texts:
        pages[_page(b)].append(b)
    cells = [b for b in blocks if _closed(b)]
    bands=_title_bands(pages,cells)
    marked = _band_marks(bands, pages)
    marked.update(_whole_banner_marks(bands,pages,cells,marked))
    # This second pass is suspicion only. The sole relaxed check is bbox vs
    # the measured polygon. Method, convex edges, print content and repeated
    # local layout still apply. Such a source cannot authorize a deletion.
    candidates = _band_marks(_title_bands(pages, [b for b in blocks if _closed(b, check_box=False)]), pages)
    uncertain = {bid: proof for bid, proof in candidates.items() if bid not in marked and proof[1]['geometry_mismatches']}
    def mark(items, role, evidence):
        for b in items:
            marked[b['block_id']] = (role, deepcopy(evidence))
    # A repeated fixed document code beside explicit page counters is footer
    # print. An ordinary code request or a code followed by a response is not.
    footer_codes = defaultdict(list)
    for page, local in pages.items():
        height = max(_box(b)[3] for b in local)
        counters = [b for b in local if _box(b)[1] > height*.88 and re.fullmatch(
            r'page\s*(?:no\.?\s*[:：]?)?\s*\d+\s*(?:of|/)\s*\d+', b['text'].strip(), re.I)]
        for b in local:
            if not (_box(b)[1] > height*.88 and re.fullmatch(r'[A-Za-z0-9]+(?:[-–_/][A-Za-z0-9]+)+', b['text'].strip())
                    and any(c.isdigit() for c in b['text']) and any(c.isalpha() for c in b['text'])):
                continue
            adjacent = [c for c in counters if abs(_center(c)[1]-_center(b)[1]) < 20 and _box(c)[0] > _box(b)[2]]
            if len(adjacent) == 1:
                footer_codes[_norm(b)].append((b, adjacent[0]))
    for group in footer_codes.values():
        if len({_page(b) for b, _ in group}) < 3:
            continue
        for b, counter in group:
            mark([b], 'repeated_header_footer', {'kind': 'repeated_identifier_beside_page_counter',
                'counter_block_id': counter['block_id'], 'peer_block_ids': [x['block_id'] for x, _ in group]})
    for items, proof in _matrix_superheaders(fields, cells, texts, lookup):
        mark(items, 'matrix_column_header', proof)
    # Corrections, accepted original values, and authorized answers override
    # derived ownership. Do not mutate their source role either.
    protected = set()
    def protect(items, inherited=False):
        for f in items:
            guarded = inherited or _protected(f)
            if guarded or not _retirable(f, {**candidates, **marked}, lookup):
                for refs in ((f.get('label_refs') or f.get('question_refs') or []),
                             f.get('answer_value_refs') or [], f.get('answer_refs') or []):
                    protected.update(r.get('block_id') for r in refs)
            protect(f.get('children') or [], guarded)
    protect(fields)
    for bid in protected:
        marked.pop(bid, None)
        uncertain.pop(bid, None)
    def visit(items, inherited=False):
        retained = []
        for f in items:
            guarded = inherited or _protected(f)
            refs = f.get('label_refs') or f.get('question_refs') or []
            valid = bool(refs) and all(r.get('block_id') in marked and isinstance(r.get('start'), int) and
                isinstance(r.get('end'), int) and 0 <= r['start'] < r['end'] <= len(lookup[r['block_id']]['text']) for r in refs)
            if not guarded and _retirable(f, marked, lookup) and valid:
                record = {'field_id': f['field_id'], 'label_refs': deepcopy(refs),
                    'prior_derived_field':deepcopy(f),
                    'old_answer_region_refs': deepcopy(f.get('answer_region_refs') or []),
                    'old_unvalidated_answer_refs': deepcopy(f.get('unvalidated_answer_refs') or []),
                    'old_observed_answer_text': f.get('observed_answer_text'),
                    'old_options': deepcopy(f.get('options') or []),
                    'reason': 'Source positions prove fixed print or a shared column heading, not an independent response.',
                    'proofs': [marked[r['block_id']][1] for r in refs]}
                report['removed_fields'].append(record)
                for r in refs:
                    lookup[r['block_id']].setdefault('structure', {}).setdefault('suppressed_derived_fields', []).append(deepcopy(record))
                continue
            suspect = bool(refs) and any(r.get('block_id') in uncertain for r in refs)
            evidence = {**marked, **uncertain}
            valid_suspect = suspect and all(r.get('block_id') in evidence and isinstance(r.get('start'), int) and
                isinstance(r.get('end'), int) and 0 <= r['start'] < r['end'] <= len(lookup[r['block_id']]['text']) for r in refs)
            if not guarded and valid_suspect and _retirable(f, {**candidates, **marked}, lookup):
                reason = '疑似重复印刷标题或文号，但闭合轮廓与边界框不一致；保留原文和字段，填写位置须核对后才能自动复用。'
                record = {'field_id': f['field_id'], 'label_refs': deepcopy(refs),
                    'reason': reason, 'proofs': [uncertain[r['block_id']][1] for r in refs if r['block_id'] in uncertain]}
                report['unresolved_fields'].append(record)
                f.update(pairing_validation='needs_review', validation_status='needs_review',
                    review_status='needs_review', auto_fill_eligible=False,
                    auto_fill_eligible_if_condition_met=False, fillable_status='review_required',
                    furniture_ownership_suspicion=deepcopy(record))
                for key in ('validation_reasons', 'review_reasons'):
                    f[key] = list(dict.fromkeys([*(f.get(key) or []), reason]))
                if isinstance(f.get('form_definition'), dict):
                    definition = f['form_definition']
                    definition.update(validation_status='needs_review', auto_fill_eligible=False,
                        auto_fill_eligible_if_condition_met=False, fillable_status='review_required')
                    definition['validation_reasons'] = list(dict.fromkeys([*(definition.get('validation_reasons') or []), reason]))
                for r in refs:
                    if r['block_id'] in uncertain:
                        lookup[r['block_id']].setdefault('structure', {})['furniture_ownership_suspicion'] = deepcopy(record)
            if f.get('children') and not guarded:
                visit(f['children'])
            retained.append(f)
        items[:] = retained
    visit(fields)
    for bid, (role, evidence) in marked.items():
        b = lookup[bid]
        b.setdefault('structure', {}).update(resolved_layout_role=role,
            furniture_ownership_version=VERSION, furniture_ownership_evidence=evidence)
        report['non_qa_items'].append({'role': role, 'refs': [ref(b)],
            'text_original': b['text'], 'reason': evidence['kind']})
    return report
