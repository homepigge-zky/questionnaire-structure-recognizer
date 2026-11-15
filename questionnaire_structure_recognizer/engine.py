from __future__ import annotations

import json
import os
import hashlib
import time
from pathlib import Path

from .parser_core import extract_document
from .understanding import ModelUnavailable, analyze_blocks
from .understanding.ollama import analyze_field_hints
from .understanding.local_pairs import refine_stacked_pairs
from .form_positions import enrich_result_form_positions
from .validation import validate_and_assemble
from .pipeline_revision import PIPELINE_REVISION


ENGINE_VERSION = "qsr-engine-v36-reviewed-region-boundaries"


def _native_cache_material(source: Path, checksum: str, ocr_enabled: bool, *, parser_version=None) -> dict:
    from .parser_core.docx_parser import PARSER_VERSION as WORD_VERSION
    from .parser_core.pdf_parser import PARSER_VERSION as PDF_VERSION
    is_pdf = source.suffix.lower() == '.pdf'
    # recognize() resolves the global default before this point. An explicit
    # task permission is authoritative; keying the unused global default again
    # made identical approved OCR results miss whenever a worker set it false.
    options = {k: v for k, v in os.environ.items() if k.startswith(('QSR_OCR_', 'QSR_MAX_', 'QSR_PDF_'))
               and k != 'QSR_OCR_ENABLED'}
    return {'hash': checksum, 'parser': parser_version or (PDF_VERSION if is_pdf else WORD_VERSION),
        'ocr': bool(ocr_enabled) if is_pdf else False, 'options': options,
        'conversion': 'doc-conversion-v2-isolated-macro-disabled' if source.suffix.lower() == '.doc' else None}


def native_cache_key(source: Path, checksum: str, ocr_enabled: bool, *, parser_version=None) -> str:
    material = _native_cache_material(source, checksum, ocr_enabled, parser_version=parser_version)
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _equivalent_native_cache_keys(source, checksum, ocr_enabled, *, parser_version=None):
    """Canonical first, then exact legacy keys differing only in unused default.

    Effective OCR permission, hash, parser and every other extraction/resource
    option stay identical. No cache-directory guessing or omitted safeguards.
    """
    material = _native_cache_material(source, checksum, ocr_enabled, parser_version=parser_version)
    keys = [hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()]
    for setting in dict.fromkeys([os.getenv('QSR_OCR_ENABLED'), 'false', 'true', '0', '1', 'no', 'yes']):
        if setting is None:
            continue
        legacy = {**material, 'options': {**material['options'], 'QSR_OCR_ENABLED': setting}}
        keys.append(hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest())
    return list(dict.fromkeys(keys))


