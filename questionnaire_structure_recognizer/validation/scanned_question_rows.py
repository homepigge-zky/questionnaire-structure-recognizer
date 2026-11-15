"""Local source geometry and complete question runs bound scanned choices.

The original OCR, captions and tick states are never rewritten. A closed pair
may contain several independent questions, but each must have its own complete
source sentence and one unambiguous, vertically aligned physical choice row.
"""
from copy import deepcopy
import re

from .field_boundaries import ref
from .pdf_source_relations import source_cells_share_row, source_text_inside_cell

VERSION = 'scan-question-rows-v1-source-closed-pair-and-complete-runs'
START = re.compile(r'^\s*(?:\d+(?:\.\d+)*[.)]?\s*)?(?:is|are|was|were|has|have|do|does|did|can|could|will|would|if|what|when|where|which|how|whether)[A-Za-z\s]', re.I)


def _box(b): return b.get('location', {}).get('bbox', [])
def _cy(b):
    bb = _box(b)
    return (bb[1] + bb[3]) / 2


def _closed(b):
    s = b.get('structure', {}); e = s.get('closed_region_evidence') or {}
    return (s.get('scan_region_kind') == 'text_box' and s.get('source_method') == 'raster_geometry'
            and len(_box(b)) == 4 and e.get('method') == 'observed_four_sided_polygon_v1'
            and len(e.get('polygon', [])) == 4 and len(e.get('edge_ink_coverage', [])) == 4
            and min(e['edge_ink_coverage']) >= .9)


def source_question_runs(left, blocks):
    local = sorted([b for b in blocks if b.get('text', '').strip()
                    and b.get('location', {}).get('extraction') == 'ocr'
                    and source_text_inside_cell(b, left, label=True)],
                   key=lambda b: (_box(b)[1], _box(b)[0]))
    runs = []; complete = False
    for b in local:
        value = b['text'].strip()
        if re.fullmatch(r'\d+(?:\.\d+)*[.)]?', value): continue
        if re.match(r'^(?:SECTION\s*[IVX\d]+|第.{1,8}部分)', value, re.I): return []
        if START.match(value):
            if runs and not complete: return []
            runs.append([b]); complete = bool(re.search(r'[?？]\s*$', value))
        elif runs and re.match(r'^[\u3400-\u9fff]', value):
            # A source-language counterpart follows its English question. It
            # cannot be used as a second answerable field or an answer value.
            if not complete: return []
            runs[-1].append(b)
        elif runs and not complete and re.match(r'^[a-z(]', value):
            runs[-1].append(b); complete = bool(re.search(r'[?？]\s*$', value))
        else:
            return []
    return runs if runs and complete else []


def _verified_label_band(left, right, lookup):
    """Recheck the existing source-derived band; it never becomes writable."""
    s = left.get('structure', {}); refs = s.get('grid_evidence_refs') or []
    if (s.get('source_method') != 'evidenced_neighbor_label_band' or not s.get('label_only_region')
            or len(refs) != 5 or refs[0].get('block_id') != right['block_id']): return False
    evidence = [lookup.get(r.get('block_id'), {}) for r in refs]
    _, peer, peer_right, top, bottom = evidence
    if not all(_closed(b) for b in (right, peer, peer_right)) or not source_cells_share_row(peer, peer_right): return False
    page = right['location']['page']
    if any(b.get('location', {}).get('page') != page for b in evidence + [left]): return False
    pb, pr, rb = _box(peer), _box(peer_right), _box(right)
    band = [pb[0] + rb[0] - pr[0], rb[1] + pb[1] - pr[1], rb[0], rb[3] + pb[3] - pr[3]]
    if any(abs(a - b) > .001 for a, b in zip(band, _box(left))): return False
    for line, edge in [(top, band[1]), (bottom, band[3])]:
        bb = _box(line); ls = line.get('structure', {})
        if (len(bb) != 4 or ls.get('scan_region_kind') != 'write_line' or ls.get('source_method') != 'raster_geometry'
                or ls.get('line_style') != 'solid' or abs(bb[3] - edge) >= 7
                or max(0, min(band[2], bb[2]) - max(band[0], bb[0])) <= (band[2] - band[0]) * .3): return False
    return True


def source_choice_partition(mark_refs, blocks, lookup):
    """Return a source-bound partition, or None; never choose nearest prose."""
    marks = [lookup.get(r.get('block_id'), {}) for r in mark_refs]
    if len(marks) not in (2, 3) or len({m.get('block_id') for m in marks}) != len(marks): return None
    if any(m.get('structure', {}).get('scan_region_kind') != 'checkbox' for m in marks): return None
    cells = [b for b in blocks if _closed(b)]
    rights = [b for b in cells if all(source_text_inside_cell(m, b) for m in marks)]
    if len(rights) != 1: return None
    right = rights[0]
    lefts = [b for b in blocks if b is not right and source_cells_share_row(b, right)
             and (_closed(b) or _verified_label_band(b, right, lookup))]
    if len(lefts) != 1: return None
    left = lefts[0]; runs = source_question_runs(left, blocks)
    if not runs: return None
    all_marks = [b for b in blocks if b.get('structure', {}).get('scan_region_kind') == 'checkbox'
                 and source_text_inside_cell(b, right)
                 and b.get('structure', {}).get('resolved_layout_role') not in {'option_caption_glyph', 'page_counter_glyph'}]
    groups = []
    for m in sorted(all_marks, key=lambda b: (_cy(b), _box(b)[0])):
        group = next((g for g in groups if abs(_cy(m) - _cy(g[0])) < 5), None)
        if group is None: groups.append([m])
        else: group.append(m)
    if len(groups) != len(runs) or any(len(g) not in (2, 3) for g in groups): return None
    matches = []
    for run in runs:
        top, bottom = min(_box(b)[1] for b in run), max(_box(b)[3] for b in run)
        aligned = [g for g in groups if all(top - 2 <= _cy(m) <= bottom + 2 for m in g)]
        if len(aligned) != 1: return None
        matches.append(aligned[0])
    if len({id(g) for g in matches}) != len(runs): return None
    ids = {m['block_id'] for m in marks}
    paired = [(run, g) for run, g in zip(runs, matches) if {m['block_id'] for m in g} == ids]
    if len(paired) != 1: return None
    run, group = paired[0]
    return {'version': VERSION, 'label_cell_ref': ref(left), 'response_cell_ref': ref(right),
            'label_refs': [ref(b) for b in run], 'answer_region_refs': deepcopy(mark_refs),
            'neighbor_question_refs': [[ref(b) for b in r] for r in runs if r is not run],
            'method': 'complete_source_questions_with_distinct_aligned_physical_choice_rows'}


def cross_question_boundary_reason(field, lookup):
    marks = [r for o in field.get('options', []) for r in o.get('answer_region_refs', [])]
    proof = source_choice_partition(marks, list(lookup.values()), lookup)
    if not proof: return None
    labels = field.get('label_refs') or field.get('question_refs') or []
    expected = {(r['block_id'], r['start'], r['end']) for r in proof['label_refs']}
    actual = [(r.get('block_id'), r.get('start'), r.get('end')) for r in labels]
    if any(r not in expected for r in actual) or len(actual) != len(set(actual)):
        return '题目引用跨越实体答复组对应的独立题干或重复引用；必须按原页局部结构重新配对。'
    return None
