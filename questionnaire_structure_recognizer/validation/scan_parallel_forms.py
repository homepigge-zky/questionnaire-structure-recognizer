"""Parallel source columns and bounded optional free-text response areas.

Captions, labels, physical response cells and actual values remain distinct.
No values or missing table edges are manufactured here.
"""
from copy import deepcopy
import re
from .field_boundaries import ref
from .scan_closed_local_grids import box,page,row,contents,blank,field,install,walk,header_closed,shares_horizontal_border
from .scan_matrix_structure import _aligned_rows
from .pdf_furniture_ownership import _closed
from .pdf_source_relations import source_cells_share_row

VERSION='scan-parallel-forms-v1-source-columns-and-bounded-optional-area'


def parallel_groups(blocks):
    cells=[b for b in blocks if b.get('location',{}).get('type')=='pdf' and _closed(b)]
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('type')=='pdf']
    headers=[b for b in blocks if b.get('location',{}).get('type')=='pdf' and header_closed(b,cells)
             and box(b)[3]-box(b)[1]<35 and contents(b,texts)]
    out=[]
    for first in headers:
        hs=row(first,headers)
        if len(hs)!=2 or any(re.fullmatch(r'yes|no|n/?a',t['text'].strip(),re.I) for h in hs for t in contents(h,texts)):continue
        columns=[_aligned_rows(h,cells) for h in hs]
        if min(map(len,columns))<2 or len(columns[0])!=len(columns[1]):continue
        if not all(source_cells_share_row(a,b) for a,b in zip(*columns)):continue
        if any(not shares_horizontal_border(a,b,max(box(a)[0],box(b)[0]),min(box(a)[2],box(b)[2]))
               for h,col in zip(hs,columns) for a,b in zip([h]+col,col)):continue
        # A form label spans the column-caption row and first response row.
        # In a record table the first data row starts below all its headings.
        starts=[c for c in cells if page(c)==page(first) and source_cells_share_row(c,columns[0][0])
                and box(c)[1]<max(box(h)[3] for h in hs)-3 and contents(c,texts)]
        if len(starts)!=1:continue
        out.append({'headers':hs,'columns':columns,'first_label_cell':starts[0],'cells':cells,'texts':texts})
    return out


def caption_body_plans(blocks):
    """Geometry candidates; role and pixel evidence are required later."""
    plans=[]
    for g in parallel_groups(blocks):
        for h,col in zip(g['headers'],g['columns']):
            c=col[0];ts=contents(c,g['texts'])
            if len(ts)!=1:continue
            t=ts[0];tb=box(t);cb=box(c)
            if (not re.fullmatch(r'\([^()\n]{2,40}\)|（[^（）\n]{2,40}）',t['text'].strip())
                or tb[1]>box(h)[3]+5 or tb[3]>cb[1]+25 or cb[3]-tb[3]<25):continue
            bbox=[cb[0]+4,tb[3]+4,cb[2]-4,cb[3]-4]
            plans.append((c,t,contents(h,g['texts'])+ts,bbox,
                          {'kind':'column_caption_continuation','header_ref':ref(h),'header_bbox':deepcopy(box(h)),
                           'caption_refs':[ref(t)]}))
    return plans


def optional_area_plans(blocks):
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('type')=='pdf']
    lanes=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='blank_lane'
           and b['structure'].get('blank_state')=='blank']
    plans=[]
    for label in texts:
        if not re.fullmatch(r'\s*(?:\d+[.)]?\s*)?(?:others?|additional\s+(?:information|comments)|其他|补充说明)\s*(?:\(\s*if\s+any\s*\)|（如有）)\s*[:：]?\s*',label['text'],re.I):continue
        lb=box(label)
        rights=[b for b in lanes if page(b)==page(label) and 0<box(b)[0]-lb[2]<12 and abs(box(b)[1]-lb[1])<3]
        if len(rights)!=1:continue
        following=sorted([b for b in texts if page(b)==page(label) and box(b)[1]>lb[3]+4],key=lambda b:box(b)[1])
        if not following:continue
        end=following[0]
        if not 25<box(end)[1]-lb[3]<180:continue
        bbox=[lb[0],lb[3]+4,box(rights[0])[2],box(end)[1]-5]
        if any(page(t)==page(label) and box(t)[0]<bbox[2] and box(t)[2]>bbox[0]
               and box(t)[1]<bbox[3] and box(t)[3]>bbox[1] for t in texts):continue
        plans.append((label,label,[label],bbox,{'kind':'bounded_optional_free_text','following_ref':ref(end),
            'following_text':end['text'],'following_bbox':deepcopy(box(end)),'lane_ref':ref(rights[0]),
            'lane_bbox':deepcopy(box(rights[0]))}))
    return plans


