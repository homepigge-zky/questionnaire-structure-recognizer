"""Check native PDF overlays inside or immediately beside scanned form fields."""
from collections import defaultdict
from copy import deepcopy
import re

from .field_boundaries import ref


def _bbox(block):
    return block['location']['bbox']


def _kind(block):
    return block.get('structure', {}).get('scan_region_kind')


def _printed(block):
    return bool(block.get('text', '').strip() and not _kind(block)
                and block['location'].get('extraction') in {'ocr', 'ocr_region'})


def _walk(fields, inherited=False):
    for field in fields:
        guarded = bool(inherited or field.get('manual_override') or field.get('answer_origin') in {'manual', 'knowledge'}
                       or field.get('answer_source') in {'manual', 'knowledge'})
        yield field, guarded
        yield from _walk(field.get('children', []), guarded)


def _recover_middle_lines(field, labels, regions, page, owners, lookup, read_values, set_values):
    """A second native-written line may lie between two already owned rules."""
    if len(regions) < 2 or any(_kind(r) != 'write_line' for r in regions):
        return []
    ordered = sorted(regions,key=lambda r:_bbox(r)[1])
    recovered = []
    for left, right in zip(ordered, ordered[1:]):
        top, bottom = _bbox(left), _bbox(right)
        if not 9 < bottom[1] - top[3] < 42 or abs(top[2] - bottom[2]) > 4:
            continue
        if any(_printed(b) and b not in labels and top[3]-2 <= (_bbox(b)[1]+_bbox(b)[3])/2 <= bottom[1]+2
               and _bbox(b)[2] > bottom[0] and _bbox(b)[0] < bottom[2] for b in page):
            continue
        candidates = [b for b in page if _kind(b) == 'write_line' and top[3] < _bbox(b)[1] < _bbox(b)[3] < bottom[1]
                      and abs(_bbox(b)[0]-bottom[0]) <= 3 and abs(_bbox(b)[2]-bottom[2]) <= 4]
        if len(candidates) != 1:
            continue
        middle = candidates[0]
        if owners.get(middle['block_id']) or not read_values(middle, lookup):
            continue
        recovered.append(middle)
    if recovered:
        available={b['block_id'] for region in ordered+recovered for b in read_values(region, lookup)}
        if any(rr['block_id'] not in available for rr in field.get('answer_value_refs',[])):
            return []
        field['answer_region_refs'] = [ref(r) for r in sorted(ordered+recovered,key=lambda b:_bbox(b)[1])]
        set_values(field, lookup)
        for r in recovered:
            owners[r['block_id']].add(field['field_id'])
    return [r['block_id'] for r in recovered]


def _bound_conditional_slot(field, labels, regions, page):
    """Do not authorize a conditional narrative from a preceding checkbox prompt."""
    if len(labels) < 2 or not regions or field.get('parent_question_id') or field.get('explicit_condition'):
        return
    prompt, anchor = labels[-1], labels[-2]
    if not re.match(r'^\s*(?:if\s*(?:yes|no)\b|如为[是否]|若[是否])', prompt.get('text',''), re.I):
        return
    anchor_box, prompt_box = _bbox(anchor), _bbox(prompt)
    first = min(regions,key=lambda b:_bbox(b)[1])
    if (not 0 < prompt_box[1] - anchor_box[3] < 28 or _kind(first) != 'write_line'
            or not -2 <= _bbox(first)[0] - prompt_box[2] < 14
            or not _bbox(first)[1]-5 <= (prompt_box[1]+prompt_box[3])/2 <= _bbox(first)[3]+3):
        return
    def on_prompt_row(b):
        box = _bbox(b)
        return box[0] >= anchor_box[2]-2 and anchor_box[1]-3 <= (box[1]+box[3])/2 <= anchor_box[3]+3
    captions=[b for b in page if _printed(b) and on_prompt_row(b) and
              re.fullmatch(r'\s*yes\s*(?:[/|]\s*)?no\s*',b['text'],re.I)]
    options=[b for b in page if _kind(b)=='checkbox' and on_prompt_row(b)]
    if len(captions)!=1 or len(options)<2:
        return
    original = deepcopy(field['label_refs'])
    field['conditional_parent_label_refs'] = original[:-1]
    field['context_refs'] = [*field.get('context_refs', []),*deepcopy(original[:-1])]
    field.update(label_refs=[original[-1]],question_refs=[deepcopy(original[-1])],
                 question_text_original=prompt['text'][original[-1]['start']:original[-1]['end']],
                 explicit_condition={'status':'unresolved','source_text':prompt['text'],'parent_field_id':None,
                                     'parent_label_refs':original[:-1], 'printed_option_refs':[ref(captions[0])],
                                     'candidate_region_refs':[ref(b) for b in options]})


