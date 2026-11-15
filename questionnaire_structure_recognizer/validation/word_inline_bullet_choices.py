"""Bound native square-list options and an indented write-in in one Word cell.

The whole cell is not a question or an answer. Every occupied paragraph must
have a supported role before any earlier derived relationship is retired.
"""
from copy import deepcopy
import re

from .field_boundaries import ref
from .form_completion import new_field

VERSION='native-embedded-bullet-writeins-v2-bounded-caption-continuations'


def layout(block):
    from .word_bullet_answer_groups import _indent
    if (block.get('kind')!='table_cell' or block.get('location',{}).get('type')!='docx'
            or block.get('controls') or block.get('structure',{}).get('is_merge_continuation')):
        return None
    text=block.get('text','');s=block.get('structure',{})
    ps=[p for p in s.get('paragraph_spans',[]) if p.get('text','').strip()]
    if len(ps)<3:return None
    for p in ps:
        a,z=p.get('start'),p.get('end')
        if not isinstance(a,int) or not isinstance(z,int) or not 0<=a<z<=len(text) or text[a:z]!=p['text']:return None
    stem=ps[0]
    if stem.get('style',{}).get('numbering') or not re.search(r'[?？]|(?<![A-Za-z])(?:which|does|do|how|what|select|check)\b|是否|請|请|哪',stem['text'],re.I):return None
    options=[];writeins=[];notes=[];numbering=None;closed=False
    for p in ps[1:]:
        number=p.get('style',{}).get('numbering') or {}
        square=number.get('format')=='bullet' and number.get('marker') in {'□','☐'}
        if square:
            if closed:return None
            identity=(number.get('num_id'),number.get('level'))
            if not identity[0] or (numbering is not None and identity!=numbering):return None
            numbering=identity;options.append(dict(p,continuations=[]));continue
        if number:return None
        slots=[x for x in s.get('logical_slots',[]) if x.get('kind')=='inline_placeholder'
               and x.get('paragraph_index')==p.get('paragraph_index')]
        if slots:
            if closed or len(slots)!=1 or not options or _indent(p)<=_indent(options[-1]):return None
            slot=slots[0];a,z=slot.get('start'),slot.get('end');la,lz=slot.get('label_start'),slot.get('label_end')
            if not all(isinstance(x,int) for x in (a,z,la,lz)):return None
            if not p['start']<=la<lz<=a<z<=p['end'] or not re.fullmatch(r'[_＿]{3,}',text[a:z]):return None
            if any(ch.isalnum() for index,ch in enumerate(text[p['start']:p['end']],p['start'])
                   if not la<=index<lz and not a<=index<z):return None
            if not text[la:lz].strip():return None
            writeins.append((p,slot,options[-1]));continue
        if options and re.match(r'^\s*[（(]\s*\*?\s*:?\s*(?:If\b|Note\b|若|如|注)',p['text'],re.I):
            notes.append(p);closed=True;continue
        # The source's hanging indent associates a bilingual continuation
        # with its square marker. A flush-left instruction or another question
        # cannot silently become the last checkbox's caption.
        if (closed or not options or _indent(p)<=_indent(options[-1])
            or re.match(r'\s*(?:if\b|note\b|若|如|否则|否則)',p['text'],re.I)
            or any(entry[2] is options[-1] for entry in writeins)):
            return None
        options[-1]['continuations'].append(p)
    if len(options)<2:return None
    if any(any(slot.get('kind')=='inline_placeholder' and p['start']<=slot.get('start',-1)<p['end']
               for slot in s.get('logical_slots',[])) for p in options):return None
    return stem,options,writeins,notes


def verified_multiple_choices(field,lookup):
    if field.get('embedded_bullet_layout_version')!=VERSION:return False
    labels=field.get('label_refs') or []
    if len(labels)!=1:return False
    block=lookup.get(labels[0].get('block_id'),{});parts=layout(block)
    if not parts:return False
    stem,ps,_,_=parts
    if labels!=[ref(block,stem['start'],stem['end'])]:return False
    options=field.get('options') or []
    if options and all(re.fullmatch(r'yes|no|n\s*/?\s*a|是|否|不适用|不適用',o.get('text_original','').strip(),re.I) for o in options):
        return False
    return len(options)==len(ps) and all(o.get('refs')==[ref(block,c['start'],c['end']) for c in [p]+p['continuations']]
        and o.get('answer_region_refs')==[ref(block,p['start'],p['end'],
            f"{block['structure'].get('cell_id',block['block_id'])}:bullet-option:p{p['paragraph_index']}")]
        for o,p in zip(options,ps))


