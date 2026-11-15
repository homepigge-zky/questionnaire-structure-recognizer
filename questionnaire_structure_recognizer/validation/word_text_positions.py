"""Bind exact native text result ranges and closed paragraph response boxes.

No nearest-text answers: each association needs a bounded native control or
an explicit bordered response panel immediately owned by a numbered prompt.
"""
from collections import defaultdict
from copy import deepcopy
import re
from .field_boundaries import ref
from .form_completion import new_field
from .word_form_ownership import _protected

VERSION='native-word-text-positions-v2-bounded-column-controls'
def _walk(fs):
    for f in fs:
        yield f
        yield from _walk(f.get('children',[]))
def _s(b):return b.get('structure') or {}
def _controls(b):
    return [c for c in b.get('controls',[]) if c.get('source')=='legacy_form_field' and c.get('type')=='text'
        and c.get('legacy_result_supported') and c.get('offset_evidence')=='native_xml_character_order'
        and isinstance(c.get('result_start'),int) and isinstance(c.get('result_end'),int)
        and 0<=c['result_start']<=c['result_end']<=len(b.get('text',''))]
def _control_ref(b,c):
    slot={'slot_id':f"{b['block_id']}:native-result:p{c['paragraph_index']}:i{c['control_index']}",
          'kind':'native_text_control_result','start':c['result_start'],'end':c['result_end'],
          'offset':c['result_start'],'paragraph_index':c['paragraph_index'],
          'paragraph_id':c.get('paragraph_id'),'evidence':VERSION}
    slots=_s(b).setdefault('logical_slots',[])
    if not any(s.get('slot_id')==slot['slot_id'] for s in slots):slots.append(slot)
    return {**ref(b,c['result_start'],c['result_end'],slot['slot_id']),
            'control_id':c.get('control_id'),'control_index':c['control_index'],'control_paragraph_index':c['paragraph_index']}
def verified_control_row(label,area,rr,lookup):
    ls,rs=_s(label),_s(area)
    if (not ls.get('table_id') or ls.get('table_id')!=rs.get('table_id') or
        rs.get('row_start')!=ls.get('row_end',0)+1 or
        (ls.get('grid_col_start'),ls.get('grid_col_end'))!=(rs.get('grid_col_start'),rs.get('grid_col_end'))):return False
    before=[b for b in lookup.values()if _s(b).get('table_id')==ls['table_id'] and _s(b).get('row_start')==ls.get('row_start') and not _s(b).get('is_merge_continuation')]
    after=[b for b in lookup.values()if _s(b).get('table_id')==ls['table_id'] and _s(b).get('row_start')==rs.get('row_start') and not _s(b).get('is_merge_continuation')]
    if len(before)!=2 or len(after)!=2:return False
    number=next((b for b in before if b['block_id']!=label['block_id']),{})
    gutter=next((b for b in after if b['block_id']!=area['block_id']),{})
    cs=_controls(area)
    return bool(re.fullmatch(r'\d+[.)]?|[A-Za-z][.)]',number.get('text','').strip()) and
        not gutter.get('text','').strip() and not gutter.get('controls') and len(cs)==len(area.get('controls',[]))==1 and
        area.get('text','').strip()==str(cs[0].get('current_value','')).strip() and
        rr==_control_ref(area,cs[0]))

