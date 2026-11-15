"""Native PDF document furniture and explicitly bounded fixed instructions.

Only actual repeated geometry, a closed occupied attachment-list table, or an
explicit prose lead-in establishes a role. Missing targets alone prove nothing.
"""
from collections import defaultdict
from copy import deepcopy
import math
import re
from .field_boundaries import ref

VERSION='native-pdf-readonly-roles-v1-bounded-native-evidence'

def _box(b):
    value=b.get('location',{}).get('bbox')
    return value if isinstance(value,(list,tuple)) and len(value)==4 and all(isinstance(x,(int,float))and math.isfinite(x)for x in value) and value[0]<value[2] and value[1]<value[3] else []

def _s(b):return b.get('structure',{})
def _page(b):return b.get('location',{}).get('page')
def _text(b):return re.sub(r'\s+',' ',b.get('text','')).strip()
def _native(b):return b.get('location',{}).get('type')=='pdf' and b.get('location',{}).get('extraction')=='native' and bool(_box(b))
def _occupied_cell(b):
    return (_native(b) and b.get('kind')=='table_cell' and _s(b).get('layout_evidence')=='visible_vector_borders'
        and bool(_s(b).get('native_text_spans')) and b.get('text','').strip() and not b.get('controls')
        and all(_box({'location':{'bbox':x.get('bbox')}})for x in _s(b).get('native_text_spans',[]))
        and not _s(b).get('is_merge_continuation') and not _s(b).get('logical_slots'))

def _roles(lookup):
    blocks=list(lookup.values());native=[b for b in blocks if _native(b)];tables=defaultdict(list);marked={}
    for b in native:
        if b.get('kind')=='table_cell' and _s(b).get('table_id'):tables[(_page(b),_s(b)['table_id'])].append(b)
    # Repeated controlled-document header: source issue/revision/document IDs
    # plus a real changing page counter in the same top-of-page physical table.
    banners=defaultdict(list)
    for (page,tid),cells in tables.items():
        boxes=[_box(b)for b in cells]
        if max(bb[3]for bb in boxes)>110 or len(cells)<5:continue
        occupied=[b for b in cells if _occupied_cell(b)]
        kinds={}
        for b in occupied:
            for key,pattern in [('issue',r'Issue Date\s*:\s*\S.+'),('document',r'Document No\.?\s*:\s*\S.+'),
                                ('version',r'Version No\.?\s*:\s*\S.+'),('page',r'Page\s*:\s*(\d+)\s*(?:of|/)\s*(\d+)')]:
                m=re.fullmatch(pattern,_text(b),re.I)
                if m:kinds[key]=(b,m)
        if set(kinds)!={'issue','document','version','page'}:continue
        counter=kinds['page'][1]
        if int(counter[1])!=page or int(counter[2])<3:continue
        header=[b for b in occupied if _s(b).get('row_start')==1 and _s(b).get('column_span',1)>1]
        if len(header)!=1 or any(b.get('controls')for b in cells):continue
        fixed=[header[0]]+[kinds[k][0]for k in ('issue','document','version')]
        signature=tuple(_text(b)for b in fixed)+(int(counter[2]),)
        banners[signature].append((page,fixed+[kinds['page'][0]]))
    for entries in banners.values():
        if len({p for p,_ in entries})<3:continue
        for page,cells in entries:
            peers=[p for p,cs in entries if all(max(abs(a-z)for a,z in zip(_box(x),_box(y)))<3 for x,y in zip(cells,cs))]
            if len(set(peers))<3:continue
            for b in cells:marked[b['block_id']]=('repeated_header_footer',{'kind':'native_repeated_controlled_document_banner','table_id':_s(b)['table_id'],'peer_pages':peers,'source_refs':[ref(x)for x in cells]})
    for (page,tid),cells in tables.items():
        rows=defaultdict(list)
        for b in cells:rows[_s(b).get('row_start')].append(b)
        if not rows or not all(isinstance(i,int)for i in rows):continue
        first=min(rows);headers=rows[first]
        if len(headers)!=1 or not _occupied_cell(headers[0]):continue
        header=headers[0]
        if not re.search(r'\b(?:please\s+)?(?:forward|attach|enclose|provide)\s+(?:copies\s+of\s+)?(?:the\s+)?following\s+documents\b|请将以下文件的复印件',_text(header),re.I):continue
        if _s(header).get('grid_col_start')!=1 or _s(header).get('grid_col_end')!=2:continue
        ordered=sorted(rows)
        if len(ordered)<3 or ordered!=list(range(first,max(rows)+1)):continue
        names=[]
        for ordinal,row in enumerate(ordered[1:],1):
            line=sorted(rows[row],key=lambda b:_s(b).get('grid_col_start',0))
            if len(line)!=2 or not all(_occupied_cell(b)for b in line):break
            index,name=line
            if (_s(index).get('grid_col_start')!=1 or _s(index).get('grid_col_end')!=1
                or _s(name).get('grid_col_start')!=2 or _s(name).get('grid_col_end')!=2
                or not re.fullmatch(str(ordinal)+r'[.)]?',_text(index))
                or re.search(r'[?？]|_{2,}',name['text'])):break
            # Both columns are occupied source content, not a tall blank
            # response cell merely carrying a short label at its top.
            spans=_s(name)['native_text_spans'];bb=_box(name)
            if max(s['bbox'][3]for s in spans)<bb[3]-8:break
            names.append(name)
        if len(names)!=len(ordered)-1:continue
        for b in names:marked[b['block_id']]=('attachment_requirement',{'kind':'native_two_column_numbered_attachment_list','heading_ref':ref(header),'table_id':tid,'row':_s(b)['row_start']})
    paragraphs=defaultdict(list)
    for b in native:
        if b.get('kind')=='paragraph' and b.get('text','').strip():paragraphs[_page(b)].append(b)
    for page,items in paragraphs.items():
        items.sort(key=lambda b:(_box(b)[1],_box(b)[0]))
        for i,lead in enumerate(items):
            text=_text(lead)
            role=('declaration' if re.match(r'^I\s+(?:certify|declare)\s+that\s+the\s+information\b',text,re.I)
                  else 'instruction' if re.match(r'^Attached\s+please\s+find\s+a\s+questionnaire\b',text,re.I) else None)
            if not role:continue
            group=[];last=_box(lead)
            for b in items[i:]:
                bb=_box(b)
                if len(group)>=14 or (group and (bb[1]-last[3]>6 or bb[1]<last[3]-2)) or abs(bb[0]-_box(lead)[0])>3:break
                if (b.get('controls') or _s(b).get('logical_slots') or re.search(r'[?？]|_{2,}',b['text'])
                    or (group and re.match(r'^(?:\d+[.)]\s*)?(?:What|Which|How|Are|Is|Do|Does)\b|^(?:是否|请说明|請說明)',_text(b),re.I))):break
                group.append(b);last=bb
            if len(group)<3:continue
            # Keep the complete bounded prose readable in standard exports.
            for b in group:marked[b['block_id']]=('instruction',{'kind':'native_contiguous_'+role+'_prose','lead_ref':ref(lead),'paragraph_refs':[ref(x)for x in group]})
    return marked

