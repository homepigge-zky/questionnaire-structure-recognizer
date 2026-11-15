"""Native same-cell question, square-bullet choices and a separate Ans slot.

Word list markers live in numbering.xml, not in paragraph text. Keep their
physical paragraph identity; never turn the later Ans placeholder into the
entire question or use the option captions as an already supplied answer.
"""
from copy import deepcopy
import re

from .field_boundaries import ref
from .form_completion import new_field

VERSION = 'word-bullet-answer-groups-v3-bounded-embedded-writeins'


def _walk(items):
    for field in items:
        yield field
        yield from _walk(field.get('children', []))


def _protected(field):
    return (field.get('manual_override') or field.get('human_corrected')
            or field.get('review_status') == 'human_corrected'
            or (field.get('form_definition') or {}).get('mapping_review_status') == 'human_corrected'
            or field.get('answer_origin') in {'manual', 'knowledge', 'reviewed_knowledge'})


def _indent(paragraph):
    values = paragraph.get('indent') or {}
    return max([int(v) for k, v in values.items() if k.rsplit('}', 1)[-1] in {'start', 'left'} and str(v).lstrip('-').isdigit()] + [0])


def _layout(block):
    if block.get('kind') != 'table_cell' or block.get('location', {}).get('type') != 'docx' or block.get('controls'):
        return None
    s = block.get('structure', {}); text = block.get('text', '')
    paragraphs = [p for p in s.get('paragraph_spans', []) if p.get('text', '').strip()]
    bullets = [i for i, p in enumerate(paragraphs) if p.get('style', {}).get('numbering', {}).get('format') == 'bullet'
               and p['style']['numbering'].get('marker') in {'□', '☐'}]
    slots = [slot for slot in s.get('logical_slots', []) if slot.get('kind') == 'inline_placeholder'
             and re.fullmatch(r'(?:答|回答|答案)?\s*(?:Ans(?:wer)?\.?)?', text[slot.get('label_start', 0):slot.get('label_end', 0)].strip(), re.I)
             and slot.get('label_end', 0) > slot.get('label_start', 0)]
    if len(bullets) < 2 or len(slots) != 1 or bullets[0] == 0:
        return None
    slot = slots[0]
    if slot['start'] <= paragraphs[bullets[-1]]['end']:
        return None
    answers = [i for i, p in enumerate(paragraphs) if p['paragraph_index'] == slot.get('paragraph_index')]
    if len(answers) != 1 or answers[0] != len(paragraphs) - 1:
        return None
    answer_index = answers[0]
    stem = paragraphs[:bullets[0]]
    if any(p.get('style', {}).get('numbering') for p in stem):
        return None
    # A source prompt plus explicit native choice markers is required. An
    # arbitrary bulleted list followed by a form field is not a question.
    if not re.search(r'[?？]|\b(?:which|does|do|is|are|how|what|select|check)\b|(?:請|请|哪|是否)', '\n'.join(p['text'] for p in stem), re.I):
        return None
    groups = []
    for ordinal, index in enumerate(bullets):
        end = bullets[ordinal + 1] if ordinal + 1 < len(bullets) else answer_index
        group = [paragraphs[index]]
        for p in paragraphs[index + 1:end]:
            # Continuation belongs to the same printed choice only with the
            # source's hanging/positive indentation. A flush-left conditional
            # instruction remains separate from the last option.
            if _indent(p) <= _indent(paragraphs[index]):
                break
            if p.get('style', {}).get('numbering') or re.match(r'\s*(?:if\b|若|如|否则|否則)', p['text'], re.I):
                break
            group.append(p)
        if ordinal + 1 < len(bullets) and len(group) != end - index:
            return None
        groups.append(group)
    final_end = paragraphs.index(groups[-1][-1]) + 1
    tail = paragraphs[final_end:answer_index]
    if not tail or any(p.get('style', {}).get('numbering') for p in tail):
        return None
    if not re.search(r'\b(?:if|please|provide|explain|describe)\b|若|如|請|请', '\n'.join(p['text'] for p in tail), re.I):
        return None
    return stem, groups, tail, paragraphs[answer_index], slot


def verified_independent_bullet_questions(field, lookup):
    """Independent printed question captions must not become radio buttons."""
    if field.get('boundary_evidence') != 'native_same_cell_bullet_answers':
        return False
    if re.search(r'\b(?:choose|select|check)\s+(?:only\s+)?one\b|单选|單選|選擇一項|选择一项', field.get('question_text_original', ''), re.I):
        return False
    from .word_inline_bullet_choices import verified_multiple_choices
    if verified_multiple_choices(field,lookup):
        return True
    labels = field.get('label_refs', [])
    if len(labels) != 1:
        return False
    block = lookup.get(labels[0].get('block_id'), {})
    layout = _layout(block)
    if not layout:
        return False
    stem, groups, _, _, _ = layout
    if re.search(r'\b(?:choose|select|check)\s+(?:only\s+)?one\b|单选|單選|選擇一項|选择一项', field.get('question_text_original', ''), re.I):
        return False
    options = field.get('options', [])
    return len(options) == len(groups) and all(
        option.get('refs') == [ref(block, p['start'], p['end']) for p in group]
        and group[-1]['text'].rstrip().endswith(('?', '？'))
        for option, group in zip(options, groups))


