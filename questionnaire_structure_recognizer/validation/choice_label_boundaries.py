"""Keep local choice questions separate from sourced response annotations."""
from copy import deepcopy
import re
from .field_boundaries import ref, placeholder
from .form_completion import new_field

VERSION='choice-label-paragraph-boundaries-v1'
CONDITIONAL=re.compile(r'^\s*If\s+(?:the\s+)?(?:answer\s+is\s+)?(?P<option>Yes|No)\s*[,;:]?\s*(?:(?:please|pl\.)\s*)?(?:specify|explain|describe|provide\s+details)\s*[:：]',re.I)
QUESTION=re.compile(r'^\s*(?:Do|Does|Did|Is|Are|Have|Has|Can|Will|Would|是否|有无|能否)\b',re.I)


def separate_choice_label_responses(fields,lookup):
    """Return source notes; never edit source text or manual corrections.

    A complete numbered choice prompt followed by an explicit conditional
    response lane is distinct from a continuation line. A separate *wholly
    tracked-inserted declarative* paragraph is retained as a source note, not
    silently appended to the canonical question or erased.
    """
    notes=[];consumed_children=[]
    for field in fields:
        if field.get('manual_override')or field.get('answer_origin')in{'manual','knowledge'}:continue
        labels=field.get('label_refs',[])
        if len(labels)!=1 or not field.get('options'):continue
        label=labels[0];b=lookup.get(label.get('block_id'),{});s=b.get('structure',{})
        if b.get('location',{}).get('type')!='docx'or s.get('local_layout_type')!='choice_matrix':continue
        paragraphs=[p for p in s.get('paragraph_spans',[])if p.get('text','').strip()and label['start']<=p.get('start',-1)<p.get('end',-1)<=len(b.get('text',''))]
        if len(paragraphs)<2:continue
        first=paragraphs[0];text=b.get('text','')
        main=text[first['start']:first['end']]
        if not QUESTION.match(main)or not re.search(r'[?？]',main):continue
        plans=[]
        for p in paragraphs[1:]:
            value=text[p['start']:p['end']];condition=CONDITIONAL.match(value)
            if condition:
                a=p['start']+condition.end();z=p['end']
                while a<z and text[a].isspace():a+=1
                while z>a and text[z-1].isspace():z-=1
                # Empty source slots need a printed placeholder. Do not create
                # a writable position merely because an instruction ends ':'.
                if a==z:break
                plans.append(('conditional',p,condition,a,z))
            elif (p.get('fully_tracked_insertion')is True and not p.get('style',{}).get('numbering')
                    and not re.search(r'[?？]|\b(?:please|must|shall|should|required)\b',value,re.I)
                    and not re.match(r'\s*(?:If|When|Note|Example|For example|Instructions?|若|如果|例如)',value,re.I)
                    and re.search(r'\b(?:is|are|was|were|has|have)\b|(?:是|为|已)',value,re.I)):
                plans.append(('note',p,None,None,None))
            else:break
        if len(plans)!=len(paragraphs)-1:continue
        main_ref=ref(b,first['start'],first['end'])
        field.update(label_refs=[main_ref],question_refs=[deepcopy(main_ref)],question_text_original=main,
            choice_label_boundary_version=VERSION)
        for kind,p,condition,a,z in plans:
            if kind=='note':
                nr=ref(b,p['start'],p['end'])
                field.setdefault('source_note_refs',[]).append(nr)
                notes.append({'role':'note','refs':[nr],'text_original':text[p['start']:p['end']],
                    'reason':'完整判断题后的独立修订补充段落，保留原文说明，不混入题干。'})
                continue
            label_end=p['start']+condition.end()
            lr=ref(b,p['start'],label_end)
            sid=f"{s.get('cell_id',b['block_id'])}:prompt-slot:conditional-{p['paragraph_index']}"
            slot={'slot_id':sid,'kind':'inline_prompt_candidate','start':a,'end':z,
                'label_start':lr['start'],'label_end':lr['end'],'paragraph_index':p['paragraph_index'],
                'source_evidence':'explicit_conditional_response_after_completed_choice_prompt','version':VERSION}
            if not any(x.get('slot_id')==sid for x in s.setdefault('logical_slots',[])):s['logical_slots'].append(slot)
            rr=ref(b,a,z,sid);child=new_field(b,lr,[rr],field['field_id'])
            child.update(boundary_evidence='conditional_response_in_choice_label_cell',boundary_version=VERSION,
                section_refs=deepcopy(field.get('section_refs',[])),section_text_original=field.get('section_text_original',''),context_label=field.get('context_label',''),
                explicit_condition={'parent_field_id':field['field_id'],'equals_option':condition['option'].title(),
                    'source_text':text[lr['start']:lr['end']],'source_refs':[lr],'status':'unresolved'})
            if not placeholder(b,text[a:z]):
                vr=deepcopy(rr);child.update(answer_refs=[vr],answer_value_refs=[deepcopy(vr)],answer_text_original=text[a:z],answer_status='filled')
            previous=next((c for c in field.get('children',[])if any(r.get('block_id')==b['block_id']and r.get('start')==p['start']for r in c.get('label_refs',[]))),None)
            if previous and (previous.get('manual_override')or previous.get('answer_origin')in{'manual','knowledge'}):continue
            field['children']=[c for c in field.get('children',[])if c is not previous]+[child]
            consumed_children.append((field,b['block_id'],p['start'],p['end']))
    # Earlier placeholder recovery can already expose this same child as a
    # top-level entry. Remove only the duplicate derived instance by source.
    for parent,bid,a,z in consumed_children:
        fields[:]=[f for f in fields if f is parent or f.get('manual_override')or f.get('answer_origin')in{'manual','knowledge'}
            or not (f.get('label_refs')and all(r['block_id']==bid and a<=r['start']<r['end']<=z for r in f['label_refs']))]
    return notes
