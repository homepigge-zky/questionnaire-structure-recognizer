"""Corroborate repeated native choice groups against an unchanged rendering.

No marker text is injected. PDF coordinates only prove association; captions,
states and writable identities always remain native Word source references.
"""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

from .native_layout import _occurrences, _rendered_runs, _key, unique_cell_sequence

VERSION = 'word-choice-layout-v2-native-floating-anchor'


def compound_candidates(blocks):
    rows = defaultdict(list)
    for b in blocks:
        s = b.get('structure', {})
        if b.get('kind') == 'table_cell' and b.get('location', {}).get('type') == 'docx' and not s.get('is_merge_continuation'):
            rows[(s.get('table_id'), s.get('row_start'), s.get('row_end'))].append(b)
    pairs = []
    for cells in rows.values():
        for label in cells:
            ps = [p for p in label.get('structure', {}).get('paragraph_spans', []) if p.get('text', '').strip()]
            if len(ps) < 3 or ps[0].get('style', {}).get('numbering'): continue
            if not all(p.get('style', {}).get('numbering', {}).get('format') == 'bullet' for p in ps[1:]): continue
            targets = [b for b in cells if b['structure'].get('grid_col_start') == label['structure'].get('grid_col_end', -2) + 1
                       and len(b.get('controls', [])) >= 2 * (len(ps) - 1)
                       and all(c.get('type') == 'checkbox' for c in b.get('controls', []))]
            if len(targets) == 1: pairs.append((label, targets[0], ps))
    return pairs