def restore_word_bullet_answer_groups(fields, blocks, lookup):
    from .word_inline_bullet_choices import restore_embedded_bullet_writeins
    restore_embedded_bullet_writeins(fields,blocks,lookup)
    for block in blocks:
        layout = _layout(block)
        if not layout:
            continue
        stem, groups, tail, answer, slot = layout
        touching = [f for f in _walk(fields) if any(r.get('block_id') == block['block_id'] for r in f.get('label_refs', []) + f.get('answer_region_refs', []))]
        if any(_protected(f) for f in touching):
            continue
        # Do not absorb a field whose scope deliberately reaches another cell.
        if any(any(r.get('block_id') != block['block_id'] and
                   lookup.get(r.get('block_id'), {}).get('text', '')[r.get('start', 0):r.get('end', 0)].strip()
                   for r in f.get('label_refs', [])) for f in touching):
            continue
        s = block['structure']; options = []
        for group in groups:
            first = group[0]
            option_refs = [ref(block, p['start'], p['end']) for p in group]
            sid = f"{s.get('cell_id', block['block_id'])}:bullet-option:p{first['paragraph_index']}"
            write_slot = {'slot_id': sid, 'kind': 'printed_option', 'start': first['start'], 'end': first['end'],
                          'option_label': first['text'], 'paragraph_index': first['paragraph_index'],
                          'paragraph_id': first['paragraph_id'], 'source_evidence': 'native_square_numbering_bullet',
                          'numbering': deepcopy(first['style']['numbering'])}
            slots = s.setdefault('logical_slots', [])
            if not any(x['slot_id'] == sid for x in slots):
                slots.append(write_slot)
            options.append({'refs': option_refs, 'text_original': '\n'.join(p['text'] for p in group),
                            'selected': False, 'selection_source': 'native_numbering_square_bullet',
                            'answer_region_refs': [ref(block, first['start'], first['end'], sid)]})
        parent = new_field(block, ref(block, stem[0]['start'], stem[-1]['end']))
        parent.update(options=options, answer_region_refs=[deepcopy(o['answer_region_refs'][0]) for o in options],
                      answer_status='blank', boundary_evidence='native_same_cell_bullet_answers', boundary_version=VERSION,
                      pairing_evidence='native_choice_regions')
        region = ref(block, slot['start'], slot['end'], slot['slot_id'])
        child = new_field(block, ref(block, tail[0]['start'], slot['label_end']), [region], parent['field_id'])
        # Preserve the already persisted physical inline slot identity.
        fid = f"inline:{block['block_id']}:{slot['slot_id']}"
        child.update(field_id=fid, question_id=fid, boundary_evidence='native_same_cell_bullet_writein', boundary_version=VERSION,
                     context_refs=deepcopy(parent['label_refs']),
                     explicit_condition={'status': 'unresolved', 'source_text': '\n'.join(p['text'] for p in tail),
                                         'source_refs': [ref(block, p['start'], p['end']) for p in tail],
                                         'parent_field_id': parent['field_id']})
        parent['children'] = [child]
        # Retire the earlier whole-cell label guard only after every native
        # range has a bounded role. Choice captions are rewritten solely by
        # the printed-option target which preserves their exact source text.
        protected = parent['label_refs'] + child['label_refs']
        protected += [r for o in options for r in o['refs'][1:]]
        s['protected_label_ranges'] = [{'start': r['start'], 'end': r['end']} for r in protected]
        removed = {id(f) for f in touching}
        def retain(items):
            kept = []
            for item in items:
                if id(item) in removed:
                    continue
                item['children'] = retain(item.get('children', [])); kept.append(item)
            return kept
        fields[:] = retain(fields)
        fields.append(parent)
        s.setdefault('native_form_recovery', []).append({'version': VERSION, 'field_id': parent['field_id'],
            'evidence': 'same_physical_cell_stem_square_numbering_options_and_separate_answer_placeholder',
            'retired_derived_field_ids': [f.get('field_id') for f in touching],
            'retired_derived_label_refs': [deepcopy(r) for f in touching for r in f.get('label_refs', [])]})
    fields.sort(key=lambda f: lookup.get((f.get('label_refs') or [{}])[0].get('block_id'), {}).get('order', 10**9))
