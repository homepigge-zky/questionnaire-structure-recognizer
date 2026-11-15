"""Identify closed, shallow section bars above repeated choice columns.

Uses physical page layout and source option captions, never a heading word list.
A model-proposed heading answer is removed only when it borrowed those exact
column captions; independently located responses and manual corrections stay.
"""
from collections import Counter
from copy import deepcopy
import re

VERSION = 'pdf-section-banners-v1-closed-bars-and-option-columns'


def resolve_pdf_section_banners(fields, blocks, lookup):
    def box(block): return block.get('location',{}).get('bbox',[])
    def page(block): return block.get('location',{}).get('page')
    def full(block): return {'block_id':block['block_id'],'start':0,'end':len(block['text'])}
    def inside(inner,outer):
        return len(inner)==len(outer)==4 and outer[0]-1<=inner[0]<inner[2]<=outer[2]+1 and outer[1]-1<=inner[1]<inner[3]<=outer[3]+1
    def flat(items):
        for field in items:
            yield field
            yield from flat(field.get('children',[]))
    evidence=[]
    for region in blocks:
        rb=box(region);rs=region.get('structure',{})
        if (region.get('location',{}).get('type')!='pdf' or rs.get('scan_region_kind')!='text_box'
                or len(rb)!=4 or not 5<=rb[3]-rb[1]<=20 or rb[2]-rb[0]<250 or region.get('controls')):continue
        content=[b for b in blocks if page(b)==page(region) and b.get('text','').strip() and inside(box(b),rb)]
        if len(content)!=1:continue
        heading=content[0];hb=box(heading);text=heading['text'].strip()
        if (not 2<=len(text)<=100 or re.search(r'[?？:：]',text)
                or re.fullmatch(r'\s*(?:yes|no|n/?a|是|否|不适用)\s*',text,re.I)
                or not rb[0]+20<hb[0]<hb[2]<rb[2]-20):continue
        nearby=[];header_counts=Counter();local_headers=set()
        for field in flat(fields):
            labels=[lookup.get(r.get('block_id'),{}) for r in field.get('label_refs',[])]
            targets=[lookup.get(r.get('block_id'),{}) for r in field.get('answer_region_refs',[])]
            if (not labels or not targets or any(page(b)!=page(region) or len(box(b))!=4 for b in labels+targets)
                    or not 5<min(box(b)[1] for b in labels)-rb[3]<100
                    or not all(b.get('structure',{}).get('scan_region_kind')=='checkbox' for b in targets)):continue
            captions=[]
            for option in field.get('options',[]):
                for ref in option.get('refs',[]):
                    b=lookup.get(ref.get('block_id'),{});bb=box(b);start,end=ref.get('start'),ref.get('end')
                    if (not isinstance(start,int) or not isinstance(end,int) or not 0<=start<end<=len(b.get('text',''))
                            or not re.fullmatch(r'\s*(yes|no|n/?a|是|否|不适用)\s*',b['text'][start:end],re.I)
                            or len(bb)!=4 or page(b)!=page(region)
                            or bb[1]>rb[3]+24 or bb[3]>min(box(t)[1] for t in targets)):continue
                    if any(box(t)[0]-5<=(bb[0]+bb[2])/2<=box(t)[2]+12 for t in targets):
                        captions.append(ref)
                        if 0<=bb[1]-rb[3]<=24:local_headers.add((ref['block_id'],start,end))
            if len({(r['block_id'],r['start'],r['end']) for r in captions})>=2:
                nearby.append(field)
                header_counts.update({(r['block_id'],r['start'],r['end']) for r in captions})
        shared={k for k,count in header_counts.items() if count>=2}
        # A repeated matrix may inherit one unreadable local caption from its
        # already verified same-page column header. At least one actual local
        # header must still start immediately below this physical section bar.
        if len(shared)<2 or len(nearby)<2 or not shared & local_headers:continue
        existing=[f for f in flat(fields) if any(r.get('block_id')==heading['block_id'] for r in f.get('label_refs',[]))]
        # Never reclassify a confirmed/user field, or one with its own physical
        # response. This also protects short labels in genuine data-entry rows.
        if any(f.get('manual_override') or f.get('review_status')=='confirmed' or f.get('children') or
            any(r.get('block_id')!=heading['block_id'] for r in f.get('label_refs',[])) or
            any((r.get('block_id'),r.get('start'),r.get('end')) not in shared for r in
                f.get('answer_region_refs',[])+f.get('answer_value_refs',[])) for f in existing):continue
        removed={f['field_id'] for f in existing}
        def prune(items):
            items[:]=[f for f in items if f.get('field_id') not in removed]
            for f in items:prune(f.get('children',[]))
        prune(fields)
        proof={'version':VERSION,'physical_bar_ref':{'block_id':region['block_id']},'section_ref':full(heading),
            'shared_option_header_refs':[{'block_id':b,'start':s,'end':e} for b,s,e in sorted(shared)],
            'removed_model_field_ids':sorted(removed)}
        heading.setdefault('structure',{}).update(resolved_layout_role='section_header',section_banner_evidence=deepcopy(proof))
        for field in nearby:
            field['section_refs']=[full(heading)];field['section_text_original']=heading['text']
            for r in field.get('label_refs',[]):
                lookup[r['block_id']].setdefault('structure',{})['section_path_block_ids']=[[heading['block_id']]]
        evidence.append(proof)
    return evidence
