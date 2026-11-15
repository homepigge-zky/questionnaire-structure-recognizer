"""Source-wide response ownership checks, independent of answer wording."""
from collections import defaultdict
import json
import re

REUSE_CONSTRAINT_POLICY = 'source-reuse-constraints-v7-explicit-part-exemptions'
SHARED_TARGET_RESTRICTION = 'source_response_target_shared'
SHARED_TARGET_REASON = '同一原文答复位置同时归属多个独立字段，需重新核对字段边界；不能直接确认为可复用答案。'


# A historical statement about this submission cannot establish a new attachment.
_ATTACHED_DOCUMENT = re.compile(
    r"\b(?:list|report|chart|certificate|questionnaire|copy|documents?)\s+"
    r"(?:(?:is|are)\s+|(?:has|have)\s+been\s+)(?:also\s+)?(?:attached|enclosed)"
    r"(?:\s+(?:herein|hereto|to\s+(?:this|the\s+present)\s+(?:questionnaire|submission|response)))?"
    r"\s*(?=[.!;。；]|$)|(?:本次问卷|本次答复|本问卷)(?:中)?已(?:随附|附上)|(?:清单|证书副本|报告)已随本问卷附上",
    re.I,
)


def reviewed_value_restrictions(answer, source_codes=(), question=""):
    """Hold current enclosures and rolling dates independently of alias approval."""
    restrictions = list(source_codes or ())
    text = " ".join(str(answer or "").split())
    prompt = " ".join(str(question or "").split())
    attachment_response = re.fullmatch(r"(?:copy\s+)?(?:attached|enclosed)\s*[.!]?|已附[上件]?", text, re.I)
    attachment_request = re.search(r"^(?:\d+(?:\.\d+)*[.):]?\s*)?(?:(?:please|kindly)\s+)?(?:attach|enclose)\b", prompt, re.I)
    compact = re.sub(r'\s+', '', prompt).casefold()
    current_enclosure = bool(re.search(r'(?:didyouattach|haveyouattached|are.{0,40}attached).{0,150}(?:thisquestionnaire|thissubmission|thisresponse)', compact)
                             or re.search(r'(?:本次问卷|本问卷|本次答复).{0,30}(?:已附|随附|附上)', compact))
    if _ATTACHED_DOCUMENT.search(text) or attachment_response or attachment_request or current_enclosure:
        code = "source_attachment_requires_current_confirmation"
        if code not in restrictions:
            restrictions.append(code)
    relative_window = re.search(
        r"\b(?:within|during|over|in)\s*(?:the\s*)?(?:last|past)\s*"
        r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:years?|months?|days?)\b"
        r"|(?:过去|近)\s*[一二三四五六七八九十0-9]+\s*(?:年|个月|天)", prompt, re.I)
    if relative_window and "source_relative_period_requires_confirmation" not in restrictions:
        restrictions.append("source_relative_period_requires_confirmation")
    return restrictions


def restriction_reason(codes):
    if "source_part_exemption_requires_current_confirmation" in codes:
        return "历史答复依赖原文声明的分部不适用例外，需核对本次角色及适用范围后填写。"
    if SHARED_TARGET_RESTRICTION in codes:
        return SHARED_TARGET_REASON
    if "source_attachment_requires_current_confirmation" in codes:
        return "历史答复声称已随附资料，请核对本次问卷的附件后人工填写。"
    if "source_relative_period_requires_confirmation" in codes:
        return "原问题使用相对时间范围，请确认本次对应期间后再填写。"
    if "source_choice_requires_qualification" in codes:
        return "原文选择还附有适用条件或范围说明，请对照完整答复后人工填写。"
    return "历史来源属于签署、本次人员确认或客户审核区域，需人工填写。"


def shared_response_targets(result):
    owners = defaultdict(dict)
    def visit(fields):
        for field in fields:
            fid = field.get('field_id') or field.get('question_id')
            filled = bool(field.get('answer_text_original', '').strip()
                          or any(o.get('selected') is True for o in field.get('options', [])))
            group_only = field.get('children') and not filled and not field.get('options')
            if fid and not group_only and (field.get('label_refs') or field.get('question_refs')):
                for target in field.get('form_definition', {}).get('fill_targets', []):
                    ref = target.get('ref') or {}
                    if not ref.get('block_id'): continue
                    # A control/fragment identity, never the caption text or
                    # its position in a filtered list of nonempty answers.
                    key = json.dumps({k: ref.get(k) for k in ('block_id', 'slot_id', 'start', 'end',
                        'control_id', 'control_index', 'control_paragraph_index')}, sort_keys=True)
                    owners[key][fid] = (filled, target.get('target_id'))
            visit(field.get('children', []))
    visit(result.get('questions', []))
    conflicts = defaultdict(list)
    for identities in owners.values():
        if len(identities) < 2 or not any(value[0] for value in identities.values()): continue
        for fid, (_, target_id) in identities.items():
            conflicts[fid].append({'target_id': target_id, 'other_field_ids': sorted(set(identities)-{fid})})
    return dict(conflicts)
