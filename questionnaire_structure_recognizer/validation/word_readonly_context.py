"""Keep explicit attachment lists and already-owned column headings as source text.

These are source-role corrections, never a fallback for an unlocated answer.
"""
from copy import deepcopy
import re
from .word_form_ownership import _protected

VERSION='word-readonly-source-context-v2-shared-attachment-source-proof'

def _walk(fs):
    for f in fs:
        yield f
        yield from _walk(f.get('children',[]))

def _s(b):return b.get('structure',{})

def attachment_list_ids(blocks):
    """Source-only list membership, independent of model field omissions."""
    attachment_ids=set();active=False;bullet_id=None
    for b in blocks:
        if b.get('location',{}).get('part')!='word/document.xml':continue
        text=b.get('text','').strip();style=b.get('style',{});number=style.get('numbering',{})
        if b.get('kind')!='paragraph':active=False;bullet_id=None;continue
        if re.search(r'\bfollowing documents\b.*\b(?:attached|attach)\b.*\bquestionnaire\b',text,re.I):
            active=True;bullet_id=None;continue
        if not active:continue
        if b.get('controls') or re.search(r'[?？]|_{2,}',text):active=False;continue
        if number.get('format')=='bullet':
            if bullet_id is None:bullet_id=number.get('num_id')
            if number.get('num_id')!=bullet_id:active=False;continue
            attachment_ids.add(b['block_id']);continue
        if not text or re.search(r'^\*?\s*(?:Mandatory|Note\b)|\battached separately\b',text,re.I):continue
        active=False
    return attachment_ids

def resolve_word_readonly_context(fields,blocks,lookup):
    attachment_ids=attachment_list_ids(blocks)
    all_fields=list(_walk(fields));header_refs=[]
    for f in all_fields:
        if f.get('answer_region_refs') and f.get('boundary_evidence') in {'native_source_record_cell','source_record_cell','physical_record_row'}:
            header_refs.extend(f.get('label_refs',[]))
        elif f.get('record_index') or f.get('source_record') or f.get('record_row_index'):
            header_refs.extend(f.get('label_refs',[]))
    # Record children can carry their relation in the form/source definition.
    for f in all_fields:
        if ':record:' in f.get('field_id','') and f.get('answer_region_refs'):
            for lr in f.get('label_refs',[]):
                label=lookup.get(lr['block_id'],{});ls=_s(label)
                if ls.get('table_id') and any(_s(lookup.get(ar['block_id'],{})).get('table_id')==ls['table_id'] and _s(lookup.get(ar['block_id'],{})).get('row_start',0)>ls.get('row_start',0)for ar in f['answer_region_refs']):header_refs.append(lr)
    notes=[]
    def prune(fs):
        kept=[]
        for f in fs:
            if _protected(f):kept.append(f);continue
            f['children']=prune(f.get('children',[]))
            refs=f.get('label_refs',[]);valid=[r for r in refs if r.get('end',0)>r.get('start',0)]
            role=None
            if valid and not f.get('answer_region_refs') and not f.get('children') and not f.get('answer_text_original','').strip() and not f.get('options'):
                if len(valid)==len(refs) and all(r['block_id']in attachment_ids for r in refs):role='attachment_requirement'
                elif len(valid)>=2 and len(valid)==len(refs) and all(any(h['block_id']==r['block_id']and h['start']<=r['start']<r['end']<=h['end']for h in header_refs)for r in refs):role='table_header'
                elif len(valid)==1:
                    label=lookup.get(valid[0]['block_id'],{});ls=_s(label);text=label.get('text','')
                    if re.search(r'\b(?:any|all|each) of the following\s*[:?]?\s*$',text,re.I) and ls.get('table_id'):
                        next_fields=[other for other in all_fields if other is not f and other.get('answer_region_refs') and any(_s(lookup.get(r['block_id'],{})).get('table_id')==ls['table_id']and _s(lookup.get(r['block_id'],{})).get('row_start')==ls.get('row_end',0)+1 and re.match(r'^\s*\(?[aA]\)',lookup.get(r['block_id'],{}).get('text',''))for r in other.get('label_refs',[]))]
                        if len(next_fields)==1:
                            role='instruction';next_fields[0].setdefault('context_refs',[]).extend(deepcopy(valid))
            if role:
                for r in valid:
                    b=lookup[r['block_id']];_s(b).update(resolved_layout_role=role,nonfillable_evidence=VERSION)
                notes.append({'role':role,'refs':deepcopy(valid),'text_original':f['question_text_original'],'evidence':VERSION,'retired_field_id':f['field_id']})
            else:kept.append(f)
        return kept
    fields[:]=prune(fields)
    return notes
