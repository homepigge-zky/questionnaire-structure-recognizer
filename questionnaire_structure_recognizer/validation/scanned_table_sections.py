"""Explicit OCR SECTION captions over connected, physically ruled table rows.

No nearest-paragraph scope and no cross-page propagation. Printed footer text
is retained; only repeatedly located non-business footer roles are corrected.
"""
from copy import deepcopy
from difflib import SequenceMatcher
import math
import re

from .field_boundaries import ref
from .pdf_source_relations import source_cells_share_row, source_text_inside_cell
from .scan_closed_local_grids import shares_horizontal_border, walk
from .pdf_furniture_ownership import _protected

VERSION = 'scanned-table-sections-v1-explicit-caption-connected-grid'
CAPTION = re.compile(r'^\s*SECTION\s*[IVXLCDM\d]+\s*[:：]', re.I)
TRANSLATION = re.compile(r'^\s*第[一二三四五六七八九十百\d]+部分\s*[:：]')


def box(block):
    bounds=block.get('location',{}).get('bbox')
    return bounds if (isinstance(bounds,(list,tuple)) and len(bounds)==4
        and all(type(v) in (int,float) and math.isfinite(v) for v in bounds)
        and bounds[0]<bounds[2] and bounds[1]<bounds[3]) else []


def page(block):
    return block.get('location',{}).get('page')


def _protected_ids(fields):
    ids = set()
    def visit(items, inherited=False):
        for field in items:
            guarded = inherited or _protected(field)
            if guarded:
                ids.update(r['block_id'] for key in ('label_refs', 'answer_region_refs', 'answer_value_refs', 'section_refs')
                           for r in field.get(key, []))
            visit(field.get('children', []), guarded)
    visit(fields)
    return ids


def _row_groups(cells):
    groups = []
    for cell in sorted(cells, key=lambda c: (box(c)[1], box(c)[0])):
        group = next((g for g in groups if any(source_cells_share_row(c, cell) or source_cells_share_row(cell, c) for c in g)), None)
        if group is None:
            groups.append([cell])
        else:
            group.append(cell)
    return groups


def _connected_cells(seeds, local, left, right):
    selected = {c['block_id']: c for c in seeds}
    top = min(box(c)[1] for c in seeds)
    candidates = [c for c in local if left - 4 <= box(c)[0] and box(c)[2] <= right + 4 and box(c)[1] >= top - 3]
    while True:
        added = []
        for cell in candidates:
            if cell['block_id'] in selected:
                continue
            if any(source_cells_share_row(old, cell) or source_cells_share_row(cell, old) or
                   (min(box(old)[2], box(cell)[2]) - max(box(old)[0], box(cell)[0]) > 8 and
                    shares_horizontal_border(old, cell, max(box(old)[0], box(cell)[0]), min(box(old)[2], box(cell)[2])))
                   for old in selected.values()):
                added.append(cell)
        if not added:
            break
        selected.update((c['block_id'], c) for c in added)
    return list(selected.values())


def _rule_groups(rules):
    """Only co-linear observed segments that touch/overlap form one rule."""
    groups = [[r] for r in rules]
    for first in rules:
        for second in rules:
            if first is second or box(first)[0] >= box(second)[0]:
                continue
            if abs(box(first)[3] - box(second)[3]) < 3 and box(second)[0] <= box(first)[2] + 3:
                groups.append([first, second])
    return groups


