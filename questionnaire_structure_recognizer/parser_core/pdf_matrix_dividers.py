"""Re-observe matrix row contours from original pixels, retaining native evidence.

Adjacent measured label/response cells propose coordinates. They never prove a
cell: all four edges must separately exist in the source raster. In particular
a vertically merged response without a printed separator is never divided.
"""
from copy import deepcopy
import hashlib
import io
from pathlib import Path
import time

import fitz
import numpy as np
from PIL import Image

VERSION = 'matrix-source-edges-v3-shared-adjacent-response-rules'


def _box(b): return b['location']['bbox']
def _page(b): return b['location']['page']


def _edges(b):
    p=(b.get('structure',{}).get('closed_region_evidence') or {}).get('polygon')
    if not p or len(p)!=4:return None
    s=sorted(p,key=lambda p:p[0]);return sorted(s[:2],key=lambda p:p[1]),sorted(s[2:],key=lambda p:p[1])


def _plans(blocks):
    from ..validation.scan_matrix_structure import matrix_groups
    from ..validation.pdf_source_relations import source_text_inside_cell
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box' and _edges(b)]
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr']
    plans=[]
    for group in matrix_groups(cells,texts):
        if len(group)!=3:continue
        header_left=_box(group[0][0])[0];header_right=_box(group[-1][0])[2]
        for left in cells:
            lb=_box(left)
            if (_page(left)!=_page(group[0][0]) or lb[1]<_box(group[0][0])[3]-3 or
                not 80<lb[2]-lb[0]<500 or abs(lb[2]-header_left)>16 or
                not any(source_text_inside_cell(t,left,label=True) for t in texts if _page(t)==_page(left))):continue
            le=_edges(left)[1]
            # A separately bounded last-column cell supplies this same row's
            # far right endpoint. A tall merged cell is not an eligible anchor.
            anchors=[c for c in cells if _page(c)==_page(left) and abs(_box(c)[2]-header_right)<16 and
                     abs((_box(c)[2]-_box(c)[0])-(_box(group[-1][0])[2]-_box(group[-1][0])[0]))<8 and
                     abs((_box(c)[3]-_box(c)[1])-(le[1][1]-le[0][1]))<7 and
                     abs(_box(c)[1]-le[0][1])<10]
            if len(anchors)!=1:continue
            right=anchors[0];re=_edges(right)[1]
            # Column proportions come from the physical heading, not ordinal
            # pairing or arbitrary equally spaced boxes.
            def point(x,edge):
                ratio=(x-header_left)/(header_right-header_left)
                return [le[edge][0]+ratio*(re[edge][0]-le[edge][0]),le[edge][1]+ratio*(re[edge][1]-le[edge][1])]
            for header,caption in group:
                hb=_box(header);poly=[point(hb[0],0),point(hb[2],0),point(hb[2],1),point(hb[0],1)]
                bb=[min(p[0] for p in poly),min(p[1] for p in poly),max(p[0] for p in poly),max(p[1] for p in poly)]
                existing=[c for c in cells if _page(c)==_page(left) and all(abs(a-b)<4 for a,b in zip(_box(c),bb))]
                if existing:continue
                # The established middle-cell path already measures this
                # exact gap when both immediate neighbours exist. Preserve
                # its stable source IDs and observation audit.
                if header is group[1][0]:
                    first_box=[point(_box(group[0][0])[0],0)[0],point(_box(group[0][0])[2],0)[1],
                               point(_box(group[0][0])[2],1)[0],point(_box(group[0][0])[0],1)[1]]
                    if any(_page(c)==_page(left) and all(abs(a-b)<4 for a,b in zip(_box(c),first_box)) for c in cells):continue
                if any(t.get('location',{}).get('page')==_page(left) and
                       bb[0]+2<sum(_box(t)[::2])/2<bb[2]-2 and bb[1]+2<sum(_box(t)[1::2])/2<bb[3]-2 for t in texts):continue
                plans.append((left,right,header,poly,bb))
    return plans