def reconcile_native_pdf_readonly_roles(fields,lookup,non_qa):
    from .pdf_furniture_ownership import _protected,_unused
    marked=_roles(lookup);retired=[]
    def clean(items):
        kept=[]
        for f in items:
            if _protected(f):kept.append(f);continue
            # A business container, any observed answer or a real response
            # region wins over this fixed-content role proposal.
            if f.get('children') or f.get('answer_region_refs') or f.get('options') or not _unused(f):kept.append(f);continue
            refs=f.get('label_refs') or f.get('question_refs') or []
            if not refs or any(r.get('block_id')not in marked for r in refs):kept.append(f);continue
            if any(type(r.get('start'))is not int or type(r.get('end'))is not int or not 0<=r['start']<r['end']<=len(lookup[r['block_id']]['text'])for r in refs):kept.append(f);continue
            roles={marked[r['block_id']][0]for r in refs}
            if len(roles)!=1:kept.append(f);continue
            role=next(iter(roles));proof={'version':VERSION,'evidence':[deepcopy(marked[r['block_id']][1])for r in refs]}
            note={'role':role,'refs':deepcopy(refs),'text_original':'\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']]for r in refs),'reason':'原生版面及明确原文证明为固定说明、页眉或附件清单','source_role_evidence':proof}
            non_qa.append(note)
            for r in refs:lookup[r['block_id']].setdefault('structure',{}).update(resolved_layout_role=role,nonanswerable_source_evidence=deepcopy(proof))
            retired.append({'field_id':f['field_id'],'role':role,'refs':deepcopy(refs),'proof':proof})
        return kept
    fields[:]=clean(fields)
    return {'version':VERSION,'retired':retired}
