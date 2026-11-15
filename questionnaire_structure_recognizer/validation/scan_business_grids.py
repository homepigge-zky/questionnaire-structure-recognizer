"""Recover reserved records and parallel option groups from closed source cells.

No names, dates, years or selections are inferred. All response cells must be
observed and empty; occupied and human-edited local grids are left untouched.
"""
from copy import deepcopy
import re
from .field_boundaries import ref
from .pdf_furniture_ownership import _closed
from .pdf_source_relations import source_cells_share_row
from .scan_matrix_structure import _aligned_rows, matrix_groups
from .scan_closed_local_grids import box, page, contents, row, blank, field, install, walk, shares_horizontal_border, header_closed

VERSION='scan-business-grids-v1-physical-record-panels-and-response-columns'


def _norm(items):return re.sub(r'\s+','',''.join(x['text'] for x in items)).casefold()


def verified_adjacent_cell(cell,cells):
    if _closed(cell):return True
    proof=cell.get('structure',{}).get('closed_region_evidence') or {}
    if proof.get('method')!='observed_adjacent_four_rules_v1':return False
    seeds=[c for c in cells if page(c)==page(cell) and box(c)==proof.get('seed_cell_bbox') and _closed(c)]
    rule=proof.get('observed_horizontal_rule')
    if len(seeds)!=1 or not isinstance(rule,list) or len(rule)!=4 or rule[2]<=rule[0]:return False
    checked=deepcopy(cell);checked['structure']['closed_region_evidence']['method']='observed_four_sided_polygon_v1'
    below=box(cell)[1]>box(seeds[0])[1]
    upper,lower=(seeds[0],cell) if below else (cell,seeds[0])
    if not _closed(checked) or not shares_horizontal_border(upper,lower,max(box(seeds[0])[0],box(cell)[0]),min(box(seeds[0])[2],box(cell)[2])):return False
    poly=proof['polygon']
    edges=[(a,b) for a,b in zip(poly,poly[1:]+poly[:1]) if abs(a[0]-b[0])>.7*(box(cell)[2]-box(cell)[0])]
    if len(edges)!=2:return False
    left,right=sorted(edges,key=lambda e:sum(p[1] for p in e)/2)[-1 if below else 0]
    # The measured outer rule, not a projected row height, closes this cell.
    for x,y in (left,right):
        line_y=rule[1]+(x-rule[0])*(rule[3]-rule[1])/(rule[2]-rule[0])
        if not rule[0]-3<=x<=rule[2]+3 or abs(y-line_y)>3:return False
    return True


def _column_path(header,headers,cells,texts,content_lookup):
    cells=[c for c in cells if page(c)==page(header)]
    current=header;lane=box(header);accepted={};previous_band=None;seen={header['block_id']}
    for _ in range(len(cells)):
        following=[]
        for c in cells:
            cb=box(c)
            if c['block_id'] in seen or page(c)!=page(header):continue
            labels=content_lookup[c['block_id']]
            narrow=abs(cb[0]-lane[0])<6 and abs(cb[2]-lane[2])<6
            band=(cb[0]<box(headers[0])[0]-150 and cb[2]>box(headers[-1])[2]-6 and cb[3]-cb[1]<40
                  and 0<sum(len(t['text']) for t in labels)<100 and not any(re.search(r'[?？]',t['text']) for t in labels))
            if (narrow or band) and shares_horizontal_border(current,c,max(lane[0],cb[0]),min(lane[2],cb[2])):
                following.append((c,band))
        if len(following)!=1:break
        c,band=following[0];seen.add(c['block_id'])
        if band:previous_band=c
        elif c['structure'].get('blank_state')=='blank' and not content_lookup[c['block_id']]:accepted[c['block_id']]=previous_band;lane=box(c)
        else:break
        current=c
    return accepted


def _parent_record(parent,labels,columns):
    p=field(parent,labels,[])
    p.update(field_type_hint='repeating_record',children=[],answer_status='blank',record_rows=[],record_context_values=[],
             record_columns=[{'label':'\n'.join(t['text'] for t in ts),'label_refs':[ref(t) for t in ts],
                              'label_ref':ref(ts[0]),'grid_col_start':i,'grid_col_end':i} for i,ts in enumerate(columns,1)])
    return p


