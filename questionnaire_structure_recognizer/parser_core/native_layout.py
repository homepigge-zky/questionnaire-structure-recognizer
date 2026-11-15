"""Locate native paragraphs in an unmodified local rendering.

Rendering must not inject marker text: even tiny runs can change line breaks.
Coordinates are evidence only; write locations remain the original XML IDs.
"""
from collections import defaultdict
from pathlib import Path
import tempfile
import unicodedata


def _key(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value) if not c.isspace())


def _rendered_runs(pdf):
    runs = []
    for page_number, page in enumerate(pdf):
        for block in page.get_text('rawdict')['blocks']:
            chars = []
            for line in block.get('lines', []):
                raw = [c for span in line.get('spans', []) for c in span.get('chars', [])]
                for i, char in enumerate(raw):
                    values = _key(char['c'])
                    for part, value in enumerate(values):
                        start = part == 0 and (i == 0 or not raw[i - 1]['c'].isalnum())
                        end = part == len(values) - 1 and (i == len(raw) - 1 or not raw[i + 1]['c'].isalnum())
                        chars.append((value, char['origin'], char['bbox'], start, end))
            if chars:
                runs.append((page_number, ''.join(c[0] for c in chars), chars))
    return runs


def _occurrences(text, runs):
    needle = _key(text)
    if not needle:
        return []
    found = {}
    for page, rendered, chars in runs:
        offset = 0
        while (index := rendered.find(needle, offset)) >= 0:
            selected = chars[index:index + len(needle)]
            offset = index + 1
            if (needle[0].isalnum() and not selected[0][3]) or (needle[-1].isalnum() and not selected[-1][4]):
                continue
            x, y = selected[0][1]
            end_y = max(c[1][1] for c in selected)
            # A native paragraph cannot jump sideways into another column.
            # Normal wrapping may move its last line back to the same margin.
            occurrence = (page, float(x), float(y), float(end_y))
            found[(page, round(x, 2), round(y, 2), round(end_y, 2))] = occurrence
    return sorted(found.values(), key=lambda c: (c[0], c[2], c[1]))


def unique_cell_sequence(entries, candidates, regions=None):
    """Return only origins uniquely supported by the entire native cell order."""
    active = [(pid, candidates.get(pid, [])) for pid, _ in entries if candidates.get(pid)]
    if not active:
        return {}
    # A unique long paragraph gives the physical column. Short repeated values
    # may not borrow an occurrence from a neighbouring table column.
    anchors = [(i, values[0]) for i, (_, values) in enumerate(active) if len(values) == 1]
    bounded = {}
    for page in {value[0] for _, value in anchors}:
        points = [value for _, value in anchors if value[0] == page]
        rectangles = [r for r in (regions or {}).get(page, [])
                      if all(r[0] - 1 <= v[1] <= r[2] + 1 and r[1] - 1 <= v[2] <= r[3] + 1 for v in points)]
        if rectangles:
            bounded[page] = min(rectangles, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
    filtered = []
    for i, (pid, values) in enumerate(active):
        if anchors and len(values) > 1:
            nearest = min(anchors, key=lambda a: abs(a[0] - i))[1]
            values = [v for v in values if abs(v[1] - nearest[1]) <= 32]
            if nearest[0] in bounded:
                box = bounded[nearest[0]]
                values = [v for v in values if v[0] == nearest[0]
                          and box[0] - 1 <= v[1] <= box[2] + 1 and box[1] - 1 <= v[2] <= box[3] + 1]
        if not values:
            return {}
        filtered.append((pid, values))

    def before(a, b):
        return a[0] < b[0] or (a[0] == b[0] and a[3] < b[2] - 0.1)

    forward = []
    for i, (_, values) in enumerate(filtered):
        forward.append([i == 0 or any(ok and before(previous, value)
            for previous, ok in zip(filtered[i - 1][1], forward[-1])) for value in values])
    if not any(forward[-1]):
        return {}
    backward = [None] * len(filtered)
    for i in range(len(filtered) - 1, -1, -1):
        backward[i] = [i == len(filtered) - 1 or any(ok and before(value, following)
            for following, ok in zip(filtered[i + 1][1], backward[i + 1])) for value in filtered[i][1]]
    result = {}
    for (pid, values), left, right in zip(filtered, forward, backward):
        feasible = [v for v, a, b in zip(values, left, right) if a and b]
        if len(feasible) == 1:
            result[pid] = feasible[0][:3]
    return result


def document_paragraph_positions(document, blocks, wanted):
    from .router import _libreoffice_executable, prepare_lo_profile, _run_lo
    from ..exporter import locate_docx_target, TemplateFillError
    import fitz
    executable = _libreoffice_executable()
    if not executable:
        raise TemplateFillError('并排多字段段落需要本地排版核对；请安装 LibreOffice 或使用标准 Word 导出。')
    cells = []
    for block in blocks.values():
        spans = block.get('structure', {}).get('paragraph_spans', [])
        if not any(p['paragraph_id'] in wanted for p in spans):
            continue
        located = locate_docx_target(document, {'location': block['location'], 'structure': block['structure']})
        if not located or located[1] != 'cell':
            continue
        entries = [(span['paragraph_id'], located[0].paragraphs[span['paragraph_index'] - 1].text)
                   for span in spans if 0 < span['paragraph_index'] <= len(located[0].paragraphs)]
        cells.append(entries)
    with tempfile.TemporaryDirectory(prefix='qsr-native-layout-') as temporary:
        folder = Path(temporary); source = folder / 'unchanged.docx'; document.save(source)
        profile = prepare_lo_profile(executable, folder / 'profile')
        completed = _run_lo([executable, '-env:UserInstallation=' + profile.as_uri(), '--headless',
            '--nologo', '--nodefault', '--norestore', '--convert-to', 'pdf', '--outdir', str(folder), str(source)], 90)
        pdf_path = folder / 'unchanged.pdf'
        if completed.returncode or not pdf_path.is_file():
            raise TemplateFillError('本地原文排版核对未完成；可重试或使用标准 Word 导出。')
        with fitz.open(pdf_path) as pdf:
            runs = _rendered_runs(pdf)
            result, regions = {}, {}
            for entries in cells:
                candidates = {pid: _occurrences(text, runs) for pid, text in entries if _key(text)}
                if any(len(v) > 1 for v in candidates.values()):
                    for page in {v[0][0] for v in candidates.values() if len(v) == 1}:
                        if page not in regions:
                            regions[page] = [r for table in pdf[page].find_tables(strategy='lines_strict').tables
                                             for r in table.cells if r]
                result.update(unique_cell_sequence(entries, candidates, regions))
    return {pid: value for pid, value in result.items() if pid in wanted}