def _upgrade_pdf_span_cache(source, cache_dir, checksum, ocr_enabled, progress):
    """Reuse unchanged OCR only after an actual, exact native-source re-read.

    v19 adds native line spans; v20 rejects bad text encoding. Neither changes
    OCR for a page with valid encoding. Bad-encoding pages always need a fresh
    extraction. A scan with OCR disabled has no text, which is not corruption.
    """
    from .parser_core.models import ExtractedDocument
    from .parser_core.pdf_parser import PARSER_VERSION
    import fitz
    v18 = 'pdf-native-v18-cross-border-mark-position-evidence'
    v19 = 'pdf-native-v19-physical-native-line-spans'
    v20 = 'pdf-native-v20-validated-native-encoding'
    allowed = {v19: [v18], v20: [v19, v18]}
    if PARSER_VERSION not in allowed or not ocr_enabled:
        return None
    candidates = [(version, key) for version in allowed[PARSER_VERSION]
                  for key in _equivalent_native_cache_keys(source, checksum, True, parser_version=version)]
    existing = [(version, key, Path(cache_dir) / 'native' / (key + '.json'))
                for version, key in candidates if (Path(cache_dir) / 'native' / (key + '.json')).exists()]
    if not existing:
        return None
    try:
        # Check the physical file even when this helper is called directly.
        if hashlib.sha256(Path(source).read_bytes()).hexdigest() != checksum:
            return None
        with fitz.open(source) as pdf:
            if pdf.needs_pass or pdf.is_repaired or not len(pdf):
                return None
            page_count = len(pdf)
            actual_images = set()
            for page_number, page in enumerate(pdf, 1):
                for info in page.get_images(full=True):
                    for rect in page.get_image_rects(info[0]):
                        actual_images.add((page_number, info[0], *(round(v, 3) for v in rect)))
        # Prefer the newest audited checkpoint. A disagreement does not fall
        # back through older checkpoints to find a more permissive result.
        before, old_key, old_file = existing[0]
        old = ExtractedDocument.model_validate_json(old_file.read_text(encoding='utf8'))
        if (old.source_hash != checksum or old.parser_version != before or old.extraction_status == 'failed'
                or old.metadata.get('ocr_enabled') is not True
                or old.metadata.get('unusable_native_text_pages')):
            return None
        fresh = extract_document(source, work_dir=Path(cache_dir) / 'converted', ocr_enabled=False)
        if (fresh.source_hash != checksum or fresh.parser_version != PARSER_VERSION
                or fresh.metadata.get('unusable_native_text_pages')):
            return None
        pages = set(range(1, page_count + 1))
        old_native_pages = set(old.metadata.get('native_text_pages') or [])
        old_ocr_pages = set(old.metadata.get('ocr_pages') or [])
        old_unread_pages = {r.get('location', {}).get('page') for r in old.unread_regions if r.get('kind') == 'page'}
        if (old_native_pages != set(fresh.metadata.get('native_text_pages') or [])
                or old_native_pages | old_ocr_pages | old_unread_pages != pages):
            return None
        image_ledger = set()
        for block in fresh.source_blocks:
            if block.location.get('extraction') == 'native_image_reference':
                loc = block.location
                image_ledger.add((loc['page'], loc['xref'], *(round(v, 3) for v in loc['bbox'])))
        if image_ledger != actual_images:
            return None
        if fresh.extraction_status == 'failed':
            # All scan pages must have actual images and saved successful OCR.
            # Other failed parses, absent image evidence, or unknown warnings
            # never qualify as the expected OCR-disabled/no-text case.
            if (old_native_pages or old_ocr_pages != pages
                    or {x[0] for x in image_ledger} != pages
                    or any(b.kind != 'image' or b.text for b in fresh.source_blocks)
                    or {w.get('code') for w in fresh.warnings} - {'ocr_disabled', 'unread_image_content'}
                    or {r.get('location', {}).get('page') for r in fresh.unread_regions
                        if r.get('kind') == 'page'} != pages):
                return None
        old_native = {b.block_id: b for b in old.source_blocks if b.location.get('extraction') == 'native'}
        fresh_native = {b.block_id: b for b in fresh.source_blocks if b.location.get('extraction') == 'native'}
        # Comparing both sets catches newly appearing or omitted physical cells.
        if old_native.keys() != fresh_native.keys():
            return None
        if (len(old_native) != sum(b.location.get('extraction') == 'native' for b in old.source_blocks)
                or len(fresh_native) != sum(b.location.get('extraction') == 'native' for b in fresh.source_blocks)):
            return None
        enriched = 0
        for block_id, block in old_native.items():
            current = fresh_native[block_id]
            if (current.kind != block.kind or current.text != block.text or current.location != block.location
                    or current.controls != block.controls or current.style != block.style):
                return None
            # These existing keys come from the saved OCR overlay, not from
            # native reading. Retain them verbatim after native evidence agrees.
            previous_structure = dict(block.structure)
            current_structure = dict(current.structure)
            if (previous_structure.get('scan_page_context') is True
                    and block.text.strip() in {'√', '✓', '✔', '☑', '☒'}
                    and previous_structure.get('resolved_layout_role') in {'checkbox_symbol', 'invisible_native_mark'}
                    and 'resolved_layout_role' not in current_structure):
                previous_structure.pop('resolved_layout_role')
            for key in ('scan_page_context', 'native_overlay_answer_region', 'native_overlay_overflow_evidence'):
                previous_structure.pop(key, None)
                current_structure.pop(key, None)
            previous_spans = previous_structure.pop('native_text_spans', None)
            spans = current_structure.pop('native_text_spans', None)
            if previous_structure != current_structure or (before == v19 and previous_spans != spans):
                return None
            if spans is not None:
                block.structure['native_text_spans'] = spans
                enriched += 1
        old.parser_version = PARSER_VERSION
        old.filename = source.name
        old.metadata['native_span_upgrade'] = {
            'from_parser': before, 'to_parser': PARSER_VERSION,
            'source_hash': checksum, 'verified_native_cells': enriched,
            'verified_native_blocks': len(old_native), 'verified_page_count': page_count,
            'verified_image_regions': len(image_ledger), 'native_encoding_verified': True,
            'method': 'actual_native_reread_and_exact_structure_comparison',
            'ocr_reused': True, 'original_cache_key': old_key,
        }
        if progress:
            progress('extracting', '已重新校验原生位置，并复用同一原件的本地 OCR 结果', 1, 1)
        return old
    except (ValueError, KeyError, OSError, RuntimeError, TypeError):
        return None


