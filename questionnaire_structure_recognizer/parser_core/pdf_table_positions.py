"""Observe native PDF table blank areas and prefix checkboxes, without OCR."""
from copy import deepcopy
import hashlib
import re

VERSION = 'native-table-position-observations-v1'


def _rectangles(drawings):
    for i, path in enumerate(drawings):
        items = path.get('items', [])
        if len(items) == 1 and items[0][0] == 're': yield i, path, list(items[0][1])


def _cell_edges(cell, drawings):
    bb = cell['location']['bbox']; evidence = []
    for orientation, at, start, end in [('h', bb[1], bb[0], bb[2]), ('h', bb[3], bb[0], bb[2]),
                                       ('v', bb[0], bb[1], bb[3]), ('v', bb[2], bb[1], bb[3])]:
        spans = []
        for i, path, box in _rectangles(drawings):
            if path.get('fill') is None or min(path['fill']) > .2 or path.get('fill_opacity', 1) < .99: continue
            thickness = box[3] - box[1] if orientation == 'h' else box[2] - box[0]
            middle = (box[1] + box[3]) / 2 if orientation == 'h' else (box[0] + box[2]) / 2
            a, z = (box[0], box[2]) if orientation == 'h' else (box[1], box[3])
            if 0 < thickness <= 1.5 and abs(middle - at) <= 1 and z >= start and a <= end:
                spans.append((max(start, a), min(end, z), i))
        cursor = start; used = []
        for a, z, i in sorted(spans):
            if a > cursor + 1: break
            if z > cursor: cursor = z; used.append(i)
        if cursor < end - 1: return None
        evidence.append({'orientation': orientation, 'at': at, 'span': [start, end], 'drawing_indices': used})
    return evidence


def _blank(page, bbox):
    import fitz
    if bbox[2] - bbox[0] < 12 or bbox[3] - bbox[1] < 5: return None
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=fitz.Rect(bbox), colorspace=fitz.csGRAY, alpha=False)
    values = pix.samples
    return {'bbox': bbox, 'method': 'local_native_pdf_render_no_ocr', 'pixel_sha256': hashlib.sha256(values).hexdigest(),
            'width': pix.width, 'height': pix.height, 'blank': bool(values) and min(values) >= 248,
            'nonwhite_pixels': sum(x < 248 for x in values)}


