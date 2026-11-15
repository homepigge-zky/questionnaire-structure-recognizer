"""Observe V marks inside source-proved Yes/No/NA table cells, after the model.

This is not OCR. It neither modifies the extraction cache nor supplies altered
model input. Selection is accepted only from actual pixels in a closed cell
whose column and question-row relationships have independently been proved.
"""
from copy import deepcopy
import hashlib
import io
import math
from numbers import Real
from pathlib import Path
import re
import time

import fitz
import numpy as np
from PIL import Image

from .ocr import _two_stroke_tick, _internal_joint_fit_tick

VERSION='pdf-matrix-marks-v5-preserve-unknown-review-control'


def _blank_observation(pixels,sx,sy):
    import cv2
    interior=cv2.bitwise_and(pixels['ink'],pixels['mask'])
    ink_count=int(np.count_nonzero(interior));area=int(np.count_nonzero(pixels['mask']))
    if not area:return False,{'method':'insufficient_interior_pixels'}
    _,_,stats,_=cv2.connectedComponentsWithStats(interior,8);components=stats[1:]
    tiny=bool(ink_count/area<.005 and all(w<=sx and h<=sy and a<=sx*sy for _,_,w,h,a in components))
    return ink_count==0 or tiny,{'method':'zero_ink_or_disconnected_subpoint_specks','ink_pixels':ink_count,
        'component_count':len(components),'component_bounds_pixels':components.tolist(),
        'pixels_per_point':[sx,sy],'all_components_subpoint':tiny,'interior_ink_ratio':ink_count/area}