def _read_native(path, cache_dir, password, ocr_enabled, progress):
    """Versioned extraction cache, separate from model and workflow answers."""
    from .parser_core.models import ExtractedDocument
    from .parser_core.docx_parser import PARSER_VERSION as WORD_VERSION
    from .parser_core.pdf_parser import PARSER_VERSION as PDF_VERSION
    source = Path(path)
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    protected = bool(password)
    if source.suffix.lower() == '.pdf':
        import fitz
        with fitz.open(source) as pdf:
            protected = protected or pdf.needs_pass
    key = native_cache_key(source, checksum, ocr_enabled)
    cache = Path(cache_dir) / 'native' / (key + '.json')
    existing = next((Path(cache_dir) / 'native' / (candidate + '.json')
        for candidate in _equivalent_native_cache_keys(source, checksum, ocr_enabled)
        if (Path(cache_dir) / 'native' / (candidate + '.json')).exists()), None) if not protected else None
    if existing is not None:
        try:
            value = ExtractedDocument.model_validate_json(existing.read_text(encoding='utf8'))
            current_parser = PDF_VERSION if source.suffix.lower() == '.pdf' else WORD_VERSION
            permission_matches = (source.suffix.lower() != '.pdf'
                                  or value.metadata.get('ocr_enabled') is bool(ocr_enabled))
            if value.source_hash == checksum and value.parser_version == current_parser and permission_matches:
                if source.suffix.lower() == '.doc':
                    from .parser_core.router import converted_doc_template
                    template = converted_doc_template(source, Path(cache_dir) / 'converted')
                    if hashlib.sha256(template.read_bytes()).hexdigest() != value.metadata.get('converted_template_hash'):
                        raise ValueError('转换模板已变化')
                value.filename = source.name
                if existing != cache:
                    value.metadata['native_cache_key_migration'] = {
                        'original_cache_key': existing.stem, 'canonical_cache_key': key,
                        'method': 'same_effective_ocr_permission_ignoring_unused_global_default',
                    }
                    temporary = cache.with_suffix('.' + str(time.time_ns()) + '.tmp')
                    temporary.write_text(value.model_dump_json(), encoding='utf8')
                    temporary.replace(cache)
                if progress: progress('extracting', '已复用相同文件的原文提取检查点', 1, 1)
                return value, True
        except (ValueError, KeyError):
            pass
    upgraded = (_upgrade_pdf_span_cache(source, cache_dir, checksum, ocr_enabled, progress)
        if not protected and source.suffix.lower() == '.pdf' else None)
    value = upgraded or extract_document(path, password=password, work_dir=Path(cache_dir) / 'converted',
        progress=lambda done, total, state: progress('extracting', f'读取 PDF：第 {done}/{total} 页 · {state}', done, total) if progress else None,
        ocr_enabled=ocr_enabled)
    if not protected and value.extraction_status != 'failed':
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix('.' + str(time.time_ns()) + '.tmp')
        temporary.write_text(value.model_dump_json(), encoding='utf8')
        temporary.replace(cache)
    return value, False