def discover_sections(fields, blocks):
    from .scan_section_boundaries import _partition_cell
    protected = _protected_ids(fields)
    texts = [b for b in blocks if b.get('location', {}).get('type') == 'pdf'
             and b.get('location', {}).get('extraction') == 'ocr' and len(box(b)) == 4 and b.get('text', '').strip()]
    cells = [b for b in blocks if _partition_cell(b)]
    rules = [b for b in blocks if len(box(b)) == 4 and b.get('structure', {}).get('scan_region_kind') == 'write_line'
             and b['structure'].get('source_method') == 'raster_geometry' and b['structure'].get('line_style') == 'solid']
    headings = []
    for caption in texts:
        if caption['block_id'] in protected or not CAPTION.match(caption['text']) or '?' in caption['text'] or '？' in caption['text']:
            continue
        cb = box(caption)
        local = [c for c in cells if page(c) == page(caption)]
        band = [c for c in local if box(c)[2] - box(c)[0] > 280 and box(c)[3] - box(c)[1] < 50
                and source_text_inside_cell(caption, c, label=True)]
        proof = None
        if len(band) == 1:
            band = band[0]; bb = box(band)
            below = [c for c in local if c is not band and shares_horizontal_border(band, c, max(bb[0], box(c)[0]), min(bb[2], box(c)[2]))]
            if below and abs(min(box(c)[0] for c in below) - bb[0]) < 4 and abs(max(box(c)[2] for c in below) - bb[2]) < 4:
                proof = {'kind': 'explicit_section_closed_band', 'band_ref': ref(band)}
                left, top, right, bottom = bb
                seeds = below
        if proof is None:
            # Raster rule boxes include eight points of space above the line.
            # Use the observed lower edge and the closed next row, not the
            # word's distance to an arbitrary following paragraph.
            candidates = []
            lines = _rule_groups([r for r in rules if page(r) == page(caption) and abs(box(r)[3] - cb[3]) < 45])
            for upper in lines:
                left, right, top = min(box(r)[0] for r in upper), max(box(r)[2] for r in upper), max(box(r)[3] for r in upper)
                if right - left < 280 or not left - 3 <= cb[0] <= left + 30 or cb[2] > right or not -5 <= cb[1] - top < 10:
                    continue
                for lower in lines:
                    bottom = max(box(r)[3] for r in lower)
                    if (any(r in upper for r in lower) or not 10 < bottom - top < 50 or not -4 <= bottom - cb[3] < 25 or
                            abs(min(box(r)[0] for r in lower) - left) >= 4 or abs(max(box(r)[2] for r in lower) - right) >= 4):
                        continue
                    for group in _row_groups([c for c in local if abs(box(c)[1] - bottom) < 5 and left - 4 <= box(c)[0] and box(c)[2] <= right + 4]):
                        component = _connected_cells(group, local, left, right)
                        # A partly recovered first row is usable for chapter
                        # ownership only when its shared borders lead to an
                        # independently divided, full-width source table row.
                        full_rows = [g for g in _row_groups(component) if len(g) >= 2 and
                                     abs(min(box(c)[0] for c in g) - left) < 4 and abs(max(box(c)[2] for c in g) - right) < 4]
                        if full_rows:
                            candidates.append((group, left, right, top, bottom, upper, lower))
            unique = {tuple(c['block_id'] for c in entry[0]): entry for entry in candidates}
            if len(unique) == 1:
                seeds, left, right, top, bottom, upper, lower = next(iter(unique.values()))
                proof = {'kind': 'explicit_section_observed_rules_closed_next_row', 'rule_refs': [ref(r) for r in upper + lower]}
            elif not unique:
                # A printed bilingual caption can precede an undivided long
                # response row. Its lower boundary is that row's verified
                # full-width polygon edge; no empty rectangle is invented.
                candidates = []
                for cell in local:
                    bb = box(cell)
                    if bb[2] - bb[0] < 280 or not 0 <= bb[1] - cb[3] < 20 or not bb[0] - 3 <= cb[0] <= bb[0] + 20:
                        continue
                    subtitles = [t for t in texts if page(t) == page(caption) and TRANSLATION.match(t['text'])
                                 and abs(box(t)[0] - cb[0]) < 12 and cb[3] - 2 <= box(t)[1] < bb[1] and box(t)[3] <= bb[1] + 3]
                    upper = [r for r in rules if page(r) == page(caption) and abs(box(r)[0] - bb[0]) < 4
                             and box(r)[2] - box(r)[0] >= .8 * (bb[2] - bb[0]) and box(r)[2] <= bb[2] + 4
                             and -5 <= cb[1] - box(r)[3] < 10]
                    if len(subtitles) == 1 and upper:
                        candidates.append((cell, upper[0]))
                if len(candidates) != 1:
                    continue
                cell, upper = candidates[0]
                left, bottom, right, _ = box(cell); top = box(upper)[3]; seeds = [cell]
                proof = {'kind': 'explicit_bilingual_section_before_closed_full_row', 'rule_refs': [ref(upper)], 'first_row_ref': ref(cell)}
            else:
                continue
        fragments = [caption] + [t for t in texts if t is not caption and page(t) == page(caption)
                    and t['block_id'] not in protected and TRANSLATION.match(t['text'])
                    and abs(box(t)[0] - cb[0]) < 12 and cb[1] <= box(t)[1] and box(t)[3] <= bottom + 3]
        if len(fragments) > 2:
            continue
        component = _connected_cells(seeds, local, left, right)
        proof.update(version=VERSION, heading_refs=[ref(t) for t in fragments], first_row_refs=[ref(c) for c in seeds],
                     connected_cell_refs=[ref(c) for c in component], page=page(caption))
        headings.append({'fragments': fragments, 'cells': component, 'proof': proof,
                         'left': left, 'right': right,
                         'refs': [ref(t) for t in fragments], 'text': '\n'.join(t['text'] for t in fragments)})
    return headings


