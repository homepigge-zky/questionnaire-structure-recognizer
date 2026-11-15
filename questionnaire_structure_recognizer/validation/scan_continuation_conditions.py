"""Explicit conditional follow-up across consecutive printed table rows.

A preceding page alone is not a relationship. Require consecutive source row
numbers, matching physical choice-column headers, and the next page's first
body row directly under that header. Never infer the condition's answer.
"""
from copy import deepcopy
import re

from .field_boundaries import ref
from .scanned_local_tables import _manual
from .scan_matrix_structure import matrix_groups
from .pdf_source_relations import source_text_inside_cell

VERSION='scan-continuation-condition-v2-numbered-adjacent-and-continued-rows'


def box(b):return b.get('location',{}).get('bbox',[])
def page(b):return b.get('location',{}).get('page')

def _number(cell,texts):
    cb=box(cell)
    found=[b for b in texts if page(b)==page(cell) and re.fullmatch(r'[1-9]\d{0,2}',b.get('text','').strip())
           and 0<cb[0]-box(b)[2]<50 and cb[1]<(box(b)[1]+box(b)[3])/2<cb[3]]
    return found[0] if len(found)==1 else None

def _cell_for(labels,cells):
    # Two overlapping candidate contours are not a unique physical row.
    found=[c for c in cells if all(page(b)==page(c) and source_text_inside_cell(b,c,label=True) for b in labels)
           and box(c)[2]-box(c)[0]>180]
    return found[0] if len(found)==1 else None

def _headers(cell,cells,texts):
    groups=matrix_groups([c for c in cells if page(c)==page(cell)],
                         [t for t in texts if page(t)==page(cell)])
    groups=[g for g in groups if len(g)==3
            and [re.sub(r'\W','',caption['text']).lower() for _,caption in g]==['yes','no','na']
            and abs(box(g[0][0])[0]-box(cell)[2])<16 and box(g[0][0])[1]<box(cell)[1]]
    return groups[-1] if len(groups)==1 else None

def _same_page_above(child,labels,flat,cells,texts,lookup):
    from .scan_closed_local_grids import shares_horizontal_border
    match=re.match(r'^\s*if\s+(?:the\s+)?above\s+answer\s+is\s+(yes|no)\b',labels[0]['text'],re.I)
    if not match:return False
    cell=_cell_for(labels,cells)
    if not cell:return False
    number=_number(cell,texts);headers=_headers(cell,cells,texts)
    if not number or not headers:return False
    candidates=[]
    for parent in flat:
        if parent is child or len(parent.get('options',[]))!=3:continue
        pl=[lookup.get(r.get('block_id'),{}) for r in parent.get('label_refs',[])]
        if not pl or any(not b.get('text') for b in pl):continue
        pc=_cell_for(pl,cells)
        if not pc or page(pc)!=page(cell) or abs(box(pc)[0]-box(cell)[0])>4 or abs(box(pc)[2]-box(cell)[2])>4:continue
        pn=_number(pc,texts);ph=_headers(pc,cells,texts)
        if not pn or not ph or int(number['text'])!=int(pn['text'])+1:continue
        if [h['block_id'] for h,_ in ph]!=[h['block_id'] for h,_ in headers]:continue
        if not shares_horizontal_border(pc,cell,box(cell)[0],box(cell)[2]):continue
        candidates.append((parent,pn,pc))
    if len(candidates)!=1:return False
    parent,pn,pc=candidates[0]
    child['explicit_condition']={'status':'unresolved','source_text':labels[0]['text'],'source_refs':[ref(labels[0])],
        'parent_field_id':parent['field_id'],'equals_option':match[1].capitalize(),
        'source_relationship':'consecutive_numbered_shared_border',
        'reason':'原文明确引用上一题；连续题号、共用实体边界及同一选项表头已核对。',
        'continuation_evidence':{'version':VERSION,'previous_number_ref':ref(pn),'current_number_ref':ref(number),
            'previous_cell_ref':ref(pc),'current_cell_ref':ref(cell),'header_refs':[ref(h) for h,_ in headers]}}
    child['parent_question_id']=parent['field_id'];child['context_refs']=deepcopy(parent.get('label_refs',[]))
    return True