def _current_pdf_geometry(path, value, cache_dir, password, ocr_enabled, progress=None):
    """Recompute changed raster evidence while preserving the original OCR cache."""
    from .parser_core.ocr import RASTER_GEOMETRY_VERSION
    from .parser_core.models import ExtractedDocument
    if (Path(path).suffix.lower() != '.pdf' or not ocr_enabled or not value.metadata.get('ocr_pages')
            or value.metadata.get('raster_geometry_version') == RASTER_GEOMETRY_VERSION):
        return value
    original_digest = hashlib.sha256(value.model_dump_json().encode()).hexdigest()
    key = hashlib.sha256((original_digest + RASTER_GEOMETRY_VERSION +
        native_cache_key(Path(path), value.source_hash, bool(ocr_enabled))).encode()).hexdigest()
    checkpoint = Path(cache_dir) / 'native-geometry' / (key + '.json')
    if not password and checkpoint.is_file():
        try:
            existing = ExtractedDocument.model_validate_json(checkpoint.read_text(encoding='utf8'))
            if (existing.source_hash == value.source_hash and existing.parser_version == value.parser_version
                    and existing.metadata.get('raster_geometry_version') == RASTER_GEOMETRY_VERSION
                    and existing.metadata.get('geometry_parent_digest') == original_digest):
                if progress: progress('extracting', '已复用校验过的填写区域与勾选位置检查点', 1, 1)
                return existing
        except (ValueError, OSError):
            pass
    from .parser_core.pdf_geometry_cache import refresh_scan_geometry
    try:
        refreshed = refresh_scan_geometry(Path(path), value, progress)
    except ValueError:
        # Incomplete mixed-page OCR ledgers cannot be reconstructed from the
        # filtered visible text. Re-read the actual source using existing
        # explicit per-task OCR consent; never invent missing observations.
        if progress: progress('extracting', '旧位置检查点证据不完整，重新读取原页并核对填写位置', 0, None)
        refreshed = extract_document(path, password=password, work_dir=Path(cache_dir) / 'converted',
            ocr_enabled=bool(ocr_enabled), progress=lambda done, total, state:
                progress('extracting', f'读取 PDF：第 {done}/{total} 页 · {state}', done, total) if progress else None)
    if refreshed.source_hash != value.source_hash:
        raise ValueError('填写位置校验期间原文件指纹变化，不能复用旧分析')
    refreshed.metadata['geometry_parent_digest'] = original_digest
    if not password and refreshed.extraction_status != 'failed':
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = checkpoint.with_suffix('.' + str(time.time_ns()) + '.tmp')
        temporary.write_text(refreshed.model_dump_json(), encoding='utf8')
        temporary.replace(checkpoint)
    return refreshed


