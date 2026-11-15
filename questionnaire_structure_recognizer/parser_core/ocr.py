"""Local OCR used only for PDF pages without a usable native text layer."""
from __future__ import annotations
import os
import re
from functools import lru_cache
from pathlib import Path
import numpy as np
from PIL import Image

RASTER_GEOMETRY_VERSION='scan-geometry-v13-observed-right-exit-joint'


def _closed_cell_interior(ink, contour, scale):
    """Measure a ruled cell inside its observed polygon, never its bbox.

    A sloping border crosses an axis-aligned crop and used to make genuinely
    empty cells look occupied. A small handwriting notch may touch a border;
    its convex hull is accepted only if all four edges are independently
    inked. Neither this helper nor OCR boxes can invent a missing border.
    """
    import cv2
    x,y,w,h=cv2.boundingRect(contour)
    tolerance=min(.025*cv2.arcLength(contour,True),.12*min(w,h))
    poly=cv2.approxPolyDP(contour,tolerance,True)
    hull_used=False
    if len(poly)!=4 or not cv2.isContourConvex(poly):
        poly=cv2.approxPolyDP(cv2.convexHull(contour),tolerance,True)
        hull_used=True
    if len(poly)!=4 or not cv2.isContourConvex(poly):return None
    corners=sorted((tuple(point[0]) for point in poly),key=lambda point:point[0])
    left=sorted(corners[:2],key=lambda point:point[1]);right=sorted(corners[2:],key=lambda point:point[1])
    if not (abs(left[0][0]-left[1][0])<max(3,scale+h*.08) and
            abs(right[0][0]-right[1][0])<max(3,scale+h*.08) and
            abs(left[0][1]-right[0][1])<max(3,scale+w*.03) and
            abs(left[1][1]-right[1][1])<max(3,scale+w*.03)):
        return None
    if cv2.contourArea(contour)/max(1,cv2.contourArea(poly))<=.9:return None
    local=(poly-np.array([[[x,y]]])).astype(np.int32)
    crop=ink[y:y+h,x:x+w]
    # Sample actual pixels close to each side, including narrow faded edges.
    # This also blocks a U-shaped outline from becoming a closed rectangle.
    radius=max(1,min(round(scale*3.5),round(min(w,h)*.12)))
    near=cv2.dilate(crop,np.ones((2*radius+1,)*2,np.uint8))
    scores=[]
    for i in range(4):
        a=local[i,0];b=local[(i+1)%4,0]
        count=max(2,int(np.linalg.norm(a-b))+1)
        xs=np.clip(np.rint(np.linspace(a[0],b[0],count)).astype(int),0,w-1)
        ys=np.clip(np.rint(np.linspace(a[1],b[1],count)).astype(int),0,h-1)
        scores.append(float(np.mean(near[ys,xs]>0)))
    # A closed contour already proves its boundary; the straight polygon is
    # only its approximation. Requiring every approximate edge pixel rejects
    # slightly bowed scanned rules. Hull repairs need the extra line proof.
    if hull_used and min(scores)<.8:return None
    mask=np.zeros((h,w),np.uint8)
    # Use the full observed boundary, including slight scan curvature; the
    # four-corner approximation proves local layout but is not the ink mask.
    cv2.drawContours(mask,[(contour-np.array([[[x,y]]])).astype(np.int32)],-1,255,-1)
    pad=max(3,round(scale*1.5))
    mask=cv2.erode(mask,np.ones((2*pad+1,2*pad+1),np.uint8),borderType=cv2.BORDER_CONSTANT,borderValue=0)
    count=int(np.count_nonzero(mask))
    if not count:return None
    interior=(crop>0)&(mask>0)
    return {'bbox':[x,y,x+w,y+h],'mask':mask,'ink':interior,
            'density':float(np.count_nonzero(interior)/count),
            'proof':{'method':'observed_four_sided_polygon_v1','polygon':poly[:,0].tolist(),
                'edge_ink_coverage':[round(v,4) for v in scores],
                'edge_ink_search_radius_pixels':radius,
                'handwriting_notch_hull':hull_used,'eroded_interior_pixels':count}}


def _source_text_in_cell(cell, recognized):
    """A wide OCR box alone cannot turn an empty physical cell into a value."""
    x,y,x1,y1=cell['bbox'];inside=cell['ink'];mask=cell['mask']
    for item in recognized:
        b=item['bbox']
        a=max(0,int(b[0]-x));c=min(x1-x,int(np.ceil(b[2]-x)))
        top=max(0,int(b[1]-y));bottom=min(y1-y,int(np.ceil(b[3]-y)))
        if c<=a or bottom<=top:continue
        area=max(1,(b[2]-b[0])*(b[3]-b[1]))
        if np.count_nonzero(mask[top:bottom,a:c])/area<=.25:continue
        # Both overlapping source text and actual non-border ink are needed.
        if np.count_nonzero(inside[top:bottom,a:c])>=3:return True
    return False


def _recover_adjacent_closed_cells(ink,cells,recognized,scale,hough):
    """Recover a neighboring row only when all four raster rules exist.

    Ink touching a rule can split the inside contour. An existing cell supplies
    a local column alignment, not permission to fill an arbitrary blank area:
    a separately observed horizontal rule and both actual side rules must
    still enclose the candidate. Values remain unread until source association.
    """
    import cv2
    proposed=[]
    near=cv2.dilate(ink,np.ones((max(3,2*round(scale*1.2)+1),)*2,np.uint8))
    segments=[tuple(map(int,item[0])) for item in hough if abs(int(item[0][2])-int(item[0][0]))>80*scale] if hough is not None else []
    for seed in cells:
        proof=seed.get('closed_region_evidence') or {};poly=proof.get('polygon')
        if not poly or len(poly)!=4:continue
        ordered=sorted(poly,key=lambda p:p[0]);left=sorted(ordered[:2],key=lambda p:p[1]);right=sorted(ordered[2:],key=lambda p:p[1])
        xl,yl=left[0];xr,yr=right[0];width=xr-xl
        if width<80*scale:continue
        candidates=[]
        for x1,y1,x2,y2 in segments:
            if x2==x1 or abs((y2-y1)/(x2-x1))>.04:continue
            if min(x1,x2)>xl+4*scale or max(x1,x2)<xr-4*scale:continue
            a=(y2-y1)/(x2-x1);topl=y1+(xl-x1)*a;topr=y1+(xr-x1)*a
            if not (6*scale<yl-topl<48*scale and 6*scale<yr-topr<48*scale):continue
            if abs((yl-topl)-(yr-topr))>3*scale:continue
            candidates.append((yl-topl,topl,topr,[x1,y1,x2,y2]))
        for _,topl,topr,rule in sorted(candidates):
            # Only a tiny continuation of already observed side positions is
            # used; the exact new segment still must contain an actual rule.
            ldx=(left[1][0]-xl)/max(1,left[1][1]-yl);rdx=(right[1][0]-xr)/max(1,right[1][1]-yr)
            vertices=np.rint([[xl+ldx*(topl-yl),topl],[xr+rdx*(topr-yr),topr],[xr,yr],[xl,yl]]).astype(np.int32)
            x,y,w,h=cv2.boundingRect(vertices)
            if x<0 or y<0 or x+w>ink.shape[1] or y+h>ink.shape[0]:continue
            if any(abs(x-r['bbox'][0])<3*scale and abs(y-r['bbox'][1])<3*scale and abs((y+h)-r['bbox'][3])<3*scale for r in cells+proposed):break
            # A neighboring row may switch to several columns. Never wrap
            # already observed separate cells in one invented response cell.
            if any(r['bbox'][2]-r['bbox'][0]<w*.9 and r['bbox'][3]-r['bbox'][1]>h*.45
                   and x-3*scale<=r['bbox'][0] and r['bbox'][2]<=x+w+3*scale
                   and cv2.pointPolygonTest(vertices,((r['bbox'][0]+r['bbox'][2])/2,(r['bbox'][1]+r['bbox'][3])/2),False)>0
                   for r in cells):continue
            scores=[]
            for i in range(4):
                p=vertices[i];q=vertices[(i+1)%4];n=max(2,int(np.linalg.norm(p-q))+1)
                xs=np.rint(np.linspace(p[0],q[0],n)).astype(int);ys=np.rint(np.linspace(p[1],q[1],n)).astype(int)
                scores.append(float((near[ys,xs]>0).mean()))
            if min(scores)<.9:continue
            local=vertices-np.array([x,y]);mask=np.zeros((h,w),np.uint8);cv2.fillConvexPoly(mask,local,255)
            pad=max(3,round(scale*1.5));mask=cv2.erode(mask,np.ones((2*pad+1,)*2,np.uint8),borderType=cv2.BORDER_CONSTANT,borderValue=0)
            area=int(np.count_nonzero(mask))
            if not area:continue
            interior=(ink[y:y+h,x:x+w]>0)&(mask>0);density=np.count_nonzero(interior)/area
            cell={'bbox':[x,y,x+w,y+h],'mask':mask,'ink':interior};text_inside=_source_text_in_cell(cell,recognized)
            # Bold labels in short ruled rows can occupy most interior ink.
            # Existing text evidence keeps their physical cell; it is still
            # an unresolved layout candidate, never automatically an answer.
            if density>=.35 and not text_inside:continue
            proposed.append({'kind':'text_box','bbox':cell['bbox'],'candidate_only':True,'text_overlap':text_inside,
                'unreadable_ink':bool(density>=.015 and not text_inside),'interior_ink_ratio':round(float(density),6),
                'closed_region_evidence':{'method':'observed_adjacent_four_rules_v1','polygon':vertices.tolist(),
                    'edge_ink_coverage':[round(v,4) for v in scores],'seed_cell_bbox':seed['bbox'],
                    'observed_horizontal_rule':rule,'eroded_interior_pixels':area}})
            break
    return proposed


