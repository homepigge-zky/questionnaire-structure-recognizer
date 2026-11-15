"""Source-backed choice identity; visible labels are never rewritten."""
from collections import Counter
import hashlib
import json


def option_text(option):
    return str(option.get('text_original') or option.get('text') or '')


def option_value(option):
    return str(option.get('value') or option_text(option))


def _clean(value):
    return ' '.join(str(value).casefold().split())


def assign_option_identities(fields):
    for field in fields:
        options=field.get('options') or []
        counts=Counter(_clean(option_text(o)) for o in options)
        for option in options:
            evidence={'field_id':field.get('field_id') or field.get('question_id'),
                      'label_refs':option.get('refs',[]),'answer_region_refs':option.get('answer_region_refs',[])}
            identity='option-'+hashlib.sha256(json.dumps(evidence,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]
            option['option_id']=identity
            option['value']=identity if counts[_clean(option_text(option))]>1 else option_text(option)
        assign_option_identities(field.get('children') or [])


def selected_indices(options,selected):
    """Legacy text is accepted only when exactly one physical option owns it."""
    indices=set()
    for value in selected:
        matches=[i for i,o in enumerate(options) if option_value(o)==str(value)]
        if not matches:
            matches=[i for i,o in enumerate(options) if _clean(option_text(o))==_clean(value)]
        if len(matches)!=1:
            raise ValueError('选项重名或来源不明确，请按原文位置重新选择；不能按同名文字同时勾选多个位置')
        indices.add(matches[0])
    return indices


def selected_labels(options,selected):
    chosen=selected_indices(options,selected)
    return [option_text(o) for i,o in enumerate(options) if i in chosen]