def _tall_row_plans(blocks):
    """Propose a missing tall row from measured rules above and below it.

    The initial contour detector can omit all three large empty cells. A
    spanning section rule plus the next row's physical columns locates their
    boundaries; the caller must still verify every edge against source pixels.
    No section text or field vocabulary is used to decide an answer.
    """
    from ..validation.scan_matrix_structure import matrix_groups
    from ..validation.pdf_source_relations import source_text_inside_cell
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box' and _edges(b)]
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr']
    plans=[]
    def crossing(a,b,c,d):
        v=np.asarray(b)-a;w=np.asarray(d)-c
        try:t=np.linalg.solve(np.column_stack((v,-w)),np.asarray(c)-a)[0]
        except np.linalg.LinAlgError:return None
        return (np.asarray(a)+t*v).tolist()
    for group in matrix_groups(cells,texts):
        if len(group)!=3:continue
        first,last=_box(group[0][0]),_box(group[-1][0]);p=_page(group[0][0])
        local=[b for b in cells if _page(b)==p]
        for left in local:
            lb=_box(left);le=_edges(left)[1]
            if not (70<le[1][1]-le[0][1]<200 and abs(lb[2]-first[0])<16
                    and 100<lb[2]-lb[0]<500
                    and any(source_text_inside_cell(t,left,label=True) for t in texts if _page(t)==p)):continue
            bands=[b for b in local if _box(b)[0]<lb[0]+5 and _box(b)[2]>last[2]-8
                   and _box(b)[3]-_box(b)[1]<45 and abs(_edges(b)[1][1][1]-le[0][1])<8]
            if len(bands)!=1:continue
            band=bands[0];top=[_edges(band)[i][1] for i in (0,1)];anchors=[]
            for header,caption in group:
                hb=_box(header);candidates=[b for b in local if abs(_box(b)[1]-le[1][1])<8
                    and abs((_box(b)[2]-_box(b)[0])-(hb[2]-hb[0]))<10
                    and abs(sum(_box(b)[::2])/2-sum(hb[::2])/2)<16]
                candidates.sort(key=lambda b:abs(_edges(b)[0][0][1]-le[1][1]))
                if not candidates or (len(candidates)>1 and abs(_edges(candidates[0])[0][0][1]-_edges(candidates[1])[0][0][1])<2):break
                anchors.append(candidates[0])
            if len(anchors)!=3:continue
            polygons=[]
            for anchor in (anchors[0],anchors[2]):
                e=_edges(anchor);a=crossing(*e[0],*top);b=crossing(*e[1],*top)
                if a is None or b is None:break
                polygons.append([a,b,e[1][0],e[0][0]])
            if len(polygons)!=2:continue
            # Shared boundaries come from the adjacent observed columns;
            # extrapolating the middle cell independently can duplicate rules.
            polys=[polygons[0],[polygons[0][1],polygons[1][0],polygons[1][3],polygons[0][2]],polygons[1]]
            for (header,caption),anchor,poly in zip(group,anchors,polys):
                bb=[min(v[0] for v in poly),min(v[1] for v in poly),max(v[0] for v in poly),max(v[1] for v in poly)]
                if any(all(abs(a-b)<4 for a,b in zip(_box(c),bb)) for c in local):continue
                if not (10<bb[2]-bb[0]<100 and 65<bb[3]-bb[1]<210):continue
                if any(bb[0]+2<sum(_box(t)[::2])/2<bb[2]-2 and bb[1]+2<sum(_box(t)[1::2])/2<bb[3]-2
                       for t in texts if _page(t)==p):continue
                plans.append((left,anchor,header,poly,bb))
    return plans