def _recover_below_closed_cells(ink,cells,recognized,scale,hough):
    """Use the same four actual rule checks below, in a reflected frame."""
    from copy import deepcopy
    height=ink.shape[0];flipped=[]
    for cell in cells:
        value=deepcopy(cell);a,b,c,d=cell['bbox'];value['bbox']=[a,height-d,c,height-b]
        proof=value.get('closed_region_evidence') or {}
        if 'polygon' in proof:proof['polygon']=[[x,height-1-y] for x,y in proof['polygon']]
        flipped.append(value)
    observations=[{**r,'bbox':[r['bbox'][0],height-r['bbox'][3],r['bbox'][2],height-r['bbox'][1]]} for r in recognized]
    lines=np.asarray([[[x1,height-1-y1,x2,height-1-y2]] for [[x1,y1,x2,y2]] in hough]) if hough is not None else None
    result=_recover_adjacent_closed_cells(np.ascontiguousarray(ink[::-1]),flipped,observations,scale,lines)
    for cell in result:
        a,b,c,d=cell['bbox'];cell['bbox']=[a,height-d,c,height-b]
        proof=cell['closed_region_evidence'];proof['direction']='below'
        proof['polygon']=[[x,height-1-y] for x,y in proof['polygon']]
        a,b,c,d=proof['seed_cell_bbox'];proof['seed_cell_bbox']=[a,height-d,c,height-b]
        a,b,c,d=proof['observed_horizontal_rule'];proof['observed_horizontal_rule']=[a,height-1-b,c,height-1-d]
    return result


def _recover_right_closed_cells(ink,cells,recognized,scale):
    """Verify a broken right contour against all four visible raster edges."""
    import cv2
    result=[];near=cv2.dilate(ink,np.ones((max(3,2*round(scale)+1),)*2,np.uint8))
    for left in cells:
        lb=left['bbox'];poly=left.get('closed_region_evidence',{}).get('polygon',[])
        if len(poly)!=4 or lb[3]-lb[1]>55*scale:continue
        peers=[c for c in cells if c is not left and abs(c['bbox'][0]-lb[2])<4*scale and c['bbox'][2]-c['bbox'][0]>60*scale and
               min(abs(c['bbox'][1]-lb[3]),abs(c['bbox'][3]-lb[1]))<6*scale]
        if not peers:continue
        peer=min(peers,key=lambda c:min(abs(c['bbox'][1]-lb[3]),abs(c['bbox'][3]-lb[1])))
        ordered=sorted(poly,key=lambda p:p[0]);lo=sorted(ordered[:2],key=lambda p:p[1]);ro=sorted(ordered[2:],key=lambda p:p[1]);xr=peer['bbox'][2]-1
        def endpoint(i):
            a,b=lo[i],ro[i];return [xr,b[1]+(xr-b[0])*(b[1]-a[1])/max(1,b[0]-a[0])]
        vertices=np.rint([ro[0],endpoint(0),endpoint(1),ro[1]]).astype(np.int32);x,y,w,h=cv2.boundingRect(vertices)
        if x<0 or y<0 or x+w>ink.shape[1] or y+h>ink.shape[0]:continue
        if any(abs(c['bbox'][0]-x)<4*scale and abs(c['bbox'][1]-y)<4*scale and abs(c['bbox'][3]-(y+h))<4*scale for c in cells+result):continue
        scores=[]
        for i in range(4):
            a,b=vertices[i],vertices[(i+1)%4];n=max(2,int(np.linalg.norm(a-b))+1)
            xs=np.rint(np.linspace(a[0],b[0],n)).astype(int);ys=np.rint(np.linspace(a[1],b[1],n)).astype(int)
            scores.append(float((near[ys,xs]>0).mean()))
        if min(scores)<.9:continue
        mask=np.zeros((h,w),np.uint8);cv2.fillConvexPoly(mask,vertices-[x,y],255);pad=max(3,round(scale*1.5))
        mask=cv2.erode(mask,np.ones((2*pad+1,)*2,np.uint8),borderType=cv2.BORDER_CONSTANT,borderValue=0);area=int(np.count_nonzero(mask))
        if not area:continue
        interior=(ink[y:y+h,x:x+w]>0)&(mask>0);density=float(np.count_nonzero(interior)/area)
        cell={'bbox':[x,y,x+w,y+h],'mask':mask,'ink':interior};text=_source_text_in_cell(cell,recognized)
        if density>=.35 and not text:continue
        result.append({'kind':'text_box','bbox':cell['bbox'],'candidate_only':True,'text_overlap':text,'unreadable_ink':bool(density>=.015 and not text),'interior_ink_ratio':round(density,6),
            'closed_region_evidence':{'method':'observed_right_four_rules_v1','polygon':vertices.tolist(),'edge_ink_coverage':[round(v,4) for v in scores],
                'left_cell_bbox':lb,'column_peer_bbox':peer['bbox'],'eroded_interior_pixels':area}})
    return result