def _column_controls(label,lookup):
    s=_s(label);tid=s.get('table_id');rn=s.get('row_start',0)
    if not tid:return []
    rows={r:[b for b in lookup.values()if _s(b).get('table_id')==tid and _s(b).get('row_start')==r and not _s(b).get('is_merge_continuation')]for r in (rn,rn+1)}
    headers=sorted([b for b in rows[rn]if b.get('text','').strip()],key=lambda b:_s(b)['grid_col_start'])
    if len(headers)<2 or any(b.get('controls') or re.search(r'[?？]',b['text'])for b in headers):return []
    previous=sorted([b for b in lookup.values()if _s(b).get('table_id')==tid and _s(b).get('row_start')==rn-1],key=lambda b:_s(b).get('grid_col_start',0))
    if len(previous)<2 or not re.fullmatch(r'\d+[.)]?',previous[0].get('text','').strip()):return []
    prompts=[b for b in previous[1:]if _s(b).get('grid_col_start')==_s(headers[0])['grid_col_start'] and _s(b).get('grid_col_end')==_s(headers[-1])['grid_col_end'] and b.get('text','').strip()]
    if len(prompts)!=1 or not re.search(r'[?？]|\b(?:please|provide|describe|list)\b',prompts[0]['text'],re.I):return []
    controls=[b for b in rows[rn+1]if b.get('controls') or b.get('text','').strip()]
    if len(controls)!=len(headers):return []
    if any(b.get('text','').strip() or b.get('controls')for b in rows[rn]if b not in headers):return []
    if any(b.get('text','').strip()for b in rows[rn+1]if b not in controls):return []
    pairs=[]
    for h in headers:
        targets=[b for b in controls if (_s(b).get('grid_col_start'),_s(b).get('grid_col_end'))==(_s(h).get('grid_col_start'),_s(h).get('grid_col_end'))]
        if len(targets)!=1:return []
        a=targets[0];cs=_controls(a)
        if a.get('controls') and (len(cs)!=len(a['controls']) or len(cs)!=1):return []
        if any(_s(a).get('native_cell_borders',{}).get(side) in {None,'nil','none'}for side in ('top','left','bottom','right')):return []
        if cs and a.get('text','').strip()!=str(cs[0].get('current_value','')).strip():return []
        pairs.append((h,a,cs[0]if cs else None))
    return pairs

def verified_column_controls(label,area,rr,lookup):
    return any(h['block_id']==label['block_id'] and a['block_id']==area['block_id'] and rr==(_control_ref(a,c)if c else ref(a))
               for h,a,c in _column_controls(label,lookup))

def _restore_columns(fields,rows,lookup):
    for rn,row in rows.items():
        labels=[b for b in row if b.get('text','').strip()]
        if not labels:continue
        pairs=_column_controls(labels[0],lookup)
        if not pairs:continue
        ids={h['block_id']for h,a,c in pairs}
        touching=[f for f in _walk(fields)if any(r['block_id']in ids for r in f.get('label_refs',[]))]
        if any(_protected(f) or f.get('children')for f in touching):continue
        children=[]
        for h,a,c in pairs:
            f=_put(fields,h,ref(h),a,_control_ref(a,c)if c else ref(a),'native_column_text_controls')
            if f:children.append(f)
        if len(children)!=len(pairs):continue
        value_refs=[r for f in children for r in f.get('answer_value_refs',[])]
        def keep(fs):
            result=[]
            for f in fs:
                owned=bool(f.get('label_refs')) and all(any(r['block_id']==v['block_id'] and v['start']<=r['start']<r['end']<=v['end']for v in value_refs)for r in f['label_refs'])
                if owned and not _protected(f) and not f.get('children') and not f.get('answer_region_refs'):continue
                f['children']=keep(f.get('children',[]));result.append(f)
            return result
        fields[:]=keep(fields)
        # A numbered prompt immediately above spans these header columns. Its
        # open trailing gutter is not a response box; keep it as the parent.
        before=sorted(rows.get(rn-1,[]),key=lambda b:_s(b)['grid_col_start'])
        if len(before)<2 or not re.fullmatch(r'\d+[.)]?',before[0].get('text','').strip()):continue
        candidates=[b for b in before[1:]if _s(b).get('grid_col_start')==_s(pairs[0][0])['grid_col_start'] and _s(b).get('grid_col_end')==_s(pairs[-1][0])['grid_col_end'] and b.get('text','').strip()]
        if len(candidates)!=1:continue
        parent_label=candidates[0];parents=[f for f in _walk(fields)if len(f.get('label_refs',[]))==1 and f['label_refs'][0]['block_id']==parent_label['block_id']]
        if len(parents)!=1 or _protected(parents[0]) or parents[0].get('children') or parents[0].get('answer_text_original','').strip():continue
        parent=parents[0]
        targets=[lookup.get(r['block_id'],{})for r in parent.get('answer_region_refs',[])]
        if any(t.get('controls') or t.get('text','').strip() or _s(t).get('row_start')!=rn-1 or any(_s(t).get('native_cell_borders',{}).get(side)not in {None,'nil','none'}for side in ('top','left','bottom','right')) for t in targets):continue
        parent.update(children=children,question_type='compound',answer_region_refs=[],answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='not_located',boundary_evidence='native_column_controls_parent')
        for child in children:child['parent_question_id']=parent['field_id']
        fields[:]=[f for f in fields if f not in children]