def _append_record(p,headers,labels,regions,parent_labels,record_index,**extra):
    rowids=[]
    for ci,(h,ls,c) in enumerate(zip(headers,labels,regions),1):
        child=field(h,ls,[c],suffix=':record:'+c['block_id'],context=parent_labels)
        child.update(parent_question_id=p['field_id'],record_index=record_index,record_column_index=ci,
                     record_slot_status='reserved',record_identity_status='ambiguous',record_context_refs=[],
                     record_identity_reason='预留记录位置没有已填写对象，保留空白，不复制历史记录。',
                     auto_fill_eligible=False,review_status='needs_review',
                     review_reasons=['记录行的填写对象尚未确定，保留原行列位置。'],**extra)
        p['children'].append(child);rowids.append(child['field_id'])
    p['record_rows'].append({'row':record_index,'field_ids':rowids,'reserved_blank_row':True})


def reconcile_business_grids(fields,blocks,lookup):
    base_cells=[b for b in blocks if b.get('location',{}).get('type')=='pdf' and _closed(b)]
    cells=[b for b in blocks if b.get('location',{}).get('type')=='pdf' and verified_adjacent_cell(b,base_cells)]
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('type')=='pdf']
    report={'version':VERSION,'restored':[],'protected':[]}
    used=set()
    content_lookup={c['block_id']:contents(c,texts) for c in cells}
    # Numbered question rows can continue through actual full-width section
    # bands. Follow shared physical borders in each response column; a later
    # occupied narrow header ends the chain and cannot borrow earlier options.
    for group in matrix_groups(cells,texts):
        if len(group)!=3:continue
        headers=[x[0] for x in group];captions=[x[1] for x in group]
        allowed=[_column_path(h,headers,cells,texts,content_lookup) for h in headers]
        for label_cell in cells:
            if (page(label_cell)!=page(headers[0]) or box(label_cell)[2]-box(label_cell)[0]<150
                    or not content_lookup[label_cell['block_id']]
                    or abs(box(label_cell)[2]-box(headers[0])[0])>18):continue
            chain=row(label_cell,cells)
            if len(chain)!=4:continue
            if any(c['block_id'] not in col or not blank(c,texts) for c,col in zip(chain[1:],allowed)):continue
            labels=contents(label_cell,texts)
            if not labels:continue
            from .scan_ruled_text_order import cell_reading_order, separate_question_prompts
            labels=cell_reading_order(labels,label_cell)
            if separate_question_prompts(labels):continue
            wanted_labels={t['block_id'] for t in labels};wanted_regions={c['block_id'] for c in chain[1:]}
            if any({rr['block_id'] for rr in f.get('label_refs',[])}==wanted_labels and
                   {rr['block_id'] for rr in f.get('answer_region_refs',[])}==wanted_regions and
                   [o.get('text_original') for o in f.get('options',[])]==[t['text'] for t in captions]
                   and f.get('pairing_validation')=='valid' for f in walk(fields)):
                continue
            numbers=[c for c in cells if source_cells_share_row(c,label_cell) and
                     len(ts:=contents(c,texts))==1 and re.fullmatch(r'\d+(?:\.\d+)*[.)]?',ts[0]['text'].strip())]
            if len(numbers)!=1:continue
            f=field(label_cell,labels,chain[1:])
            f.update(field_type_hint='single_select',boundary_evidence='closed_scan_column_choice_row',
                     original_number=contents(numbers[0],texts)[0]['text'].rstrip('.)'),
                     number_refs=[ref(t) for t in contents(numbers[0],texts)],
                     scan_column_header_refs=[ref(t) for t in captions],options=[
                         {'refs':[ref(t)],'text_original':t['text'],'selected':False,
                          'selection_source':'bounded_scan_column_empty','answer_region_refs':[ref(c)]}
                         for t,c in zip(captions,chain[1:])])
            evidence={'kind':'numbered_matrix_across_closed_section_bands','version':VERSION,
                      'label_cell':ref(label_cell),'number_cell':ref(numbers[0]),'header_cells':[ref(h) for h in headers],
                      'response_cells':[ref(c) for c in chain[1:]],
                      'preceding_bands':[ref(col[c['block_id']]) for col,c in zip(allowed,chain[1:]) if col[c['block_id']] is not None]}
            install(fields,[f],[label_cell,numbers[0]],chain[1:],texts,lookup,evidence,report)
    # A repeated sequence of header columns forms parallel record panels,
    # not multiple answers for a single first-column question.
    for first in cells:
        headers=row(first,cells)
        if not 4<=len(headers)<=8 or any(box(h)[3]-box(h)[1]>35 for h in headers):continue
        labels=[contents(h,texts) for h in headers]
        if any(not ls or len(_norm(ls))>80 for ls in labels):continue
        width=next((n for n in range(2,len(headers)//2+1) if len(headers)%n==0 and
                    all(_norm(labels[i])==_norm(labels[i%n]) for i in range(len(headers)))),None)
        if width is None:continue
        columns=[_aligned_rows(h,cells) for h in headers]
        n=len(columns[0])
        if not 2<=n<=12 or any(len(col)!=n or any(not blank(c,texts) for c in col) for col in columns):continue
        if any(not source_cells_share_row(a,b) for responses in zip(*columns) for a,b in zip(responses,responses[1:])):continue
        parents=[c for c in cells if page(c)==page(first) and abs(box(c)[2]-box(first)[0])<5 and
                 abs(box(c)[1]-box(first)[1])<8 and abs(box(c)[3]-box(columns[0][-1])[3])<9 and contents(c,texts)]
        if len(parents)!=1:continue
        parent=parents[0];pl=contents(parent,texts)
        if parent['block_id'] in used or all(re.fullmatch(r'\d+[.)]?',t['text'].strip()) for t in pl):continue
        p=_parent_record(parent,pl,labels[:width]);ri=0
        for panel in range(len(headers)//width):
            for row_index in range(n):
                ri+=1;sl=slice(panel*width,(panel+1)*width)
                _append_record(p,headers[sl],labels[sl],[col[row_index] for col in columns[sl]],pl,ri,
                               physical_panel_index=panel+1,physical_row_index=row_index+1)
        evidence={'kind':'side_by_side_record_panels','version':VERSION,'parent_cell':ref(parent),
                  'header_cells':[ref(h) for h in headers], 'panel_columns':width,
                  'response_columns':[[ref(c) for c in col] for col in columns]}
        if install(fields,[p],[parent]+headers,[c for col in columns for c in col],texts,lookup,evidence,report):used.add(parent['block_id'])
    # Transposed records: each explicitly printed row label is one data
    # attribute and each physically separated blank column is one record.
    for parent in cells:
        pb=box(parent);pl=contents(parent,texts)
        if parent['block_id'] in used or not pl or pb[3]-pb[1]<30 or pb[2]-pb[0]>220:continue
        # Destination-specific columns (for example factory vs headquarters)
        # are a compound form, not interchangeable reserved record slots.
        if any(f.get('physical_label_region_ref',{}).get('block_id')==parent['block_id'] and
               any(child.get('response_object_refs') for child in walk(f.get('children',[])))
               for f in walk(fields)):continue
        hs=sorted([h for h in cells if page(h)==page(parent) and abs(box(h)[0]-pb[2])<5 and
                   box(h)[1]>=pb[1]-8 and box(h)[3]<=pb[3]+8 and contents(h,texts)],key=lambda h:box(h)[1])
        if not 2<=len(hs)<=4 or abs(box(hs[0])[1]-pb[1])>8 or abs(box(hs[-1])[3]-pb[3])>8:continue
        chains=[row(h,cells) for h in hs]
        count=len(chains[0])
        if not 3<=count<=7 or any(len(ch)!=count or any(not blank(c,texts) for c in ch[1:]) for ch in chains):continue
        if any(abs(box(a)[3]-box(b)[1])>8 for ch1,ch2 in zip(chains,chains[1:]) for a,b in zip(ch1,ch2)):continue
        labels=[contents(h,texts) for h in hs]
        if any(len(_norm(ls))>60 for ls in labels):continue
        p=_parent_record(parent,pl,labels)
        for ci in range(1,count):_append_record(p,hs,labels,[ch[ci] for ch in chains],pl,ci,physical_column_index=ci)
        evidence={'kind':'transposed_record_grid','version':VERSION,'parent_cell':ref(parent),
                  'label_cells':[ref(h) for h in hs],'response_rows':[[ref(c) for c in ch[1:]] for ch in chains]}
        if install(fields,[p],[parent]+hs,[c for ch in chains for c in ch[1:]],texts,lookup,evidence,report):used.add(parent['block_id'])
    # Repeated Yes/No panels. Each caption is tied to its own two measured
    # response columns; a missing final label never becomes an extra question.
    for group in matrix_groups(cells,texts):
        if len(group)!=2:continue
        yes,no=[p[0] for p in group];captions=[p[1] for p in group]
        heads=[c for c in cells if source_cells_share_row(c,yes) and contents(c,texts)]
        if len(heads)!=1:continue
        header=heads[0];hb=box(header)
        # Require a second adjacent panel with the same independent header.
        peers=[c for c in cells if c is not header and page(c)==page(header) and
               abs(sum(box(c)[1::2])-sum(hb[1::2]))<35 and _norm(contents(c,texts))==_norm(contents(header,texts))]
        if not peers:continue
        yesrows=_aligned_rows(yes,cells);norows=_aligned_rows(no,cells)
        if not 2<=len(yesrows)==len(norows)<=12:continue
        if any(not source_cells_share_row(a,b) or not blank(a,texts) or not blank(b,texts) for a,b in zip(yesrows,norows)):continue
        bodies=[c for c in cells if page(c)==page(header) and abs(box(c)[0]-hb[0])<5 and abs(box(c)[2]-hb[2])<5 and
                box(c)[1]>=hb[3]-8 and box(c)[3]<=max(box(yesrows[-1])[3],box(norows[-1])[3])+10 and contents(c,texts)]
        if not bodies:continue
        candidates=[]
        for ri,(a,b) in enumerate(zip(yesrows,norows),1):
            local=[];owners=[]
            for c in bodies:
                for t in contents(c,texts):
                    # OCR center inside the actual row interval, extrapolated
                    # only across its immediately adjacent label cell.
                    y=sum(box(t)[1::2])/2
                    slope=(sum(box(b)[1::2])-sum(box(a)[1::2]))/2/max(1,(sum(box(b)[::2])-sum(box(a)[::2]))/2)
                    dx=sum(box(t)[::2])/2-sum(box(a)[::2])/2
                    if box(a)[1]+slope*dx-2<=y<=box(a)[3]+slope*dx+2:
                        local.append(t);owners.append(c)
            if not local:continue
            local.sort(key=lambda t:(box(t)[1],box(t)[0]));lc=owners[0]
            if any(c is not lc for c in owners):continue
            f=field(lc,local,[a,b],suffix=':choice:'+a['block_id'],context=contents(header,texts))
            f.update(field_type_hint='single_select',boundary_evidence='closed_scan_column_choice_row',
                     scan_column_header_refs=[ref(c) for c in captions],options=[
                         {'refs':[ref(caption)],'text_original':caption['text'],'selected':False,
                          'selection_source':'bounded_scan_column_empty','answer_region_refs':[ref(region)]}
                         for caption,region in zip(captions,(a,b))])
            candidates.append(f)
        if not candidates:continue
        evidence={'kind':'parallel_yes_no_matrix','version':VERSION,'label_header':ref(header),
                  'option_headers':[ref(yes),ref(no)],'label_cells':[ref(c) for c in bodies],
                  'response_rows':[[ref(a),ref(b)] for a,b in zip(yesrows,norows)]}
        install(fields,candidates,[header,yes,no]+bodies,yesrows+norows,texts,lookup,evidence,report)
    return report
