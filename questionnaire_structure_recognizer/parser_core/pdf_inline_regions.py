"""Measure bounded response strips in original PDF pixels; never run OCR."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import time
import re
import fitz
import numpy as np
from PIL import Image

VERSION='pdf-inline-regions-v4-polygon-bounded-conditional-tail'


def _option_dotted_plans(blocks):
    from ..validation.scan_closed_local_grids import contents
    from ..validation.pdf_furniture_ownership import _closed
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('type')=='pdf']
    plans=[]
    for cell in blocks:
        if cell.get('location',{}).get('type')!='pdf' or not _closed(cell):continue
        labels=contents(cell,texts)
        if len(labels)!=1:continue
        label=labels[0];cb=cell['location']['bbox'];lb=label['location']['bbox']
        if (re.search(r'\((?:please\s+)?specify\)\s*[.·…]*$',label['text'],re.I)
                and cb[2]-lb[2]>35 and cb[3]-cb[1]<60):
            plans.append((cell,label,labels,None,'measure_dotted_option_tail'))
    return plans


def _observe_dotted_tail(image,cell,label,sx,sy):
    """A sequence of small printed dots after a source instruction, not a border."""
    import cv2
    cb=cell['location']['bbox'];lb=label['location']['bbox']
    bounds=[lb[2]+1,lb[1],cb[2]-4,min(cb[3]-3,lb[3]+4)]
    x0,y0,x1,y1=[round(v*s) for v,s in zip(bounds,(sx,sy,sx,sy))]
    crop=image[y0:y1,x0:x1]
    if not crop.size:return None
    ink=cv2.threshold(cv2.cvtColor(crop,cv2.COLOR_RGB2GRAY),0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    count,_,stats,centers=cv2.connectedComponentsWithStats(ink)
    dots=[]
    for i in range(1,count):
        x,y,w,h,area=stats[i];cx,cy=centers[i]
        if .25<=w/sx<=3 and .25<=h/sy<=3 and .1<=area/(sx*sy)<=5:
            dots.append((float((x0+cx)/sx),float((y0+cy)/sy),float((y0+y)/sy)))
    dots.sort()
    if len(dots)<6:return None
    gaps=np.diff([d[0] for d in dots]);median=float(np.median(gaps))
    if not 2<=median<=7 or max(abs(gaps-median))>1.5 or max(d[1] for d in dots)-min(d[1] for d in dots)>3:return None
    if dots[0][0]>lb[2]+12 or dots[-1][0]-dots[0][0]<25:return None
    bbox=[dots[0][0],lb[1]+.5,dots[-1][0],min(d[2] for d in dots)-.6]
    if bbox[3]-bbox[1]<5:return None
    return bbox,{'kind':'observed_dotted_option_tail','dot_centers':[[d[0],d[1]] for d in dots],
                 'dot_count':len(dots),'median_gap_points':median}


def _inner_rule_plans(blocks):
    from ..validation.scan_inline_response_regions import _number_labels
    from ..validation.scan_closed_local_grids import box,contents
    from ..validation.pdf_source_relations import source_text_inside_cell
    from ..validation.pdf_furniture_ownership import _closed
    from ..validation.scan_ruled_text_order import cell_reading_order
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('type')=='pdf']
    candidates=[]
    for cell in blocks:
        if cell.get('location',{}).get('type')!='pdf' or not _closed(cell):continue
        cb=box(cell);labels=cell_reading_order(contents(cell,texts),cell)
        conditional=bool(labels and re.match(r'^if\s+yes\s*[:,]?\s*(?:please\s+)?(?:specify|provide|name|list)\b',labels[0]['text'],re.I))
        if (cb[2]-cb[0]<180 or not 20<cb[3]-cb[1]<100 or not 1<=len(labels)<=3
            or (len(_number_labels(cell,texts))!=1 and not conditional)
            or not (conditional or re.match(r'^(?:name|describe|provide|specify|list)\b|^(?:请|说明|列出)',labels[0]['text'],re.I))
            or any(re.search(r'[?？]',x['text']) for x in labels)):continue
        if max(box(t)[3] for t in labels)>cb[3]-7:continue
        if any(b.get('structure',{}).get('scan_region_kind')=='write_line' and
               b['structure'].get('blank_state')=='blank' and source_text_inside_cell(b,cell)
               for b in blocks):continue
        candidates.append((cell,labels[-1],labels))
    return candidates


def _observe_inner_rule(image,cell,labels,sx,sy):
    """A second short rule inside a cell, distinct from the cell border.

    Both endpoints must terminate inside the source polygon. Cropping a long
    table edge or merely finding an empty lower strip is insufficient.
    """
    import cv2
    cb=cell['location']['bbox'];top=max(t['location']['bbox'][3] for t in labels)+1
    polygon=cell['structure']['closed_region_evidence']['polygon']
    sides=sorted(polygon,key=lambda p:p[0]);left=sorted(sides[:2],key=lambda p:p[1]);right=sorted(sides[2:],key=lambda p:p[1])
    source=np.float32([[p[0]*sx,p[1]*sy] for p in [left[0],right[0],right[1],left[1]]])
    width=round((right[0][0]-left[0][0])*sx);height=round((left[1][1]-left[0][1])*sy)
    if min(width,height)<10:return None
    matrix=cv2.getPerspectiveTransform(source,np.float32([[0,0],[width,0],[width,height],[0,height]]))
    inverse=np.linalg.inv(matrix);crop=cv2.warpPerspective(image,matrix,(width+1,height+1))
    ink=cv2.threshold(cv2.cvtColor(crop,cv2.COLOR_RGB2GRAY),0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    horizontal=cv2.morphologyEx(ink,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(round(55*sx),1)))
    contours,_=cv2.findContours(horizontal,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    candidates=[]
    for contour in contours:
        x,y,w,h=map(int,cv2.boundingRect(contour))
        if w<70*sx or h>3*sy or x>25*sx or x+w>width-15*sx or y<height*.6 or y>height-round(sy):continue
        # The underline may touch the lower border in a skewed scan. Inspect
        # its upper contour independently, where the short right endpoint is
        # visibly white, rather than treating the joined lower contour as ink.
        corridor=ink[y:y+min(3,h),x:x+w]
        beyond=ink[y:y+min(3,h),x+w+round(3*sx):min(width,x+w+round(12*sx))]
        if not corridor.size or not beyond.size or (corridor>0).any(axis=0).mean()<.95 or (beyond>0).any(axis=0).mean()>.15:continue
        mapped=cv2.perspectiveTransform(np.float32([[[x,y],[x+w,y]]]),inverse)[0]
        points=[[float(a/sx),float(b/sy)] for a,b in mapped]
        candidates.append(points)
    if len(candidates)!=1:return None
    points=candidates[0]
    # Keep the writing region above the measured short underline, leaving the
    # underline and the lower table boundary outside the blank measurement.
    bbox=[points[0][0],top,points[1][0],min(p[1] for p in points)-.8]
    if bbox[3]-bbox[1]<5:return None
    return bbox,{'kind':'independent_short_inner_rule','endpoints':points,'edge_coverage_minimum':.95,
                 'right_endpoint_white_span_points':[3,12]}


def _observe_label_tail_rule(image,cell,label,sx,sy):
    """Read a short rule after a printed label; a full cell border is excluded."""
    import cv2
    cb=cell['location']['bbox'];lb=label['location']['bbox']
    x0,y0,x1,y1=[round(v*s) for v,s in zip(cb,(sx,sy,sx,sy))]
    crop=image[y0:y1,x0:x1]
    if not crop.size:return None
    ink=cv2.threshold(cv2.cvtColor(crop,cv2.COLOR_RGB2GRAY),0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    horizontal=cv2.morphologyEx(ink,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(round(35*sx),1)))
    contours,_=cv2.findContours(horizontal,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    matches=[]
    for contour in contours:
        x,y,w,h=cv2.boundingRect(contour);left=(x+x0)/sx;right=(x+x0+w)/sx;top=(y+y0)/sy
        if (lb[2]-3<=left<=lb[2]+20 and right<cb[2]-15 and w/sx>=40 and h/sy<4
            and lb[3]-4<top<lb[3]+7):matches.append((left,right,top))
    if len(matches)!=1:return None
    left,right,top=matches[0]
    bbox=[max(left,lb[2]+2),lb[1]+1,right,top-.8]
    if bbox[3]-bbox[1]<5:return None
    return bbox,{'kind':'independent_label_tail_rule','endpoints':[[left,top],[right,top]]}


def observe_inline_response_regions(path,native,*,ocr_enabled=False):
    if not ocr_enabled:return {'status':'disabled','version':VERSION,'restored':[]}
    from ..validation.scan_inline_response_regions import tail_region_plans
    from ..validation.field_boundaries import ref
    from .pdf_matrix_marks import _blank_observation
    import cv2
    started=time.perf_counter();path=Path(path);checksum=hashlib.sha256(path.read_bytes()).hexdigest()
    if native.get('source_hash')!=checksum or native.get('file_type')!='PDF':
        raise ValueError('填写尾部核对必须使用相同指纹的 PDF 原件。')
    plans=tail_region_plans(native['source_blocks']);by_page={};restored=[];rejected=[]
    for plan in plans:
        mode='measure_label_tail_rule' if plan[1]['text'].strip().endswith('.') else None
        by_page.setdefault(plan[0]['location']['page'],[]).append((*plan,mode))
    for cell,label,labels in _inner_rule_plans(native['source_blocks']):
        by_page.setdefault(cell['location']['page'],[]).append((cell,label,labels,None,'measure_inner_rule'))
    for plan in _option_dotted_plans(native['source_blocks']):
        by_page.setdefault(plan[0]['location']['page'],[]).append(plan)
    from ..validation.scan_parallel_forms import caption_body_plans, optional_area_plans
    from ..validation.scan_inline_response_regions import conditional_tail_plans
    for plan in caption_body_plans(native['source_blocks'])+optional_area_plans(native['source_blocks'])+conditional_tail_plans(native['source_blocks']):
        by_page.setdefault(plan[0]['location']['page'],[]).append(plan)
    with fitz.open(path) as pdf:
        for page_number,local in sorted(by_page.items()):
            page=pdf[page_number-1];pix=page.get_pixmap(dpi=220,alpha=False)
            image=Image.open(io.BytesIO(pix.tobytes('png')));sx=image.width/page.rect.width;sy=image.height/page.rect.height
            rgb=np.asarray(image.convert('RGB'))
            # Threshold the complete printed page. Otsu on an almost-white
            # slot would classify faint compression variation as handwriting.
            threshold,_=cv2.threshold(cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY),0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
            for cell,label,labels,bbox,inner_rule in local:
                if inner_rule=='measure_inner_rule':
                    measured=_observe_inner_rule(rgb,cell,labels,sx,sy)
                    if measured is None:continue
                    bbox,inner_rule=measured
                elif inner_rule=='measure_label_tail_rule':
                    measured=_observe_label_tail_rule(rgb,cell,label,sx,sy)
                    if measured is None:continue
                    bbox,inner_rule=measured
                elif inner_rule=='measure_dotted_option_tail':
                    measured=_observe_dotted_tail(rgb,cell,label,sx,sy)
                    if measured is None:continue
                    bbox,inner_rule=measured
                x0,y0,x1,y1=[round(v*s) for v,s in zip(bbox,(sx,sy,sx,sy))]
                crop=rgb[y0:y1,x0:x1]
                if crop.size==0:continue
                ink=cv2.threshold(cv2.cvtColor(crop,cv2.COLOR_RGB2GRAY),threshold,255,cv2.THRESH_BINARY_INV)[1]
                mask=np.zeros(ink.shape,np.uint8);px,py=round(2.2*sx),round(2.2*sy)
                if len(ink)<=2*py or ink.shape[1]<=2*px:continue
                mask[py:-py,px:-px]=255
                blank,measurement=_blank_observation({'ink':ink,'mask':mask},sx,sy)
                proof={'method':'source_pixel_blank_tail_v1','source_sha256':checksum,'region_id':cell['block_id'],
                    'page':page_number,'region_bbox':deepcopy(cell['location']['bbox']),'region_text':cell.get('text',''),
                    'slot_bbox':deepcopy(bbox),'tail_label_ref':ref(label),'label_text':label['text'],
                    'label_bbox':deepcopy(label['location']['bbox']),'cell_patch_sha256':hashlib.sha256(crop.tobytes()).hexdigest(),
                    'source_label_snapshots':[{'ref':ref(t),'text':t['text'],'bbox':deepcopy(t['location']['bbox'])} for t in labels],
                    'pixel_crop':[x0,y0,x1,y1],'interior_margin_points':2.2,'page_ink_threshold':threshold,'measurement':measurement,'blank':blank}
                if inner_rule:proof['inner_rule_evidence']=inner_rule
                proof['proof_digest']=hashlib.sha256(json.dumps(proof,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                if not blank:rejected.append(proof);continue
                sid='pdf-tail:'+hashlib.sha256((checksum+cell['block_id']+label['block_id']).encode()).hexdigest()[:20]
                slot={'slot_id':sid,'kind':'scan_observed_blank_tail','start':0,'end':0,'bbox':deepcopy(bbox),
                    'label':label['text'],'label_refs':[ref(label)],'tail_label_ref':ref(label),'expected_text':'',
                    'blank':True,'answer_status':'blank','source_sha256':checksum,'blank_observation':proof,
                    'protected_label_refs':[ref(t) for t in labels],'write_method':'pdf_standard_only'}
                slots=cell.setdefault('structure',{}).setdefault('logical_slots',[])
                old=next((s for s in slots if s.get('slot_id')==sid),None)
                if old is None:slots.append(slot)
                elif old!=slot:raise ValueError('同一填写位置的已有原图核对记录不一致。')
                restored.append({'region_id':cell['block_id'],'slot_id':sid,'proof':proof})
    report={'status':'complete','version':VERSION,'restored':restored,'rejected':rejected,'source_sha256':checksum,
            'ocr_calls':0,'model_calls':0,'seconds':round(time.perf_counter()-started,4)}
    native.setdefault('metadata',{})['inline_source_regions']=report
    return report