def _split_observed_cell_rows(ink,cells,recognized,scale,hough):
    """Split an outer contour only at actual rules matching adjacent rows."""
    import cv2
    proposed=[];near=cv2.dilate(ink,np.ones((max(3,2*round(scale)+1),)*2,np.uint8))
    segments=[tuple(map(int,item[0])) for item in hough] if hough is not None else []
    for parent in cells:
        pb=parent['bbox'];x0,y0,x1,y1=pb
        peers=[c for c in cells if c is not parent and abs(c['bbox'][2]-x0)<4*scale and
            y0-3*scale<=c['bbox'][1] and c['bbox'][3]<=y1+3*scale and c['bbox'][3]-c['bbox'][1]<(y1-y0)*.8]
        if len(peers)<2:continue
        peers.sort(key=lambda c:c['bbox'][1]);cuts=[]
        for upper,lower in zip(peers,peers[1:]):
            y=(upper['bbox'][3]+lower['bbox'][1])/2
            if abs(upper['bbox'][3]-lower['bbox'][1])>4*scale:continue
            matching=[]
            for a,b,c,d in segments:
                if c==a or abs((d-b)/(c-a))>.04 or min(a,c)>x0+3*scale or max(a,c)<x1-3*scale:continue
                left=b+(x0-a)*(d-b)/(c-a);right=b+(x1-a)*(d-b)/(c-a)
                if max(abs(left-y),abs(right-y))>4*scale:continue
                matching.append((left,right,[a,b,c,d]))
            if matching:cuts.append(min(matching,key=lambda v:abs(v[0]-y)))
        if not cuts:continue
        bounds=[(y0,y0,None)]+sorted(cuts)+[(y1-1,y1-1,None)];children=[]
        for top,bottom in zip(bounds,bounds[1:]):
            if min(bottom[0]-top[0],bottom[1]-top[1])<6*scale:break
            vertices=np.rint([[x0,top[0]],[x1-1,top[1]],[x1-1,bottom[1]],[x0,bottom[0]]]).astype(np.int32)
            scores=[]
            for p,q in zip(vertices,np.roll(vertices,-1,axis=0)):
                n=max(2,int(np.linalg.norm(q-p)));xs=np.rint(np.linspace(p[0],q[0],n)).astype(int);ys=np.rint(np.linspace(p[1],q[1],n)).astype(int)
                scores.append(float((near[ys,xs]>0).mean()))
            if min(scores)<.9:break
            x,y,w,h=cv2.boundingRect(vertices);mask=np.zeros((h,w),np.uint8);cv2.fillConvexPoly(mask,vertices-np.array([x,y]),255)
            pad=max(3,round(scale*1.5));mask=cv2.erode(mask,np.ones((pad*2+1,)*2,np.uint8),borderType=cv2.BORDER_CONSTANT,borderValue=0)
            area=np.count_nonzero(mask)
            if not area:break
            interior=(ink[y:y+h,x:x+w]>0)&(mask>0);density=float(np.count_nonzero(interior)/area)
            occupied=_source_text_in_cell({'bbox':[x,y,x+w,y+h],'mask':mask,'ink':interior},recognized)
            children.append({'kind':'text_box','bbox':[x,y,x+w,y+h],'candidate_only':True,'text_overlap':occupied,
                'unreadable_ink':bool(density>=.015 and not occupied),'interior_ink_ratio':round(density,6),
                'closed_region_evidence':{'method':'observed_inner_rule_and_adjacent_rows_v1','polygon':vertices.tolist(),
                    'edge_ink_coverage':scores,'seed_cell_bbox':pb,'observed_horizontal_rule':top[2] or bottom[2],
                    'eroded_interior_pixels':int(area)}})
        if len(children)==len(bounds)-1:
            parent['kind']='grid_container';proposed.extend(children)
    return proposed


def _two_stroke_tick(crop,scale):
    evidence=_edge_to_edge_tick(crop,scale)
    if evidence:return evidence
    evidence=_inset_two_stroke_tick(crop,scale)
    return evidence or _internal_joint_fit_tick(crop)


def _internal_joint_fit_tick(crop):
    """Joint-fit thick arms without treating a printed bottom rule as ink."""
    import hashlib
    import cv2
    mask=(crop>0).astype(np.uint8);h,w=mask.shape
    if min(h,w)<12:return None
    rows=mask.mean(axis=1);cols=mask.mean(axis=0)
    for x in range(w):
        if (x<w*.15 or x>=w*.85) and cols[x]>=.5:mask[:,x]=0
    for y in range(h):
        if (y<h*.22 or y>=h*.78) and rows[y]>=.7:mask[y,:]=0
    py,px=np.nonzero(mask)
    if len(px)<12:return None
    count,components,stats,_=cv2.connectedComponentsWithStats(mask,8)
    if count<2 or max(stats[1:,cv2.CC_STAT_AREA])<len(px)*.9:return None
    aa=np.arange(.75,12.,.5);bb=np.arange(-4.,-.5,.25);best=None
    for jx in np.arange(w*.2,w*.6,.5):
        for jy in np.arange(h*.6,h-2,.5):
            if not mask[max(0,int(jy)-1):int(jy)+2,max(0,int(jx)-1):int(jx)+2].any():continue
            da=abs(py[None,:]-jy-aa[:,None]*(px[None,:]-jx))/np.sqrt(1+aa[:,None]**2)
            db=abs(py[None,:]-jy-bb[:,None]*(px[None,:]-jx))/np.sqrt(1+bb[:,None]**2)
            distances=np.minimum(da[:,None,:],db[None,:,:]);scores=np.mean(np.minimum(distances,5)**2,axis=2)
            a,b=np.unravel_index(np.argmin(scores),scores.shape);score=float(scores[a,b])
            if best is None or score<best[0]:best=(score,jx,jy,float(aa[a]),float(bb[b]),da[a],db[b])
    if best is None:return None
    _,jx,jy,a,b,da,db=best;short=da<db;fit=float(np.mean(np.minimum(da,db)<=max(1.5,w*.10)))
    if fit<.92 or sum(short)<len(px)*.18 or sum(~short)<len(px)*.18 or np.mean(py>jy+max(2,h*.1))>.05:return None
    if jy-min(py[short])<h*.4 or jy-min(py[~short])<h*.5:return None
    arms=[]
    for selected in (short,~short):
        vx,vy,xc,yc=cv2.fitLine(np.column_stack((px[selected],py[selected])).astype(np.float32),cv2.DIST_L2,0,.01,.01).flatten()
        if abs(vx)<.01:return None
        slope=float(vy/vx);arms.append((slope,float(yc-slope*xc)))
    if arms[0][0]<=.4 or arms[1][0]>=-.4:return None
    ix=(arms[1][1]-arms[0][1])/(arms[0][0]-arms[1][0]);iy=arms[0][0]*ix+arms[0][1]
    # A clipped outside V can fit two broad diagonals but their independently
    # measured axes still meet on/below the removed border. Reject it.
    if not w*.15<ix<w*.65 or not h*.55<iy<h-2:return None
    return {'method':'bounded_two_stroke_tick_v1','shape_variant':'observed_internal_joint_fit',
        'patch_sha256':hashlib.sha256(crop.tobytes()).hexdigest(),'crop_shape':list(crop.shape),
        'observed_joint':[round(float(jx),3),round(float(jy),3)],'observed_arm_intersection':[round(ix,3),round(iy,3)],
        'short_arm_slope':a,'long_arm_slope':b,
        'ink_fit_fraction':round(fit,4),'two_stroke_shape_verified':True}


def _near_border_joined_tick(ink,x,y,w,h,scale):
    """A V may genuinely join just below a printed box's bottom rule.

    Require the joined ink outside that rule, plus two fitted interior arms.
    This cannot turn disconnected strokes touching the rule into a tick.
    """
    import hashlib
    margin=max(2,round(h*.22))
    if y+h+margin>ink.shape[0]:return None
    crop=ink[y:y+h+margin,x:x+w];mask=(crop>0).astype(np.uint8);inside=mask.copy()
    rows=inside[:h].mean(axis=1);cols=inside[:h].mean(axis=0)
    for xx in range(w):
        if (xx<w*.15 or xx>=w*.85) and cols[xx]>=.5:inside[:,xx]=0
    for yy in range(h):
        if (yy<h*.22 or yy>=h*.78) and rows[yy]>=.7:inside[yy,:]=0
    outside=inside[h:]
    if np.count_nonzero(outside)<max(3,w*.15):return None
    # A stroke continuing below this small neighborhood is not a local tick
    # junction. In particular a far outside V must retain unknown state.
    if np.any(ink[y+h+margin:y+h+margin+1,x+round(w*.15):x+round(w*.55)]):return None
    py,px=np.nonzero(inside);best=None
    if len(px)<12:return None
    # Fit both arms jointly around an actually observed external vertex.
    # Sloped thick strokes need orthogonal distances; a per-column median
    # biases near-vertical short arms toward the printed border.
    slopes_a=np.arange(1.,12.,.5);slopes_b=np.arange(-3.,-.5,.25)
    for jx in np.arange(w*.2,w*.5,.5):
        for jy in np.arange(h-1,h+margin,.5):
            if not inside[max(h,int(jy)-1):int(jy)+2,max(0,int(jx)-1):int(jx)+2].any():continue
            da=abs(py[None,:]-jy-slopes_a[:,None]*(px[None,:]-jx))/np.sqrt(1+slopes_a[:,None]**2)
            db=abs(py[None,:]-jy-slopes_b[:,None]*(px[None,:]-jx))/np.sqrt(1+slopes_b[:,None]**2)
            distances=np.minimum(da[:,None,:],db[None,:,:]);scores=np.mean(np.minimum(distances,5)**2,axis=2)
            a,b=np.unravel_index(np.argmin(scores),scores.shape);score=float(scores[a,b])
            if best is None or score<best[0]:best=(score,jx,jy,float(slopes_a[a]),float(slopes_b[b]),da[a],db[b])
    if best is None:return None
    _,jx,jy,short_slope,long_slope,da,db=best;distance=np.minimum(da,db);short=da<db
    fit=float(np.mean(distance<=max(1.5,w*.10)))
    if fit<.9 or sum(short)<len(px)*.18 or sum(~short)<len(px)*.18:return None
    if jy-min(py[short])<h*.4 or jy-min(py[~short])<h*.65:return None
    return {'method':'bounded_two_stroke_tick_v1','shape_variant':'observed_near_border_junction',
        'patch_sha256':hashlib.sha256(crop.tobytes()).hexdigest(),'crop_shape':list(crop.shape),
        'observed_box_shape':[h,w],'external_junction':[round(float(jx),3),round(float(jy),3)],
        'short_arm_slope':short_slope,'long_arm_slope':long_slope,
        'ink_fit_fraction':round(fit,4),'two_stroke_shape_verified':True}


