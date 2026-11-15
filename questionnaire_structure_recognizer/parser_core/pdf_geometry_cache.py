"""Re-measure scanned form geometry without re-reading or inventing OCR text."""
from __future__ import annotations

import hashlib
import io
import os
import time
from copy import deepcopy
from pathlib import Path

import fitz
from PIL import Image

from .models import ExtractedDocument
from .ocr import RASTER_GEOMETRY_VERSION


def refresh_scan_geometry(path: Path, native: ExtractedDocument, progress=None) -> ExtractedDocument:
    """Return a new native checkpoint, preserving exact recorded OCR evidence.

    Only a same-file, explicitly OCR-enabled, successfully read scan checkpoint
    qualifies. Mixed native/image pages require a complete saved background OCR
    ledger; silently substituting OCR already filtered by native text would
    make native-overlay validation unreliable, so those checkpoints are refused.
    No OCR engine, model, external service or database is invoked here.
    """
    from .pdf_parser import _scan_layout_blocks, _scan_background, _attach_native_overlay

    started=time.perf_counter();path=Path(path)
    checksum=hashlib.sha256(path.read_bytes()).hexdigest()
    if native.source_hash!=checksum or native.file_type!='PDF' or native.metadata.get('ocr_enabled') is not True:
        raise ValueError('OCR 几何刷新必须使用相同指纹、已经明确启用 OCR 的原 PDF')
    if native.extraction_status=='failed':raise ValueError('失败的原文检查点不能用来刷新几何')
    updated=native.model_copy(deep=True);old_by_page={}
    for block in native.source_blocks:old_by_page.setdefault(block.location.get('page'),[]).append(block)
    ocr_pages=set(native.metadata.get('ocr_pages') or [])
    native_pages=set(native.metadata.get('native_text_pages') or [])
    dpi=int(os.getenv('QSR_OCR_DPI','220'));ledger=[];output=[]
    background_ledgers=native.metadata.get('ocr_background_ledgers',{})
    with fitz.open(path) as pdf:
        if pdf.needs_pass or pdf.is_repaired:raise ValueError('受保护或已修复 PDF 需要重新原生读取')
        if ocr_pages-set(range(1,len(pdf)+1)):raise ValueError('OCR 页码不属于当前原件')
        for pn,page in enumerate(pdf,1):
            old=old_by_page.get(pn,[])
            if pn not in ocr_pages:
                output.extend(b.model_copy(deep=True) for b in old);continue
            if progress:progress('extracting',f'重新校验第 {pn}/{len(pdf)} 页填写位置（复用已有 OCR 文字）',pn,len(pdf))
            mixed=pn in native_pages
            if mixed and str(pn) not in background_ledgers:
                raise ValueError(f'第 {pn} 页是混合页面，缺少未过滤的背景 OCR 检查点；需要重新读取该页')
            pix=page.get_pixmap(dpi=dpi,alpha=False)
            visible=Image.open(io.BytesIO(pix.tobytes('png')))
            image=_scan_background(page,dpi) if mixed else visible
            sx,sy=image.width/page.rect.width,image.height/page.rect.height
            original_text=[b for b in old if b.text and b.location.get('extraction') in {'ocr','ocr_region'}]
            if not original_text:raise ValueError(f'第 {pn} 页没有可复用的 OCR 原文')
            if any(len(b.location.get('bbox',[]))!=4 or not isinstance(b.style.get('ocr_confidence'),(int,float)) for b in original_text):
                raise ValueError('OCR 检查点缺少实际位置或置信度')
            if mixed:
                recognized=deepcopy(background_ledgers[str(pn)])
                recognized=[{**r,'bbox':[r['bbox'][0]*sx,r['bbox'][1]*sy,r['bbox'][2]*sx,r['bbox'][3]*sy]} for r in recognized]
            else:
                recognized=[{'text':b.text,'confidence':b.style['ocr_confidence'],
                    'bbox':[b.location['bbox'][0]*sx,b.location['bbox'][1]*sy,b.location['bbox'][2]*sx,b.location['bbox'][3]*sy]} for b in original_text]
            loc={'type':'pdf','page':pn,'coordinate_system':'PDF points, top-left origin'}
            physical=_scan_layout_blocks(image,recognized,loc,page.rect.width,page.rect.height)
            kept=[b.model_copy(deep=True) for b in old if not b.structure.get('scan_candidate')]
            if mixed:
                for b in kept:
                    for key in ('native_overlay_answer_region','native_overlay_overflow_evidence'):b.structure.pop(key,None)
                    if b.structure.get('resolved_layout_role') in {'checkbox_symbol','invisible_native_mark'}:b.structure.pop('resolved_layout_role',None)
                background=[{**r,'bbox':[r['bbox'][0]/sx,r['bbox'][1]/sy,r['bbox'][2]/sx,r['bbox'][3]/sy]} for r in recognized]
                _attach_native_overlay(physical,kept,visible,page.rect.width,background)
                for b in physical+kept:b.structure['scan_page_context']=True
            page_blocks=kept+physical
            if not any(b.structure.get('rotation') for b in kept):
                page_blocks.sort(key=lambda b:(round(b.location.get('bbox',[0,0])[1],2),b.location.get('bbox',[0])[0],0 if b.kind=='table_cell' else 1))
            output.extend(page_blocks)
            ledger.append({'page':pn,'ocr_text_blocks_preserved':len(original_text),
                'old_region_ids':[b.block_id for b in old if b.structure.get('scan_candidate')],
                'new_region_ids':[b.block_id for b in physical],
                'render_sha256':hashlib.sha256(image.tobytes()).hexdigest()})
    updated.source_blocks=output
    for order,block in enumerate(updated.source_blocks,1):block.order=order
    old_text={b.block_id:(b.text,b.location,b.style) for b in native.source_blocks if b.text}
    new_text={b.block_id:(b.text,b.location,b.style) for b in updated.source_blocks if b.text}
    if old_text!=new_text:raise ValueError('几何刷新不得改变任何原文、位置或 OCR 置信度')
    updated.metadata['raster_geometry_version']=RASTER_GEOMETRY_VERSION
    updated.metadata['geometry_refresh']={'from_version':native.metadata.get('raster_geometry_version','legacy'),
        'to_version':RASTER_GEOMETRY_VERSION,'source_hash':checksum,'ocr_reused':True,'model_called':False,
        'pages':ledger,'seconds':round(time.perf_counter()-started,4)}
    return updated