def observe_table_positions(page, blocks, source_hash, page_number):
    from .pdf_vector_forms import _native_runs, _state, _visible, _inside
    from .models import make_block
    drawings = page.get_drawings(); chars = _native_runs(page, blocks); controls = []
    blank_paint = {t['seqno'] for t in page.get_texttrace() if t.get('opacity', 1) == 1 and t['chars']
                   and all(c[0] >= 0 and chr(c[0]).isspace() for c in t['chars'])}
    cells = [b for b in blocks if b.get('kind') == 'table_cell' and b['location'].get('extraction') == 'native'
             and b.get('structure', {}).get('layout_evidence') == 'visible_vector_borders'
             and not b.get('structure', {}).get('scan_page_context')]
    for cell in cells:
        s = cell['structure']; text = cell.get('text', ''); cb = cell['location']['bbox']
        for key in ('native_table_position_evidence','native_question_blank_tail','native_prefix_choice_ids','native_underlined_blank_gaps','native_unassociated_square_drawings'):
            s.pop(key,None)
        edges = _cell_edges(cell, drawings)
        if not edges or not text.strip(): continue
        base = {'version': VERSION, 'source_hash': source_hash, 'source_text': text,
                'cell_bbox': list(cb), 'page': page_number, 'observed_cell_edges': edges}
        s['native_table_position_evidence'] = deepcopy(base)
        spans = s.get('native_text_spans') or []; runs = s.get('native_pdf_font_runs') or []
        if (spans and all(0 <= x['start'] < x['end'] <= len(text) for x in spans)
                and re.search(r'[?？]|\bplease\s+(?:list|provide|describe|state|specify)\b|请(?:列|提供|描述|说明)', text, re.I)):
            top = max(x['bbox'][3] for x in spans) + 3
            if cb[3] - 3 - top >= 12:
                blank = _blank(page, [cb[0] + 3, top, cb[2] - 3, cb[3] - 3])
                if blank: s['native_question_blank_tail'] = {**deepcopy(base), **blank}
        local_controls = []; square_indices=[]
        for index, path, box in _rectangles(drawings):
            w, h = box[2] - box[0], box[3] - box[1]
            if (not _visible(path) or path.get('fill') is not None or not 4 <= w <= 20 or not 4 <= h <= 20
                    or not .8 <= w / h <= 1.25 or not all(_inside(p, cb) for p in [(box[0], box[1]), (box[2], box[3])])): continue
            square_indices.append(index)
            candidates = [r for r in runs if 0 <= r['bbox'][0] - box[2] <= 8
                          and abs(sum(r['bbox'][1::2]) / 2 - sum(box[1::2]) / 2) < 3
                          and 1 <= r['paint_seqno'] - path['seqno'] <= 3
                          and all(k in blank_paint for k in range(path['seqno'] + 1, r['paint_seqno']))]
            if len(candidates) != 1: continue
            first = candidates[0]; chain = [first]
            for r in runs[runs.index(first) + 1:]:
                old = chain[-1]
                if (abs(r['baseline'] - first['baseline']) >= 1 or not -.3 <= r['bbox'][0] - old['bbox'][2] <= 4
                        or text[old['end']:r['start']].strip() or r['start'] < old['end']
                        or not 1 <= r['paint_seqno'] - old['paint_seqno'] <= 3
                        or not all(k in blank_paint for k in range(old['paint_seqno'] + 1, r['paint_seqno']))): break
                chain.append(r)
            a, z = first['start'], chain[-1]['end']; caption = text[a:z]
            if not caption.strip() or len(caption) > 180: continue
            selected, mark = _state(box, index, drawings, chars)
            refs = [{'block_id': cell['block_id'], 'start': a, 'end': z, 'slot_id': ''}]
            mark.update(version=VERSION, outline_bbox=box, outline_paint_seqno=path['seqno'],
                        caption_paint_seqno=first['paint_seqno'], caption_after_outline=True,
                        intervening_blank_text_paint=list(range(path['seqno'] + 1, first['paint_seqno'])),
                        caption_bbox=[first['bbox'][0], min(r['bbox'][1] for r in chain), chain[-1]['bbox'][2], max(r['bbox'][3] for r in chain)],
                        caption_runs=deepcopy(chain), cell_evidence=deepcopy(base))
            location = {'type': 'pdf', 'page': page_number, 'bbox': box, 'coordinate_system': 'PDF points, top-left origin',
                        'extraction': 'native_vector', 'drawing_index': index, 'paint_seqno': path['seqno']}
            b = make_block('form_field', '', 0, location, structure={'physical_region_kind': 'native_vector_checkbox',
                'blank_state': 'filled' if selected is True else 'blank' if selected is False else 'unreadable',
                'native_vector_caption_refs': refs, 'native_vector_version': VERSION},
                controls=[{'type': 'checkbox', 'source': 'native_pdf_vector', 'control_id': f'pdf-vector:{page_number}:{index}',
                           'selected': selected, 'caption_refs': refs, 'mark_evidence': mark}]).model_dump()
            local_controls.append(b)
        # A solitary outlined decoration is never promoted by this observer.
        if len(local_controls) >= 2:
            controls.extend(local_controls)
            s['native_prefix_choice_ids'] = [b['block_id'] for b in local_controls]
        mapped={b['location']['drawing_index']for b in local_controls}
        s['native_unassociated_square_drawings']=[i for i in square_indices if i not in mapped]
        gaps = []
        for previous, following in zip(runs, runs[1:]):
            a, z = previous['end'], following['start']
            if z <= a or text[a:z].strip() or abs(previous['baseline'] - following['baseline']) >= 1: continue
            pb, nb = previous['bbox'], following['bbox']
            if nb[0] - pb[2] < 18: continue
            lines = [(i, b) for i, path, b in _rectangles(drawings) if path.get('fill') is not None and min(path['fill']) < .2
                     and path.get('fill_opacity', 1) >= .99 and 0 < b[3] - b[1] <= 1
                     and -1 <= b[0] - pb[2] <= pb[3]-pb[1] and abs(b[2] - nb[0]) < 2
                     and -1 <= b[1] - previous['baseline'] <= 2]
            if len(lines) != 1: continue
            index, line = lines[0]; blank = _blank(page, [line[0] + 1, line[1] - 7, line[2] - 1, line[1] - 1])
            if blank and blank['blank']:
                gaps.append({**deepcopy(base), **blank, 'start': a, 'end': z, 'line_bbox': line,
                             'drawing_index': index, 'preceding_run': deepcopy(previous), 'following_run': deepcopy(following)})
        if gaps: s['native_underlined_blank_gaps'] = gaps
    return controls


def verified_prefix_control(block):
    controls = block.get('controls') or []
    if len(controls) != 1: return False
    c = controls[0]; e = c.get('mark_evidence') or {}; base = e.get('cell_evidence') or {}
    runs = e.get('caption_runs') or []; refs = c.get('caption_refs') or []
    if (e.get('version') != VERSION or not e.get('caption_after_outline') or not runs or len(refs) != 1
            or c.get('type')!='checkbox' or c.get('source')!='native_pdf_vector'
            or block.get('structure',{}).get('physical_region_kind')!='native_vector_checkbox'
            or block.get('location',{}).get('extraction')!='native_vector'
            or block.get('location',{}).get('page')!=base.get('page')
            or block.get('structure',{}).get('native_vector_caption_refs')!=refs
            or not isinstance(c.get('selected'), bool) or e.get('unclassified_ink_indices') or e.get('unclassified_text_inside')
            or len(base.get('source_hash', '')) != 64 or len(base.get('observed_cell_edges', [])) != 4): return False
    first = runs[0]; rr = refs[0]; text = base.get('source_text', '')
    if not 0 <= rr.get('start', -1) < rr.get('end', -1) <= len(text): return False
    return (rr['start'] == first['start'] and rr['end'] == runs[-1]['end']
            and e['caption_paint_seqno'] == first['paint_seqno']
            and 1 <= e['caption_paint_seqno'] - e['outline_paint_seqno'] <= 3
            and e.get('intervening_blank_text_paint') == list(range(e['outline_paint_seqno'] + 1, e['caption_paint_seqno']))
            and e.get('outline_bbox') == block.get('location', {}).get('bbox')
            and bool(e.get('positive_diagonal_indices')) == bool(e.get('negative_diagonal_indices'))
            and bool(e.get('positive_diagonal_indices') and e.get('negative_diagonal_indices')) == c['selected'])
