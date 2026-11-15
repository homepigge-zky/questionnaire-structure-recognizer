"""Source-bounded recovery of multiline raster form grids.

All bounds are original closed contours. No missing caption, selection, value,
or row is invented. Occupied/manual response data causes the local grid to be
left unchanged.
"""
from copy import deepcopy
import re
from .pdf_furniture_ownership import _closed, _protected
from .pdf_source_relations import source_cells_share_row, source_text_inside_cell
from .scanned_cells import _replace_owned
from .scan_matrix_structure import _aligned_rows
from .field_boundaries import ref
from .form_completion import new_field

VERSION = 'scan-closed-local-grids-v3-complete-contact-record-cells'
NUMBER = re.compile(r'^\s*\d+(?:\.\d+)*[.)]?\s*$')
CHOICE = re.compile(r'^(?:yes|no|n\s*/?\s*a)$', re.I)

def box(b): return b['location']['bbox']
def page(b): return b['location']['page']
def walk(fields):
    for f in fields:
        yield f
        yield from walk(f.get('children', []))
def ids(refs): return {r['block_id'] for r in refs}
def contents(c, texts):
    return sorted([b for b in texts if source_text_inside_cell(b, c, label=True)], key=lambda b: (box(b)[1], box(b)[0]))
def row(c, cells, limit=10):
    chain=[c]
    for _ in range(limit):
        following=[v for v in cells if v not in chain and source_cells_share_row(chain[-1],v)]
        if not following: return chain
        if len(following)!=1: return []
        chain+=following
    return []
def blank(c, texts):
    return c['structure'].get('blank_state')=='blank' and not contents(c,texts)
def shares_horizontal_border(upper,lower,xleft,xright):
    if page(upper)!=page(lower) or box(lower)[1]<=box(upper)[1]:return False
    edges=[]
    for c,bottom in [(upper,True),(lower,False)]:
        poly=c['structure']['closed_region_evidence']['polygon']
        horizontal=[(a,b) for a,b in zip(poly,poly[1:]+poly[:1]) if abs(a[0]-b[0])>.7*(box(c)[2]-box(c)[0])]
        if len(horizontal)!=2:return False
        a,b=sorted(horizontal,key=lambda edge:(edge[0][1]+edge[1][1])/2)[-1 if bottom else 0]
        if min(a[0],b[0])>xleft+3 or max(a[0],b[0])<xright-3:return False
        edges.append([a[1]+(x-a[0])*(b[1]-a[1])/(b[0]-a[0]) for x in (xleft+3,xright-3)])
    return all(abs(a-b)<4 for a,b in zip(*edges))
def matrix_column_body(header,headers,cells,texts):
    """Follow measured common borders, allowing observed full-table bands.

    A new occupied narrow header ends the path. A short full-width section
    bar may bridge it, but is retained as context rather than another answer.
    """
    hb=box(header);right=box(headers[-1])[2];left=box(headers[0])[0]
    candidates=[]
    for c in cells:
        cb=box(c)
        if page(c)!=page(header) or cb[1]<=hb[1]:continue
        narrow=abs(cb[0]-hb[0])<6 and abs(cb[2]-hb[2])<6
        ts=contents(c,texts)
        band=(cb[0]<left-150 and abs(cb[2]-right)<6 and cb[3]-cb[1]<40 and
              0<sum(len(t['text']) for t in ts)<100 and not any('?' in t['text'] or '？' in t['text'] for t in ts))
        if narrow or band:candidates.append((c,band))
    current=header;accepted={};last_band=None
    for _ in range(len(candidates)):
        following=[(c,band) for c,band in candidates if shares_horizontal_border(current,c,max(hb[0],box(c)[0]),min(hb[2],box(c)[2]))]
        if len(following)!=1:break
        c,band=following[0]
        if band:last_band=c
        elif blank(c,texts):accepted[c['block_id']]=last_band
        else:break
        current=c
    return accepted