def restore_embedded_bullet_writeins(fields,blocks,lookup):
    from .word_bullet_answer_groups import _walk,_protected
    for block in blocks:
        parts=layout(block)
        if not parts:continue
        stem,option_ps,writeins,notes=parts;s=block['structure'];bid=block['block_id']
        touching=[f for f in _walk(fields) if any(r.get('block_id')==bid for r in
                  f.get('label_refs',[])+f.get('answer_region_refs',[]))]
        if any(_protected(f) or f.get('answer_value_refs') or f.get('answer_text_original')
               or any(o.get('selected') is True for o in f.get('options',[])) for f in touching):continue
        if any(any(r.get('block_id')!=bid and lookup.get(r.get('block_id'),{}).get('text','')[r.get('start',0):r.get('end',0)].strip()
                   for r in f.get('label_refs',[])) for f in touching):continue
        parent=new_field(block,ref(block,stem['start'],stem['end']))
        parent.update(boundary_evidence='native_same_cell_bullet_answers',boundary_version=VERSION,
                      embedded_bullet_layout_version=VERSION,pairing_evidence='native_choice_regions',answer_status='blank')
        old=next((f for f in touching if any(r.get('block_id')==bid and r.get('start',-1)<=stem['start']<r.get('end',-1)
                  for r in f.get('label_refs',[]))),None)
        if old:
            for key in ('section_refs','section_text_original','context_label'):parent[key]=deepcopy(old.get(key,parent[key]))
        parent['instruction_refs']=[ref(block,p['start'],p['end']) for p in notes]
        parent['source_note_refs']=deepcopy(parent['instruction_refs'])
        for p in option_ps:
            sid=f"{s.get('cell_id',bid)}:bullet-option:p{p['paragraph_index']}"
            slot={'slot_id':sid,'kind':'printed_option','start':p['start'],'end':p['end'],'option_label':p['text'],
                  'paragraph_index':p['paragraph_index'],'paragraph_id':p['paragraph_id'],
                  'source_evidence':'native_square_numbering_bullet','numbering':deepcopy(p['style']['numbering'])}
            if not any(x.get('slot_id')==sid for x in s.setdefault('logical_slots',[])):s['logical_slots'].append(slot)
            rr=ref(block,p['start'],p['end'],sid)
            parent['options'].append({'refs':[ref(block,c['start'],c['end']) for c in [p]+p['continuations']],'answer_region_refs':[rr],
                'text_original':'\n'.join(c['text'] for c in [p]+p['continuations']),'selected':False,'selection_source':'native_numbering_square_bullet'})
            parent['answer_region_refs'].append(deepcopy(rr))
        for p,slot,option in writeins:
            lr=ref(block,slot['label_start'],slot['label_end']);rr=ref(block,slot['start'],slot['end'],slot['slot_id'])
            child=new_field(block,lr,[rr],parent['field_id']);fid=f"inline:{bid}:{slot['slot_id']}"
            child.update(field_id=fid,question_id=fid,boundary_evidence='native_same_cell_bullet_writein',boundary_version=VERSION,
                context_refs=deepcopy(parent['label_refs']),section_refs=deepcopy(parent['section_refs']),
                section_text_original=parent['section_text_original'],context_label=parent['context_label'],
                explicit_condition={'status':'unresolved','parent_field_id':parent['field_id'],'equals_option':option['text'],
                    'source_text':option['text']+'\n'+block['text'][lr['start']:lr['end']],
                    'source_refs':[ref(block,option['start'],option['end']),deepcopy(lr)],
                    'source_evidence':'indented_writein_between_native_square_list_options'})
            parent['children'].append(child)
        protected=parent['label_refs']+parent['instruction_refs']+[r for c in parent['children'] for r in c['label_refs']]
        protected += [ref(block,c['start'],c['end']) for p in option_ps for c in p['continuations']]
        s['protected_label_ranges']=[{'start':r['start'],'end':r['end']}for r in protected]
        remove={id(f)for f in touching}
        def retain(items):
            kept=[]
            for f in items:
                if id(f) in remove:continue
                f['children']=retain(f.get('children',[]));kept.append(f)
            return kept
        fields[:]=retain(fields);fields.append(parent)
        s.setdefault('native_form_recovery',[]).append({'version':VERSION,'field_id':parent['field_id'],
            'evidence':'physical_question_square_list_options_indented_placeholder_and_bounded_notes',
            'retired_derived_field_ids':[f.get('field_id')for f in touching],
            'retired_derived_label_refs':[deepcopy(r)for f in touching for r in f.get('label_refs',[])]})
