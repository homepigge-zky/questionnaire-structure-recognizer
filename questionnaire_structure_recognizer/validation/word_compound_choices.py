"""Build independent child fields from source-measured choice baselines."""
from copy import deepcopy
from .field_boundaries import ref
from .form_completion import new_field


def restore_word_compound_choices(fields, blocks, lookup):
    def walk(items):
        for f in items:
            yield f
            yield from walk(f.get('children', []))
    for response in blocks:
        s = response.get('structure', {})
        label = lookup.get(s.get('compound_choice_candidate_label'))
        if not label: continue
        touching = [f for f in walk(fields) if any(r.get('block_id') in {label['block_id'], response['block_id']}
                    for r in f.get('label_refs', []) + f.get('answer_region_refs', []))]
        if any(f.get('manual_override') or f.get('answer_origin') in {'manual', 'knowledge'} for f in touching): continue
        parent = next((f for f in touching if f.get('label_refs') == [ref(label)]), None)
        if parent is None:
            parent = new_field(label, ref(label), []); fields.append(parent)
        mappings = s.get('rendered_compound_choices', [])
        ps = [p for p in label.get('structure', {}).get('paragraph_spans', []) if p.get('text', '').strip()]
        valid = len(mappings) == len(ps) - 1 and bool(mappings)
        targets = []
        from ..parser_core.word_choice_layout import native_options, VERSION
        native = native_options(response)
        for m in mappings:
            valid = valid and m.get('label_block') == label['block_id'] and m.get('answer_block') == response['block_id']
            valid = valid and any(p['start'] == m.get('label_start') and p['end'] == m.get('label_end') for p in ps[1:])
            valid = valid and m.get('version') == VERSION and m.get('evidence') == 'unchanged_word_render_native_bullet_choice_baseline'
            valid = valid and all(o in native for o in m.get('options', []))
            targets.extend((r.get('control_paragraph_index'), r.get('control_index')) for o in m.get('options', []) for r in o.get('answer_region_refs', []))
        valid = valid and len(set(targets)) == len(targets) == len(response.get('controls', []))
        # A failed render must not leave one multi-answer parent masquerading
        # as a single reusable Yes/No answer.
        parent.update(answer_region_refs=[], answer_value_refs=[], answer_refs=[], answer_text_original='', options=[],
                      answer_status='blank' if valid else 'unresolved',
                      review_reasons=[] if valid else ['多个子问题与重复选择组的实际版面关系尚未核实'],
                      boundary_evidence='rendered_native_compound_choice_parent')
        if not valid: continue
        context = ref(label, mappings[0]['context_start'], mappings[0]['context_end'])
        parent.update(label_refs=[context], question_refs=[context], question_text_original=label['text'][context['start']:context['end']])
        children = []
        for m in mappings:
            options = deepcopy(m['options']); selected = [o for o in options if o.get('selected') is True]
            child = new_field(label, ref(label, m['label_start'], m['label_end']), [], parent['field_id'])
            child.update(options=options, answer_region_refs=[r for o in options for r in deepcopy(o['answer_region_refs'])],
                answer_value_refs=[r for o in selected for r in deepcopy(o['refs'])],
                answer_refs=[r for o in selected for r in deepcopy(o['refs'])],
                answer_text_original='\n'.join(o['text_original'] for o in selected),
                answer_status='unresolved' if any(o.get('selected') is None for o in options) else 'filled' if selected else 'blank',
                context_refs=[context], context_label=parent['question_text_original'],
                pairing_evidence='native_choice_regions', boundary_evidence='rendered_native_compound_choice',
                rendered_choice_evidence={k: deepcopy(m[k]) for k in ('page', 'baseline', 'evidence', 'version')}, review_reasons=[])
            children.append(child)
        parent['children'] = children
        def retain(items):
            kept = []
            for f in items:
                if f is parent: kept.append(f); continue
                if f in touching and f.get('label_refs') and all(r['block_id'] in {label['block_id'], response['block_id']} for r in f['label_refs']): continue
                f['children'] = retain(f.get('children', [])); kept.append(f)
            return kept
        fields[:] = retain(fields)
        s['protected_label_ranges'] = []


def restore_word_floating_choices(fields, blocks, lookup):
    for response in blocks:
        m = response.get('structure', {}).get('rendered_floating_choices')
        if not m or m.get('answer_block') != response['block_id']: continue
        label = lookup.get(m.get('label_block'), {})
        if label.get('kind') != 'paragraph' or m.get('evidence') != 'native_floating_text_anchor_and_unchanged_render': continue
        ids = {label['block_id'], response['block_id']}
        def walk(fs):
            for f in fs:
                yield f
                yield from walk(f.get('children', []))
        touching = [f for f in walk(fields) if any(r.get('block_id') in ids for r in f.get('label_refs', []) + f.get('answer_region_refs', []))]
        if any(f.get('manual_override') or f.get('answer_origin') in {'manual','knowledge'} for f in touching): continue
        options = deepcopy(m['options']); selected = [o for o in options if o.get('selected') is True]
        f = new_field(label, ref(label), [])
        f.update(options=options, answer_region_refs=[r for o in options for r in deepcopy(o['answer_region_refs'])],
            answer_value_refs=[r for o in selected for r in deepcopy(o['refs'])],
            answer_refs=[r for o in selected for r in deepcopy(o['refs'])],
            answer_text_original='\n'.join(o['text_original'] for o in selected),
            answer_status='unresolved' if any(o.get('selected') is None for o in options) else 'filled' if selected else 'blank',
            pairing_evidence='native_choice_regions', boundary_evidence='rendered_native_floating_choice',
            rendered_choice_evidence={k:deepcopy(m[k]) for k in ('page','baseline','anchor_baseline','native_vertical_offset','evidence','version')},
            review_reasons=[])
        def retain(items):
            kept=[]
            for item in items:
                if item.get('label_refs') and all(r['block_id'] in ids for r in item['label_refs']): continue
                item['children']=retain(item.get('children',[])); kept.append(item)
            return kept
        fields[:]=retain(fields);fields.append(f)
        response['structure'].update(protected_label_ranges=[], semantic_role_hint='option')