def recognize(path: str | Path, cache_dir: str | Path, password: str = "", use_model: bool = True, progress=None, ocr_enabled: bool | None = None, source_text_reviews: list | None = None, source_choice_reviews: list | None = None):
    started = time.perf_counter()
    if ocr_enabled is None:
        ocr_enabled = os.getenv("QSR_OCR_ENABLED", "false").lower() in {"true", "1", "yes"}
    if progress:
        progress("extracting", "忠实提取原文与来源位置", 0, None)
    extracted, native_cache_hit = _read_native(path, cache_dir, password, ocr_enabled, progress)
    extracted = _current_pdf_geometry(path, extracted, cache_dir, password, ocr_enabled, progress)
    extraction_seconds = round(time.perf_counter() - started, 4)
    native_result=extracted.model_dump()
    review_audit = None
    choice_audit = None
    model_cache = Path(cache_dir) / 'model'
    if source_text_reviews or source_choice_reviews:
        from .validation.validator import _unique_pdf_observations
        native_result['source_blocks'], coincident = _unique_pdf_observations(native_result['source_blocks'])
        if coincident:
            native_result.setdefault('metadata', {})['coincident_source_observations'] = coincident
    if source_text_reviews:
        from .source_text_review import apply_source_text_reviews
        native_result['source_blocks'], review_audit = apply_source_text_reviews(
            extracted.source_hash, native_result['source_blocks'], source_text_reviews)
        native_result.setdefault('metadata', {})['source_text_review'] = review_audit
        # Recompute relations and offsets from corrected text. The immutable
        # original/native cache and unrelated model checkpoints remain intact.
        model_cache = model_cache / 'source-reviews'
        if progress: progress('validating', '已应用原页文字校对，重新分析来源关系', 0, None)
    if source_choice_reviews:
        # Choice readings are applied after local geometry observation. The
        # model sees original source positions; validated source choices, not
        # model-selected states, determine the assembled existing answers.
        model_cache = Path(cache_dir) / 'model' / 'source-reviews'
    from .validation.form_completion import prepare_form_regions
    prepare_form_regions(native_result['source_blocks'])
    outputs, model_info = [], None
    if use_model and any(b.text.strip() for b in extracted.source_blocks):
        try:
            if progress:
                progress("understanding", "准备本地结构理解模型", 0, None)
            # Physical blank cells are location evidence, not disposable text.
            # Merge continuations and declared structural spacers are excluded;
            # actual blank cells reach Qwen together with row/column geometry.
            model_blocks = [
                b for b in native_result['source_blocks']
                if b.get('structure',{}).get("blank_state") not in {"structural_spacer", "merge_continuation"}
                and b['kind'] not in {'image','unsupported'}
            ]
            outputs, model_info = analyze_blocks(
                model_blocks, extracted.source_hash,
                extracted.parser_version, model_cache,
                lambda done, total, state: progress("understanding", f"结构批次 {done}/{total}：{state}", done, total) if progress else None,
            )
        except ModelUnavailable as exc:
            extracted.warnings.append({"code": "model_unavailable", "message": str(exc)})
            native_result.setdefault('warnings', []).append({"code": "model_unavailable", "message": str(exc)})
    if Path(path).suffix.lower() == '.pdf':
        # Deterministic native drawing checks supplement the model's source
        # references. They neither regenerate text nor require unchanged model
        # batches to run again. Do not claim the model saw this added geometry.
        from copy import deepcopy
        from .parser_core.pdf_vector_forms import enrich_native_vector_forms
        candidate = deepcopy(native_result)
        try:
            vector_info = enrich_native_vector_forms(path, candidate, password=password)
            if vector_info.get('status') == 'complete':
                vector_info['analysis_stage'] = 'post_model_native_geometry_validation'
                native_result = candidate
        except (ValueError, RuntimeError, OSError) as exc:
            native_result.setdefault('warnings', []).append({'code':'native_vector_validation_failed',
                'message':'PDF 原生选择框和填写边界未完成校验，相关位置须核对原页。','diagnostic':type(exc).__name__})
        if ocr_enabled:
            from .parser_core.pdf_caption_fragments import enrich_scanned_caption_fragments
            try:
                enrich_scanned_caption_fragments(path,native_result,Path(cache_dir),ocr_enabled=True)
            except (ValueError, RuntimeError, OSError) as exc:
                native_result.setdefault('warnings',[]).append({'code':'scan_caption_fragment_failed',
                    'message':'部分合并选项文字的局部原页核对未完成，已保留原文和未确认位置。','diagnostic':type(exc).__name__})
            from .parser_core.pdf_matrix_marks import observe_matrix_choice_cells
            try:
                observe_matrix_choice_cells(path,native_result,ocr_enabled=True)
            except (ValueError,RuntimeError,OSError) as exc:
                native_result.setdefault('warnings',[]).append({'code':'scan_matrix_mark_check_failed',
                    'message':'部分表格整格勾选未完成原页核对，选中状态保留待确认。','diagnostic':type(exc).__name__})
            from .parser_core.pdf_inline_regions import observe_inline_response_regions
            try:
                observe_inline_response_regions(path,native_result,ocr_enabled=True)
            except (ValueError,RuntimeError,OSError) as exc:
                native_result.setdefault('warnings',[]).append({'code':'scan_inline_region_check_failed',
                    'message':'部分题目内填写片段未完成原图边界和空白校验，相关位置保留待核对。','diagnostic':type(exc).__name__})
    if Path(path).suffix.lower() in {'.docx', '.doc'}:
        if progress:progress('validating','校验 Word 复合填写位置与实际版面',0,None)
        from .parser_core.word_layout import align_compound_paragraphs
        try:
            layout_path = path
            if Path(path).suffix.lower() == '.doc':
                from .parser_core.router import converted_doc_template
                layout_path = converted_doc_template(Path(path), Path(cache_dir) / 'converted')
            native_result.setdefault('metadata',{})['layout_alignment']=align_compound_paragraphs(layout_path,native_result,Path(cache_dir)/'layout')
            from .parser_core.filled_word_layout import align_filled_lanes
            native_result['metadata']['filled_layout_alignment']=align_filled_lanes(layout_path,native_result,Path(cache_dir)/'layout')
            from .parser_core.word_choice_layout import align_word_choice_groups
            native_result['metadata']['choice_layout_alignment']=align_word_choice_groups(layout_path,native_result,Path(cache_dir)/'layout')
        except Exception as exc:
            native_result.setdefault('warnings',[]).append({'code':'layout_alignment_failed','message':'复合段落的本地版面校验未完成，相关位置保留待核对。','diagnostic':type(exc).__name__})
    if source_choice_reviews:
        from .source_choice_review import apply_source_choice_reviews
        native_result['source_blocks'], choice_audit = apply_source_choice_reviews(
            extracted.source_hash, native_result['source_blocks'], source_choice_reviews)
        native_result.setdefault('metadata', {})['source_choice_review'] = choice_audit
        if progress: progress('validating', '已应用原页选中状态核对，重新校验字段关联', 0, None)
    reviewed_lookup = {b['block_id']: b for b in native_result['source_blocks']}
    if review_audit:
        from .source_text_review import verified_reviewed_text
        expected_text = {e['block_id']: e['after'] for r in source_text_reviews for e in r['edits']}
        expected_location = {e['block_id']: e['location'] for r in source_text_reviews for e in r['edits']}
        if any(reviewed_lookup.get(bid, {}).get('text') != text for bid, text in expected_text.items()):
            raise ValueError('原页校对文字被后续提取步骤改变，停止生成旧引用，请重新核对。')
        for bid, text in expected_text.items():
            block = reviewed_lookup[bid]
            if block.get('location') != expected_location[bid] or block.get('kind') != 'ocr_text':
                raise ValueError('原页校对的位置被后续步骤改变，停止使用旧定位。')
            if text and not verified_reviewed_text(block):
                raise ValueError('原页校对文字的来源或位置校验失效，停止使用该结果。')
    if choice_audit:
        from .source_choice_review import verified_reviewed_choice
        for record in source_choice_reviews:
            for edit in record['edits']:
                if not verified_reviewed_choice(reviewed_lookup.get(edit['block_id'], {}), edit['control_index']):
                    raise ValueError('原页人工核对的选中状态或位置被后续步骤改变，停止使用该结果。')
    if model_info is not None:
        model_info['local_pair_metrics']=refine_stacked_pairs(native_result,model_cache,
            progress=lambda done,total:progress('understanding',f'局部字段关系复核：{done}/{total}',done,total) if progress else None)
    if progress:
        progress("validating", "校验原文引用、覆盖和关联", 0, None)
    result = validate_and_assemble(native_result, outputs, model_info)
    if model_info is not None and result.get("questions"):
        try:
            if progress: progress("understanding", "判断字段类型、填写责任和条件", 0, 1)
            flat_fields = []
            def collect(field):
                flat_fields.append(field)
                for child in field.get("children") or []: collect(child)
            for field in result.get("questions") or []: collect(field)
            hints, hint_metric = analyze_field_hints(flat_fields, extracted.source_hash, model_cache,
                progress=lambda done,total: progress('understanding',f'字段类型与责任：已完成 {done}/{total} 组',done,total) if progress else None)
            for field, hint in zip(flat_fields, hints): field["model_hints"] = hint
            enrich_result_form_positions(result)
            model_info["field_hint_metric"] = hint_metric
            if progress: progress("understanding", "字段类型与填写责任判断完成", 1, 1)
        except Exception as exc:
            result.setdefault("warnings", []).append({"code": "field_hint_failed", "message": "字段关系已完成；类型和填写责任的模型提示失败，已保留程序校验结果。", "diagnostic": type(exc).__name__})
    result["engine_version"] = ENGINE_VERSION
    if review_audit:
        result['source_text_review'] = review_audit
        from copy import deepcopy
        result['source_text_review_records'] = deepcopy(source_text_reviews)
    if choice_audit:
        from copy import deepcopy
        result['source_choice_review'] = choice_audit
        result['source_choice_review_records'] = deepcopy(source_choice_reviews)
    result["pipeline_revision"] = PIPELINE_REVISION
    result["processing_options"] = {"ocr_enabled": bool(ocr_enabled)}
    result["timings"] = {
        "native_cache_hit": native_cache_hit,
        "native_extraction_seconds": extraction_seconds,
        "model_seconds": round(sum(x.get("seconds", 0) for x in (model_info or {}).get("metrics", [])) + (model_info or {}).get("field_hint_metric", {}).get("seconds", 0) + sum(x.get('seconds',0) for x in (model_info or {}).get('local_pair_metrics',[])), 4),
        "total_seconds": round(time.perf_counter() - started, 4),
    }
    return result


def save_json(result: dict, path: str | Path):
    Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