def _inset_two_stroke_tick(crop,scale):
    """Verify both ink arms when a thick printed border surrounds the tick."""
    import cv2
    import hashlib
    inside=(crop>0).astype(np.uint8);h,w=inside.shape
    if min(h,w)<12:return None
    # Remove only actual long straight border columns/rows at the perimeter.
    # The observed square has already passed the four-border detector.
    cols=inside.mean(axis=0);rows=inside.mean(axis=1)
    for x in range(w):
        if (x<w*.22 or x>=w*.78) and cols[x]>=.7:inside[:,x]=0
    for y in range(h):
        if (y<h*.22 or y>=h*.78) and rows[y]>=.7:inside[y,:]=0
    inside[0,:]=inside[-1,:]=0;inside[:,0]=inside[:,-1]=0
    count,labels,stats,_=cv2.connectedComponentsWithStats(inside,8)
    if count<2:return None
    index=1+int(np.argmax(stats[1:,cv2.CC_STAT_AREA]));component=labels==index
    if component.sum()<inside.sum()*.88:return None
    ys_all,xs_all=np.nonzero(component)
    if len(xs_all)<12:return None
    spanx=int(xs_all.max()-xs_all.min());spany=int(ys_all.max()-ys_all.min())
    if spanx<w*.55 or spany<h*.5:return None
    columns=[(x,float(np.median(np.flatnonzero(component[:,x])))) for x in range(w) if component[:,x].any()]
    xs=np.asarray([p[0] for p in columns]);ys=np.asarray([p[1] for p in columns]);k=int(np.argmax(ys))
    if k<2 or k>len(xs)*.45 or len(xs)-k<max(4,spanx*.45):return None
    if ys[k]-ys[0]<h*.15 or ys[k]-ys[-1]<h*.4 or ys[k]<h*.6:return None
    left=np.polyfit(xs[:k+1],ys[:k+1],1);right=np.polyfit(xs[k:],ys[k:],1)
    if not (.4<left[0]<4 and -3<right[0]<-.4):return None
    junction_x=(right[1]-left[1])/(left[0]-right[0]);junction_y=left[0]*junction_x+left[1]
    # An outside V can appear joined only by the printed bottom border. Both
    # fitted arms must actually meet inside the observed box, not below it.
    if not 1<junction_x<w-2 or not 1<junction_y<h-2 or abs(junction_y-ys[k])>max(2,h*.12):return None
    is_short=xs_all<=xs[k]
    prediction=np.where(is_short,left[0]*xs_all+left[1],right[0]*xs_all+right[1])
    # Perpendicular distance measures physical stroke thickness. Vertical
    # residuals unfairly reject a steep short arm of the same printed width.
    distance=abs(ys_all-prediction)/np.sqrt(1+np.where(is_short,left[0],right[0])**2)
    coverage=float(np.mean(distance<=max(1.5,min(h,w)*.08)))
    if coverage<.88:return None
    return {'method':'bounded_two_stroke_tick_v1','shape_variant':'inset_arms_after_observed_borders',
        'patch_sha256':hashlib.sha256(crop.tobytes()).hexdigest(),'crop_shape':list(crop.shape),
        'short_arm_slope':round(float(left[0]),4),'long_arm_slope':round(float(right[0]),4),
        'ink_fit_fraction':round(coverage,4),'two_stroke_shape_verified':True}


def _edge_to_edge_tick(crop,scale):
    """Verify a connected short-down/long-up check shape, not ink density.

    Only the interior of an independently found four-sided box is examined.
    A straight slash, cross, spot, printed letter or broad scribble must not
    acquire a selected value merely because it contains black pixels.
    """
    import hashlib
    pad=max(1,round(scale*.6));inside=(crop[pad:-pad,pad:-pad]>0).astype(np.uint8)
    if not inside.size or min(inside.shape)<8:return None
    h,w=inside.shape
    # Strip residual border pixels only at the outermost edge. Do not erase
    # unexplained interior strokes in order to force a tick classification.
    for x in range(w):
        if (x<max(1,round(w*.09)) or x>=w-max(1,round(w*.09))) and inside[:,x].mean()>.7:inside[:,x]=0
    for y in range(h):
        if (y<max(1,round(h*.09)) or y>=h-max(1,round(h*.09))) and inside[y,:].mean()>.7:inside[y,:]=0
    columns=[(x,float(np.median(np.flatnonzero(inside[:,x])))) for x in range(w) if inside[:,x].any()]
    if len(columns)<w*.8:return None
    xs=np.asarray([p[0] for p in columns]);ys=np.asarray([p[1] for p in columns]);k=int(np.argmax(ys))
    if k<2 or k>len(xs)*.4 or len(xs)-k<max(4,w*.45):return None
    if ys[k]-ys[0]<h*.1 or ys[k]-ys[-1]<h*.5 or ys[k]<h*.65:return None
    left=np.polyfit(xs[:k+1],ys[:k+1],1);right=np.polyfit(xs[k:],ys[k:],1)
    if not (.4<left[0]<3 and -2.5<right[0]<-.4):return None
    yy,xx=np.nonzero(inside);prediction=np.where(xx<=xs[k],left[0]*xx+left[1],right[0]*xx+right[1])
    coverage=float(np.mean(abs(yy-prediction)<=max(2,h*.16)))
    if coverage<.86:return None
    return {'method':'bounded_two_stroke_tick_v1','patch_sha256':hashlib.sha256(crop.tobytes()).hexdigest(),
            'crop_shape':list(crop.shape),'short_arm_slope':round(float(left[0]),4),
            'long_arm_slope':round(float(right[0]),4),'ink_fit_fraction':round(coverage,4),
            'two_stroke_shape_verified':True}