def native_options(block):
    from ..validation.native_choices import _control_options
    options = []
    for p in block.get('structure', {}).get('paragraph_spans', []):
        cs = [c for c in block.get('controls', []) if c.get('paragraph_index') == p['paragraph_index']]
        if not cs: continue
        current, issue = _control_options(block, p['start'], p['end'], cs)
        if issue: return []
        options.extend(current)
    labels = [o['text_original'].lower().replace('/', '').replace(' ', '') for o in options]
    # Only complete, repeated binary/ternary groups qualify, not a general
    # multi-select list or an arbitrary matching count.
    unit = ['yes', 'no', 'na'] if labels[:3] == ['yes', 'no', 'na'] else ['yes', 'no']
    if len(labels) <= len(unit) or labels != unit * (len(labels) // len(unit)): return []
    return options


def floating_candidates(path, blocks):
    from docx import Document
    from docx.oxml.ns import qn
    from ..validation.native_choices import PLAIN_CHOICES
    document = Document(path)
    paragraph_indices = {p._p: i for i, p in enumerate(document.paragraphs, 1)}
    paragraphs = {b['location']['paragraph']: b for b in blocks if b.get('kind') == 'paragraph'
                  and b.get('location', {}).get('part') == 'word/document.xml' and 'paragraph' in b['location']}
    candidates = []
    for b in blocks:
        loc = b.get('location', {}); s = b.get('structure', {})
        n = loc.get('table')
        if not isinstance(n, int) or loc.get('part') != 'word/document.xml' or s.get('table_row_count') != 1 or s.get('table_column_count') != 1: continue
        if not b.get('controls') or not all(c.get('type') == 'checkbox' for c in b['controls']) or not PLAIN_CHOICES.fullmatch(b.get('text', '')): continue
        table = document.tables[n - 1]._tbl
        floating = table.tblPr.find(qn('w:tblpPr'))
        if floating is None or floating.get(qn('w:vertAnchor')) != 'text': continue
        offset = floating.get(qn('w:tblpY'))
        if offset is None or not re.fullmatch(r'-?\d+', offset): continue
        label = paragraphs.get(paragraph_indices.get(table.getnext()))
        if not label or not re.search(r'\?|？|\b(?:do|does|is|are|have|has|can)\b|是否', label.get('text', ''), re.I): continue
        candidates.append((label, b, int(offset) / 20))
    return candidates


def associate_floating(label, response, vertical_offset, runs):
    from ..validation.native_choices import _control_options
    anchors = _occurrences(label.get('text', ''), runs)
    if len(anchors) != 1: return None
    page, x, y, _ = anchors[0]
    options, issue = _control_options(response, 0, len(response['text']), response['controls'])
    if issue: return None
    matches = [p for p in _occurrences(response['text'], runs)
               if p[0] == page and p[1] > x + 60 and abs(p[2] - y - vertical_offset) <= 3]
    if len(matches) != 1: return None
    return {'label_block': label['block_id'], 'answer_block': response['block_id'], 'options': options,
            'page': page + 1, 'baseline': matches[0][2], 'anchor_baseline': y,
            'native_vertical_offset': vertical_offset, 'evidence': 'native_floating_text_anchor_and_unchanged_render', 'version': VERSION}


def associate_compound(label, response, paragraphs, pdf, runs):
    options = native_options(response)
    if not options: return []
    entries = [(p['paragraph_id'], p['text']) for p in paragraphs]
    candidates = {pid: _occurrences(text, runs) for pid, text in entries}
    anchor = candidates.get(entries[0][0], [])
    if len(anchor) != 1: return []
    page, x, y, last_y = anchor[0]
    # Same actual label cell and native paragraph order; repeated Autoclaves
    # elsewhere in the document cannot supply this group's coordinates.
    for p in paragraphs[1:]:
        candidates[p['paragraph_id']] = [v for v in candidates[p['paragraph_id']]
            if v[0] == page and x - 3 <= v[1] <= x + 35 and last_y < v[2] < last_y + 180]
    positions = unique_cell_sequence(entries, candidates)
    if len(positions) != len(paragraphs): return []
    bullets = paragraphs[1:]
    a = label['structure'].get('grid_x_twips', [])
    z = response['structure'].get('grid_x_twips', [])
    if len(a) != 2 or len(z) != 2 or abs(a[1] - z[0]) > 2: return []
    left = x + (z[0] - a[0]) / 20 - 12
    right = x + (z[1] - a[0]) / 20 + 12
    min_y = min(positions[p['paragraph_id']][2] for p in bullets)
    max_y = max(positions[p['paragraph_id']][2] for p in bullets)
    # Word carries the actual captions as text. Restrict to the measured
    # response column, retain character origins, and require the complete
    # source sequence, including groups that wrap inside a single paragraph.
    chars = []
    for block in pdf[page].get_text('rawdict')['blocks']:
        for line in block.get('lines', []):
            for span in line.get('spans', []):
                for c in span.get('chars', []):
                    xx, yy = c['origin']
                    if left <= xx <= right and min_y - 3 <= yy <= max_y + 3:
                        for ch in _key(c['c']):
                            if ch.isalpha() or ch == '/': chars.append((ch.lower(), float(xx), float(yy)))
    chars.sort(key=lambda c: (round(c[2], 1), c[1]))
    expected = ''.join(_key(o['text_original']).lower() for o in options)
    if ''.join(c[0] for c in chars) != expected: return []
    by_bullet = {p['paragraph_id']: [] for p in bullets}
    offset = 0
    for option in options:
        count = len(_key(option['text_original']))
        occupied = chars[offset:offset + count]; offset += count
        baselines = {round(c[2], 1) for c in occupied}
        if len(baselines) != 1: return []
        yy = occupied[0][2]
        matches = [p for p in bullets if abs(positions[p['paragraph_id']][2] - yy) <= 3]
        if len(matches) != 1: return []
        by_bullet[matches[0]['paragraph_id']].append(option)
    result = []
    for p in bullets:
        chosen = by_bullet[p['paragraph_id']]
        names = [o['text_original'].lower().replace('/', '').replace(' ', '') for o in chosen]
        if names not in (['yes', 'no'], ['yes', 'no', 'na']): return []
        result.append({'label_block': label['block_id'], 'label_start': p['start'], 'label_end': p['end'],
            'context_start': paragraphs[0]['start'], 'context_end': paragraphs[0]['end'],
            'answer_block': response['block_id'], 'options': chosen,
            'page': page + 1, 'baseline': positions[p['paragraph_id']][2],
            'evidence': 'unchanged_word_render_native_bullet_choice_baseline', 'version': VERSION})
    return result


def render_original(path, cache_dir):
    from .router import _libreoffice_executable, prepare_lo_profile, _run_lo
    folder = Path(cache_dir); folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    pdf = folder / (digest + '-original.pdf')
    if not pdf.exists():
        executable = _libreoffice_executable()
        if not executable: raise RuntimeError('Local LibreOffice is unavailable')
        with tempfile.TemporaryDirectory(prefix='qsr-native-choice-') as temp:
            temp = Path(temp); original = temp / (digest + '-original.docx')
            shutil.copyfile(path, original)
            profile = prepare_lo_profile(executable, temp / 'profile')
            completed = _run_lo([executable, '-env:UserInstallation=' + profile.as_uri(), '--headless',
                '--nologo', '--nodefault', '--norestore', '--convert-to', 'pdf', '--outdir', str(folder.resolve()), str(original)], 180)
            if completed.returncode or not pdf.exists(): raise RuntimeError('Original Word rendering failed')
    return pdf


def align_word_choice_groups(path, native, cache_dir):
    import fitz
    pairs = compound_candidates(native['source_blocks'])
    floating = floating_candidates(path, native['source_blocks'])
    if not pairs and not floating: return {'status': 'not_needed', 'resolved': 0, 'version': VERSION}
    # A failed local converter must leave an explicit unresolved compound,
    # never silently retain one parent with several indistinguishable answers.
    for label, response, ps in pairs:
        response['structure']['compound_choice_candidate_label'] = label['block_id']
        response['structure']['rendered_compound_choices'] = []
    cache = Path(cache_dir); cache.mkdir(parents=True, exist_ok=True)
    template_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    key = hashlib.sha256((native['source_hash'] + template_hash + native['parser_version'] + VERSION).encode()).hexdigest()
    target = cache / (key + '-choice-groups.json')
    hit = target.exists()
    if hit:
        cached = json.loads(target.read_text(encoding='utf8')); mappings = cached['compound']; floats = cached['floating']
    else:
        with fitz.open(render_original(path, cache)) as pdf:
            runs = _rendered_runs(pdf)
            mappings = [m for label, response, ps in pairs for m in associate_compound(label, response, ps, pdf, runs)]
            floats = [m for label, response, dy in floating if (m := associate_floating(label, response, dy, runs))]
        target.write_text(json.dumps({'compound': mappings, 'floating': floats}, ensure_ascii=False, indent=2), encoding='utf8')
    lookup = {b['block_id']: b for b in native['source_blocks']}
    for label, response, ps in pairs:
        found = [m for m in mappings if m['label_block'] == label['block_id'] and m['answer_block'] == response['block_id']]
        response['structure']['rendered_compound_choices'] = found
        response['structure']['compound_choice_candidate_label'] = label['block_id']
    for m in floats:
        lookup[m['answer_block']]['structure']['rendered_floating_choices'] = m
    return {'status': 'complete', 'resolved': len(mappings), 'floating_resolved': len(floats), 'cache_hit': hit, 'version': VERSION}