def reconcile_parallel_forms(fields,blocks,lookup,non_qa=()):
    from .scan_inline_response_regions import verified_tail_slot
    report={'version':VERSION,'restored':[],'protected':[]}
    readonly={r['block_id'] for item in non_qa if item.get('role') in {'title','section','table_header','instruction'} for r in item.get('refs',[])}
    for group in parallel_groups(blocks):
        hs,columns,cells,texts=group['headers'],group['columns'],group['cells'],group['texts']
        header_labels=[contents(h,texts) for h in hs]
        for i,col in enumerate(columns):
            ts=contents(col[0],texts)
            if ts and all(t['block_id'] in readonly for t in ts):header_labels[i]+=ts
        for pair in zip(*columns):
            left=[c for c in cells if page(c)==page(pair[0]) and source_cells_share_row(c,pair[0]) and contents(c,texts)]
            if len(left)!=1:continue
            lc=left[0];labels=contents(lc,texts);label_ids={t['block_id'] for t in labels}
            # An already recovered compound contact group retains its parent
            # requirements instead of turning each child into a scalar answer.
            if any(f.get('parent_question_id') and any(r['block_id'] in label_ids for r in f.get('label_refs',[])) for f in walk(fields)):continue
            new=[]
            for ci,target in enumerate(pair):
                rr=ref(target)
                if not blank(target,texts):
                    ts=contents(target,texts)
                    slots=[s for s in target.get('structure',{}).get('logical_slots',[])
                           if verified_tail_slot(target,s,lookup) and s.get('blank_observation',{}).get('inner_rule_evidence',{}).get('kind')=='column_caption_continuation']
                    if not ts or not all(t['block_id'] in readonly for t in ts) or len(slots)!=1:break
                    rr={**ref(target),'start':0,'end':0,'slot_id':slots[0]['slot_id']}
                f=field(lc,labels,[target],suffix=':object:'+hs[ci]['block_id'],context=header_labels[ci])
                f.update(answer_region_refs=[rr],response_object_refs=[ref(t) for t in header_labels[ci]],
                         record_context_refs=[ref(t) for t in header_labels[ci]],field_type_hint='long_text',
                         review_reasons=[],review_status='model_proposed')
                new.append(f)
            if len(new)!=2:continue
            evidence={'kind':'parallel_named_response_columns','version':VERSION,'label_cell':ref(lc),
                      'column_headers':[ref(h) for h in hs],'response_cells':[ref(c) for c in pair]}
            install(fields,new,[lc],list(pair),texts,lookup,evidence,report)
    for label in blocks:
        slots=[s for s in label.get('structure',{}).get('logical_slots',[]) if verified_tail_slot(label,s,lookup)
               and s.get('blank_observation',{}).get('inner_rule_evidence',{}).get('kind')=='bounded_optional_free_text']
        if len(slots)!=1:continue
        prior=[f for f in walk(fields) if any(r['block_id']==label['block_id'] for r in f.get('label_refs',[]))]
        if len(prior)!=1 or prior[0].get('answer_text_original') or prior[0].get('manual_override'):continue
        from .pdf_furniture_ownership import _protected
        if _protected(prior[0]):continue
        f=prior[0];f.update(answer_region_refs=[{'block_id':label['block_id'],'start':0,'end':0,'slot_id':slots[0]['slot_id']}],
            answer_refs=[],answer_value_refs=[],answer_text_original='',answer_status='blank',options=[],field_type_hint='long_text',
            review_reasons=[],review_status='model_proposed',optional_response_evidence=deepcopy(slots[0]['blank_observation']))
        label.setdefault('structure',{})['scan_association_valid']=True
        report['restored'].append({'kind':'bounded_optional_free_text','field_id':f['field_id']})
    return report
