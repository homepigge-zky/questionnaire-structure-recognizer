"""Distinguish source prompts inside a response cell from actual values.

These checks require an adjacent parent label and complete local paragraph
structure. They do not classify arbitrary words as answers or delete text.
"""
from collections import defaultdict
from copy import deepcopy
import re
from .field_boundaries import ref
from .form_completion import new_field

VERSION='response-prompt-groups-v1-native-paragraph-bounds'
STAFF_CONTEXT=re.compile(r'人员.*(?:组成|构成|人数)|员工.*(?:构成|组成|人数)|(?:staff|personnel|employee).*?(?:composition|breakdown|number|count)',re.I)
STAFF_LINE=re.compile(r'(?P<label>[^。；;!?？\n]{1,45}?(?:人员|人数|员工|职工|employees|staff|headcount))(?P<separator>[\s:：]*)(?P<value>(?:(?:about|approximately|approx\.?|约|大约)\s*|[~≈])?\d[\d,]*(?:\.\d+)?(?:\s*(?:人|persons?|employees))?)?\s*$',re.I)
URL_LABEL=re.compile(r'网站|网址|\b(?:website|web site|url)\b',re.I)
URL_PREFIX=re.compile(r'(?:https?://(?:www\.)?|www\.)',re.I)


def _walk(fields):
    for f in fields:
        yield f
        yield from _walk(f.get('children',[]))


def _slot(block,p,a,z,kind='inline_placeholder',**extra):
    slot={'slot_id':p['paragraph_id']+(':after-content' if a==z else ':prompt-slot:source-value'),
        'kind':kind,'start':a,'end':z,'paragraph_index':p['paragraph_index'],'paragraph_id':p['paragraph_id'],**extra}
    slots=block['structure'].setdefault('logical_slots',[])
    slots[:]=[s for s in slots if s.get('slot_id')!=slot['slot_id']];slots.append(slot)
    return ref(block,a,z,slot['slot_id'])


def _set_value(field,refs,lookup):
    value='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in refs).strip()
    field.update(answer_value_refs=deepcopy(refs),answer_refs=deepcopy(refs),answer_text_original=value,
                 answer_status='filled' if value else 'blank',review_reasons=[])


def _staff_children(parent,label,answer):
    if not STAFF_CONTEXT.search(label['text']):return None
    ps=[p for p in answer['structure'].get('paragraph_spans',[]) if p.get('text','').strip()]
    parsed=[STAFF_LINE.fullmatch(p['text'])for p in ps]
    if len(ps)<2 or not all(parsed):return None
    if len({m['label'].strip()for m in parsed})!=len(ps):return None
    children=[]
    for p,m in zip(ps,parsed):
        a=p['start'];z=a+m.end('label');value=m['value']
        va=a+m.start('value') if value else p['end'];vz=a+m.end('value') if value else p['end']
        rr=_slot(answer,p,va,vz,'inline_placeholder' if value else 'inline_prompt_candidate')
        child=new_field(answer,ref(answer,a,z),[rr],parent['field_id'])
        child.update(context_refs=[ref(label)],boundary_evidence='native_response_staff_breakdown',boundary_version=VERSION)
        _set_value(child,[ref(answer,va,vz,rr['slot_id'])] if value else [],{answer['block_id']:answer})
        children.append(child)
    return children


def _related_caption(parent,prompt):
    a=re.sub(r'\s','',parent).casefold();b=re.sub(r'\s','',prompt).casefold()
    if len(b)>65 or re.search(r'[。！？!?]',b):return False
    if re.search(r'[\u4e00-\u9fff]',a+b):
        tokens={a[i:i+2]for i in range(len(a)-1) if re.fullmatch(r'[\u4e00-\u9fff]{2}',a[i:i+2])}
        return len([t for t in tokens if t in b])>=3
    tokens={w for w in re.findall(r'[a-z]{4,}',parent.casefold())}
    return len(tokens&set(re.findall(r'[a-z]{4,}',prompt.casefold())))>=2


def _question_prompt(text):
    return len(text)<120 and bool(re.fullmatch(r'[^。;；\n]{1,110}[?？]',text) or
        re.fullmatch(r'[^。;；\n]{0,25}是否[^。;；\n]{1,90}',text) or
        re.match(r'^(?:Is|Are|Do|Does|Can|Have|Has|Please)\b',text,re.I))