def _cell_pixels(image,polygon,scale):
    import cv2
    points=np.rint(polygon).astype(np.int32)
    if points.shape!=(4,2) or not cv2.isContourConvex(points):return None
    x,y,w,h=cv2.boundingRect(points)
    if x<0 or y<0 or x+w>image.width or y+h>image.height or min(w,h)<10*scale:return None
    # Native contours describe the inside of the printed rule. Retain source
    # pixels just outside those vertices when independently rechecking edges.
    margin=max(3,round(2*scale));x0=max(0,x-margin);y0=max(0,y-margin)
    w=min(image.width,x+w+margin)-x0;h=min(image.height,y+h+margin)-y0;x,y=x0,y0
    crop=np.asarray(image.convert('RGB'))[y:y+h,x:x+w]
    ink=cv2.threshold(cv2.cvtColor(crop,cv2.COLOR_RGB2GRAY),0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    local=points-[x,y];near=cv2.dilate(ink,np.ones((max(3,2*round(scale)+1),)*2,np.uint8));edges=[]
    for a,b in zip(local,np.roll(local,-1,axis=0)):
        count=max(2,int(np.linalg.norm(a-b))+1);xs=np.rint(np.linspace(a[0],b[0],count)).astype(int);ys=np.rint(np.linspace(a[1],b[1],count)).astype(int)
        edges.append(float((near[ys,xs]>0).mean()))
    if min(edges)<.9:return None
    mask=np.zeros((h,w),np.uint8);cv2.fillConvexPoly(mask,local,255);polygon_mask=mask.copy();pad=max(3,round(2.2*scale))
    mask=cv2.erode(mask,np.ones((2*pad+1,)*2,np.uint8),borderType=cv2.BORDER_CONSTANT,borderValue=0)
    return {'crop':crop,'ink':ink,'mask':mask,'polygon_mask':polygon_mask,'box':[x,y,x+w,y+h],'edge_coverage':edges}


def measure_cell_mark(image,polygon,scale,printed_boxes=()):
    import cv2
    cells=_cell_pixels(image,polygon,scale)
    if cells is None:return None
    x,y,x1,y1=cells['box'];ink=cells['ink'];mask=cells['mask']
    for box in printed_boxes:
        a,c=max(0,int(box[0]-x)),min(x1-x,int(np.ceil(box[2]-x)))
        b,d=max(0,int(box[1]-y)),min(y1-y,int(np.ceil(box[3]-y)))
        if a<c and b<d and np.count_nonzero(mask[b:d,a:c])>5:return None
    interior=cv2.bitwise_and(ink,mask);count,components,stats,_=cv2.connectedComponentsWithStats(interior,8)
    if count<2:return None
    total=int(np.count_nonzero(interior));index=1+int(np.argmax(stats[1:,4]));a,b,w,h,area=map(int,stats[index]);fraction=area/max(1,total)
    if fraction<.95 or area<12 or min(w,h)<3*scale:return None
    # Recheck the whole connected source stroke before erosion. A near-rule V
    # can enter the safety margin but cannot borrow any ink connected to the
    # printed boundary. The full component must remain strictly in the cell.
    _,original_components,original_stats,_=cv2.connectedComponentsWithStats(ink,8)
    original_ids=np.unique(original_components[components==index]);original_ids=original_ids[original_ids>0]
    if len(original_ids)!=1:return None
    original_index=int(original_ids[0]);a,b,w,h,whole_area=map(int,original_stats[original_index])
    safe_polygon=cv2.erode(cells['polygon_mask'],np.ones((3,3),np.uint8))
    if whole_area>area*1.5 or np.any((original_components==original_index)&(safe_polygon==0)):return None
    pad=max(3,round(scale));mark=np.where(original_components[b:b+h,a:a+w]==original_index,255,0).astype(np.uint8)
    mark=cv2.copyMakeBorder(mark,pad,pad,pad,pad,cv2.BORDER_CONSTANT,value=0)
    tick=_two_stroke_tick(mark,scale)
    if not tick:return None
    # Reuse the same strict evidence verifier as downstream consumers. When
    # the short arm is steep, fit the actually observed interior joint; never
    # relax the ordinary checkbox slope thresholds to accept a boolean.
    from ..validation.selection_evidence import verified_raster_tick_control
    if not verified_raster_tick_control({'source':'raster_two_stroke_tick','selected':True,'mark_evidence':tick}):
        tick=_internal_joint_fit_tick(mark)
        if not tick or not verified_raster_tick_control({'source':'raster_two_stroke_tick','selected':True,'mark_evidence':tick}):return None
    return {'method':VERSION,'cell_patch_sha256':hashlib.sha256(cells['crop'].tobytes()).hexdigest(),
            'cell_crop_box_pixels':cells['box'],'mark_box_pixels':[x+a,y+b,x+a+w,y+b+h],
            'cell_edge_coverage':cells['edge_coverage'],'interior_component_fraction':fraction,
            'tick_evidence':tick,'printed_text_checked':True,'whole_connected_stroke_checked':True}


def _candidates(blocks):
    from ..validation.scanned_local_tables import _column_choice_rows
    lookup={b['block_id']:b for b in blocks};fields=[]
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box']
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr']
    _column_choice_rows(fields,blocks,lookup,cells,texts)
    return fields,lookup


def _closed_polygon(block):
    """Missing native contour evidence is unproved, never an implicit cell."""
    structure=block.get('structure') or {}
    evidence=structure.get('closed_region_evidence')
    if not isinstance(evidence,dict):return None
    polygon=evidence.get('polygon')
    if not isinstance(polygon,(list,tuple)) or len(polygon)!=4:return None
    if any(not isinstance(p,(list,tuple)) or len(p)!=2 or
           any(not isinstance(v,Real) or isinstance(v,bool) or not math.isfinite(v) for v in p)
           for p in polygon):return None
    return polygon


def _middle_cell_polygon(left,right):
    lp,rp=_closed_polygon(left),_closed_polygon(right)
    if lp is None or rp is None:return None
    edge1=sorted(sorted(lp,key=lambda p:p[0])[2:],key=lambda p:p[1])
    edge2=sorted(sorted(rp,key=lambda p:p[0])[:2],key=lambda p:p[1])
    if not 10<edge2[0][0]-edge1[0][0]<=100 or max(abs(edge1[i][1]-edge2[i][1]) for i in [0,1])>8:return None
    return [edge1[0],edge2[0],edge2[1],edge1[1]]


def _recover_middle_cells(image,page_number,fields,lookup,sx,sy,checksum):
    import cv2
    from .models import make_block
    from ..validation.field_boundaries import ref
    new=[]
    for field in fields:
        opts=field['options']
        if len(opts)!=3 or opts[1]['answer_region_refs'] or len(opts[0]['answer_region_refs'])!=1 or len(opts[2]['answer_region_refs'])!=1:continue
        left=lookup[opts[0]['answer_region_refs'][0]['block_id']];right=lookup[opts[2]['answer_region_refs'][0]['block_id']]
        if left['location']['page']!=page_number or right['location']['page']!=page_number:continue
        polygon=_middle_cell_polygon(left,right)
        if not polygon:continue
        header=lookup[field['scan_column_header_refs'][1]['block_id']];hb=header['location']['bbox']
        if abs((polygon[1][0]-polygon[0][0])-(hb[2]-hb[0]))>10:continue
        pixels=_cell_pixels(image,[[x*sx,y*sy] for x,y in polygon],sx)
        if pixels is None:continue
        # An unlocated cell with printed content needs separate transcription.
        x0=min(x for x,y in polygon);x1=max(x for x,y in polygon);y0=min(y for x,y in polygon);y1=max(y for x,y in polygon)
        if any(b.get('text','').strip() and b.get('location',{}).get('page')==page_number and
               x0<sum(b['location']['bbox'][::2])/2<x1 and y0<sum(b['location']['bbox'][1::2])/2<y1 for b in lookup.values()):continue
        blank,blank_evidence=_blank_observation(pixels,sx,sy)
        if 'interior_ink_ratio' not in blank_evidence:continue
        location={**left['location'],'bbox':[x0,y0,x1,y1],'matrix_middle_region_from':[left['block_id'],right['block_id']]}
        block=make_block('ocr_text','',left.get('order',0),location,structure={
            'scan_region_kind':'text_box','scan_candidate':True,'source_method':'post_model_matrix_boundary',
            'blank_state':'blank' if blank else 'unreadable','content_state':'empty' if blank else 'unreadable',
            'write_requires_association':True,'interior_ink_ratio':blank_evidence['interior_ink_ratio'],'blank_observation':blank_evidence,
            'closed_region_evidence':{'method':'observed_matrix_middle_four_edges','polygon':polygon,'edge_ink_coverage':pixels['edge_coverage'],
                'source_sha256':checksum,'cell_patch_sha256':hashlib.sha256(pixels['crop'].tobytes()).hexdigest(),
                'neighbor_refs':[ref(left),ref(right)],'header_ref':ref(header)}}).model_dump()
        if block['block_id'] not in lookup:new.append(block)
    return new


def observe_matrix_choice_cells(path,native,*,ocr_enabled=False):
    if not ocr_enabled:return {'status':'disabled','version':VERSION,'selected':0}
    path=Path(path);checksum=hashlib.sha256(path.read_bytes()).hexdigest()
    if native.get('source_hash')!=checksum or native.get('file_type')!='PDF':raise ValueError('矩阵选择核对必须使用相同指纹的 PDF 原件')
    from .pdf_matrix_dividers import restore_observed_matrix_cells,reobserve_bounded_rule_gap
    started=time.perf_counter()
    restore_observed_matrix_cells(path,native,ocr_enabled=ocr_enabled)
    blocks=native['source_blocks'];local=deepcopy(blocks);fields,lookup=_candidates(local)
    originals={b['block_id']:b for b in blocks};by_page={};proofs=[];restored=[];state_checks=[]
    for field in fields:
        for option in field['options']:
            if option['selected'] is True or len(option['answer_region_refs'])!=1:continue
            region=lookup[option['answer_region_refs'][0]['block_id']]
            if _closed_polygon(region) is None:continue
            by_page.setdefault(region['location']['page'],[]).append((field,option,region))
    with fitz.open(path) as pdf:
        pages=set(by_page)|{lookup[f['label_refs'][0]['block_id']]['location']['page'] for f in fields if not f['options'][1]['answer_region_refs']}
        for page_number in sorted(pages):
            page=pdf[page_number-1];pix=page.get_pixmap(dpi=220,alpha=False);image=Image.open(io.BytesIO(pix.tobytes('png')))
            sx,sy=image.width/page.rect.width,image.height/page.rect.height
            additional=_recover_middle_cells(image,page_number,fields,lookup,sx,sy,checksum)
            for block in additional:
                blocks.append(block);originals[block['block_id']]=block;restored.append(block['block_id'])
            if additional:
                # A newly proved middle region may contain a small real mark;
                # it must enter this same source observation, never wait until
                # another run or be assumed empty from low total ink density.
                fresh_fields,fresh_lookup=_candidates(deepcopy(blocks));new_ids={b['block_id'] for b in additional}
                for fresh_field in fresh_fields:
                    for option in fresh_field['options']:
                        if option['selected'] is True or len(option['answer_region_refs'])!=1:continue
                        bid=option['answer_region_refs'][0]['block_id']
                        if bid in new_ids:by_page.setdefault(page_number,[]).append((fresh_field,option,fresh_lookup[bid]))
            text_boxes=[[v*(sx if i%2==0 else sy) for i,v in enumerate(b['location']['bbox'])] for b in blocks if b.get('text','').strip() and b.get('location',{}).get('page')==page_number]
            for field,option,region in by_page.get(page_number,[]):
                from ..source_matrix_choices import reviewed_matrix_state
                reviewed=reviewed_matrix_state(region,lookup)
                if isinstance(reviewed,bool) and region['structure']['matrix_choice_review_region']['source_hash']==checksum:
                    # An explicit source review is version/geometry-bound and
                    # must not be overwritten by another automatic ink pass.
                    continue
                polygon=_closed_polygon(region)
                if polygon is None:continue
                points=[[x*sx,y*sy] for x,y in polygon]
                measured=measure_cell_mark(image,points,sx,text_boxes)
                if not measured:
                    # Old native blank state used an area-density threshold.
                    # Even a clear V can have low density in a tall answer
                    # cell. Recheck actual pixels; unproved ink is unknown.
                    pixels=_cell_pixels(image,points,sx)
                    if pixels is None:pixels=reobserve_bounded_rule_gap(image,region,lookup,sx,sy)
                    blank,observation=_blank_observation(pixels,sx,sy) if pixels is not None else (False,{'method':'source_edges_unproved'})
                    observation.update(source_sha256=checksum,region_id=region['block_id'],page=page_number,
                        polygon=deepcopy(polygon),cell_patch_sha256=hashlib.sha256(pixels['crop'].tobytes()).hexdigest() if pixels else None)
                    original=originals[region['block_id']];prior=original.get('structure',{}).get('blank_state')
                    original.setdefault('structure',{}).update(blank_observation=observation,
                        blank_state='blank' if blank else 'unreadable',content_state='empty' if blank else 'unreadable',matrix_mark_prior_blank_state=prior)
                    if not blank:
                        from ..source_matrix_choices import retain_unknown_matrix_control
                        retain_unknown_matrix_control(original,region,option,observation,pixels,checksum,lookup)
                    state_checks.append({'region_id':region['block_id'],'prior':prior,'state':'blank' if blank else 'unreadable'})
                    continue
                header=region['structure']['scan_column_header_ref'];label=region['structure']['scan_matrix_label_region_ref']
                measured.update(source_sha256=checksum,region_id=region['block_id'],region_bbox=deepcopy(region['location']['bbox']),
                    page=page_number,polygon=deepcopy(polygon),header_ref=deepcopy(header),label_region_ref=deepcopy(label),caption_refs=deepcopy(option['refs']))
                original=originals[region['block_id']]
                prior=original.get('structure',{}).get('blank_state')
                original.setdefault('structure',{}).update(matrix_mark_observation=measured,content_state='selection',blank_state='filled',matrix_mark_prior_blank_state=prior)
                original['controls']=[{'type':'checkbox','source':'raster_tick_in_choice_cell','selected':True,
                                       'bbox':deepcopy(region['location']['bbox']),'mark_evidence':measured}]
                proofs.append({'region_id':region['block_id'],'page':page_number,'caption_refs':option['refs'],'cell_patch_sha256':measured['cell_patch_sha256']})
    report={'status':'complete','version':VERSION,'source_sha256':checksum,'candidate_count':sum(map(len,by_page.values())),
            'selected':len(proofs),'proofs':proofs,'restored_middle_cells':restored,'empty_or_unknown_checks':state_checks,
            'ocr_calls':0,'model_calls':0,'seconds':round(time.perf_counter()-started,4)}
    native.setdefault('metadata',{})['matrix_mark_observation']=report
    return report


def verified_matrix_cell_mark(region,lookup):
    from ..validation.selection_evidence import verified_raster_tick_control
    from ..validation.pdf_source_relations import source_cells_share_row,scan_label_inside
    s=region.get('structure',{});proof=s.get('matrix_mark_observation') or {};loc=region.get('location',{});box=loc.get('bbox',[])
    polygon=_closed_polygon(region)
    if polygon is None:return False
    # v5 adds unknown review controls; the strict tick and cell evidence from
    # v4 is unchanged and still checked in full below, never accepted by age.
    if (proof.get('method') not in {VERSION,'pdf-matrix-marks-v4-local-source-divider-observation'} or proof.get('region_id')!=region.get('block_id') or proof.get('region_bbox')!=box or
        proof.get('page')!=loc.get('page') or s.get('scan_region_kind')!='text_box' or not proof.get('printed_text_checked') or not proof.get('whole_connected_stroke_checked') or
        proof.get('polygon')!=polygon or
        not re.fullmatch('[0-9a-f]{64}',str(proof.get('source_sha256',''))) or not re.fullmatch('[0-9a-f]{64}',str(proof.get('cell_patch_sha256',''))) or
        len(proof.get('cell_edge_coverage',[]))!=4 or min(proof['cell_edge_coverage'])<.9 or proof.get('interior_component_fraction',0)<.95):return False
    header=lookup.get(proof.get('header_ref',{}).get('block_id'),{});hl=header.get('location',{});hb=hl.get('bbox',[])
    label=lookup.get(proof.get('label_region_ref',{}).get('block_id'),{});lb=label.get('location',{}).get('bbox',[])
    if len(box)!=4 or len(hb)!=4 or len(lb)!=4 or hl.get('page')!=loc.get('page') or label.get('location',{}).get('page')!=loc.get('page'):return False
    if not hb[3]<=box[1] or not box[0]-3<=(hb[0]+hb[2])/2<=box[2]+3:return False
    # In a sloping table the far NA cell is displaced vertically from the
    # question. Traverse real shared vertical edges, at most three columns;
    # do not widen a global same-row bounding-box tolerance.
    frontier=[label];seen={label.get('block_id')};connected=False
    for _ in range(3):
        adjacent=[b for b in lookup.values() if b.get('block_id') not in seen and b.get('location',{}).get('page')==loc.get('page')
                  and b.get('structure',{}).get('scan_region_kind')=='text_box' and
                  any(b.get('location',{}).get('bbox',[0])[0]>=a['location']['bbox'][2]-3 and source_cells_share_row(a,b) for a in frontier)]
        if region.get('block_id') in {b['block_id'] for b in adjacent}:connected=True;break
        seen.update(b['block_id'] for b in adjacent);frontier=adjacent
        if not frontier:break
    if not (connected or abs(lb[1]-box[1])<12 and abs(lb[3]-box[3])<12 and lb[2]<box[0]+3):return False
    refs=proof.get('caption_refs',[])
    if len(refs)!=1:return False
    r=refs[0];caption=lookup.get(r.get('block_id'),{});a,z=r.get('start'),r.get('end');text=caption.get('text','')
    if not isinstance(a,int) or not isinstance(z,int) or not 0<=a<z<=len(text) or not re.fullmatch(r'yes|no|n\s*/?\s*a',text[a:z],re.I):return False
    if caption.get('location',{}).get('page')!=loc.get('page') or not scan_label_inside(caption.get('location',{}).get('bbox',[]),hb):return False
    return verified_raster_tick_control({'source':'raster_two_stroke_tick','selected':True,'mark_evidence':proof.get('tick_evidence')})