def _adjacent_column_plans(blocks):
    """Recover a missing first choice cell from two measured response columns.

    A label contour may include a small scan notch and therefore has an
    inaccurate top corner. The other two choice cells independently retain
    the same printed rule. Their common line may propose the missing border;
    every resulting edge still has to pass the strict source-pixel verifier.
    """
    from ..validation.scan_matrix_structure import matrix_groups
    from ..validation.pdf_source_relations import source_text_inside_cell,source_cells_share_row
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box' and _edges(b)]
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr']
    plans=[]
    def crossing(a,b,c,d):
        v=np.asarray(b)-a;w=np.asarray(d)-c
        try:t=np.linalg.solve(np.column_stack((v,-w)),np.asarray(c)-a)[0]
        except np.linalg.LinAlgError:return None
        return (np.asarray(a)+t*v).tolist()
    for group in matrix_groups(cells,texts):
        if len(group)!=3:continue
        first=group[0][0];p=_page(first)
        for left in cells:
            lb=_box(left);le=_edges(left)[1]
            if (_page(left)!=p or lb[1]<_box(first)[3]-3 or not 80<lb[2]-lb[0]<500
                or abs(lb[2]-_box(first)[0])>16 or
                not any(source_text_inside_cell(t,left,label=True) for t in texts if _page(t)==p)):continue
            neighbors=[]
            for header,_ in group[1:]:
                hb=_box(header)
                matches=[c for c in cells if _page(c)==p and abs(_box(c)[2]-hb[2])<16
                    and abs((_box(c)[2]-_box(c)[0])-(hb[2]-hb[0]))<8
                    and abs((_box(c)[3]-_box(c)[1])-(le[1][1]-le[0][1]))<7
                    and abs(_box(c)[1]-le[0][1])<8]
                if len(matches)!=1:break
                neighbors.append(matches[0])
            if len(neighbors)!=2 or not source_cells_share_row(*neighbors):continue
            middle,last=neighbors;me=_edges(middle)[0];re=_edges(last)[1]
            a=crossing(*le,me[0],re[0]);z=crossing(*le,me[1],re[1])
            if a is None or z is None:continue
            if abs(a[1]-le[0][1])>4 or abs(z[1]-le[1][1])>4:continue
            poly=[a,me[0],me[1],z]
            bb=[min(v[0] for v in poly),min(v[1] for v in poly),max(v[0] for v in poly),max(v[1] for v in poly)]
            if any(_page(c)==p and all(abs(x-y)<4 for x,y in zip(_box(c),bb)) for c in cells):continue
            if any(bb[0]+2<sum(_box(t)[::2])/2<bb[2]-2 and bb[1]+2<sum(_box(t)[1::2])/2<bb[3]-2
                   for t in texts if _page(t)==p):continue
            marker=deepcopy(left)
            marker['_adjacent_column_boundary']={'method':'two_measured_response_column_rules',
                'column_refs':[{'block_id':b['block_id'],'start':0,'end':0,'slot_id':''} for b in neighbors]}
            plans.append((marker,last,first,poly,bb))
    return plans


