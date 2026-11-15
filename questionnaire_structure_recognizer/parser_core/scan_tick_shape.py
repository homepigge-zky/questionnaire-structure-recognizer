"""Prove a right-exiting check mark using original pixels beyond its box.

The independently detected four-sided checkbox is an input requirement.  A
nearby word, dark pixel count, or a clipped diagonal never establishes a mark.
"""
import hashlib

import numpy as np


def right_exit_tick(ink, x, y, w, h, scale):
    import cv2

    if min(w,h)<12:return None
    right=round(w*.50);bottom=round(h*.23)
    if x<0 or y<0 or x+w+right>ink.shape[1] or y+h+bottom>ink.shape[0]:return None
    crop=ink[y:y+h+bottom,x:x+w+right]
    mask=(crop>0).astype(np.uint8)
    # Remove only actual long straight edges belonging to the known box.
    # Their ink must not serve as an artificial joint between unrelated arms.
    edge=max(1,round(scale*.8))
    # Raster contours can describe the inner edge of a printed square. Check
    # the adjacent original pixels too; do not mistake its outer border for
    # an unexplained arm or an external junction.
    rows=mask[:h+edge,:w+edge].mean(axis=1);cols=mask[:h+edge,:w+edge].mean(axis=0)
    for xx in range(w+edge):
        if (xx<w*.15 or xx>=w*.85) and cols[xx]>=.5:mask[:h+edge,xx]=0
    for yy in range(h+edge):
        if (yy<h*.22 or yy>=h*.78) and rows[yy]>=.7:mask[yy,:w+edge]=0
    # An inner contour can also leave a broken second print-border row. Strip
    # only horizontal runs reaching a known side, never an isolated middle
    # junction. Keep all off-border ink for the following shape check.
    for yy in list(range(edge))+list(range(h-1,min(mask.shape[0],h+edge+1))):
        xs=np.flatnonzero(mask[yy,:w+edge])
        for run in np.split(xs,np.where(np.diff(xs)>1)[0]+1):
            if len(run)>=max(3,w*.15) and (run[0]<w*.15 or run[-1]>=w*.85):mask[yy,run]=0
    mask[0,:w]=0;mask[:h,0]=0
    py,px=np.nonzero(mask)
    outside=(px>=w+1)
    if len(px)<20 or np.count_nonzero(outside)<max(5,h*.25):return None
    # The long arm must cross the right edge, rather than merely coexist with
    # a printed word. No stroke may escape below this local joint window.
    if mask[-1,:round(w*.8)].any():return None
    aa=np.arange(.75,12.,.5);bb=np.arange(-4.,-.5,.25);best=None
    for jx in np.arange(w*.2,w*.75,.5):
        for jy in np.arange(h*.5,h+bottom-1,.5):
            if not mask[max(0,int(jy)-1):int(jy)+2,max(0,int(jx)-1):int(jx)+2].any():continue
            da=abs(py[None,:]-jy-aa[:,None]*(px[None,:]-jx))/np.sqrt(1+aa[:,None]**2)
            db=abs(py[None,:]-jy-bb[:,None]*(px[None,:]-jx))/np.sqrt(1+bb[:,None]**2)
            # Each arm occupies its own side of the joint; an X is not a V.
            da[:,px>jx+2]=50;db[:,px<jx-2]=50
            distances=np.minimum(da[:,None,:],db[None,:,:])
            scores=np.mean(np.minimum(distances,5)**2,axis=2)
            a,b=np.unravel_index(np.argmin(scores),scores.shape);score=float(scores[a,b])
            if best is None or score<best[0]:best=(score,jx,jy,float(aa[a]),float(bb[b]),da[a],db[b])
    if best is None:return None
    _,jx,jy,a,b,da,db=best;short=da<db
    fit=float(np.mean(np.minimum(da,db)<=max(1.5,w*.10)))
    if fit<.94 or sum(short)<len(px)*.15 or sum(~short)<len(px)*.2:return None
    if jy-min(py[short])<h*.35 or jy-min(py[~short])<h*.55:return None
    if np.mean(py>jy+max(2,h*.1))>.04:return None
    axes=[]
    for selected in (short,~short):
        vx,vy,xc,yc=cv2.fitLine(np.column_stack((px[selected],py[selected])).astype(np.float32),cv2.DIST_L2,0,.01,.01).flatten()
        if abs(vx)<.01:return None
        slope=float(vy/vx);axes.append((slope,float(yc-slope*xc)))
    if axes[0][0]<=.4 or axes[1][0]>=-.4:return None
    ix=(axes[1][1]-axes[0][1])/(axes[0][0]-axes[1][0]);iy=axes[0][0]*ix+axes[0][1]
    if not w*.15<ix<w*.8 or not h*.45<iy<h+bottom+1:return None
    if abs(ix-jx)>3 or abs(iy-jy)>3:return None
    joint=mask[max(0,int(jy)-1):int(jy)+2,max(0,int(jx)-1):int(jx)+2]
    if not joint.any():return None
    # Require original connected ink around the observed junction.  A bottom
    # border cannot connect separated arms because it was removed above.
    near=mask[max(0,int(jy)-4):min(mask.shape[0],int(jy)+4),max(0,int(jx)-5):min(mask.shape[1],int(jx)+6)]
    count,_,stats,_=cv2.connectedComponentsWithStats(near,8)
    if count<2 or max(stats[1:,cv2.CC_STAT_AREA])<near.sum()*.85:return None
    return {'method':'bounded_two_stroke_tick_v1','shape_variant':'observed_right_exit_joint',
        'patch_sha256':hashlib.sha256(crop.tobytes()).hexdigest(),'crop_shape':list(crop.shape),
        'observed_box_shape':[h,w],'observed_joint':[round(float(jx),3),round(float(jy),3)],
        'observed_arm_intersection':[round(ix,3),round(iy,3)],'right_exit_pixels':int(np.count_nonzero(outside)),
        'short_arm_slope':a,'long_arm_slope':b,'ink_fit_fraction':round(fit,4),
        'two_stroke_shape_verified':True}