def _bounded_checkbox_crop(ink,x,y,w,h,scale):
    """Recover real four edges when a handwritten stroke exits the square.

    A contour's bounding box includes the exiting stroke. Search only inside
    that contour for four actually inked straight edges; never extrapolate a
    missing border from the neighboring option or from its caption.
    """
    import cv2
    crop=ink[y:y+h,x:x+w]
    rows=(crop>0).mean(axis=1);cols=(crop>0).mean(axis=0)
    top=[i for i in range(max(1,int(h*.25))) if rows[i]>=.6]
    bottom=[i for i in range(int(h*.6),h) if rows[i]>=.6]
    left=[i for i in range(max(1,int(w*.25))) if cols[i]>=.6]
    right=[i for i in range(int(w*.6),w) if cols[i]>=.6]
    if not (top and bottom and left and right):return None
    a,b,c,d=min(left),min(top),max(right)+1,max(bottom)+1
    nw,nh=c-a,d-b
    if not (4*scale<=nw<=24*scale and 4*scale<=nh<=16*scale and .7<=nw/nh<=1.5):return None
    edge=max(1,round(scale*.6));inner=crop[b:d,a:c]
    # A real border may be just one pixel wide. Require a long actual line on
    # each side; averaging two rows with an empty neighbor rejects that line.
    scores=[(inner[:edge,:]>0).mean(axis=1).max(),(inner[-edge:,:]>0).mean(axis=1).max(),
            (inner[:,:edge]>0).mean(axis=0).max(),(inner[:,-edge:]>0).mean(axis=0).max()]
    if min(scores)<.7:return None
    pad=max(2,round(scale*1.2));center=inner[pad:-pad,pad:-pad]
    # This recovery is exclusively for an exiting mark. An empty rounded O
    # in printed prose must not acquire a new rectangle interpretation.
    if not center.size or (center>0).mean()<.08:return None
    return x+a,y+b,nw,nh