def attach_explicit_below_conditions(fields,lookup):
    all_fields=list(_walk(fields))
    for child in all_fields:
        if child.get('boundary_evidence')!='native_trailing_text_control' or _protected(child) or child.get('explicit_condition'):continue
        labels=child.get('label_refs',[])
        if len(labels)!=1:continue
        c=lookup.get(labels[0]['block_id'],{});cs=_s(c)
        noun=child.get('question_text_original','').rstrip(':： ').casefold()
        candidates=[]
        for parent in all_fields:
            if _protected(parent) or not {'yes','no'}<={o.get('text_original','').strip().casefold()for o in parent.get('options',[])}:continue
            for rr in parent.get('label_refs',[]):
                b=lookup.get(rr['block_id'],{});s=_s(b)
                if not s.get('table_id') or s['table_id']!=cs.get('table_id') or cs.get('row_start')!=s.get('row_end',0)+1 or cs.get('grid_col_start')!=s.get('grid_col_start'):continue
                text=b.get('text','')[rr['start']:rr['end']]
                m=re.search(r'\bIf\s+(yes|no)\s*,?\s*(?:please\s+)?(?:provide|give)\s+(.+?)\s+below\s*[:.]?\s*$',text,re.I)
                if m and m[2].strip().casefold()==noun:candidates.append((parent,rr,m))
        if len(candidates)!=1:continue
        parent,rr,m=candidates[0]
        child['explicit_condition']={'parent_field_id':parent['field_id'],'equals_option':m[1].title(),
            'source_refs':[{'block_id':rr['block_id'],'start':rr['start']+m.start(),'end':rr['start']+m.end()}],
            'source_text':m[0],'status':'unresolved','evidence':'native_adjacent_named_below_control','route_version':VERSION}
def _put(fields,label,lr,area,rr,evidence):
    touching=[f for f in _walk(fields) if any(r['block_id']==label['block_id'] for r in f.get('label_refs',[]))]
    if any(_protected(f) for f in touching):return
    prior=next((f for f in touching if len(f.get('label_refs',[]))==1),None)
    if prior and prior.get('children'):return
    f=new_field(label,lr,[rr])
    if prior:
        for k in ('field_id','question_id','parent_question_id','section_refs','section_text_original','context_label','model_hints'):
            if k in prior:f[k]=deepcopy(prior[k])
    f.update(boundary_evidence=evidence,boundary_version=VERSION)
    value=area.get('text','')[rr['start']:rr['end']].strip()
    f.update(question_text_original=label['text'][lr['start']:lr['end']].strip(),answer_text_original=value,
             answer_value_refs=[deepcopy(rr)] if value else [],answer_refs=[deepcopy(rr)] if value else [],
             answer_status='filled'if value else'blank')
    def keep(fs):
        result=[]
        for old in fs:
            if old in touching:continue
            old['children']=keep(old.get('children',[]));result.append(old)
        return result
    fields[:]=keep(fields)+[f]
    _s(label)['protected_label_ranges']=[{'start':lr['start'],'end':lr['end']}]
    _s(label)['semantic_role_hint']='field_label'
    return f