def header_closed(c, cells):
    if _closed(c):return True
    proof=c.get('structure',{}).get('closed_region_evidence') or {}
    if proof.get('method')!='observed_adjacent_four_rules_v1':return False
    seed=proof.get('seed_cell_bbox');rule=proof.get('observed_horizontal_rule')
    if not isinstance(rule,list) or len(rule)!=4 or not all(type(v) in (int,float) for v in rule) or rule[2]<=rule[0]:return False
    seeds=[v for v in cells if page(v)==page(c) and box(v)==seed and _closed(v)]
    if len(seeds)!=1:return False
    checked=deepcopy(c);checked['structure']['closed_region_evidence']['method']='observed_four_sided_polygon_v1'
    if not _closed(checked):return False
    poly=proof['polygon'];top=sorted(poly,key=lambda p:p[1])[:2]
    # Top two vertices of a tilted thin header may both be on one side; test
    # the minimum y vertex at each observed x boundary against the actual rule.
    left=min(poly,key=lambda p:(p[0],p[1]));right=min(poly,key=lambda p:(-p[0],p[1]))
    def line_y(x):return rule[1]+(x-rule[0])*(rule[3]-rule[1])/(rule[2]-rule[0])
    if any(not rule[0]-3<=x<=rule[2]+3 or abs(y-line_y(x))>3 for x,y in [left,right]):return False
    return abs(box(c)[3]-box(seeds[0])[1])<8
def protected(fields, owned, inherited=False):
    for f in fields:
        controlled=inherited or _protected(f)
        refs=[r for key in ('label_refs','question_refs','answer_region_refs','answer_value_refs','answer_refs') for r in f.get(key,[])]
        if ids(refs)&owned and (controlled or f.get('answer_status')=='filled' or f.get('answer_text_original') or f.get('answer_value_refs') or f.get('answer_refs') or f.get('observed_answer_text')):
            return True
        if protected(f.get('children',[]),owned,controlled):return True
    return False
def replace_owned_recursive(fields, owned, lookup):
    for f in fields:
        if f.get('children'):replace_owned_recursive(f['children'],owned,lookup)
    _replace_owned(fields,owned,lookup)
def field(label_cell, labels, regions, *, suffix='', context=()):
    f=new_field(labels[0],ref(labels[0]),[ref(c) for c in regions])
    fid='closed-grid:'+label_cell['block_id']+suffix
    f.update(field_id=fid,question_id=fid,label_refs=[ref(b) for b in labels],question_refs=[ref(b) for b in labels],
        question_text_original='\n'.join(b['text'] for b in labels),context_refs=[ref(b) for b in context],
        context_label='\n'.join(b['text'] for b in context),physical_label_region_ref=ref(label_cell),
        section_refs=[],section_text_original='',section_path=[],
        answer_region_refs=[ref(c) for c in regions],answer_value_refs=[],answer_refs=[],answer_text_original='',
        answer_status='blank',boundary_evidence='closed_local_grid',boundary_version=VERSION)
    return f
def local_section_band(label_cells,response_cells,cells,texts):
    first=min(label_cells,key=lambda c:box(c)[1]);y=box(first)[1]
    xleft=min(box(c)[0] for c in label_cells)
    xright=max(box(c)[2] for c in response_cells)
    candidates=[]
    for c in cells:
        cb=box(c);ts=contents(c,texts)
        if (page(c)==page(first) and cb[0]<xleft-5 and abs(cb[2]-xright)<7 and cb[3]<=y+8 and
            cb[3]-cb[1]<40 and 0<sum(len(t['text']) for t in ts)<100 and
            not any('?' in t['text'] or '？' in t['text'] for t in ts)):
            candidates.append(c)
    return max(candidates,key=lambda c:box(c)[3]) if candidates else None