def _pixels_with_single_print_gap(image,points,scale,left,right,edge_points):
    """A short scan dropout within a printed row rule, not a missing divider.

    The ordinary four-edge verifier stays strict. This separate proof requires
    one interior gap <= 6 PDF points, observed line on both sides and >= 98% of
    the full-width source row rule. An intentionally merged cell has a whole
    missing segment and cannot satisfy these checks.
    """
    import cv2
    points=np.rint(points).astype(np.int32)
    if points.shape!=(4,2) or not cv2.isContourConvex(points):return None
    ink=cv2.threshold(cv2.cvtColor(np.asarray(image.convert('RGB')),cv2.COLOR_RGB2GRAY),0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    near=cv2.dilate(ink,np.ones((max(3,2*round(scale)+1),)*2,np.uint8))
    def observed(a,b):
        n=max(2,int(np.linalg.norm(np.asarray(a)-b))+1)
        xs=np.rint(np.linspace(a[0],b[0],n)).astype(int);ys=np.rint(np.linspace(a[1],b[1],n)).astype(int)
        if min(xs)<0 or max(xs)>=image.width or min(ys)<0 or max(ys)>=image.height:return None
        return near[ys,xs]>0
    arrays=[observed(a,b) for a,b in zip(points,np.roll(points,-1,axis=0))]
    if any(a is None for a in arrays):return None
    weak=[i for i,a in enumerate(arrays) if a.mean()<.9]
    if len(weak)!=1 or weak[0] not in (0,2):return None
    index=weak[0];a=arrays[index];gaps=np.flatnonzero(~a)
    if (not len(gaps) or np.any(np.diff(gaps)!=1) or gaps[0]<len(a)*.15 or
        gaps[-1]>len(a)*.85 or len(gaps)>6*scale):return None
    full=observed(*edge_points[0 if index==0 else 1])
    if full is None or full.mean()<.98:return None
    x,y,w,h=cv2.boundingRect(points);margin=max(3,round(2*scale));x0=max(0,x-margin);y0=max(0,y-margin)
    x1=min(image.width,x+w+margin);y1=min(image.height,y+h+margin)
    crop=np.asarray(image.convert('RGB'))[y0:y1,x0:x1];local=points-[x0,y0]
    local_ink=ink[y0:y1,x0:x1];mask=np.zeros(local_ink.shape,np.uint8);cv2.fillConvexPoly(mask,local,255);polygon_mask=mask.copy()
    pad=max(3,round(2.2*scale));mask=cv2.erode(mask,np.ones((2*pad+1,)*2,np.uint8),borderType=cv2.BORDER_CONSTANT,borderValue=0)
    return {'crop':crop,'ink':local_ink,'mask':mask,'polygon_mask':polygon_mask,'box':[x0,y0,x1,y1],
            'edge_coverage':[float(a.mean()) for a in arrays],
            'bounded_rule_gap_evidence':{'edge':index,'gap_pixels':len(gaps),'gap_pdf_points':len(gaps)/scale,
                'full_row_coverage':float(full.mean()),'left_region':left['block_id'],'right_region':right['block_id'],
                'full_row_endpoints_pixels':edge_points[0 if index==0 else 1], 'method':'single_short_interior_dropout_with_continuous_row_rule'}}


def reobserve_bounded_rule_gap(image,region,lookup,sx,sy):
    proof=region.get('structure',{}).get('closed_region_evidence') or {}
    refs=proof.get('neighbor_refs',[]);poly=proof.get('polygon')
    if not proof.get('bounded_rule_gap_evidence') or len(refs)!=2 or not poly:return None
    left=lookup.get(refs[0]['block_id']);right=lookup.get(refs[1]['block_id'])
    if not left or not right or not _edges(left) or not _edges(right):return None
    le=_edges(left)[0];re=_edges(right)[1]
    full=[[[le[i][0]*sx,le[i][1]*sy],[re[i][0]*sx,re[i][1]*sy]] for i in (0,1)]
    return _pixels_with_single_print_gap(image,[[x*sx,y*sy] for x,y in poly],sx,left,right,full)


def observe_empty_native_rule(image,rule,sx,sy,checksum):
    """Prove a native answer underline's empty writing strip from pixels.

    OCR rectangles can overlap the next line despite a blank source strip.
    Remove only the measured long rule; any remaining ink prevents 'blank'.
    """
    import cv2
    b=_box(rule);x0=max(0,round(b[0]*sx));x1=min(image.width,round(b[2]*sx));y0=max(0,round(b[1]*sy));y1=min(image.height,round(b[3]*sy))
    crop=np.asarray(image.convert('RGB'))[y0:y1,x0:x1]
    if crop.size==0 or x1-x0<35*sx:return None
    ink=cv2.threshold(cv2.cvtColor(crop,cv2.COLOR_RGB2GRAY),0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    lines=cv2.HoughLinesP(ink,1,np.pi/1800,threshold=max(20,round((x1-x0)*.45)),minLineLength=(x1-x0)*.75,maxLineGap=round(3*sx))
    candidates=[]
    for arr in lines if lines is not None else []:
        a,c=sorted([arr[0][:2],arr[0][2:]],key=lambda p:p[0]);dx=c[0]-a[0]
        if dx<(x1-x0)*.75 or abs(c[1]-a[1])/dx>.04 or min(a[1],c[1])<len(ink)*.45:continue
        candidates.append((a,c))
    if not candidates:return None
    a,c=max(candidates,key=lambda z:z[1][0]-z[0][0]);yy=np.arange(len(ink))[:,None];xx=np.arange(ink.shape[1])[None,:]
    line=a[1]+(xx-a[0])*(c[1]-a[1])/(c[0]-a[0])
    mask=(yy>1*sy)&(yy<line-2.2*sy)&(xx>2*sx)&(xx<ink.shape[1]-2*sx)
    if mask.sum()<20*sx*sy or np.any((ink>0)&mask):return None
    return {'method':'observed_native_underline_empty_strip','source_sha256':checksum,'region_id':rule['block_id'],
            'page':_page(rule),'source_bbox':deepcopy(b),'cell_patch_sha256':hashlib.sha256(crop.tobytes()).hexdigest(),
            'line_endpoints_pixels':[(a+[x0,y0]).tolist(),(c+[x0,y0]).tolist()],
            'strip_pixels':int(mask.sum()),'strip_ink_pixels':0}


def restore_observed_matrix_cells(path,native,*,ocr_enabled=False):
    if not ocr_enabled:return {'status':'disabled','version':VERSION,'restored':[]}
    from .pdf_matrix_marks import _cell_pixels,_blank_observation
    from .models import make_block
    from ..validation.field_boundaries import ref
    path=Path(path);checksum=hashlib.sha256(path.read_bytes()).hexdigest()
    if native.get('source_hash')!=checksum or native.get('file_type')!='PDF':
        raise ValueError('矩阵区域修复需要同一指纹的 PDF 原件。')
    from ..validation.scan_local_prompt_regions import conditional_rule_plans
    started=time.perf_counter();blocks=native['source_blocks'];plans=_plans(blocks)+_tall_row_plans(blocks);by_page={};restored=[];superseded=[]
    plans.extend(_adjacent_column_plans(blocks))
    rule_ids={r['block_id'] for c,l,r,m in conditional_rule_plans(blocks)}
    for plan in plans:by_page.setdefault(_page(plan[0]),[]).append(plan)
    with fitz.open(path) as pdf:
        for page_number,local in sorted(by_page.items()):
            page=pdf[page_number-1];pix=page.get_pixmap(dpi=220,alpha=False)
            image=Image.open(io.BytesIO(pix.tobytes('png')));sx=image.width/page.rect.width;sy=image.height/page.rect.height
            for rule in blocks:
                s=rule.get('structure',{})
                if (rule['block_id'] not in rule_ids or _page(rule)!=page_number or s.get('scan_region_kind')!='write_line' or
                    s.get('line_style')!='solid' or s.get('blank_state')=='blank'):continue
                proof=observe_empty_native_rule(image,rule,sx,sy,checksum)
                if proof:
                    s['prior_rule_blank_state']=s.get('blank_state');s['blank_state']='blank';s['content_state']='empty';s['native_rule_blank_observation']=proof
            for left,right,header,poly,bb in local:
                if any(_page(b)==page_number and b.get('structure',{}).get('scan_region_kind')=='text_box'
                       and b.get('structure',{}).get('matrix_source_edge_version')
                       and all(abs(a-z)<4 for a,z in zip(_box(b),bb)) for b in blocks):continue
                pixels=_cell_pixels(image,[[x*sx,y*sy] for x,y in poly],sx)
                following_row=abs(_box(right)[1]-_edges(left)[1][1][1])<8 and _box(right)[1]>_box(left)[1]+60
                adjacent_columns=left.get('_adjacent_column_boundary')
                if pixels is None and not following_row and not adjacent_columns:
                    le=_edges(left)[0];re=_edges(right)[1]
                    full=[[[le[i][0]*sx,le[i][1]*sy],[re[i][0]*sx,re[i][1]*sy]] for i in (0,1)]
                    pixels=_pixels_with_single_print_gap(image,[[x*sx,y*sy] for x,y in poly],sx,left,right,full)
                if pixels is None:continue
                blank,observation=_blank_observation(pixels,sx,sy)
                patch=hashlib.sha256(pixels['crop'].tobytes()).hexdigest()
                proof={'method':'observed_matrix_local_four_edges','polygon':poly,'edge_ink_coverage':pixels['edge_coverage'],
                       'source_sha256':checksum,'cell_patch_sha256':patch,'neighbor_refs':[ref(left),ref(right)],'header_ref':ref(header)}
                if following_row:proof['boundary_construction']='spanning_section_rule_and_following_row_column_edges'
                if adjacent_columns:proof['boundary_geometry']=deepcopy(adjacent_columns)
                if pixels.get('bounded_rule_gap_evidence'):proof['bounded_rule_gap_evidence']=pixels['bounded_rule_gap_evidence']
                observation.update(source_sha256=checksum,page=page_number,polygon=deepcopy(poly),cell_patch_sha256=patch)
                location={**deepcopy(header['location']),'bbox':bb,'matrix_source_edge_refs':[left['block_id'],right['block_id'],header['block_id']]}
                region=make_block('ocr_text','',left.get('order',0),location,structure={
                    'scan_region_kind':'text_box','scan_candidate':True,'source_method':'post_model_matrix_boundary',
                    'blank_state':'blank' if blank else 'unreadable','content_state':'empty' if blank else 'unreadable',
                    'write_requires_association':True,'closed_region_evidence':proof,'blank_observation':observation,
                    'matrix_source_edge_version':VERSION}).model_dump()
                if any(b['block_id']==region['block_id'] for b in blocks):continue
                observation['region_id']=region['block_id'];blocks.append(region);restored.append(region['block_id'])
    # Retain every old block and original rectangle. Only a contour crossing
    # multiple now individually measured rows can be retired as an active
    # target; source text and explicit controls are never rewritten here.
    children=[b for b in blocks if b.get('structure',{}).get('matrix_source_edge_version')
              and (b.get('structure',{}).get('closed_region_evidence') or {}).get('source_sha256')==checksum]
    for old in blocks:
        s=old.get('structure',{});bb=old.get('location',{}).get('bbox',[])
        if s.get('scan_region_kind')!='text_box' or old in children or old.get('text') or old.get('controls') or len(bb)!=4:continue
        contained=[b for b in children if _page(b)==_page(old) and abs(_box(b)[0]-bb[0])<4 and abs(_box(b)[2]-bb[2])<4
                   and bb[1]-4<=_box(b)[1] and _box(b)[3]<=bb[3]+4 and _box(b)[3]-_box(b)[1]<bb[3]-bb[1]-8]
        if len(contained)<2:continue
        s['original_scan_region_kind']='text_box';s['scan_region_kind']='superseded_text_box'
        s['superseded_by_observed_cells']={'version':VERSION,'source_sha256':checksum,'child_refs':[ref(b) for b in contained]}
        superseded.append(old['block_id'])
    report={'status':'complete','version':VERSION,'source_sha256':checksum,'restored':restored,'superseded':superseded,
            'ocr_calls':0,'model_calls':0,'seconds':round(time.perf_counter()-started,4)}
    native.setdefault('metadata',{})['matrix_source_edges']=report
    return report
