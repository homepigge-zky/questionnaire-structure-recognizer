"""Separate existing response values from their physical instructions/lines.

These operations use source cells and placeholder spans, never infer values.
"""
from collections import defaultdict
from copy import deepcopy
import re

from .field_boundaries import ref, _s, _row, _col, _end, placeholder


def restore_comment_lanes(blocks, additions, replaced, new_field):
    rows=defaultdict(list)
    for block in blocks:
        if block.get('kind')=='table_cell' and not _s(block).get('is_merge_continuation'):
            rows[(_s(block).get('table_id'),_row(block))].append(block)
    for cells in rows.values():
        cells.sort(key=_col)
        if len(cells)!=3:continue
        label,value,instruction=cells
        if not re.fullmatch(r'\s*(?:COMMENTS?|REMARKS?|备注|补充说明)\s*[:：]?\s*',label.get('text',''),re.I):continue
        if _end(label)+1!=_col(value) or _end(value)+1!=_col(instruction):continue
        if not re.search(r'\bTO\s+BE\s+(?:FILLED|FILED|COMPLETED)\s+IN\s+BY\b|由.{1,30}填写',instruction.get('text',''),re.I):continue
        if value.get('controls') or _s(value).get('blank_state')=='unreadable':continue
        f=new_field(label,ref(label),[ref(value)])
        f['instruction_refs']=[ref(instruction)]
        f['boundary_evidence']='local_comment_value_and_responsibility_instruction'
        if not placeholder(value,value.get('text','')):
            refs=[ref(value)]
            f.update(answer_value_refs=refs,answer_refs=deepcopy(refs),answer_text_original=value['text'],answer_status='filled')
        ids={b['block_id'] for b in cells}
        replaced.update(ids)
        additions[:]=[q for q in additions if not any(r['block_id'] in ids for r in q.get('label_refs',[]))]
        additions.append(f)


def restore_printed_choice_lanes(blocks,additions,replaced,new_field):
    """A whole physical response cell containing only choices has no value.

    The label may be a child caption without a question mark. A blank/number
    gutter is positional context, never a skipped answer. Native controls and
    mixed prose remain with their existing dedicated parsers.
    """
    from .native_choices import PLAIN_CHOICES
    rows=defaultdict(list)
    for b in blocks:
        if b.get('kind')=='table_cell' and not _s(b).get('is_merge_continuation'):
            rows[(_s(b).get('table_id'),_row(b))].append(b)
    for raw in rows.values():
        row=sorted(raw,key=_col)
        if len(row)==3:
            gutter=row[0]
            if gutter.get('controls') or (gutter['text'].strip() and not re.fullmatch(r'\d+(?:\.\d+)*[.)]?',gutter['text'].strip())):continue
            row=row[1:]
        if len(row)!=2:continue
        label,answer=row
        if (not label.get('text','').strip() or label.get('controls') or answer.get('controls')
                or _end(label)+1!=_col(answer) or not PLAIN_CHOICES.fullmatch(answer.get('text',''))):continue
        if any(s.get('kind')=='inline_placeholder' for s in _s(label).get('logical_slots',[])):continue
        # Preserve an already split conditional explanation after this label.
        def contains_local_explanation(fields):
            return any((q.get('boundary_evidence')=='conditional_response_after_local_choice_list'
                        and any(r['block_id']==label['block_id'] for r in q.get('label_refs',[])))
                       or contains_local_explanation(q.get('children',[])) for q in fields)
        if contains_local_explanation(additions):continue
        f=new_field(label,ref(label),[ref(answer)])
        f['boundary_evidence']='native_printed_choice_response_lane'
        ids={label['block_id'],answer['block_id']}
        additions[:]=[q for q in additions if not any(r['block_id'] in ids for r in q.get('label_refs',[]))]
        additions.append(f);replaced.update(ids)


def exclude_printed_write_lines(fields,lookup):
    """A separate native underscore paragraph is a position, not answer text.

    Keep the complete response region, but strip only an evidenced trailing
    placeholder from the value reference. Internal underscores and source
    labels are never text-cleaned or moved to another field.
    """
    for f in fields:
        exclude_printed_write_lines(f.get('children',[]),lookup)
        if f.get('manual_override') or f.get('answer_origin') in {'manual','knowledge'}:continue
        refs=f.get('answer_value_refs') or [];changed=False;values=[]
        for original in refs:
            r=deepcopy(original);b=lookup.get(r.get('block_id'),{});text=b.get('text','')
            a,z=r.get('start',-1),r.get('end',-1)
            if not (0<=a<z<=len(text)):values.append(r);continue
            m=re.search(r'\n[ \t]*[_＿]{3,}[ \t]*$',text[a:z])
            if m:
                cut=a+m.start()
                slots=_s(b).get('logical_slots',[])
                if any(s.get('kind')=='inline_placeholder' and cut<=s.get('start',-1)<s.get('end',-1)<=z for s in slots):
                    r['end']=cut;changed=True
            if r['start']<r['end']:values.append(r)
        if changed:
            f.update(answer_value_refs=values,answer_refs=deepcopy(values),
                     answer_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in values).strip(),
                     answer_status='filled' if values else 'blank')


def separate_count_guidance(fields,lookup):
    """Keep an explicit quantity question in the response cell as guidance.

    A bounded Chinese interrogative quantity prompt beside an English count
    caption is not a numerical value. Only the subsequent source span can be
    a value; no translation or answer inference is performed.
    """
    for f in fields:
        separate_count_guidance(f.get('children',[]),lookup)
        if f.get('manual_override') or f.get('answer_origin') in {'manual','knowledge'}:continue
        labels=f.get('label_refs') or [];regions=f.get('answer_region_refs') or []
        if len(labels)!=1 or len(regions)!=1 or f.get('options'):continue
        label=lookup.get(labels[0]['block_id'],{});b=lookup.get(regions[0]['block_id'],{})
        if (not label or not b or label.get('kind')!='table_cell' or b.get('kind')!='table_cell'
                or _s(label).get('table_id')!=_s(b).get('table_id') or _row(label)!=_row(b)
                or _end(label)+1!=_col(b) or b.get('controls')):continue
        label_text=f.get('question_text_original','')
        if not re.search(r'\b(?:number\s+of|count\s+of|total\s+count)\b',label_text,re.I):continue
        text=b.get('text','');first=text.split('\n',1)[0]
        if not re.fullmatch(r'(?:请(?:填写|提供|说明))?(?:总共|共有|一共|共)?(?:几个|多少)[\u4e00-\u9fff]{1,24}[？?：:]?',first.strip()):continue
        start=len(first)
        while start<len(text) and text[start].isspace():start+=1
        slot={'slot_id':b['block_id']+':prompt-slot:quantity-response','kind':'inline_prompt_candidate',
              'start':start,'end':len(text),'offset':start,'label_start':0,'label_end':len(first)}
        _s(b).setdefault('logical_slots',[]).append(slot)
        _s(b).setdefault('protected_label_ranges',[]).append({'start':0,'end':len(first)})
        region=ref(b,start,len(text),slot['slot_id']);values=[ref(b,start,len(text))] if start<len(text) else []
        f['instruction_refs']=list(f.get('instruction_refs',[]))+[ref(b,0,len(first))]
        f.update(answer_region_refs=[region],answer_value_refs=values,answer_refs=deepcopy(values),
                 answer_text_original=text[start:],answer_status='filled' if values else 'blank')
