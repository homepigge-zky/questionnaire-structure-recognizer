"""Read only ambiguous captions beside proved raster boxes, locally.

The crop recognizer cannot create source text: its output must identify one
exact, nonempty substring of the existing same-page OCR ledger. Geometry and
crop hashes are retained separately from the source text and selection state.
"""
from __future__ import annotations
import hashlib
import io
import json
import re
import time
from pathlib import Path
import fitz
import numpy as np
from PIL import Image

VERSION='pdf-caption-fragments-v4-merged-caption-budget'


def _inside(a,b):
    return len(a)==4 and len(b)==4 and b[0]-3<=a[0] and a[2]<=b[2]+3 and b[1]-3<=(a[1]+a[3])/2<=b[3]+3


def fragment_candidates(blocks):
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box']
    marks=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='checkbox' and
           any(isinstance(c.get('selected'),bool) for c in b.get('controls',[]))]
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr']
    output=[];used=set()
    for cell in sorted(cells,key=lambda b:(b['location']['bbox'][2]-b['location']['bbox'][0])*(b['location']['bbox'][3]-b['location']['bbox'][1])):
        cb=cell['location']['bbox'];page=cell['location']['page']
        local=[b for b in marks if b['location']['page']==page and _inside(b['location']['bbox'],cb)]
        if not 2<=len(local)<=16:continue
        groups=[]
        for mark in sorted(local,key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0])):
            mb=mark['location']['bbox'];cy=(mb[1]+mb[3])/2
            group=next((g for g in groups if abs(g[0]-cy)<8),None)
            if group:group[1].append(mark)
            else:groups.append([cy,[mark]])
        for _,row in groups:
            if not row:continue
            row.sort(key=lambda b:b['location']['bbox'][0]);centers=[(b['location']['bbox'][1]+b['location']['bbox'][3])/2 for b in row]
            lines=[b for b in texts if b['location']['page']==page and _inside(b['location']['bbox'],cb) and
                abs((b['location']['bbox'][1]+b['location']['bbox'][3])/2-sum(centers)/len(centers))<8]
            # A merged line or a leading mark included in the OCR glyph box
            # needs a localized caption reading. Crops remain inside the
            # independent closed panel and never include the checkbox itself.
            merged=any(sum(b['location']['bbox'][0]-3<=m['location']['bbox'][0]<=b['location']['bbox'][2] for m in row)>=2 for b in lines)
            embedded=any(b['location']['bbox'][0]<=m['location']['bbox'][0]+2 and b['location']['bbox'][2]>m['location']['bbox'][2]+5 for m in row for b in lines)
            if not merged and not embedded:continue
            for i,mark in enumerate(row):
                if mark['block_id'] in used:continue
                mb=mark['location']['bbox'];cy=(mb[1]+mb[3])/2
                source=[b for b in lines if abs((b['location']['bbox'][1]+b['location']['bbox'][3])/2-cy)<8 and b['location']['bbox'][2]>mb[2]]
                if not source:continue
                right=row[i+1]['location']['bbox'][0]-1 if i+1<len(row) else min(cb[2]-1,max(b['location']['bbox'][2] for b in source))
                crop=[mb[2]+.5,min(b['location']['bbox'][1] for b in source)-1,right,max(b['location']['bbox'][3] for b in source)+1]
                if crop[2]-crop[0]<5 or not _inside(crop,cb):continue
                output.append({'mark_id':mark['block_id'],'container_id':cell['block_id'],'page':page,'crop_bbox':crop,
                    'needs_multibox_split':merged,
                    'source_blocks':[{'block_id':b['block_id'],'text':b['text'],'bbox':b['location']['bbox']} for b in source]})
                used.add(mark['block_id'])
    # The finite local-read budget must first cover OCR lines spanning several
    # actual controls. Independently readable Yes/No blocks cannot consume it
    # merely because their containing cells are smaller.
    return sorted(output,key=lambda c:(not c['needs_multibox_split'],c['page'],c['crop_bbox'][1],c['crop_bbox'][0]))


def _caption_units(text):
    """Comparison only: preserve every source offset and every sign/digit.

    ℃ is a compatibility spelling of °C. Some local crop readings contain
    the printed degree plus the recognizer's whole ℃ glyph; collapse only
    that exact sequence, never repeated digits, signs, or arbitrary letters.
    The original ledger and returned value are never normalized.
    """
    units=[];i=0
    while i<len(text):
        if text.startswith('°℃',i):units.extend([('°',i,i+2),('C',i,i+2)]);i+=2
        elif text[i]=='℃':units.extend([('°',i,i+1),('C',i,i+1)]);i+=1
        elif text[i].isspace():i+=1
        else:units.append(({'：':':','；':';','（':'(','）':')'}.get(text[i],text[i]),i,i+1));i+=1
    return units


def _caption_equivalent(a,b):
    return ''.join(x[0] for x in _caption_units(a))==''.join(x[0] for x in _caption_units(b))


def _exact_ref(reading,candidate):
    text,score=reading
    if not isinstance(text,str) or not text.strip() or score<.9:return None
    text=text.strip();matches=[]
    needle=''.join(x[0] for x in _caption_units(text))
    for source in candidate['source_blocks']:
        units=_caption_units(source['text']);haystack=''.join(x[0] for x in units)
        for match in re.finditer(re.escape(needle),haystack):
            a,z=units[match.start()][1],units[match.end()-1][2]
            if not _caption_equivalent(source['text'][a:z],text):continue
            matches.append({'block_id':source['block_id'],'start':a,'end':z,'slot_id':''})
    return matches[0] if len(matches)==1 else None


