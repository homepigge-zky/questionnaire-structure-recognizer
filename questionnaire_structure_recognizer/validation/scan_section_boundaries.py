"""Resolve source-proved section bands without erasing their original contents."""
from copy import deepcopy
import re
from .field_boundaries import ref
from .pdf_furniture_ownership import _closed, _protected, _box as valid_box
from .pdf_source_relations import source_text_inside_cell, source_cells_share_row
from .scan_closed_local_grids import box,page,contents,shares_horizontal_border,walk,row

VERSION='scan-section-boundaries-v7-explicit-connected-table-captions'


def _spanning_partition(cells,band):
    """Verified neighboring cell edges must cover the full section width.

    Individual columns may have any width. A gap, overlapping alternatives,
    or a single undivided neighbor cannot establish a divided table row.
    The caller has already verified each cell's shared horizontal border.
    """
    if len(cells)<2:return False
    spans=sorted((box(c)[0],box(c)[2]) for c in cells);bb=box(band)
    if abs(spans[0][0]-bb[0])>5 or abs(spans[-1][1]-bb[2])>5:return False
    return all(abs(left[1]-right[0])<=4 for left,right in zip(spans,spans[1:]))


def _partition_cell(cell):
    if not valid_box(cell):return False
    if _closed(cell):return True
    proof=cell.get('structure',{}).get('closed_region_evidence') or {}
    if proof.get('method')=='observed_matrix_local_four_edges':
        s=cell.get('structure',{});edges=proof.get('edge_ink_coverage') or []
        if not (s.get('source_method')=='post_model_matrix_boundary' and s.get('matrix_source_edge_version')
                and re.fullmatch(r'[0-9a-f]{64}',proof.get('source_sha256',''))
                and re.fullmatch(r'[0-9a-f]{64}',proof.get('cell_patch_sha256',''))
                and len(edges)==4 and all(type(e) in (int,float) and .9<=e<=1 for e in edges)
                and proof.get('neighbor_refs') and proof.get('header_ref')):return False
        checked=deepcopy(cell)
        checked['structure']['source_method']='raster_geometry'
        checked['structure']['closed_region_evidence']['method']='observed_four_sided_polygon_v1'
        return _closed(checked)
    if proof.get('method')!='observed_inner_rule_and_adjacent_rows_v1':return False
    bb=box(cell);seed=proof.get('seed_cell_bbox');rule=proof.get('observed_horizontal_rule')
    if not isinstance(seed,list) or len(seed)!=4 or not isinstance(rule,list) or len(rule)!=4:return False
    if any(type(x) not in (int,float) for x in seed+rule) or rule[2]<=rule[0]:return False
    if abs(seed[0]-bb[0])>3 or abs(seed[2]-bb[2])>3 or seed[1]>bb[1]+3 or seed[3]<bb[3]-3:return False
    checked=deepcopy(cell);checked['structure']['closed_region_evidence']['method']='observed_four_sided_polygon_v1'
    if not _closed(checked):return False
    # The observed splitting rule must extend across this same source column
    # and coincide with one of the retained top/bottom polygon edges.
    polygon=proof['polygon']
    def y(x):return rule[1]+(x-rule[0])*(rule[3]-rule[1])/(rule[2]-rule[0])
    edges=[(a,b) for a,b in zip(polygon,polygon[1:]+polygon[:1]) if abs(a[0]-b[0])>.7*(bb[2]-bb[0])]
    return any(rule[0]<=min(a[0],b[0])+3 and rule[2]>=max(a[0],b[0])-3
        and all(abs(p[1]-y(p[0]))<=3 for p in (a,b)) for a,b in edges)