def install(fields, new, label_cells, response_cells, texts, lookup, evidence, reports):
    label_text=[b for c in label_cells for b in contents(c,texts)]
    owned=ids([ref(b) for b in label_text])
    touched=owned|{c['block_id'] for c in response_cells}
    if protected(fields,touched):
        reports['protected'].append(evidence);return False
    previous=[deepcopy(f) for f in walk(fields) if any(ids(f.get(key,[]))&touched for key in ('label_refs','question_refs','answer_region_refs'))]
    # Any former scalar field using a recovered answer cell but a different
    # prompt remains explicit for review; do not silently transfer its value.
    conflicting=[f for f in walk(fields) if ids(f.get('answer_region_refs',[]))&touched and not ids(f.get('label_refs',[]))<=owned]
    if any(f.get('children') or _protected(f) for f in conflicting):return False
    # Insert at the first affected root, before residual spans retained by
    # _replace_owned. The prefix has no owned labels/targets, so recursive
    # replacement cannot delete its roots and invalidate this boundary.
    insertion=next((i for i,root in enumerate(fields) if any(
        ids(f.get(key,[]))&touched for f in walk([root])
        for key in ('label_refs','question_refs','answer_region_refs'))),None)
    if insertion is None:
        # A newly discovered group may have no previous field. Source order
        # chooses only its insertion gap; never sort existing record rows by
        # their shared column-header labels (that would make them column-major).
        def first_label_order(group):
            return min((lookup[r['block_id']]['order'] for f in walk(group)
                for r in f.get('label_refs',[]) if r['block_id'] in lookup),default=float('inf'))
        new_order=first_label_order(new)
        insertion=next((i for i,root in enumerate(fields)
            if first_label_order([root])>new_order),len(fields))
    replace_owned_recursive(fields,owned,lookup)
    for f in conflicting:
        if any(f is v for v in walk(fields)):
            f.update(answer_region_refs=[],answer_status='unresolved',review_status='needs_review')
            f.setdefault('review_reasons',[]).append('原答复格被完整实体表格占用；原提示保留待核对。')
    for b in label_text:
        s=b.setdefault('structure',{});s['resolved_layout_role']='field_label'
        s['protected_label_ranges']=[{'start':0,'end':len(b['text'])}]
    section=local_section_band(label_cells,response_cells or label_cells,[b for b in lookup.values() if b.get('location',{}).get('type')=='pdf' and _closed(b)],texts)
    section_labels=contents(section,texts) if section else []
    for f in walk(new):
        if section_labels:
            f.update(section_refs=[ref(t) for t in section_labels],section_text_original='\n'.join(t['text'] for t in section_labels),
                section_path=[{'section_id':'closed-band:'+section['block_id'],'refs':[ref(t) for t in section_labels],
                    'text_original':'\n'.join(t['text'] for t in section_labels),'level':0}],
                section_evidence={'kind':'observed_full_table_band','region_ref':ref(section)})
        f['local_grid_evidence']=deepcopy(evidence)
        for rr in f.get('answer_region_refs',[]):
            lookup[rr['block_id']]['structure'].update(scan_association_valid=True,scan_field_id=f['field_id'])
    fields[insertion:insertion]=new;reports['restored'].append(evidence)
    reports.setdefault('changes',[]).append({'evidence':deepcopy(evidence),'previous_fields':previous,
        'replacement_field_ids':[f['field_id'] for f in walk(new)]})
    return True