def reconcile_scanned_table_sections(fields, blocks, lookup, sections=None, non_qa=None):
    headings = discover_sections(fields, blocks)
    protected = _protected_ids(fields)
    changed = []
    for field in walk(fields):
        labels = [lookup.get(r['block_id'], {}) for r in field.get('label_refs', [])]
        if not labels or any(b.get('block_id') in protected or len(box(b))!=4 for b in labels):
            continue
        def belongs(heading):
            def in_source_row(label):
                if any(source_text_inside_cell(label, cell, label=True) for cell in heading['cells']):
                    return True
                if field.get('pairing_validation') != 'valid' or len(box(label)) != 4:
                    return False
                targets = [lookup.get(r['block_id'], {}) for r in field.get('answer_region_refs', [])]
                for cell in heading['cells']:
                    if not any(len(box(target)) == 4 and source_text_inside_cell(target, cell, label=True) for target in targets if target.get('location')):
                        continue
                    lb, rb = box(label), box(cell)
                    if page(label) == page(cell) and heading['left'] - 3 <= lb[0] < lb[2] <= rb[0] + 3 and rb[1] - 3 <= (lb[1] + lb[3]) / 2 <= rb[3] + 3:
                        return True
                return False
            return all(in_source_row(label) for label in labels)
        candidates = [h for h in headings if belongs(h)]
        if len(candidates) != 1:
            continue
        heading = candidates[0]
        node = {'section_id': 'source-table-section:' + heading['fragments'][0]['block_id'], 'level': 0,
                'refs': deepcopy(heading['refs']), 'text_original': heading['text'], 'source_scope_evidence': deepcopy(heading['proof'])}
        field.update(section_refs=deepcopy(node['refs']), section_text_original=node['text_original'], section_path=[node],
                     section_scope_evidence=deepcopy(heading['proof']))
        for label in labels:
            label.setdefault('structure', {})['section_path_block_ids'] = [[r['block_id'] for r in node['refs']]]
        changed.append(field['field_id'])
    for heading in headings:
        for block in heading['fragments']:
            block.setdefault('structure', {}).update(resolved_layout_role='section_header', scanned_table_section_evidence=deepcopy(heading['proof']))
        if sections is not None and not any(s.get('refs') == heading['refs'] for s in sections):
            sections.append({'section_id': 'source-table-section:' + heading['fragments'][0]['block_id'], 'level': 0,
                             'refs': deepcopy(heading['refs']), 'text_original': heading['text'], 'source_scope_evidence': deepcopy(heading['proof'])})
    # Repeated last-line text outside every connected business grid has no
    # authority to supply sections. Keep its source and readable non-QA role.
    texts = [b for b in blocks if b.get('text', '').strip() and len(box(b)) == 4 and b.get('location', {}).get('type') == 'pdf']
    tails = [b for b in texts if len(b['text']) > 45 and b['block_id'] not in protected and
             box(b)[3] >= max(box(t)[3] for t in texts if page(t) == page(b)) - 3 and
             not any(page(b) == h['proof']['page'] and any(source_text_inside_cell(b, c, label=True) for c in h['cells']) for h in headings)]
    footer_ids = set()
    for block in tails:
        key = re.sub(r'\W+', '', block['text']).casefold()
        peers = [b for b in tails if abs(box(b)[0] - box(block)[0]) < 20 and abs(box(b)[3] - box(block)[3]) < 20
                 and SequenceMatcher(None, key, re.sub(r'\W+', '', b['text']).casefold()).ratio() > .9]
        if len({page(b) for b in peers}) < 3:
            continue
        footer_ids.add(block['block_id'])
        proof = {'version': VERSION, 'kind': 'repeated_last_line_outside_source_table', 'peer_refs': [ref(b) for b in peers]}
        block.setdefault('structure', {}).update(resolved_layout_role='repeated_header_footer', scanned_footer_evidence=proof)
        if non_qa is not None and not any(x.get('role') == 'repeated_header_footer' and x.get('refs') == [ref(block)] for x in non_qa):
            non_qa.append({'role': 'repeated_header_footer', 'refs': [ref(block)], 'text_original': block['text'],
                           'reason': '多页相同底部原文位于实体业务表格之外；保留原文，不作为业务章节。', 'diagnostic': proof})
    if sections is not None:
        sections[:] = [s for s in sections if not s.get('refs') or not all(r['block_id'] in footer_ids for r in s['refs'])]
    for field in walk(fields):
        refs = field.get('section_refs') or []
        if refs and all(r['block_id'] in footer_ids for r in refs) and not _protected(field):
            field['rejected_section_sources'] = {'version': VERSION, 'refs': deepcopy(refs),
                'reason': '重复页脚不能提供业务适用章节；尚未定位实体章节时保留未知范围。'}
            field.update(section_refs=[], section_text_original='', section_path=[])
    return {'version': VERSION, 'source_headings': len(headings), 'assigned_fields': changed, 'footer_ids': sorted(footer_ids)}