def _local_question_children(parent,label,answer):
    ps=answer['structure'].get('paragraph_spans',[])
    active=[p for p in ps if p.get('text','').strip()]
    if len(active)<2 or not _related_caption(label['text'],active[0]['text']):return None
    headers=[active[0]]+[p for p in active[1:]if _question_prompt(p['text'])]
    if len(headers)<2:return None
    children=[]
    for i,h in enumerate(headers):
        end=headers[i+1]['paragraph_index'] if i+1<len(headers) else len(ps)+1
        areas=[p for p in ps if h['paragraph_index']<p['paragraph_index']<end]
        if not areas:return None
        values=[p for p in areas if p.get('text','').strip()]
        chosen=values or areas[:1]
        if len(values)>1:
            slot={'slot_id':h['paragraph_id']+':native-paragraph-group','kind':'native_response_paragraph_group',
                'start':values[0]['start'],'end':values[-1]['end'],
                'paragraph_group':[{'paragraph_index':p['paragraph_index'],'paragraph_id':p['paragraph_id'],
                    'start':p['start'],'end':p['end'],'expected_text':p['text']}for p in values],
                'prompt_ref':ref(answer,h['start'],h['end']),'evidence':'separate_native_prompt_then_contiguous_response_paragraphs'}
            answer['structure'].setdefault('logical_slots',[]).append(slot)
            region=ref(answer,slot['start'],slot['end'],slot['slot_id'])
        else:
            p=chosen[0];region=_slot(answer,p,p['start'],p['end'],'inline_placeholder' if values else 'blank_paragraph_candidate')
            if not values:
                # A real blank paragraph already has a native identity. The
                # end-of-block position is not a licence to append elsewhere.
                region['slot_id']=p['paragraph_id']+':response-blank'
                answer['structure']['logical_slots'][-1]['slot_id']=region['slot_id']
        child=new_field(answer,ref(answer,h['start'],h['end']),[region],parent['field_id'])
        child.update(context_refs=[ref(label)],boundary_evidence='native_response_local_questions',boundary_version=VERSION)
        _set_value(child,[ref(answer,p['start'],p['end'],region['slot_id'])for p in values],{answer['block_id']:answer})
        children.append(child)
    return children


def restore_response_prompt_groups(fields,blocks,lookup):
    rows=defaultdict(list)
    for b in blocks:
        s=b.get('structure',{})
        if b.get('kind')=='table_cell' and b.get('location',{}).get('type')=='docx' and not s.get('is_merge_continuation'):
            rows[(s.get('table_id'),s.get('row_start'),s.get('row_end'))].append(b)
    for row in rows.values():
        if len(row)!=2:continue
        label,answer=sorted(row,key=lambda b:b['structure'].get('grid_col_start',0))
        if label['structure'].get('grid_col_end',-2)+1!=answer['structure'].get('grid_col_start') or answer.get('controls'):continue
        touching=[f for f in _walk(fields) if any(r['block_id'] in {label['block_id'],answer['block_id']}for r in f.get('label_refs',[])+f.get('answer_region_refs',[]))]
        if any(f.get('manual_override')or f.get('answer_origin')in{'manual','knowledge'}for f in touching):continue
        parent=next((f for f in touching if f.get('label_refs')==[ref(label)]),None)
        proposed=parent or new_field(label,ref(label),[])
        if URL_LABEL.search(label['text']) and URL_PREFIX.fullmatch(answer['text'].strip()):
            p=answer['structure']['paragraph_spans'][0]
            region=_slot(answer,p,0,len(answer['text']),'inline_placeholder',placeholder_text=answer['text'])
            proposed.update(answer_region_refs=[region],boundary_evidence='native_url_prefix_placeholder',boundary_version=VERSION)
            _set_value(proposed,[],lookup)
            if parent is None:fields.append(proposed)
            continue
        children=_staff_children(proposed,label,answer) or _local_question_children(proposed,label,answer)
        if not children:continue
        proposed.update(children=children,answer_region_refs=[],answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='blank',
                        options=[],review_reasons=[],boundary_evidence='native_response_prompt_parent',boundary_version=VERSION)
        if parent is None:fields.append(proposed)
        def retain(items):
            kept=[]
            for f in items:
                if f is proposed:kept.append(f);continue
                if f.get('label_refs') and all(r['block_id']==answer['block_id']for r in f['label_refs']):continue
                f['children']=retain(f.get('children',[]));kept.append(f)
            return kept
        fields[:]=retain(fields)
        answer['structure']['protected_label_ranges']=[{'start':r['start'],'end':r['end']}for c in children for r in c['label_refs']]