def reconcile_scanned_local_grids(fields, blocks, lookup):
    cells=[b for b in blocks if b.get('location',{}).get('type')=='pdf' and _closed(b)]
    header_cells=[b for b in blocks if b.get('location',{}).get('type')=='pdf' and header_closed(b,cells)]
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr']
    report={'version':VERSION,'restored':[],'protected':[]}
    taken=set()
    # Closed numbered matrix rows remain a complete question even when one
    # printed option caption is unreadable. Never normalize that caption.
    for header in cells:
        hr=row(header,cells)
        if len(hr)!=3 or any(not 15<box(c)[2]-box(c)[0]<85 or box(c)[3]-box(c)[1]>35 for c in hr):continue
        captions=[contents(c,texts) for c in hr]
        if any(len(ts)!=1 for ts in captions):continue
        known=[bool(CHOICE.fullmatch(ts[0]['text'].strip())) for ts in captions]
        if sum(known)<2 or all(known):continue  # existing full-caption path owns complete matrices
        if len({ts[0]['text'].strip().casefold() for ts,k in zip(captions,known) if k})!=sum(known):continue
        # Header ownership ends at the first occupied or missing physical cell
        # in each column. Merely matching x bounds on the same page is unsafe.
        column_bodies=[matrix_column_body(h,hr,cells,texts) for h in hr]
        for left in cells:
            if page(left)!=page(header) or box(left)[1]<max(box(h)[3] for h in hr)-3 or box(left)[2]-box(left)[0]<150:continue
            chain=row(left,cells)
            if len(chain)!=4 or any(not blank(c,texts) for c in chain[1:]):continue
            if any(abs(box(c)[0]-box(h)[0])>6 or abs(box(c)[2]-box(h)[2])>6 for c,h in zip(chain[1:],hr)):continue
            if any(c['block_id'] not in allowed for c,allowed in zip(chain[1:],column_bodies)):continue
            labels=contents(left,texts)
            if not labels or sum(b['text'].count('?')+b['text'].count('？') for b in labels)!=1:continue
            nums=[c for c in cells if source_cells_share_row(c,left) and len(ts:=contents(c,texts))==1 and NUMBER.fullmatch(ts[0]['text'])]
            if len(nums)!=1:continue
            # At least one additional numbered row in the same observed column
            # proves a matrix body; this does not authorize absent option text.
            peers=[]
            for c in cells:
                if page(c)!=page(left) or c==left or abs(box(c)[0]-box(left)[0])>=6 or abs(box(c)[2]-box(left)[2])>=6 or not contents(c,texts):continue
                other=row(c,cells)
                if len(other)!=4 or any(r['block_id'] not in allowed for r,allowed in zip(other[1:],column_bodies)):continue
                peer_numbers=[v for v in cells if source_cells_share_row(v,c) and len(ts:=contents(v,texts))==1 and NUMBER.fullmatch(ts[0]['text'])]
                if len(peer_numbers)==1:peers.append(c)
            if not peers:continue
            f=field(left,labels,chain[1:]);f.update(field_type_hint='single_select',options=[],review_status='needs_review',
                original_number=contents(nums[0],texts)[0]['text'].rstrip('.)'),number_refs=[ref(t) for t in contents(nums[0],texts)],
                answer_status='unresolved',pairing_validation='needs_review',validation_status='needs_review',auto_fill_eligible=False,
                review_reasons=['完整题干与三列答复格已定位；其中一列标题的原文读取不完整，不能推测选项含义。'])
            for h,ts,c,k in zip(hr,captions,chain[1:],known):
                f['options'].append({'refs':[ref(ts[0])],'text_original':ts[0]['text'],'selected':False if k else None,
                    'selection_source':'bounded_scan_column_empty' if k else 'bounded_scan_column_caption_unresolved','answer_region_refs':[ref(c)]})
            ev={'kind':'numbered_closed_matrix_with_partial_caption','label_cell':ref(left),'number_cell':ref(nums[0]),
                'header_cells':[ref(h) for h in hr],'response_cells':[ref(c) for c in chain[1:]],'unknown_caption_refs':[ref(ts[0]) for ts,k in zip(captions,known) if not k]}
            if install(fields,[f],[left],chain[1:],texts,lookup,ev,report):taken.add(left['block_id'])
    # Two-column record grid: an occupied parent cell spans the header and at
    # least two complete blank data rows. Reserved rows are not required data.
    for first in cells:
        headers=row(first,cells)
        if len(headers)!=2 or any(box(c)[3]-box(c)[1]>45 for c in headers):continue
        labels=[contents(c,texts) for c in headers]
        if any(not ts or sum(len(t['text']) for t in ts)>100 for ts in labels):continue
        rows=[_aligned_rows(c,cells) for c in headers]
        if not 2<=len(rows[0])==len(rows[1])<=8 or any(not _closed(c) or not blank(c,texts) for col in rows for c in col):continue
        if any(not source_cells_share_row(a,b) for a,b in zip(*rows)):continue
        parents=[c for c in cells if page(c)==page(first) and abs(box(c)[2]-box(first)[0])<5 and abs(box(c)[1]-box(first)[1])<8 and abs(box(c)[3]-box(rows[0][-1])[3])<9 and contents(c,texts)]
        if len(parents)!=1:continue
        parent=parents[0];parent_labels=contents(parent,texts)
        if any(NUMBER.fullmatch(t['text']) for t in parent_labels):continue
        p=field(parent,parent_labels,[]);p.update(field_type_hint='repeating_record',children=[],answer_status='blank',
            record_columns=[{'label':'\n'.join(t['text'] for t in ts),'label_ref':ref(ts[0]),'label_refs':[ref(t) for t in ts],'grid_col_start':i,'grid_col_end':i} for i,ts in enumerate(labels,1)],record_rows=[],record_context_values=[])
        for ri,pair in enumerate(zip(*rows),1):
            rowids=[]
            for ci,(h,ls,c) in enumerate(zip(headers,labels,pair),1):
                child=field(h,ls,[c],suffix=f':row:{c["block_id"]}',context=parent_labels)
                child.update(parent_question_id=p['field_id'],record_index=ri,record_column_index=ci,record_slot_status='reserved',
                    record_context_refs=[],
                    record_identity_status='ambiguous',record_identity_reason='原模板预留记录行，没有来源记录身份；不能把同一历史记录自动复制到所有空行。',
                    auto_fill_eligible=False,review_status='needs_review',review_reasons=['记录行的填写对象尚未确定，保留原行列位置。'])
                p['children'].append(child)
                rowids.append(child['field_id'])
            p['record_rows'].append({'row':ri,'field_ids':rowids,'reserved_blank_row':True})
        ev={'kind':'closed_parent_with_two_column_reserved_records','parent_cell':ref(parent),'header_cells':[ref(c) for c in headers],
            'rows':[[ref(c) for c in pair] for pair in zip(*rows)]}
        install(fields,[p],[parent]+headers,[c for col in rows for c in col],texts,lookup,ev,report)
    # Compound parent spanning repeated local rows and separately bordered
    # destination columns. Each destination keeps its own complete response.
    for parent in cells:
        pb=box(parent);pl=contents(parent,texts)
        if len(pl)<3 or pb[3]-pb[1]<65 or pb[2]-pb[0]>160:continue
        label_cells=sorted([c for c in cells if page(c)==page(parent) and abs(box(c)[0]-pb[2])<4 and box(c)[1]>=pb[1]-5 and box(c)[3]<=pb[3]+5 and contents(c,texts)],key=lambda c:box(c)[1])
        if not 2<=len(label_cells)<=5 or abs(box(label_cells[0])[1]-pb[1])>6 or abs(box(label_cells[-1])[3]-pb[3])>6:continue
        if any(abs(box(a)[3]-box(b)[1])>7 for a,b in zip(label_cells,label_cells[1:])):continue
        chains=[row(c,cells) for c in label_cells]
        if any(len(chain)!=3 or any(not blank(c,texts) for c in chain[1:]) for chain in chains):continue
        headers=[]
        for target in chains[0][1:]:
            hb=box(target)
            possible=[c for c in header_cells if page(c)==page(target) and box(c)[3]<hb[1] and box(c)[3]-box(c)[1]<35 and
                abs(box(c)[0]-hb[0])<5 and abs(box(c)[2]-hb[2])<5 and contents(c,texts) and target in _aligned_rows(c,cells)]
            if not possible:break
            headers.append(max(possible,key=lambda c:box(c)[3]))
        if len(headers)!=2 or not source_cells_share_row(*headers):continue
        p=field(parent,pl,[]);p.update(field_type_hint='compound',children=[],answer_status='blank',
            record_columns=[{'label':'\n'.join(t['text'] for t in contents(h,texts)),'label_ref':ref(contents(h,texts)[0]),'grid_col_start':i,'grid_col_end':i} for i,h in enumerate(headers,1)],record_rows=[],record_context_values=[])
        for ri,chain in enumerate(chains,1):
            ls=contents(chain[0],texts)
            rowids=[]
            for ci,(target,header) in enumerate(zip(chain[1:],headers),1):
                # The shared left caption names the complete record, not
                # subslots printed inside the blank destination. Keep all
                # required components in the question and its row role;
                # the destination column remains the independent object.
                hs=contents(header,texts);child=field(chain[0],pl+ls,[target],suffix=':'+target['block_id'],context=hs)
                child.update(parent_question_id=p['field_id'],field_type_hint='long_text',compound_requirement_refs=[ref(t) for t in pl],
                    response_object_refs=[ref(t) for t in hs],local_row=ri,local_column=ci,
                    whole_record_response=True,
                    record_context_refs=[ref(t) for t in ls],
                    record_index=ri,record_column_index=ci,record_context='\n'.join(t['text'] for t in ls),
                    review_status='model_proposed',review_reasons=[])
                p['children'].append(child)
                rowids.append(child['field_id'])
            p['record_rows'].append({'row':ri,'field_ids':rowids,'reserved_blank_row':False})
        ev={'kind':'closed_compound_parent_by_rows_and_destination_columns','parent_cell':ref(parent),'row_label_cells':[ref(c) for c in label_cells],
            'column_header_cells':[ref(c) for c in headers],'response_rows':[[ref(c) for c in chain[1:]] for chain in chains]}
        install(fields,[p],[parent]+label_cells,[c for chain in chains for c in chain[1:]],texts,lookup,ev,report)
    return report
