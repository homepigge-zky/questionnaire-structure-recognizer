"""Source-backed blank tails in repeated ruled PDF form rows."""
from collections import defaultdict
from copy import deepcopy
import re
from .form_completion import new_field
from .field_boundaries import ref

VERSION='native-pdf-blank-tails-v1'


def restore_native_pdf_blank_tails(fields,blocks,lookup):
    tables=defaultdict(list)
    for b in blocks:
        l=b.get('location',{});s=b.get('structure',{})
        if b.get('kind')=='table_cell' and l.get('extraction')=='native' and not b.get('controls') and not s.get('scan_page_context'):
            tables[(l.get('page'),s.get('table_id',l.get('table')))].append(b)
    recovered=[];claimed=[]
    for group in tables.values():
        # The entire local physical grid has one column, with several ruled
        # rows. Multi-column record tables and a one-off caption do not qualify.
        if len(group)<4:continue
        lefts=[b['location']['bbox'][0] for b in group];rights=[b['location']['bbox'][2] for b in group]
        if max(lefts)-min(lefts)>2 or max(rights)-min(rights)>2:continue
        candidates=[];sections=[]
        for b in group:
            text=b.get('text','');bb=b['location']['bbox'];spans=b.get('structure',{}).get('native_text_spans',[])
            if re.match(r'^\s*(?:SECTION\b|第.{1,6}[章节部分])',text,re.I):sections.append(b);continue
            if not text.strip() or len(text)>160 or not spans or re.search(r'[?？]|\b(?:please|must|shall)\b',text,re.I):continue
            if any(not isinstance(s.get('start'),int) or not isinstance(s.get('end'),int) or not 0<=s['start']<s['end']<=len(text) for s in spans):continue
            if max(s['bbox'][2] for s in spans)>bb[0]+.68*(bb[2]-bb[0]):continue
            if any(s['bbox'][0]>bb[0]+20 for s in spans):continue
            if len(spans)>3:continue
            candidates.append(b)
        if len(candidates)<3 or not sections:continue
        for b in candidates:
            bb=b['location']['bbox'];spans=b['structure']['native_text_spans']
            for n,span in enumerate(spans):
                a,z=span['start'],span['end'];label=b['text'][a:z];lb=span['bbox']
                if not label.strip() or len(label)>110:continue
                # A bilingual label on one baseline stays one source range;
                # separate printed rows such as Address and City/Country keep
                # independent value positions within the same physical cell.
                sid=f"pdf-blank-tail:{b['block_id']}:{a}:{z}"
                bottom=min(bb[3],spans[n+1]['bbox'][1]) if n+1<len(spans) else bb[3]
                target=[lb[2]+6,max(bb[1],lb[1]),bb[2]-3,bottom]
                if target[2]-target[0]<70 or target[3]-target[1]<5:continue
                lr=ref(b,a,z);rr=ref(b,z,z,sid)
                slot={'slot_id':sid,'kind':'native_pdf_blank_tail','start':z,'end':z,'bbox':target,
                    'label_refs':[deepcopy(lr)],'label_bbox':deepcopy(lb),'evidence':'single_column_repeated_ruled_form_rows',
                    'blank_verified':True,'expected_source_text':b['text']}
                slots=b.setdefault('structure',{}).setdefault('logical_slots',[])
                if not any(s.get('slot_id')==sid for s in slots):slots.append(slot)
                f=new_field(b,lr,[rr]);fid='pdf-blank-tail:'+b['block_id']+':'+str(a)
                section=max((s for s in sections if s['location']['bbox'][1]<bb[1]),key=lambda s:s['location']['bbox'][1],default=None)
                f.update(field_id=fid,question_id=fid,label_refs=[lr],question_refs=[deepcopy(lr)],question_text_original=label,
                    answer_region_refs=[rr],answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='blank',
                    boundary_evidence='native_pdf_repeated_blank_tail',boundary_version=VERSION)
                if section:f.update(section_refs=[ref(section)],context_refs=[ref(section)],section_text_original=section['text'],context_label=section['text'])
                b['structure'].setdefault('protected_label_ranges',[]).append({'start':a,'end':z})
                recovered.append(f);claimed.append(lr)
    if not recovered:return
    kept=[]
    for old in fields:
        refs=old.get('label_refs') or old.get('question_refs') or []
        if refs and all(any(rr['block_id']==c['block_id'] and c['start']<=rr.get('start',0) and rr.get('end',0)<=c['end'] for c in claimed) for rr in refs):continue
        kept.append(old)
    fields[:]=kept+recovered
    fields.sort(key=lambda f:(lookup.get((f.get('label_refs') or [{}])[0].get('block_id'),{}).get('order',0),(f.get('label_refs') or [{}])[0].get('start',0)))
