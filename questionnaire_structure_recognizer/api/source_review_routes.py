"""Authenticated visual OCR corrections, separate from answers and knowledge."""
import hashlib
import json
import math
import uuid
from pathlib import Path

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse

from ..source_text_review import create_source_text_review
from ..source_choice_review import create_source_choice_review


def initialize_source_review_schema(db):
    db.executescript((Path(__file__).resolve().parents[1] / 'migrations/005_source_text_reviews.sql').read_text('utf8'))
    db.executescript((Path(__file__).resolve().parents[1] / 'migrations/006_source_choice_reviews.sql').read_text('utf8'))


def saved_source_reviews(store, task_id, kind='text'):
    table = {'text':'source_text_reviews', 'choice':'source_choice_reviews'}[kind]
    with store.connect() as db:
        return [json.loads(row[0]) for row in db.execute(
            f'SELECT payload_json FROM {table} WHERE task_id=? ORDER BY sequence', (task_id,))]


def source_page(store, task, page_number, region=None):
    """Render only the immutable uploaded local PDF, never a client path."""
    import fitz
    original = store.files / task['storage']
    if original.suffix.lower() != '.pdf':
        raise HTTPException(422, '原页文字校对当前仅用于 PDF 的 OCR 文字；Word 请使用原文结构纠正。')
    if hashlib.sha256(original.read_bytes()).hexdigest() != task['source_hash']:
        raise HTTPException(409, '原文件指纹变化，停止使用旧位置。')
    with fitz.open(original) as pdf:
        if pdf.needs_pass:
            raise HTTPException(422, '此原文件需要密码；请通过已有密码重试流程读取，不长期保存密码。')
        if type(page_number) is not int or not 1 <= page_number <= len(pdf):
            raise HTTPException(422, '页码不存在')
        page = pdf[page_number-1]
        width, height = page.rect.width, page.rect.height
        if not (0 < width < 20000 and 0 < height < 20000):
            raise HTTPException(422, '页面尺寸异常')
        clip = None
        if region is not None:
            if not isinstance(region,list) or len(region)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in region):
                raise HTTPException(422, '来源区域坐标无效')
            clip = (fitz.Rect(region) + (-24,-24,72,24)) & page.rect
            if clip.is_empty:
                raise HTTPException(422, '来源区域不在原页内')
        bounds = clip or page.rect
        scale = min(3.5 if clip else 2.0, math.sqrt(8_000_000/(bounds.width*bounds.height)))
        folder = store.cache/'source-review-pages'/task['source_hash']
        folder.mkdir(parents=True, exist_ok=True)
        suffix = '-' + hashlib.sha256(json.dumps(region).encode()).hexdigest()[:16] if clip else ''
        path = folder/f'page-{page_number:04}{suffix}-v1.png'
        if not path.is_file():
            temporary = folder/f'{uuid.uuid4().hex}.png'
            page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, clip=clip).save(temporary)
            temporary.replace(path)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def install_source_review_routes(app, store, auth):
    @app.get('/api/tasks/{identifier}/source-text-review', dependencies=[Depends(auth)])
    def context(identifier: str, version: int | None = None):
        task = store.task(identifier)
        if not task:
            raise HTTPException(404, '任务不存在')
        envelope = store.result(identifier, version or task.get('latest_analysis_version'))
        if not envelope:
            raise HTTPException(404, '原文尚未读取')
        blocks, choices = [], []
        captions = {}
        def collect(fields):
            for f in fields:
                for option in f.get('options') or []:
                    for ref in option.get('answer_region_refs') or []:
                        captions.setdefault(ref.get('block_id'), []).append((f.get('question_text_original',''),option.get('text_original','')))
                collect(f.get('children') or [])
        collect(envelope['result'].get('questions') or [])
        from ..source_matrix_choices import valid_matrix_review_region
        source_lookup={v['block_id']:v for v in envelope['result'].get('source_blocks',[])}
        for b in envelope['result'].get('source_blocks', []):
            loc = b.get('location') or {}
            structure = b.get('structure') or {}
            matrix_option=valid_matrix_review_region(b,source_lookup)
            if structure.get('scan_candidate') is True and ((structure.get('source_method')=='raster_geometry' and structure.get('scan_region_kind')=='checkbox') or matrix_option) and not b.get('text'):
                for index, control in enumerate(b.get('controls') or []):
                    if control.get('type')=='checkbox' and control.get('bbox')==loc.get('bbox'):
                        choices.append({'block_id':b['block_id'],'control_index':index,'selected':control.get('selected'),
                                        'location':loc,'source':control.get('source'), 'field_options':captions.get(b['block_id'],[])})
            if b.get('kind') != 'ocr_text' or loc.get('extraction') != 'ocr' or not b.get('text') or b.get('controls'):
                continue
            if not b.get('structure', {}).get('source_text_is_ocr'):
                continue
            blocks.append({'block_id': b['block_id'], 'text': b['text'], 'location': loc,
                'original_extracted_text': b.get('structure', {}).get('source_text_review', {}).get('original_extracted_text', b['text'])})
        return {'task_id': identifier, 'filename': task['filename'], 'base_version': envelope['version'],
            'source_hash': task['source_hash'], 'blocks': blocks, 'choices': choices, 'reviews': saved_source_reviews(store, identifier),
            'choice_reviews': saved_source_reviews(store, identifier, 'choice'),
            'status': task['status'], 'knowledge_approval': False}

    @app.get('/api/tasks/{identifier}/source-page/{number}.png', dependencies=[Depends(auth)])
    def image(identifier: str, number: int):
        task = store.task(identifier)
        if not task:
            raise HTTPException(404, '任务不存在')
        path, digest = source_page(store, task, number)
        return FileResponse(path, media_type='image/png', headers={'X-Source-Image-SHA256': digest, 'Cache-Control': 'private, no-cache'})

    @app.get('/api/tasks/{identifier}/source-region/{block_id}.png', dependencies=[Depends(auth)])
    def region_image(identifier: str, block_id: str, version: int):
        task = store.task(identifier)
        envelope = store.result(identifier, version) if task else None
        if not envelope:
            raise HTTPException(404, '来源版本不存在')
        source = next((b for b in envelope['result'].get('source_blocks',[]) if b['block_id']==block_id), None)
        loc = (source or {}).get('location',{})
        if loc.get('type')!='pdf' or not loc.get('bbox'):
            raise HTTPException(422, '未定位到原页区域')
        path, digest = source_page(store,task,loc.get('page'),loc['bbox'])
        return FileResponse(path,media_type='image/png',headers={'X-Source-Image-SHA256':digest,'Cache-Control':'private, no-cache'})

    def save_review(identifier: str, payload: dict, kind):
        table = {'text':'source_text_reviews', 'choice':'source_choice_reviews'}[kind]
        if payload.get('confirmed_against_source') is not True:
            raise HTTPException(422, '请先对照原页确认读取内容。此操作不确认答案适用性，也不授权入库。')
        if type(payload.get('base_version')) is not int or not isinstance(payload.get('note'), str) or not payload['note'].strip():
            raise HTTPException(422, '必须提供基础版本和原文核对说明')
        with store.lock:
            task = store.task(identifier)
            if not task:
                raise HTTPException(404, '任务不存在')
            if identifier in store.running or task['status'] in {'queued', 'processing'}:
                raise HTTPException(409, '此文件正在处理，完成后再核对原文。')
            latest = task.get('latest_analysis_version') or task.get('current_version')
            if latest != payload['base_version'] or payload.get('source_hash') != task['source_hash']:
                raise HTTPException(409, '原文版本已变化，请刷新后核对。')
            envelope = store.result(identifier, latest)
            for review_kind in ('text','choice'):
                saved = saved_source_reviews(store, identifier, review_kind)
                applied = envelope['result'].get(f'source_{review_kind}_review', {}).get('review_digests', [])
                if applied != [r['review_digest'] for r in saved]:
                    raise HTTPException(409, '已有原文校对尚未完成结构分析，请先重试识别；校对记录已保存，无需重复提交。')
            blocks = envelope['result'].get('source_blocks') or []
            lookup = {b['block_id']: b for b in blocks}
            try:
                edits = payload.get('edits')
                if not isinstance(edits, list) or not 1 <= len(edits) <= 1000:
                    raise ValueError('每次校对需要 1–1000 个实际修改')
                pages = {lookup[e['block_id']]['location']['page'] for e in edits}
                evidence = {'method': 'source_image_visual_review', 'note': payload['note'],
                    'pages': [{'page': page, 'image_sha256': source_page(store, task, page)[1]} for page in sorted(pages)]}
                factory = create_source_text_review if kind=='text' else create_source_choice_review
                record = factory(task['source_hash'], blocks, edits,
                    reviewer='authenticated-local-reviewer', evidence=evidence)
            except (KeyError, ValueError, TypeError) as exc:
                raise HTTPException(422, str(exc)) from exc
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT latest_analysis_version,current_version,status FROM tasks WHERE id=?', (identifier,)).fetchone()
                if (current['latest_analysis_version'] or current['current_version']) != latest or current['status'] in {'queued','processing'}:
                    raise HTTPException(409, '原文版本或处理状态已变化')
                sequence = db.execute(f'SELECT COALESCE(MAX(sequence),0)+1 FROM {table} WHERE task_id=?', (identifier,)).fetchone()[0]
                db.execute(f'INSERT INTO {table}(id,task_id,sequence,base_version,source_hash,review_digest,payload_json) VALUES(?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, identifier, sequence, latest, task['source_hash'], record['review_digest'], json.dumps(record, ensure_ascii=False)))
                db.execute("UPDATE tasks SET status='queued',stage='原页校对已保存，准备重新校验结构',error=NULL WHERE id=?", (identifier,))
                db.commit()
        store.submit(identifier, reanalysis=True)
        return {'saved': True, 'sequence': sequence, 'review_digest': record['review_digest'],
                'reanalysis_requested': True, 'knowledge_approval': False, 'original_unchanged': True}

    @app.post('/api/tasks/{identifier}/source-text-review', status_code=202, dependencies=[Depends(auth)])
    def review_text(identifier: str, payload: dict):
        return save_review(identifier, payload, 'text')

    @app.post('/api/tasks/{identifier}/source-choice-review', status_code=202, dependencies=[Depends(auth)])
    def review_choice(identifier: str, payload: dict):
        return save_review(identifier, payload, 'choice')