def _choice_qualification(field, labels, regions, page, claimed_text):
    """A typed note below a marked row can qualify the meaning of its check."""
    if not field.get('options') or not any(o.get('selected') is True for o in field['options']):
        return None
    if not regions or any(_kind(r) != 'checkbox' for r in regions) or not all(_printed(b) for b in labels):
        return None
    left = min(_bbox(b)[0] for b in labels)
    bottom = max(_bbox(b)[3] for b in labels + regions)
    right = max(_bbox(b)[2] for b in regions)
    candidates = []
    for block in page:
        text=block.get('text','').strip()
        if (block['location'].get('extraction')!='native' or block['block_id'] in claimed_text
                or len(text)<16 or text.endswith(('?', '？', ':', '：'))
                or block.get('structure',{}).get('native_overlay_answer_region')):
            continue
        box=_bbox(block)
        if not (-1<=box[1]-bottom<=4 and abs(box[0]-left)<=5 and box[2]<=right+3 and box[3]>bottom+3):
            continue
        # Require the following printed row as the lower boundary; a footnote
        # at the page margin or a neighbouring text column is insufficient.
        following=[b for b in page if _printed(b) and 0<=_bbox(b)[1]-box[3]<=15 and abs(_bbox(b)[0]-left)<=6]
        if following:
            candidates.append(block)
    return candidates[0] if len(candidates)==1 else None


def reconcile_response_coverage(fields, pages, lookup, read_values, set_values):
    all_fields = list(_walk(fields))
    owners = defaultdict(set)
    claimed_text=set()
    for field,_ in all_fields:
        for rr in field.get('answer_region_refs',[]):
            owners[rr['block_id']].add(field['field_id'])
        for key in ('answer_value_refs','label_refs','instruction_refs'):
            claimed_text.update(rr['block_id'] for rr in field.get(key, []))
    proposals=defaultdict(list)
    audit={'interior_lines':{},'qualified_choices':{}}
    for field,guarded in all_fields:
        if guarded or field.get('boundary_evidence')!='scan_physical_form':
            continue
        labels=[lookup[r['block_id']] for r in field.get('label_refs',[]) if r['block_id'] in lookup]
        regions=[lookup[r['block_id']] for r in field.get('answer_region_refs',[]) if r['block_id'] in lookup]
        if not labels or not regions:
            continue
        number=labels[0]['location'].get('page')
        if any(b['location'].get('page')!=number for b in labels+regions):
            continue
        page=pages.get(number, [])
        if not field.get('options'):
            restored=_recover_middle_lines(field,labels,regions,page,owners,lookup,read_values,set_values)
            if restored:
                audit['interior_lines'][field['field_id']]=restored
            _bound_conditional_slot(field,labels,regions,page)
            if not field.get('explicit_condition') and not field.get('parent_question_id'):
                prompt=field.get('question_text_original','')
                if re.match(r'^\s*if\s*(?:yes|no)\s*[:：]',prompt,re.I):
                    field['explicit_condition']={'status':'unresolved','source_text':prompt,'parent_field_id':None,
                                                 'source_refs':deepcopy(field['label_refs'])}
        else:
            qualifier=_choice_qualification(field,labels,regions,page,claimed_text)
            if qualifier:
                proposals[qualifier['block_id']].append(field)
    for identifier, candidates in proposals.items():
        block=lookup[identifier]
        for field in candidates:
            field['answer_qualification_refs']=[ref(block)]
            field['answer_qualification_original']=block['text']
            field.setdefault('source_locations',{})['answer_qualifications']=[{
                'ref':ref(block),'text':block['text'],'location':deepcopy(block['location']),
                'status':'source_note' if len(candidates)==1 else 'possible_shared_note'}]
            audit['qualified_choices'][field['field_id']]=identifier
    return audit
