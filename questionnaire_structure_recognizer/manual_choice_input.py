"""Keep an explicit manual choice and its stored text consistent.

Only original option labels/identities are accepted. This is input validation,
not semantic answer generation; ambiguous captions require physical selection.
"""
import re

from .choice_identity import option_text, option_value, selected_indices


def _clean(value):
    return ' '.join(str(value).casefold().split())


def reconcile_manual_choice(payload, options, input_type, current_answer, current_selected):
    text_present = 'answer_text' in payload
    selection_present = 'selected_options' in payload
    answer = str(payload.get('answer_text', current_answer))
    selected = payload.get('selected_options', current_selected)
    if selection_present and (not isinstance(selected, list) or any(not isinstance(v, str) for v in selected)):
        raise ValueError('选项必须为原文位置值列表')
    if not options:
        if selection_present and selected:
            raise ValueError('当前字段没有可选择的原文选项')
        return answer, selected
    if not text_present and not selection_present:
        return answer, selected

    def from_text(text):
        if not text.strip():
            return set()
        exact = [i for i, option in enumerate(options) if _clean(option_text(option)) == _clean(text)]
        if len(exact) == 1:
            return set(exact)
        if input_type == 'multi_select' and not exact:
            parts = [part.strip() for part in re.split(r'[;；\n]+', text) if part.strip()]
            if len(parts) > 1:
                found = set()
                for part in parts:
                    matches = [i for i, option in enumerate(options) if _clean(option_text(option)) == _clean(part)]
                    if len(matches) != 1:
                        break
                    found.add(matches[0])
                else:
                    return found
        raise ValueError('文字答案不能唯一对应原文选项；请直接选择正确选项，补充说明请写入备注')

    if selection_present:
        chosen = selected_indices(options, selected)
        if text_present and answer.strip():
            # An explicit physical identity may disambiguate duplicate captions;
            # text alone may not. It still must match that selected caption.
            physical_caption_agrees = len(chosen) == 1 and _clean(answer) == _clean(option_text(options[next(iter(chosen))]))
            if not physical_caption_agrees and from_text(answer) != chosen:
                raise ValueError('文字答案与所选选项不一致，请核对后保存')
        # The choice-only editor must not keep an older contradictory text value.
        if not text_present:
            answer = ''
    else:
        chosen = from_text(answer)
    if input_type == 'single_select' and len(chosen) > 1:
        raise ValueError('此题只能选择一个原文选项')
    return answer, [option_value(option) for i, option in enumerate(options) if i in chosen]


def validate_saved_choice(question):
    """Reject old inconsistent snapshots; never silently migrate manual values."""
    options=question.get('options')or[]
    if not options:return
    selected=question.get('selected_options')or[]
    answer=str(question.get('answer_text')or'')
    try:
        reconcile_manual_choice({'answer_text':answer,'selected_options':selected},options,
                                question.get('input_type'),answer,selected)
    except ValueError as exc:
        raise ValueError(f"题目“{question.get('question_text','')}”的文字答案与已保存选项不一致，请重新选择并保存后导出；旧答案已保留") from exc