def reconcile_continued_conditions(fields,blocks,lookup):
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box'
           and len(b.get('structure',{}).get('closed_region_evidence',{}).get('polygon',[]))==4]
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('type')=='pdf' and len(box(b))==4]
    flat=[]
    def walk(fs):
        for f in fs:
            flat.append(f);walk(f.get('children',[]))
    walk(fields);changed=[]
    for child in flat:
        labels=[lookup.get(r.get('block_id'),{}) for r in child.get('label_refs',[])]
        if not labels or any(not b.get('text') for b in labels) or _manual(child):continue
        if not child.get('explicit_condition',{}).get('parent_field_id') and _same_page_above(child,labels,flat,cells,texts,lookup):
            changed.append(child['field_id']);continue
        match=re.match(r'^\s*if\s+(yes|no)\b',labels[0]['text'],re.I)
        if not match or child.get('explicit_condition',{}).get('parent_field_id'):continue
        cell=_cell_for(labels,cells)
        if not cell or not isinstance(page(cell),int) or page(cell)<=1:continue
        number=_number(cell,texts);headers=_headers(cell,cells,texts)
        if not number or not headers or not 0<=box(cell)[1]-max(box(h)[3] for h,_ in headers)<12:continue
        # Only the first real numbered row of this continuing column lane.
        later_numbers=[n for c in cells if page(c)==page(cell) and abs(box(c)[0]-box(cell)[0])<8
                       and abs(box(c)[2]-box(cell)[2])<8 for n in [_number(c,texts)] if n]
        if not later_numbers or int(number['text'])!=min(int(n['text']) for n in later_numbers):continue
        parents=[]
        for parent in flat:
            options=parent.get('options',[])
            if len(options)!=3 or {re.sub(r'\W','',o.get('text_original','')).lower() for o in options}!={'yes','no','na'}:continue
            pl=[lookup.get(r.get('block_id'),{}) for r in parent.get('label_refs',[])]
            if not pl or any(not b.get('text') for b in pl):continue
            pc=_cell_for(pl,cells)
            if not pc or page(pc)!=page(cell)-1 or abs(box(pc)[0]-box(cell)[0])>16 or abs(box(pc)[2]-box(cell)[2])>16:continue
            pn=_number(pc,texts);ph=_headers(pc,cells,texts)
            if not pn or not ph or int(number['text'])!=int(pn['text'])+1:continue
            previous_numbers=[n for c in cells if page(c)==page(pc) and abs(box(c)[0]-box(pc)[0])<8
                              and abs(box(c)[2]-box(pc)[2])<8 for n in [_number(c,texts)] if n]
            if not previous_numbers or int(pn['text'])!=max(int(n['text']) for n in previous_numbers):continue
            if any(abs(box(h)[0]-box(p)[0])>16 or abs((box(h)[2]-box(h)[0])-(box(p)[2]-box(p)[0]))>12
                   for (h,_),(p,_) in zip(headers,ph)):continue
            parents.append((parent,pn,ph))
        if len(parents)!=1:continue
        parent,pn,ph=parents[0]
        child['explicit_condition']={'status':'unresolved','source_text':labels[0]['text'],
            'source_refs':[ref(labels[0])],'parent_field_id':parent['field_id'],
            'equals_option':match.group(1).capitalize(),'source_relationship':'consecutive_numbered_continued_table',
            'reason':'相邻两页的连续原始题号和同一列式表头已核对；仅关联条件，不推断父题答案。',
            'continuation_evidence':{'version':VERSION,'previous_number_ref':ref(pn),'current_number_ref':ref(number),
                'previous_header_refs':[ref(h) for h,_ in ph],'current_header_refs':[ref(h) for h,_ in headers]}}
        child['parent_question_id']=parent['field_id'];child['context_refs']=deepcopy(parent.get('label_refs',[]))
        changed.append(child['field_id'])
    return {'version':VERSION,'linked_fields':changed}