def enrich_scanned_caption_fragments(path,native,cache_dir,*,ocr_enabled=False):
    if not ocr_enabled:return {'status':'disabled','version':VERSION,'ocr_calls':0}
    started=time.perf_counter();path=Path(path)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=native.get('source_hash'):raise ValueError('选项局部读取的原文件指纹不一致')
    candidates=fragment_candidates(native.get('source_blocks',[]))
    if not candidates:return {'status':'not_needed','version':VERSION,'ocr_calls':0}
    material={'version':VERSION,'source_hash':native['source_hash'],'candidates':candidates,'dpi':220}
    key=hashlib.sha256(json.dumps(material,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    cache=Path(cache_dir)/'caption-fragments';cache.mkdir(parents=True,exist_ok=True);dest=cache/(key+'.json')
    hit=dest.exists();calls=0
    if hit:results=json.loads(dest.read_text(encoding='utf8'))['results']
    else:
        from .ocr import _engine
        results=[]
        with fitz.open(path) as pdf:
            if pdf.needs_pass:raise ValueError('受保护PDF不能绕过密码读取选项')
            for page in sorted({c['page'] for c in candidates[:96]}):
                source=pdf[page-1];im=Image.open(io.BytesIO(source.get_pixmap(dpi=220,alpha=False).tobytes('png')));sx=im.width/source.rect.width;sy=im.height/source.rect.height
                items=[c for c in candidates[:96] if c['page']==page];crops=[]
                for c in items:
                    coords=[int(v*(sx if i%2==0 else sy)) for i,v in enumerate(c['crop_bbox'])]
                    crops.append(np.asarray(im.crop(tuple(coords)).convert('RGB')))
                readings,_=_engine().text_rec(crops);calls+=1
                for candidate,crop,reading in zip(items,crops,readings):
                    rr=_exact_ref(reading,candidate)
                    results.append({'candidate':candidate,'reading':str(reading[0]),'score':float(reading[1]),'ref':rr,
                        'patch_sha256':hashlib.sha256(crop.tobytes()).hexdigest(),'crop_shape':list(crop.shape)})
        temp=dest.with_suffix('.tmp');temp.write_text(json.dumps({'material':material,'results':results},ensure_ascii=False),encoding='utf8');temp.replace(dest)
    lookup={b['block_id']:b for b in native.get('source_blocks',[])};accepted=0
    for item in results:
        c=item['candidate'];rr=item.get('ref');mark=lookup.get(c['mark_id']);source=lookup.get((rr or {}).get('block_id'),{})
        if not mark or not rr or not _caption_equivalent(source.get('text','')[rr['start']:rr['end']],item['reading'].strip()):continue
        mark.setdefault('structure',{})['scan_option_caption_crop']={
            'method':VERSION,'container_ref':{'block_id':c['container_id'],'start':0,'end':0,'slot_id':''},
            'caption_refs':[rr],'source_hash':native['source_hash'],'page':c['page'],'crop_bbox':c['crop_bbox'],
            'reading':item['reading'].strip(),'source_caption':source['text'][rr['start']:rr['end']],
            'alignment':'whitespace_and_celsius_glyphs_only','confidence':item['score'],'patch_sha256':item['patch_sha256'],'crop_shape':item['crop_shape']}
        accepted+=1
    report={'status':'complete' if len(results)==len(candidates) else 'partial','version':VERSION,'candidate_count':len(candidates),
        'accepted':accepted,'unresolved':len(candidates)-accepted,'ocr_calls':calls,'cache_hit':hit,'seconds':round(time.perf_counter()-started,4)}
    native.setdefault('metadata',{})['caption_fragment_analysis']=report
    return report


def verified_caption_crop(mark,rr,lookup):
    evidence=mark.get('structure',{}).get('scan_option_caption_crop') or {};b=lookup.get(rr.get('block_id'),{})
    container=lookup.get(evidence.get('container_ref',{}).get('block_id'),{});cl=container.get('location',{});ml=mark.get('location',{});sl=b.get('location',{})
    mb=ml.get('bbox',[]);cb=cl.get('bbox',[]);crop=evidence.get('crop_bbox',[])
    a,z=rr.get('start'),rr.get('end')
    return bool(evidence.get('method')==VERSION and rr in evidence.get('caption_refs',[]) and
        re.fullmatch(r'[0-9a-f]{64}',str(evidence.get('patch_sha256',''))) and evidence.get('confidence',0)>=.9 and
        isinstance(a,int) and isinstance(z,int) and 0<=a<z<=len(b.get('text','')) and _caption_equivalent(b['text'][a:z],evidence.get('reading','')) and
        ml.get('type')=='pdf' and ml.get('page')==sl.get('page')==cl.get('page')==evidence.get('page') and
        mark.get('structure',{}).get('scan_region_kind')=='checkbox' and container.get('structure',{}).get('scan_region_kind')=='text_box' and
        _inside(mb,cb) and _inside(crop,cb) and _inside(sl.get('bbox',[]),cb) and
        0<=crop[0]-mb[2]<=2 and abs((crop[1]+crop[3]-mb[1]-mb[3])/2)<8 and
        not any(other['block_id']!=mark['block_id'] and other.get('structure',{}).get('scan_region_kind')=='checkbox' and
            other.get('location',{}).get('page')==ml['page'] and _inside(other['location'].get('bbox',[]),crop) for other in lookup.values()))