def restore_word_text_positions(fields,blocks,lookup):
    native=[b for b in blocks if b.get('location',{}).get('type')=='docx']
    tables=defaultdict(list)
    for b in native:
        if b.get('kind')=='table_cell' and not _s(b).get('is_merge_continuation'):
            tables[_s(b).get('table_id')].append(b)
        cs=_controls(b)
        # A single trailing text control with a real fixed prefix. Multi-control
        # paragraphs keep their existing more specific relation handling.
        if len(cs)!=1 or len(b.get('controls',[]))!=1:continue
        c=cs[0];ps=[p for p in _s(b).get('paragraph_spans',[]) if p.get('paragraph_index')==c['paragraph_index']]
        if len(ps)!=1:continue
        p=ps[0];a,z=c['result_start'],c['result_end']
        prefix=b['text'][p['start']:a].rstrip()
        if not prefix or b['text'][z:p['end']].strip():continue
        # Native field delimiters provide the boundary; punctuation is not
        # used to guess a value. Do not overwrite an unrelated physical target.
        old=[f for f in _walk(fields)if any(r['block_id']==b['block_id']for r in f.get('label_refs',[]))]
        if any(f.get('answer_region_refs') and any(r['block_id']!=b['block_id']for r in f['answer_region_refs'])for f in old):continue
        _put(fields,b,ref(b,p['start'],p['start']+len(prefix)),b,_control_ref(b,c),'native_trailing_text_control')
    for tid,cells in tables.items():
        rows=defaultdict(list)
        for b in cells:rows[_s(b)['row_start']].append(b)
        _restore_columns(fields,rows,lookup)
        # Numbered field followed immediately by a control-only response row,
        # same exact grid span and empty number gutter; stop at next label.
        for rn,row in rows.items():
            ordered=sorted(row,key=lambda b:_s(b)['grid_col_start'])
            if len(ordered)!=2 or not re.fullmatch(r'\d+[.)]?|[A-Za-z][.)]',ordered[0].get('text','').strip()):continue
            label=ordered[1];following=rows.get(rn+1,[])
            if not label.get('text','').strip() or label.get('controls') or len(following)!=2:continue
            below=sorted(following,key=lambda b:_s(b)['grid_col_start']);gutter,area=below
            cs=_controls(area)
            if gutter.get('text','').strip() or gutter.get('controls') or len(cs)!=1 or len(area.get('controls',[]))!=1:continue
            c=cs[0]
            if (_s(label)['grid_col_start'],_s(label)['grid_col_end'])!=(_s(area)['grid_col_start'],_s(area)['grid_col_end']):continue
            if area.get('text','').strip()!=str(c.get('current_value','')).strip():continue
            prior=[f for f in _walk(fields) if any(r['block_id']==label['block_id'] for r in f.get('label_refs',[]))]
            if any(f.get('answer_region_refs') or f.get('children')for f in prior):continue
            _put(fields,label,ref(label),area,_control_ref(area,c),'native_numbered_prompt_next_control_row')
        # A one-row closed box follows its numbered paragraph directly. Blank
        # borderless gutters are not response positions, and multiple boxes
        # or populated neighbours make this relation ambiguous.
        if len(rows)!=1:continue
        ids={_s(b).get('preceding_block_id')for b in cells}
        if len(ids)!=1:continue
        label=lookup.get(next(iter(ids)),{});text=label.get('text','')
        if label.get('kind')!='paragraph' or not (label.get('style',{}).get('numbering') or re.match(r'^\s*\d+[.)]\s+',text)):continue
        if not re.search(r'[?？]|\b(?:please|provide|describe|name|list|information|do|does|is|are)\b',text,re.I):continue
        start=min(b['order']for b in cells)
        if any(label.get('order',0)<b['order']<start and b.get('text','').strip()for b in native):continue
        boxes=[b for b in cells if all(_s(b).get('native_cell_borders',{}).get(side) not in {None,'nil','none'}for side in ('top','left','bottom','right'))]
        if len(boxes)!=1:continue
        area=boxes[0]
        if any(b is not area and (b.get('text','').strip() or b.get('controls'))for b in cells):continue
        if area.get('controls'):continue  # existing explicit-control resolver
        if any(f.get('answer_region_refs')for f in _walk(fields)if any(r['block_id']==label['block_id']for r in f.get('label_refs',[]))):continue
        _put(fields,label,ref(label),area,ref(area),'native_numbered_prompt_closed_response_box')
    attach_explicit_below_conditions(fields,lookup)
