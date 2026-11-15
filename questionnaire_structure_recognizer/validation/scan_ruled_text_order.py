"""Read source fragments inside one physical cell without rewriting text.

The enclosing ruled cell supplies local skew. This does not join distinct
cells, invent missing words, or infer checked options.
"""
from statistics import median
import re

VERSION = 'scan-ruled-text-v1-local-line-order'
START = re.compile(r'^\s*(?:is|are|has|have|does|do|can)\b', re.I)


def cell_reading_order(blocks, cell):
    if not blocks:
        return []
    box = cell.get('location', {}).get('bbox') or []
    polygon = (cell.get('structure', {}).get('closed_region_evidence') or {}).get('polygon') or []
    slopes = []
    if len(box) == 4 and len(polygon) == 4:
        for a, b in zip(polygon, polygon[1:] + polygon[:1]):
            if abs(b[0] - a[0]) > .7 * (box[2] - box[0]):
                slopes.append((b[1] - a[1]) / (b[0] - a[0]))
    slope = median(slopes) if slopes else 0
    if abs(slope) > .12:
        return sorted(blocks, key=lambda b: (b['location']['bbox'][1], b['location']['bbox'][0]))

    def position(block):
        b = block['location']['bbox']
        return (b[1] + b[3]) / 2 - slope * (b[0] + b[2]) / 2

    lines = []
    for block in sorted(blocks, key=position):
        b = block['location']['bbox']; height = b[3] - b[1]; center = position(block)
        candidates = [line for line in lines
                      if abs(center - median(position(other) for other in line)) <= .45 * min(
                          height, median(other['location']['bbox'][3] - other['location']['bbox'][1] for other in line))]
        if len(candidates) == 1:
            candidates[0].append(block)
        else:
            lines.append([block])
    lines.sort(key=lambda line: median(position(block) for block in line))
    return [block for line in lines for block in sorted(line, key=lambda b: b['location']['bbox'][0])]


def separate_question_prompts(blocks):
    """Do not mistake a wrapped subordinate verb for a second question.

    Only one terminal question in a single closed physical cell can relax the
    old per-line verb guard. Multiple/unterminated prompts remain ambiguous.
    """
    texts = [str(b.get('text') or '').strip() for b in blocks]
    starts = [i for i, text in enumerate(texts) if START.match(text)]
    if len(starts) <= 1:
        return False
    if sum(text.count('?') + text.count('？') for text in texts) != 1:
        return True
    if not texts[-1].endswith(('?', '？')):
        return True
    return any(texts[index - 1].endswith(('?', '？', '.', '。', ':', '：', ';', '；'))
               for index in starts[1:])