def reconcile_scan_section_boundaries(fields,blocks,lookup,*,missing_only=False):
    cells=[b for b in blocks if b.get('location',{}).get('type')=='pdf' and _partition_cell(b)]
    texts=[b for b in blocks if b.get('location',{}).get('type')=='pdf' and b.get('text','').strip() and valid_box(b)]
    all_fields=list(walk(fields));protected_ids=set()
    def protect(items,inherited=False):
        for f in items:
            yes=inherited or _protected(f)
            if yes:protected_ids.update(r['block_id'] for key in ('label_refs','answer_value_refs','answer_region_refs') for r in f.get(key,[]))
            protect(f.get('children',[]),yes)
    protect(fields)
    proven={}

    # The full-width cell has actual borders shared with separately divided
    # rows above and below. This is not a first-column/blank-space heuristic.
    for band in cells:
        bb=box(band);width=bb[2]-bb[0];height=bb[3]-bb[1]
        local=[c for c in cells if page(c)==page(band)]
        if width<280 or width/max(1,height)<12 or height>35:continue
        labels=contents(band,texts)
        if not labels or len(labels)>3 or sum(len(t['text']) for t in labels)>100:continue
        if any(re.search(r'[?？:：_□☐☑☒]|\.{3,}',t['text']) or t['block_id'] in protected_ids for t in labels):continue
        narrow=[c for c in local if c is not band]
        above=[c for c in narrow if shares_horizontal_border(c,band,max(box(c)[0],bb[0]),min(box(c)[2],bb[2]))]
        below=[c for c in narrow if shares_horizontal_border(band,c,max(box(c)[0],bb[0]),min(box(c)[2],bb[2]))]
        # Preserve the previously verified multiple narrow-cell path. The
        # extra path permits unequal columns only when they form a measured
        # partition covering both sides of the whole source band.
        legacy=(len([c for c in above if box(c)[2]-box(c)[0]<width*.7])>=2 and
                len([c for c in below if box(c)[2]-box(c)[0]<width*.7])>=2)
        if not legacy and (not _spanning_partition(above,band) or not _spanning_partition(below,band)):continue
        # The band must span the measured grid, including the numbered left
        # margin. A large response cell to the right does not qualify.
        if min(box(c)[0] for c in above+below)<bb[0]-5 or max(box(c)[2] for c in above+below)>bb[2]+5:continue
        evidence={'version':VERSION,'kind':'closed_spanning_section_between_divided_rows','band_ref':ref(band),
                  'above_cells':[ref(c) for c in above],'below_cells':[ref(c) for c in below]}
        for t in labels:proven[t['block_id']]=('section_header',evidence)

    # Explicit PART captions followed immediately by a physical numbered table
    # header. The suffix remains original section text, never an answer.
    for t in texts:
        match=re.fullmatch(r'\s*PART\s*[-–]?\s*[A-Z0-9]+\s*[:：]\s*(\S.{5,100})',t['text'],re.I)
        if not match or not match[1].isupper() or t['block_id'] in protected_ids:continue
        headers=[]
        for cell in cells:
            cb=box(cell)
            if page(cell)!=page(t) or not -1<cb[1]-box(t)[3]<40 or cb[3]-cb[1]>40:continue
            local=contents(cell,texts)
            if any(re.fullmatch(r'\s*S\.?\s*No\.?\s*',x['text'],re.I) for x in local):
                adjacent=[c for c in cells if source_cells_share_row(cell,c) and contents(c,texts)]
                body=[c for c in cells if shares_horizontal_border(cell,c,cb[0],cb[2]) and
                      len(ts:=contents(c,texts))==1 and re.fullmatch(r'\d+[.)]?',ts[0]['text'].strip())
                      and len(row(c,cells))>=3]
                if adjacent:headers.append((cell,adjacent[0]))
                elif body:headers.append((cell,body[0]))
            elif (cb[2]-cb[0]>280 and len(local)==1 and
                  re.search(r'put.*(?:square|sguare)\s*boxes',local[0]['text'],re.I)):
                below=[c for c in cells if page(c)==page(cell) and 0<box(c)[1]-cb[1]<100 and
                       len(row(c,cells))==2 and contents(c,texts)]
                if len(below)>=2:headers.append((cell,below[0]))
        if headers:
            proven[t['block_id']]=('section_header',{'version':VERSION,'kind':'explicit_part_before_numbered_table_header',
                                                   'header_cells':[ref(c) for c in headers[0]]})

    # Earlier furniture validation may flag a different cell in a repeated
    # title band. Independently closed cells still prove their own contents;
    # the damaged neighbor remains unchanged and retains its diagnostic.
    for t in texts:
        suspicion=t.get('structure',{}).get('furniture_ownership_suspicion') or {}
        if t['block_id'] in protected_ids:continue
        for candidate in suspicion.get('proofs',[]):
            if candidate.get('kind')!='repeated_occupied_document_title_band':continue
            peers=[lookup[bid] for bid in candidate.get('peer_title_ids',[]) if bid in lookup]
            if len({page(p) for p in peers})<3:continue
            own=[lookup[bid] for bid in candidate.get('cell_ids',[]) if bid in lookup and _closed(lookup[bid]) and source_text_inside_cell(t,lookup[bid],label=True)]
            if len(own)!=1:continue
            local=contents(own[0],texts)
            if not any(re.search(r'questionnaire|问卷|問卷',x['text'],re.I) for x in local):continue
            proven[t['block_id']]=('repeated_header_footer',{'version':VERSION,'kind':'independent_closed_title_cell',
                'cell_ref':ref(own[0]),'original_band_evidence':deepcopy(candidate)})

    retained=[];retired=set()
    def apply(items):
        kept=[]
        for f in items:
            labels=f.get('label_refs') or f.get('question_refs') or []
            bids={r['block_id'] for r in labels}
            if not _protected(f) and bids and bids<=set(proven) and not f.get('children'):
                values=f.get('answer_value_refs',[])+f.get('answer_refs',[])
                if values and not all(r['block_id'] in bids for r in values):
                    kept.append(f);continue
                # The only retired existing values are literal pieces of the
                # same proven section caption produced by an inline splitter.
                if values and f.get('boundary_evidence')!='ocr_inline_delimiter_with_row_borders':
                    kept.append(f);continue
                retained.append({'role':proven[next(iter(bids))][0],'refs':[ref(lookup[bid]) for bid in sorted(bids)],
                                 'text_original':'\n'.join(lookup[bid]['text'] for bid in sorted(bids)),
                                 'reason':'原始实体边界证明为章节或重复文档标题，保留原文供预览与导出。',
                                 'diagnostic':{'version':VERSION,'retired_field':deepcopy(f),
                                               'proofs':[deepcopy(proven[bid][1]) for bid in sorted(bids)]}})
                retired.update(bids)
                continue
            if not _protected(f):apply(f.get('children',[]))
            kept.append(f)
        items[:]=kept
    if not missing_only:apply(fields)
    for bid,(role,proof) in proven.items():
        if bid in protected_ids:continue
        lookup[bid].setdefault('structure',{}).update(resolved_layout_role=role,source_section_evidence=deepcopy(proof))
    # Earlier matrix validation may have proved a nested band that this pass
    # cannot rediscover. Keep those independently source-backed headings; an
    # ordinary nearby paragraph or a model-only section assignment is not one.
    headings=[b for b in texts if b.get('structure',{}).get('resolved_layout_role')=='section_header'
              and (b['structure'].get('source_section_evidence') or b['structure'].get('matrix_section_evidence'))]
    # Fragments can be grouped only by a section band proved again in this
    # pass. A cached band ID, similar wording or nearby glyphs do not establish
    # one section. Keep the original blocks and their separate source ranges.
    grouped={}
    for h in headings:
        current=proven.get(h['block_id'])
        proof=current[1] if current and current[0]=='section_header' else {}
        band=lookup.get(proof.get('band_ref',{}).get('block_id'),{})
        verified=(proof.get('kind')=='closed_spanning_section_between_divided_rows'
                  and _partition_cell(band) and source_text_inside_cell(h,band,label=True))
        key=('band',band['block_id']) if verified else ('text',h['block_id'])
        group=grouped.setdefault(key,{'fragments':[],'band':band if verified else None,
                                     'proof':proof if verified else {}})
        group['fragments'].append(h)
    for group in grouped.values():
        fragments=group['fragments']
        # A short measured title band may include its number as a separate OCR
        # block. Use left-to-right only when every glyph overlaps the same line;
        # genuinely separate lines keep top-to-bottom reading order.
        same_line=max(box(b)[1] for b in fragments)<min(box(b)[3] for b in fragments)
        fragments.sort(key=(lambda b:box(b)[0]) if same_line else (lambda b:(box(b)[1],box(b)[0])))
        group['refs']=[ref(b) for b in fragments]
        group['text']=' '.join(b['text'] for b in fragments)
    def heading_order(g):
        return (page(g['fragments'][0]),max(box(b)[3] for b in g['fragments']),min(box(b)[0] for b in g['fragments']))
    def is_part(g):return bool(re.match(r'\s*PART\s*[-–]?\s*[A-Z0-9]+\s*[:：]',g['text'],re.I))
    def precedes(g,first):
        hp=page(g['fragments'][0]);fp=page(first)
        if hp<fp:return True
        if hp!=fp:return False
        if max(box(b)[3] for b in g['fragments'])<box(first)[1]:return True
        # Sloping printed rules can put the first row's glyph box above the
        # caption's lowest glyph. The actual band/row common edge and the
        # caption's physical containment, not a larger bbox tolerance, decide.
        band=g['band']
        if not band:return False
        for rr in g['proof'].get('below_cells',[]):
            cell=lookup.get(rr['block_id'],{})
            if (_partition_cell(cell) and source_text_inside_cell(first,cell,label=True)
                    and shares_horizontal_border(band,cell,max(box(band)[0],box(cell)[0]),min(box(band)[2],box(cell)[2]))):
                return True
        return False
    for f in walk(fields):
        if _protected(f) or not f.get('label_refs'):continue
        if missing_only and (f.get('section_text_original') or f.get('section_refs') or
                any(r['block_id'] in protected_ids for r in f['label_refs'])):continue
        first=lookup.get(f['label_refs'][0]['block_id']);fb=valid_box(first) if first else None
        if not fb:continue
        previous=[g for g in grouped.values() if precedes(g,first)]
        if not previous:continue
        h=max(previous,key=heading_order)
        parts=[g for g in previous if is_part(g) and heading_order(g)<heading_order(h)]
        path=([max(parts,key=heading_order)] if parts and not is_part(h) else [])+[h]
        for rr in f['label_refs']:
            if rr['block_id'] in lookup:lookup[rr['block_id']].setdefault('structure',{})['section_path_block_ids']=[[b['block_id'] for b in g['fragments']] for g in path]
        f.update(section_refs=deepcopy(h['refs']),section_text_original=h['text'],section_path=[
            {'section_id':'source-band:'+g['fragments'][0]['block_id'],
             'refs':deepcopy(g['refs']),'text_original':g['text'],'level':i} for i,g in enumerate(path)])
    from .scanned_table_sections import reconcile_scanned_table_sections
    reconcile_scanned_table_sections(fields, blocks, lookup)
    return retained