def scan_form_regions(image: Image.Image, recognized: list[dict], page_width: float) -> list[dict]:
    """Preserve physical scan marks that a text recognizer normally discards.

    These are *layout candidates*, not writable/required business fields. An
    empty rectangular interior proves an unmarked box; ink in it does not prove
    a Yes/No answer, so marked/ambiguous boxes remain selected=None. Coordinates
    refer to the unrotated input image and never use OCR word-list indices.
    """
    import cv2
    gray=cv2.cvtColor(np.asarray(image.convert('RGB')),cv2.COLOR_RGB2GRAY)
    ink=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    scale=image.width/max(1,page_width)
    contours,_=cv2.findContours(ink,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    # A real tick can touch an unrelated long table rule, joining the option
    # to the entire table contour. Detach long rules only to propose a crop;
    # all four option edges and the mark are still checked on original ink.
    # No removed pixels are used as evidence for an edge or a selection.
    long_h=cv2.morphologyEx(ink,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(max(50,round(35*scale)),1)))
    long_v=cv2.morphologyEx(ink,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(1,max(50,round(35*scale)))))
    detached=cv2.bitwise_and(ink,cv2.bitwise_not(cv2.bitwise_or(long_h,long_v)))
    local_contours,_=cv2.findContours(detached,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    recovered=[]
    for contour in local_contours:
        x,y,w,h=cv2.boundingRect(contour)
        if not (4*scale<=w<=28*scale and 4*scale<=h<=20*scale):continue
        bounded=_bounded_checkbox_crop(ink,x,y,w,h,scale)
        if bounded is None:continue
        a,b,c,d=bounded
        recovered.append(np.asarray([[[a,b]],[[a+c-1,b]],[[a+c-1,b+d-1]],[[a,b+d-1]]],dtype=np.int32))
    contours=list(contours)+recovered
    boxes=[];text_boxes=[]
    for contour in contours:
        x,y,w,h=cv2.boundingRect(contour)
        if 25*scale<=w<=image.width*.98 and 6*scale<=h<=260*scale and w/h>=.45:
            cell=_closed_cell_interior(ink,contour,scale)
            if cell:
                density=cell['density'];text_inside=_source_text_in_cell(cell,recognized)
                if (density<.35 or text_inside) and not any(abs(x-b['bbox'][0])<3*scale and abs(y-b['bbox'][1])<3*scale and abs(w-(b['bbox'][2]-b['bbox'][0]))<5*scale for b in text_boxes):
                    # A bounded occupied cell is retained as unresolved layout,
                    # never declared an answer until its label and value spans
                    # pass the separate physical relationship validator.
                    text_boxes.append({'kind':'text_box','bbox':[x,y,x+w,y+h],'candidate_only':True,
                        'text_overlap':text_inside,'unreadable_ink':bool(density>=.015 and not text_inside),
                        'interior_ink_ratio':round(density,6),'closed_region_evidence':cell['proof']})
        if not (4*scale <= w <= 24*scale and 4*scale <= h <= 16*scale and .55<=w/h<=3.2):continue
        poly=cv2.approxPolyDP(contour,.035*cv2.arcLength(contour,True),True)
        if len(poly)!=4 or not cv2.isContourConvex(poly) or cv2.contourArea(contour)/(w*h)<.65:
            bounded=_bounded_checkbox_crop(ink,x,y,w,h,scale)
            if bounded is None:continue
            x,y,w,h=bounded
        # Reject printed letter shapes and rectangles with missing edges.
        edge=max(1,round(scale*.8));pad=max(2,round(scale*1.2))
        crop=ink[y:y+h,x:x+w]
        straight=[(crop[:edge,:]>0).mean(),(crop[-edge:,:]>0).mean(),(crop[:,:edge]>0).mean(),(crop[:,-edge:]>0).mean()]
        if min(straight)<.4:
            bounded=_bounded_checkbox_crop(ink,x,y,w,h,scale)
            if bounded is None:continue
            x,y,w,h=bounded;crop=ink[y:y+h,x:x+w]
            straight=[(crop[:edge,:]>0).mean(),(crop[-edge:,:]>0).mean(),(crop[:,:edge]>0).mean(),(crop[:,-edge:]>0).mean()]
            if min(straight)<.4:continue
        if any(abs(x-b['bbox'][0])<3*scale and abs(y-b['bbox'][1])<3*scale and abs(w-(b['bbox'][2]-b['bbox'][0]))<4*scale for b in boxes):continue
        interior=crop[pad:-pad,pad:-pad]
        density=float((interior>0).mean()) if interior.size else 1
        # OCR sometimes omits a box, but a small rectangle fully embedded in
        # a long printed word/line is usually B/O/0. Preserve actual option
        # strings (Yes/No/etc.) and standalone mark detections for review.
        embedded=False
        for item in recognized:
            b=item['bbox'];value=item.get('text','')
            overlap=max(0,min(x+w,b[2])-max(x,b[0]))*max(0,min(y+h,b[3])-max(y,b[1]))
            if overlap/(w*h)>.7 and b[0]<x-w*.5 and len(value)>4 and b[2]-b[0]>w*2 and not re.search(r'\b(?:yes|no|n/?a)\b|[□☐☑☒]',value,re.I):
                embedded=True;break
        # A real marked option may be embedded in an OCR line spanning several
        # captions. Defer only this exclusion until repeated physical borders
        # AND an independently verified two-stroke mark can be checked. An
        # arbitrary printed glyph still cannot become a checkbox value.
        boxes.append({'kind':'checkbox','bbox':[x,y,x+w,y+h],'selected':False if density<.025 else None,'interior_ink_ratio':round(density,4),'candidate_only':True,
            'embedded_ocr_candidate':bool(embedded and density>=.025)})
    # Repeated rectangles or a directly adjacent, explicit option caption
    # provide evidence. Real forms can put Yes and No on separate lines;
    # requiring another box at the same height silently loses those slots.
    retained=[]
    for box in boxes:
        bx=box['bbox'];cy=(bx[1]+bx[3])/2
        siblings=any(box is not other and abs(bx[1]-other['bbox'][1])<4*scale and 2*scale<abs(bx[0]-other['bbox'][0])<page_width*.95*scale and abs((bx[3]-bx[1])-(other['bbox'][3]-other['bbox'][1]))<3*scale for other in boxes)
        captions=[]
        for item in recognized:
            if abs((item['bbox'][1]+item['bbox'][3])/2-cy)>=6*scale:continue
            matches=list(re.finditer(r'(?<![A-Za-z])(?:yes|no|n\s*/?\s*a|planned|in preparation|是|否|不适用)(?![A-Za-z])',item.get('text',''),re.I))
            if not matches and re.fullmatch(r'[ZV](?:Yes|No)',item.get('text','')):
                # OCR can include a sloping tick as Z/V immediately before the
                # printed option. Keep the literal option substring only when
                # a separately detected physical box supports this baseline.
                matches=[re.search(r'(?:Yes|No)',item['text'])]
            for match in matches:
                estimated_end=item['bbox'][0]+(item['bbox'][2]-item['bbox'][0])*match.end()/max(1,len(item['text']))
                caption_contains_box=(len(matches)==1 and re.fullmatch(r'\s*(?:yes|no|n\s*/?\s*a|planned|in preparation|是|否|不适用)[\s□口O()]*',item.get('text',''),re.I) and item['bbox'][0]+(item['bbox'][2]-item['bbox'][0])*.5<bx[0]<=item['bbox'][2] and item['bbox'][2]-bx[0]<16*scale)
                simple_caption=len(matches)==1 and (re.fullmatch(r'[\s□☐☑☒口O()ZV]*'+re.escape(match.group())+r'[\s□☐☑☒口O()]*',item.get('text',''),re.I) or
                    re.match(r'^[\s□☐☑☒口√✓✔]*'+re.escape(match.group())+r'\s*[,，:：]',item.get('text',''),re.I))
                caption_after_box=bool(simple_caption and (-3*scale<=item['bbox'][0]-bx[2]<18*scale or
                    item['bbox'][0]-3*scale<=bx[0]<=item['bbox'][0]+(item['bbox'][2]-item['bbox'][0])*.45 and
                    bx[2]<item['bbox'][2] and item['bbox'][2]-item['bbox'][0]>(bx[2]-bx[0])*1.6))
                if -2*scale<=bx[0]-estimated_end<18*scale or caption_contains_box or caption_after_box:
                    captions.append({'text':match.group()});break
        stacked=any(box is not other and abs(bx[0]-other['bbox'][0])<3*scale and
            12*scale<abs(bx[1]-other['bbox'][1])<75*scale and
            abs((bx[3]-bx[1])-(other['bbox'][3]-other['bbox'][1]))<3*scale for other in boxes)
        leading_caption=any(len(item.get('text','').strip())>3 and
            abs((item['bbox'][1]+item['bbox'][3])/2-cy)<7*scale and
            (-3*scale<=item['bbox'][0]-bx[0]<20*scale or -3*scale<=item['bbox'][0]-bx[2]<18*scale)
            and item['bbox'][2]>bx[2]+20*scale for item in recognized)
        if siblings or len(captions)==1 or stacked and leading_caption:
            box['same_row_physical_peer']=siblings
            if captions:box['option_caption_evidence']=captions[0]['text']
            box['choice_layout_evidence']='adjacent_option_caption' if captions else 'stacked_boxes_with_leading_captions' if stacked and leading_caption else 'repeated_physical_rectangles'
            retained.append(box)
    boxes=retained
    for box in boxes:
        if box.get('selected') is not None:continue
        rb=box['bbox'];inside=[]
        for item in recognized:
            if item.get('confidence',0)<.9 or item.get('text','').strip() not in {'√','✓','✔','☑','☒','X','x'}:continue
            b=item['bbox'];area=max(1,(b[2]-b[0])*(b[3]-b[1]))
            overlap=max(0,min(rb[2],b[2])-max(rb[0],b[0]))*max(0,min(rb[3],b[3])-max(rb[1],b[1]))
            if overlap/area>.7:inside.append(item)
        # Recognized mark + a unique physical option box, not arbitrary ink.
        if len(inside)==1 and sum(max(0,min(other['bbox'][2],inside[0]['bbox'][2])-max(other['bbox'][0],inside[0]['bbox'][0]))*max(0,min(other['bbox'][3],inside[0]['bbox'][3])-max(other['bbox'][1],inside[0]['bbox'][1]))>0 for other in boxes)==1:
            box.update(selected=True,selection_source='ocr_mark_in_scan_rectangle',selection_mark=inside[0])
        elif box.get('choice_layout_evidence'):
            x,y,x1,y1=map(int,box['bbox']);evidence=_two_stroke_tick(ink[y:y1,x:x1],scale)
            if not evidence:evidence=_near_border_joined_tick(ink,x,y,x1-x,y1-y,scale)
            if not evidence:
                from .scan_tick_shape import right_exit_tick
                evidence=right_exit_tick(ink,x,y,x1-x,y1-y,scale)
            if evidence:box.update(selected=True,selection_source='raster_two_stroke_tick',selection_mark=evidence)
    boxes=[b for b in boxes if not b.get('embedded_ocr_candidate') or
        b.get('same_row_physical_peer') and b.get('selection_source')=='raster_two_stroke_tick' and
        b.get('selection_mark',{}).get('two_stroke_shape_verified') is True]
    horizontal=cv2.morphologyEx(ink,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(max(20,round(24*scale)),1)))
    line_contours,_=cv2.findContours(horizontal,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    segments=[list(cv2.boundingRect(c)) for c in line_contours]
    # Preserve dotted signing lines only when supported by repeated raster
    # gaps and a nearby printed caption. Morphological closing is used to
    # locate their extent, never to turn arbitrary blank space into a field.
    closed=cv2.morphologyEx(ink,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_RECT,(max(3,round(4*scale)),1)))
    dotted_mask=cv2.morphologyEx(closed,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(round(45*scale),1)))
    dotted_contours,_=cv2.findContours(dotted_mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    dotted_segments=[]
    for contour in dotted_contours:
        x,y,w,h=cv2.boundingRect(contour)
        if w<45*scale or h>3*scale:continue
        cols=(ink[y:y+h,x:x+w]>0).any(axis=0)
        gaps=int(np.count_nonzero(cols[1:]!=cols[:-1]))
        caption=any(abs(b['bbox'][0]-x)<15*scale and 0<b['bbox'][1]-(y+h)<14*scale and len(b.get('text',''))>3 for b in recognized)
        printed_line=any(b['bbox'][1]-scale<=y+h/2<=b['bbox'][3]+scale and max(0,min(x+w,b['bbox'][2])-max(x,b['bbox'][0]))/w>.25 for b in recognized)
        if gaps>=10 and .1<float(cols.mean())<.8 and caption and not printed_line:
            dotted_segments.append([x,y,w,h])
            if not any(abs(y-s[1])<2*scale and abs(x-s[0])<5*scale and abs(w-s[2])<5*scale for s in segments):segments.append([x,y,w,h])
    # Scans can be skewed by a fraction of a degree. A horizontal-only kernel
    # fragments those real answer lines; retain long near-horizontal raster
    # segments in the same coordinate frame instead of rotating source text.
    hough=cv2.HoughLinesP(ink,1,np.pi/1800,threshold=max(30,round(30*scale)),minLineLength=round(45*scale),maxLineGap=max(2,round(1.2*scale)))
    for values in hough if hough is not None else []:
        x1,y1,x2,y2=[int(v) for v in values[0]]
        if abs(x2-x1)<45*scale or abs(y2-y1)/max(1,abs(x2-x1))>.025:continue
        x,y,w,h=min(x1,x2),min(y1,y2),abs(x2-x1)+1,abs(y2-y1)+1
        related=next((s for s in segments if max(s[0],x)<=min(s[0]+s[2],x+w)+3*scale and abs((s[1]+s[3]/2)-(y+h/2))<2*scale),None)
        if related:
            end=max(related[0]+related[2],x+w);bottom=max(related[1]+related[3],y+h);related[0]=min(related[0],x);related[1]=min(related[1],y);related[2]=end-related[0];related[3]=bottom-related[1]
        else:segments.append([x,y,w,h])
    text_boxes.extend(_split_observed_cell_rows(ink,text_boxes,recognized,scale,hough))
    text_boxes.extend(_recover_adjacent_closed_cells(ink,[c for c in text_boxes if c['kind']=='text_box'],recognized,scale,hough))
    text_boxes.extend(_recover_below_closed_cells(ink,[c for c in text_boxes if c['kind']=='text_box'],recognized,scale,hough))
    text_boxes.extend(_recover_right_closed_cells(ink,[c for c in text_boxes if c['kind']=='text_box'],recognized,scale))
    lines=[]
    for x,y,w,h in segments:
        if w<35*scale or w>image.width*.9 or h>max(5,scale*2+w*.025):continue
        if any(b['bbox'][0]-3*scale<=x and x+w<=b['bbox'][2]+3*scale and min(abs(y-b['bbox'][1]),abs(y-b['bbox'][3]))<3*scale for b in text_boxes):continue
        # Keep visual location without declaring that every border is a field.
        region=[x,max(0,y-8*scale),x+w,y+h]
        overlapping=[]
        for item in recognized:
            b=item['bbox'];overlap=max(0,min(region[2],b[2])-max(region[0],b[0]))*max(0,min(region[3],b[3])-max(region[1],b[1]))
            width_overlap=max(0,min(x+w,b[2])-max(x,b[0]))
            embedded_stroke=b[1]-scale<=y+h/2<=b[3]+scale and width_overlap/max(1,w)>.4
            if embedded_stroke or overlap/max(1,(b[2]-b[0])*(b[3]-b[1]))>.25:overlapping.append(b)
        style='dotted' if any(abs(y-s[1])<2*scale and abs(x-s[0])<5*scale and abs(w-s[2])<5*scale for s in dotted_segments) else 'solid'
        lines.append({'kind':'write_line','bbox':region,'candidate_only':True,'text_overlap':bool(overlapping),'line_style':style})
    lanes=[]
    # Explicit short labels and numbered short form rows may intentionally
    # have no drawn rule. Preserve their physically empty response lane, not
    # arbitrary page whitespace. The semantic layer still must associate it.
    for item in recognized:
        value=item.get('text','').strip();b=item['bbox'];cy=(b[1]+b[3])/2
        if not value or len(value)>90 or re.fullmatch(r'[\d.()]+',value):continue
        numbered=any(re.fullmatch(r'\d+(?:\.\d+){1,3}',other.get('text','').strip()) and other['bbox'][2]<b[0] and b[0]-other['bbox'][2]<35*scale and abs((other['bbox'][1]+other['bbox'][3])/2-cy)<5*scale for other in recognized)
        if not value.endswith((':','：')) and not numbered:continue
        if re.search(r'\b(?:yes|no)\b',value,re.I) or '?' in value:continue
        same=[r for r in recognized if r is not item and abs((r['bbox'][1]+r['bbox'][3])/2-cy)<6*scale and r['bbox'][0]>=b[2]]
        right=min((r['bbox'][0]-5*scale for r in same),default=image.width*.92)
        x0=b[2]+5*scale;y0=b[1];y1=b[3]+2*scale
        if right-x0<45*scale:continue
        if any(max(x0,r['bbox'][0])<min(right,r['bbox'][2]) and abs(r['bbox'][3]-y1)<7*scale for r in lines+text_boxes+boxes):continue
        crop=ink[max(0,int(y0)):min(image.height,int(y1)),max(0,int(x0)):min(image.width,int(right))]
        if crop.size and (crop>0).mean()<.003:
            lanes.append({'kind':'blank_lane','bbox':[x0,y0,right,y1],'candidate_only':True,'text_overlap':False,'evidence':'explicit_label_or_numbered_short_row_and_empty_raster'})
    return sorted(boxes+text_boxes+lines+lanes,key=lambda r:(r['bbox'][1],r['bbox'][0]))

def available() -> bool:
    try:
        import rapidocr_onnxruntime
        return len(list((Path(rapidocr_onnxruntime.__file__).parent / "models").glob("*.onnx"))) >= 3
    except (ImportError, TypeError):
        return False

@lru_cache(maxsize=1)
def _engine():
    if not available():
        raise RuntimeError("本地 RapidOCR/PP-OCRv4 模型未安装完整")
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR(intra_op_num_threads=max(1, int(os.getenv("QSR_OCR_THREADS", "2"))), inter_op_num_threads=1, det_limit_side_len=int(os.getenv("QSR_OCR_SIDE_LIMIT", "1920")), print_verbose=False)

def recognize(image: Image.Image) -> list[dict]:
    if image.width * image.height > int(os.getenv("QSR_MAX_OCR_PIXELS", "40000000")):
        raise RuntimeError("页面渲染像素超过 OCR 资源限制")
    source = image.convert("RGB"); working = source; rotation = 0
    result, _ = _engine()(np.asarray(working))

    def vertical(entries):
        return sum(max(p[1] for p in pts)-min(p[1] for p in pts) > 2*(max(p[0] for p in pts)-min(p[0] for p in pts)) for pts, _, _ in entries or []) > len(entries or []) * .5

    def upside(entries, source_image):
        crops = []
        for pts, _, _ in (entries or [])[:10]:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            crop = np.asarray(source_image.crop((max(0,int(min(xs))),max(0,int(min(ys))),min(source_image.width,int(max(xs))+1),min(source_image.height,int(max(ys))+1))))
            if crop.shape[0] and crop.shape[1]: crops.append(crop)
        if not crops: return False
        _, votes, _ = _engine().text_cls(crops)
        return sum(label == "180" and score > .75 for label, score in votes) > len(votes)/2

    if vertical(result):
        rotation = 90; working = source.rotate(90, expand=True); result, _ = _engine()(np.asarray(working))
        if upside(result, working): rotation = 270; working = source.rotate(270, expand=True); result, _ = _engine()(np.asarray(working))
    elif upside(result, working):
        rotation = 180; working = source.rotate(180, expand=True); result, _ = _engine()(np.asarray(working))
    lines = []
    for points, value, score in result or []:
        if not str(value).strip(): continue
        mapped=[]
        for x,y in points:
            mapped.append((source.width-y,x) if rotation==90 else ((y,source.height-x) if rotation==270 else ((source.width-x,source.height-y) if rotation==180 else (x,y))))
        xs, ys = [float(p[0]) for p in mapped], [float(p[1]) for p in mapped]
        reading_x, reading_y = [float(p[0]) for p in points], [float(p[1]) for p in points]
        lines.append({"text": str(value).strip(), "bbox": [min(xs), min(ys), max(xs), max(ys)], "reading_bbox": [min(reading_x),min(reading_y),max(reading_x),max(reading_y)], "rotation": rotation, "confidence": round(float(score), 3)})
    # A full A4 scan is downscaled by the detector. Thin/underlined words can
    # disappear completely even while the remaining page looks successful.
    # Audit uncovered character-sized raster components, then re-read only
    # those horizontal bands at native resolution. This is local OCR, not
    # inferred text; existing line IDs are built only after this final ledger.
    if not rotation:
        lines = recover_unread_bands(source, lines)
        lines = recover_choice_captions(source, lines)
    key_box = "reading_bbox" if rotation else "bbox"
    return sorted(lines, key=lambda x: (round(x[key_box][1] / 8), x[key_box][0]))


def recover_unread_bands(image: Image.Image, lines: list[dict]) -> list[dict]:
    import cv2
    gray=cv2.cvtColor(np.asarray(image.convert('RGB')),cv2.COLOR_RGB2GRAY)
    ink=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    scale=image.width/595
    count,_,stats,centers=cv2.connectedComponentsWithStats(ink,8)
    rows=[]
    for i in range(1,count):
        x,y,w,h,area=stats[i];cx,cy=centers[i]
        # Underlining and compressed scans can join letters into entire words.
        # Keep those text-height connected components; thin answer rules still
        # fail the minimum height/density checks and are handled as geometry.
        if not (1*scale<w<image.width*.9 and 3*scale<h<20*scale and area>4*scale*scale and area/max(1,w*h)>.08):continue
        if any(b['bbox'][0]-2*scale<=cx<=b['bbox'][2]+2*scale and b['bbox'][1]-2*scale<=cy<=b['bbox'][3]+2*scale for b in lines):continue
        row=next((r for r in rows if abs(r['cy']-cy)<5*scale),None)
        if row:row['boxes'].append((x,y,x+w,y+h))
        else:rows.append({'cy':cy,'boxes':[(x,y,x+w,y+h)]})
    bands=[]
    for row in rows:
        boxes=row['boxes']
        character_evidence=sum(max(1,min(12,(b[2]-b[0])/(8*scale))) for b in boxes)
        if character_evidence<8 or max(b[2] for b in boxes)-min(b[0] for b in boxes)<60*scale:continue
        # A very thin full-width crop can make the detector discard a whole
        # bold/underlined sentence again. Include local vertical context at
        # native resolution, while keeping retries bounded to missed bands.
        top=max(0,int(min(b[1] for b in boxes)-60*scale));bottom=min(image.height,int(max(b[3] for b in boxes)+60*scale))
        near=next((b for b in bands if top<=b[1]+10*scale and bottom>=b[0]-10*scale),None)
        if near:near[0]=min(near[0],top);near[1]=max(near[1],bottom)
        else:bands.append([top,bottom])
    result=list(lines)
    # Bounded by page height; even a mostly missed page does not issue one call
    # per character. Nearby lines share a crop, and a crop may not exceed A4.
    for top,bottom in sorted(bands):
        raw,_=_engine()(np.asarray(image.crop((0,top,image.width,bottom))))
        recovered=[]
        for points,value,score in raw or []:
            if not str(value).strip():continue
            xs=[float(p[0]) for p in points];ys=[float(p[1])+top for p in points]
            bbox=[min(xs),min(ys),max(xs),max(ys)]
            recovered.append({'text':str(value).strip(),'bbox':bbox,'reading_bbox':bbox,'rotation':0,'confidence':round(float(score),3),'coverage_retry':True})
        for fresh in recovered:
            fb=fresh['bbox'];overlap=[]
            for old in result:
                ob=old['bbox'];area=max(0,min(fb[2],ob[2])-max(fb[0],ob[0]))*max(0,min(fb[3],ob[3])-max(fb[1],ob[1]))
                if area/max(1,min((fb[2]-fb[0])*(fb[3]-fb[1]),(ob[2]-ob[0])*(ob[3]-ob[1])))>.5:overlap.append(old)
            if overlap:
                # Replace only when the native crop covers all existing line
                # fragments; otherwise retain them and expose no duplicate.
                if not all(fb[0]-4*scale<=b['bbox'][0] and fb[2]+4*scale>=b['bbox'][2] for b in overlap):continue
                if fresh['confidence']<min(b['confidence'] for b in overlap)-.12:continue
                result=[b for b in result if b not in overlap]
            result.append(fresh)
    # Some long lines remain invisible to the detector even in a local crop.
    # The independently observed uncovered character components bound one
    # real printed line; run the same local recognizer on that exact raster
    # span. This is OCR of source pixels, not inferred or manually supplied
    # question text. Never include covered neighbouring lines or option boxes.
    recognizer=getattr(_engine(),'text_rec',None)
    if recognizer is not None:
        # A detector may retain a full-line box while its text recognizer
        # returns only one truncated word. Re-read that source box, accepting
        # only substantially more actual text at high recognition confidence.
        for old in list(result):
            ob=old['bbox'];value=old['text'];width=ob[2]-ob[0];height=ob[3]-ob[1]
            if width<120*scale or len(value)>=width/max(1,height*.55) or sum(ord(c)<128 for c in value)<len(value)*.8:continue
            bb=[max(0,int(ob[0]-scale)),max(0,int(ob[1]-scale)),min(image.width,int(ob[2]+scale)),min(image.height,int(ob[3]+scale))]
            recognized,_=recognizer([np.asarray(image.crop(tuple(bb)))])
            if not recognized:continue
            fresh,score=recognized[0]
            if score<.9 or len(fresh)<max(8,len(value)*1.3):continue
            result.remove(old);result.append({'text':str(fresh).strip(),'bbox':bb,'reading_bbox':bb,'rotation':0,'confidence':round(float(score),3),'coverage_retry':True,'coverage_method':'truncated_line_reread','previous_ocr_text':value})
        for row in rows:
            uncovered=[b for b in row['boxes'] if not any(r['bbox'][0]-2*scale<=(b[0]+b[2])/2<=r['bbox'][2]+2*scale and r['bbox'][1]-2*scale<=(b[1]+b[3])/2<=r['bbox'][3]+2*scale for r in result)]
            clusters=[]
            for b in sorted(uncovered,key=lambda b:b[0]):
                if clusters and b[0]-max(t[2] for t in clusters[-1])<20*scale:clusters[-1].append(b)
                else:clusters.append([b])
            for cluster in clusters:
                if sum(max(1,min(12,(b[2]-b[0])/(8*scale))) for b in cluster)<8:continue
                x0=min(b[0] for b in cluster);x1=max(b[2] for b in cluster)
                if x1-x0<60*scale:continue
                y0=min(b[1] for b in cluster);y1=max(b[3] for b in cluster)
                bbox=[max(0,int(x0-2*scale)),max(0,int(y0-2*scale)),min(image.width,int(x1+2*scale)),min(image.height,int(y1+2*scale))]
                entries,_=recognizer([np.asarray(image.crop(tuple(bbox)))])
                if not entries:continue
                value,score=entries[0]
                if len(str(value).strip())<4 or score<.65:continue
                result.append({'text':str(value).strip(),'bbox':bbox,'reading_bbox':bbox,'rotation':0,'confidence':round(float(score),3),'coverage_retry':True,'coverage_method':'uncovered_character_line_recognition'})
    return result


def recover_choice_captions(image: Image.Image, lines: list[dict], page_width: float = 595.0) -> list[dict]:
    """Re-read undecoded captions beside actual raster choice boxes locally.

    Checkbox geometry only bounds the crop. Text always comes from the local
    recognizer, never from an assumed Yes/No ordering or a spelling dictionary.
    """
    import re
    scale=image.width/page_width
    boxes=[r for r in scan_form_regions(image,lines,page_width) if r['kind']=='checkbox']
    rows=[]
    for box in sorted(boxes,key=lambda r:(r['bbox'][1],r['bbox'][0])):
        row=next((row for row in rows if abs(row[0]['bbox'][1]-box['bbox'][1])<5*scale),None)
        if row is None:rows.append([box])
        else:row.append(box)
    output=list(lines)
    for row in rows:
        if len(row)<2:continue
        row.sort(key=lambda r:r['bbox'][0]);top=min(r['bbox'][1] for r in row);bottom=max(r['bbox'][3] for r in row)
        nearby=[b for b in output if abs((b['bbox'][1]+b['bbox'][3])/2-(top+bottom)/2)<8*scale and b['bbox'][2]>row[0]['bbox'][0]-35*scale]
        if any(re.search(r'yes|no|planned|preparation',b['text'],re.I) for b in nearby):continue
        if all(any(b.get('confidence',0)>=.85 and b['bbox'][2]<=box['bbox'][0]+2*scale and 0<=box['bbox'][0]-b['bbox'][2]<12*scale and b['text'].strip() for b in nearby) for box in row):continue
        recovered=[]
        for index,box in enumerate(row):
            x0=max(0,box['bbox'][0]-35*scale,row[index-1]['bbox'][2]+2*scale if index else 0)
            x1=box['bbox'][0]-2*scale;y0=max(0,top-4*scale);y1=min(image.height,bottom+2*scale)
            if x1-x0<8*scale:continue
            crop=np.asarray(image.crop((int(x0),int(y0),int(x1),int(y1))).convert('RGB'))
            if not crop.size or (crop.mean(axis=2)<100).mean()<.015:continue
            readings,_=_engine().text_rec([crop])
            if not readings:continue
            value,score=readings[0];value=str(value).strip()
            if score<.85 or not re.search(r'[A-Za-z\u4e00-\u9fff]',value) or len(value)>30:continue
            recovered.append({'text':value,'bbox':[x0,y0,x1,y1],'rotation':0,'confidence':round(float(score),3),'coverage_retry':True,'recovery_method':'local_checkbox_caption_crop'})
        if len(recovered)!=len(row):continue
        left=min(b['bbox'][0] for b in recovered);right=max(r['bbox'][2] for r in row)
        output=[b for b in output if not (b['bbox'][0]>=left-2*scale and b['bbox'][2]<=right+4*scale and abs((b['bbox'][1]+b['bbox'][3])/2-(top+bottom)/2)<8*scale)]
        output.extend(recovered)
    return output
