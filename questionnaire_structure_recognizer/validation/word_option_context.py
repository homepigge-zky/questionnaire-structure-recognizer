"""Keep repeated write-ins inside explicit native option-group captions.

The caption is evidence only when its physical paragraph ends in a colon
and has no attached write-in. The answer text and physical target are never
changed. Repeated labels without distinct source groups remain manual.
"""
from collections import Counter, defaultdict
from copy import deepcopy

VERSION = 'native-option-group-context-v1'


def _identity(ref):
    return (ref.get('block_id'), ref.get('start'), ref.get('end'))


def _clean(text):
    return ' '.join(str(text).casefold().split())


def attach_writein_context(parent, block):
    text = block.get('text', '')
    children = [child for child in parent.get('children', [])
                if child.get('boundary_evidence') == 'native_control_writein']
    child_labels = {_identity(ref) for child in children for ref in child.get('label_refs', [])}
    groups = []
    for option in parent.get('options', []):
        refs = option.get('refs', [])
        if len(refs) != 1: continue
        ref = refs[0]
        if ref.get('block_id') != block['block_id'] or _identity(ref) in child_labels: continue
        a, z = ref.get('start'), ref.get('end')
        if not isinstance(a, int) or not isinstance(z, int) or not 0 <= a < z <= len(text): continue
        paragraph = next((p for p in block.get('structure', {}).get('paragraph_spans', [])
                          if p['start'] <= a < p['end']), None)
        if not paragraph or z > paragraph['end']: continue
        # This is a dedicated caption paragraph, not a sentence containing
        # a colon or the prompt of one of the write-in fields itself.
        if text[paragraph['start']:a].strip() or text[z:paragraph['end']].strip() not in {':', '：'}: continue
        groups.append(ref)

    grouped = defaultdict(list)
    for child in children:
        refs = deepcopy(parent.get('context_refs', []) + parent.get('label_refs', []))
        own = child.get('label_refs', [])
        preceding = [g for g in groups if own and g['end'] <= own[0]['start']]
        group = max(preceding, key=lambda r: r['end']) if preceding else None
        grouped[_clean(child.get('question_text_original'))].append((child, refs, group))

    for items in grouped.values():
        labels = Counter(_clean(text[group['start']:group['end']]) for _, _, group in items if group)
        for child, refs, group in items:
            if len(items) > 1:
                identified = bool(group and labels[_clean(text[group['start']:group['end']])] == 1)
                if identified: refs.append(deepcopy(group))
                child['record_identity_status'] = 'identified' if identified else 'ambiguous'
                child['record_identity_reason'] = (
                    '重复填写标签由原文独立选项组和父问题共同限定'
                    if identified else '同名补充填写位置缺少不同的原文选项组，不能跨位置复用答案')
            child['context_refs'] = list({_identity(ref): ref for ref in refs}.values())
            child['choice_context_version'] = VERSION
