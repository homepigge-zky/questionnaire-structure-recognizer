"""Restore a response cell's native option controls before prompt splitting.

This is restricted to a distinct question cell and its adjacent response cell
in the same physical row. A colon beside a real checkbox is its caption, not
a writable zero-width text prompt. Explicit underline slots remain separate.
"""
from copy import deepcopy
import re

from .field_boundaries import ref
from .form_completion import new_field

VERSION = 'word-native-response-controls-v1'


def _walk(fields):
    for field in fields:
        yield field
        yield from _walk(field.get('children', []))


def _protected(field):
    return field.get('manual_override') or field.get('answer_origin') in {'manual', 'knowledge', 'reviewed_knowledge'}


def _options(block):
    text = block.get('text', '')
    ps = block.get('structure', {}).get('paragraph_spans', [])
    controls = [c for c in block.get('controls', []) if c.get('type') == 'checkbox' and c.get('source') == 'word_symbol']
    if len(controls) < 2:
        return []
    options = []
    for i, p in enumerate(ps):
        cs = [c for c in controls if c.get('paragraph_index') == p['paragraph_index']]
        if not cs:
            continue
        # Source line breaks bound several choices in one actual paragraph.
        # Plain numeric/word columns without controls cannot use this path.
        lines = list(re.finditer(r'[^\r\n]+', text[p['start']:p['end']]))
        lines = [m for m in lines if m.group().strip()]
        if len(lines) != len(cs):
            return []
        used = set()
        for m in lines:
            # A count-only zip would assign two boxes on the first line to
            # two different captions. Require the actual XML character offset
            # beside this line's text, then bind by native control identity.
            matches = [c for c in cs if c.get('offset_evidence') == 'native_xml_character_order'
                       and isinstance(c.get('text_offset'), int) and m.start() <= c['text_offset'] <= m.end()
                       and (not p['text'][m.start():c['text_offset']].strip()
                            or not p['text'][c['text_offset']:m.end()].strip())]
            if len(matches) != 1 or matches[0]['control_index'] in used: return []
            control = matches[0]; used.add(control['control_index'])
            a, z = p['start'] + m.start(), p['start'] + m.end()
            while a < z and text[a].isspace(): a += 1
            while z > a and text[z - 1].isspace(): z -= 1
            # A source caption broken before its closing parenthesis may
            # continue in the next paragraph, before the printed write-in.
            if len(cs) == 1 and text[a:z].count('(') > text[a:z].count(')') and i + 1 < len(ps):
                following = ps[i + 1]
                if not any(c.get('paragraph_index') == following['paragraph_index'] for c in controls):
                    tail = text[following['start']:following['end']]
                    if re.match(r'[^()\n]{1,40}\)\s*[:：]', tail): z = following['end']
            placeholder = re.search(r'_{2,}|＿{2,}', text[a:z])
            if placeholder: z = a + placeholder.start()
            while z > a and (text[z - 1].isspace() or text[z - 1] in ':：'): z -= 1
            if a >= z or not control.get('paragraph_index') or not control.get('control_index'):
                return []
            identity = f"p{control['paragraph_index']}:i{control['control_index']}"
            target = {**ref(block, a, a, f"{block['block_id']}:control:{identity}"),
                      'control_id': control.get('control_id'), 'control_index': control['control_index'],
                      'control_paragraph_index': control['paragraph_index']}
            options.append({'refs': [ref(block, a, z)], 'text_original': text[a:z],
                            'selected': control.get('selected'), 'selection_source': 'native_control',
                            'answer_region_refs': [target]})
    return options if len(options) == len(controls) else []


def restore_word_control_groups(fields, blocks, lookup):
    for block in blocks:
        s = block.get('structure', {})
        if block.get('kind') != 'table_cell' or block.get('location', {}).get('type') != 'docx': continue
        options = _options(block)
        if not options: continue
        touching = [f for f in _walk(fields) if any(r.get('block_id') == block['block_id'] for r in f.get('label_refs', []) + f.get('answer_region_refs', []))]
        if any(_protected(f) for f in touching): continue
        parents = []
        for field in _walk(fields):
            labels = field.get('label_refs', [])
            if len(labels) != 1 or _protected(field): continue
            lb = lookup.get(labels[0]['block_id'], {}); ls = lb.get('structure', {})
            if (lb.get('kind') == 'table_cell' and ls.get('table_id') == s.get('table_id')
                    and ls.get('row_start') == s.get('row_start') and ls.get('row_end') == s.get('row_end')
                    and ls.get('grid_col_end', -2) + 1 == s.get('grid_col_start')):
                parents.append(field)
        if len(parents) != 1: continue
        parent = parents[0]
        selected = [o for o in options if o['selected'] is True]
        unknown = any(o['selected'] is None for o in options)
        parent.update(options=options, answer_region_refs=[deepcopy(o['answer_region_refs'][0]) for o in options],
                      answer_value_refs=[deepcopy(r) for o in selected for r in o['refs']],
                      answer_refs=[deepcopy(r) for o in selected for r in o['refs']],
                      answer_text_original='\n'.join(o['text_original'] for o in selected),
                      answer_status='unresolved' if unknown else 'filled' if selected else 'blank',
                      boundary_evidence='native_response_control_group', boundary_version=VERSION,
                      pairing_evidence='native_choice_regions', review_reasons=[])
        # Remove derived colon fields, never source blocks or manual edits.
        def retain(items):
            kept = []
            for f in items:
                own = f.get('label_refs', [])
                if f is not parent and own and all(r.get('block_id') == block['block_id'] for r in own): continue
                f['children'] = retain(f.get('children', [])); kept.append(f)
            return kept
        fields[:] = retain(fields)
        children = []
        for slot in s.get('logical_slots', []):
            if slot.get('kind') != 'inline_placeholder' or slot.get('label_start') is None: continue
            a, z = slot['label_start'], slot['label_end']
            related = [o for o in options if o['refs'][0]['start'] <= a < o['refs'][0]['end']]
            if len(related) == 1:
                a, z = related[0]['refs'][0]['start'], related[0]['refs'][0]['end']
            if a >= z: continue
            rr = ref(block, slot['start'], slot['end'], slot['slot_id'])
            child = new_field(block, ref(block, a, z), [rr], parent['field_id'])
            child.update(boundary_evidence='native_control_writein', boundary_version=VERSION,
                         answer_status='blank', section_refs=deepcopy(parent.get('section_refs', [])),
                         section_text_original=parent.get('section_text_original', ''))
            children.append(child)
        parent['children'].extend(children)
        from .word_option_context import attach_writein_context
        attach_writein_context(parent, block)
        s['protected_label_ranges'] = [{'start': r['start'], 'end': r['end']} for o in options for r in o['refs']]
        s['protected_label_ranges'].extend({'start': r['start'], 'end': r['end']} for c in children for r in c['label_refs'])
