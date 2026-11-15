from __future__ import annotations

import io
import os
from collections import defaultdict
from pathlib import Path

import fitz
import pdfplumber
from PIL import Image

from .models import ExtractedDocument, make_block
from .ocr import available as ocr_available, recognize as ocr_recognize, scan_form_regions, RASTER_GEOMETRY_VERSION

PARSER_VERSION = "pdf-native-v20-validated-native-encoding"


def native_text_encoding_evidence(lines):
    """Reject demonstrably unusable character maps, without language guessing.

    Some PDFs draw normal letters while mapping them to control characters.
    Their nonempty text stream must not be mistaken for a usable native layer.
    Tabs/newlines remain legitimate separators; Japanese and other Unicode
    letters are never judged against an English vocabulary.
    """
    raw = "".join(line.get("text", "") for line in lines)
    invalid = sum((ord(ch) < 32 and ch not in "\t\r\n") or ch == "\ufffd" for ch in raw)
    total = len(raw)
    return {"unusable": invalid >= 3 and invalid / max(total, 1) >= .02,
            "invalid_char_count": invalid, "total_char_count": total}


def _inside(box, container):
    x0, y0, x1, y1 = box
    a0, b0, a1, b1 = container
    return a0 - .05 <= (x0+x1)/2 <= a1 + .05 and b0 - .05 <= (y0+y1)/2 <= b1 + .05


def _bbox(chars):
    return [min(c["bbox"][0] for c in chars), min(c["bbox"][1] for c in chars),
            max(c["bbox"][2] for c in chars), max(c["bbox"][3] for c in chars)]


def _tagged_vector_lines(page):
    """Read outlined text from native ActualText + marked drawing geometry.

    MuPDF can attach ActualText of a path to the *previous* text cursor. That
    makes an arrow caption appear inside an unrelated option. PDFMiner exposes
    the real MCIDs of curves/rectangles/lines instead. Only use this path when
    every outlined ActualText span has geometry; ordinary glyphs remain native.
    Coordinates for an ActualText span are explicitly span-level, not invented
    per-character positions. No OCR or inferred wording is involved.
    """
    def walk(nodes):
        for node in nodes:
            if node.get('actual_text') and node.get('mcids'):
                yield node
            yield from walk(node.get('children', []))

    chars_by_mcid = defaultdict(list)
    geometry = defaultdict(list)
    for char in page.chars:
        chars_by_mcid[char.get('mcid')].append(char)
    for obj in [*page.curves, *page.rects, *page.lines]:
        if obj.get('mcid') is not None:
            geometry[obj['mcid']].append(obj)
    outlined = []
    seen = set()
    for node in walk(page.structure_tree):
        mcids = node['mcids']
        if any(chars_by_mcid[m] for m in mcids) or not node['actual_text'].strip():
            continue
        objects = [o for m in mcids for o in geometry[m]]
        if not objects or seen.intersection(mcids):
            return None  # Ambiguous native mapping: retain the original ledger.
        seen.update(mcids)
        box = [min(o['x0'] for o in objects), min(o['top'] for o in objects),
               max(o['x1'] for o in objects), max(o['bottom'] for o in objects)]
        outlined.append({'text': node['actual_text'], 'bbox': box, 'mcids': mcids})
    if not outlined:
        return None
    fragments = []
    for line in page.extract_text_lines(return_chars=True, keep_blank_chars=True):
        chars = []
        for c in line['chars']:
            raw, font = c['text'], c.get('fontname', '')
            chars.append({'text': '□' if raw == '\uf031' and 'webdings' in font.lower() else raw,
                          'raw': raw, 'font': font, 'bbox': [c['x0'], c['top'], c['x1'], c['bottom']], 'size': c['size']})
        if chars:
            fragments.append({'chars': chars, 'bbox': _bbox(chars), 'size': max(c['size'] for c in chars)})
    for span in outlined:
        # Hyphens have thin vector boxes; surrounding line height, not this
        # stroke thickness, is used for adjacency. The exact box stays intact.
        size = min(12, max(8, span['bbox'][3]-span['bbox'][1]))
        chars = [{'text': c, 'raw': c, 'font': 'PDF ActualText', 'bbox': span['bbox'], 'size': size,
                  'native_mcid': span['mcids'], 'coordinate_precision': 'actual_text_span'} for c in span['text']]
        fragments.append({'chars': chars, 'bbox': span['bbox'], 'size': size})
    # Rejoin adjacent spans on the same baseline; do not merge different
    # columns or arrow captions merely because they are on one visual line.
    rows = []
    for fragment in sorted(fragments, key=lambda f: (f['bbox'][0], f['bbox'][1])):
        b = fragment['bbox']; cy = (b[1]+b[3])/2
        adjacent = [r for r in rows if -.1 <= b[0]-r['bbox'][2] <= max(r['size'], fragment['size'])*.65
                    and abs(cy-(r['bbox'][1]+r['bbox'][3])/2) <= 4]
        if adjacent:
            row = min(adjacent, key=lambda r: b[0]-r['bbox'][2])
            gap = b[0]-row['bbox'][2]
            if gap > 2.5 and not row['chars'][-1]['text'].isspace() and not fragment['chars'][0]['text'].isspace():
                row['chars'].append({'text': ' ', 'raw': ' ', 'font': 'layout separator',
                                     'bbox': [row['bbox'][2], b[1], b[0], b[3]], 'size': fragment['size']})
            row['chars'].extend(fragment['chars']); row['bbox'] = _bbox(row['chars'])
            row['size'] = max(row['size'], fragment['size'])
        else:
            rows.append(fragment)
    for row in rows:
        row['text'] = ''.join(c['text'] for c in row['chars']).strip()
    return sorted(rows, key=lambda r: (round(r['bbox'][1], 1), r['bbox'][0]))


def _native_lines(page, structure_page=None):
    """Keep native characters so a table extractor cannot silently drop text.

    PDF glyph codes are not always Unicode. Only the verified Webdings empty
    box code is normalized, one character to one character; raw text/font stay
    attached to the source block and offsets therefore retain their meaning.
    """
    if structure_page is not None:
        tagged = _tagged_vector_lines(structure_page)
        if tagged is not None:
            return tagged
    lines = []
    for block in page.get_text("rawdict", flags=fitz.TEXTFLAGS_TEXT).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            chars = []
            for span in line.get("spans", []):
                font = span.get("font", "")
                for char in span.get("chars", []):
                    raw = char.get("c", "")
                    text = "□" if raw == "\uf031" and "webdings" in font.lower() else raw
                    chars.append({"text": text, "raw": raw, "font": font, "bbox": list(char["bbox"]), "size": span.get("size", 0)})
            if chars and "".join(c["text"] for c in chars).strip():
                lines.append({"text": "".join(c["text"] for c in chars).strip(), "bbox": _bbox(chars),
                              "chars": chars, "size": max(c["size"] for c in chars)})
    return sorted(lines, key=lambda x: (round(x["bbox"][1], 1), x["bbox"][0]))


def _intersection_ratio(box, other):
    x0, y0, x1, y1 = box
    a0, b0, a1, b1 = other
    area = max(1.0, (x1-x0) * (y1-y0))
    overlap = max(0.0, min(x1, a1)-max(x0, a0)) * max(0.0, min(y1, b1)-max(y0, b0))
    return overlap / area


def _visible_table_edges(page):
    """Separate drawn borders from shaded/white text backgrounds.

    Word often encodes every border as a thin filled rectangle. Including all
    rectangle edges also includes paragraph backgrounds and splits multiline
    cells into fictitious rows. A filled strip is a line; a filled background
    is not. Stroked rectangles and ordinary vector lines remain supported.
    """
    edges = [dict(e) for e in page.edges if e.get("object_type") == "line"]
    # Some producers encode rectangles as closed paths instead of `re`.
    # Only straight stroked segments are borders; Bezier outlines and broad
    # filled backgrounds must not introduce invented cell boundaries.
    for curve in page.curves:
        if curve.get("stroke") and all(command[0] in {"m", "l", "h"} for command in curve.get("path", [])):
            edges.extend(e for e in pdfplumber.utils.curve_to_edges(curve)
                         if e.get("orientation") in {"h", "v"} and max(e.get("width", 0), e.get("height", 0)) >= 5)
    for rect in page.rects:
        width, height = rect["x1"]-rect["x0"], rect["bottom"]-rect["top"]
        if rect.get("stroke"):
            edges.extend(pdfplumber.utils.rect_to_edges(rect))
            continue
        color = rect.get("non_stroking_color")
        values = color if isinstance(color, (tuple, list)) else (color,) if isinstance(color, (int, float)) else ()
        white = bool(values) and all(float(value) >= .98 for value in values)
        if not rect.get("fill") or white or min(width, height) > 1.5 or max(width, height) < 5:
            continue
        edge = dict(rect, object_type="line")
        if width < height:
            edge.update(x0=(rect["x0"]+rect["x1"])/2, x1=(rect["x0"]+rect["x1"])/2, width=0, orientation="v")
        else:
            top = (rect["top"]+rect["bottom"])/2
            edge.update(top=top, bottom=top, y0=page.height-top, y1=page.height-top, height=0, orientation="h")
        edges.append(edge)
    return edges


def _physical_tables(page):
    edges = _visible_table_edges(page)
    vertical = [e for e in edges if e.get("orientation") == "v"]
    horizontal = [e for e in edges if e.get("orientation") == "h"]
    if len(vertical) < 2 or len(horizontal) < 2:
        return []
    return sorted(page.find_tables({"vertical_strategy": "explicit", "horizontal_strategy": "explicit",
        "explicit_vertical_lines": vertical, "explicit_horizontal_lines": horizontal}), key=lambda t: (t.bbox[1], t.bbox[0]))


def _cell_regions(tables, page_number):
    regions = []
    for table_number, table in enumerate(tables, 1):
        boxes = sorted(set(tuple(round(float(v), 3) for v in box) for box in table.cells), key=lambda b: (b[1], b[0]))
        xs = sorted({x for box in boxes for x in (box[0], box[2])})
        ys = sorted({y for box in boxes for y in (box[1], box[3])})
        for box in boxes:
            column, column_end = xs.index(box[0])+1, xs.index(box[2])
            row, row_end = ys.index(box[1])+1, ys.index(box[3])
            cell_id = f"p{page_number}-t{table_number}:box:" + ":".join(f"{v:.3f}" for v in box)
            regions.append({"bbox": list(box), "table_bbox": list(table.bbox), "table": table_number,
                "cell_id": cell_id, "table_id": f"p{page_number}-t{table_number}", "row": row, "row_start": row,
                "row_end": row_end, "row_span": row_end-row+1, "column": column, "column_start": column,
                "column_end": column_end, "column_span": column_end-column+1, "grid_col_start": column,
                "grid_col_end": column_end, "grid_span": column_end-column+1, "table_column_count": len(xs)-1,
                "merge_anchor_id": None, "is_merge_continuation": False, "layout_evidence": "visible_vector_borders",
                "fragments": []})
    return regions


def _route_characters(lines, regions):
    """Assign each native character once, to its physical cell or body line.

    Never discard a line merely because its midpoint is inside a table bbox.
    Nested/overlapping cells use the smallest containing physical rectangle.
    """
    outside = []
    for line in lines:
        buckets = defaultdict(list)
        for char in line.get("chars", []):
            candidates = [(i, r) for i, r in enumerate(regions) if _inside(char["bbox"], r["bbox"])]
            owner = min(candidates, key=lambda item: (item[1]["bbox"][2]-item[1]["bbox"][0])*(item[1]["bbox"][3]-item[1]["bbox"][1]))[0] if candidates else None
            buckets[owner].append(char)
        for owner, chars in buckets.items():
            text = "".join(c["text"] for c in chars).strip()
            if not text:
                continue
            raw = "".join(c["raw"] for c in chars).strip()
            fragment = {"text": text, "raw_text": raw, "bbox": _bbox(chars), "size": line["size"], "chars": chars}
            (outside if owner is None else regions[owner]["fragments"]).append(fragment)
    return outside


def _fragment_text(fragments):
    ordered = sorted(fragments, key=lambda x: (round(x["bbox"][1], 1), x["bbox"][0]))
    text, raw, controls = [], [], []
    offset = 0
    for fragment in ordered:
        text.append(fragment["text"])
        raw.append(fragment["raw_text"])
        for i, char in enumerate(fragment["text"]):
            if char in "□☐☑☒✓✔":
                controls.append({"type": "checkbox", "control_type": "checkbox", "selected": char in "☑☒✓✔",
                    "checked": char in "☑☒✓✔", "start": offset+i, "end": offset+i+1,
                    "raw_glyph": fragment["raw_text"][i:i+1], "source": "native_pdf_glyph",
                    "bbox": fragment['chars'][i+next((j for j,c in enumerate(fragment['chars']) if not c['text'].isspace()),0)]['bbox']})
        offset += len(fragment["text"])+1
    return "\n".join(text), "\n".join(raw), controls


def _diagram_option_groups(page, fragments, controls):
    """Group native glyph/character ranges by actual drawn option enclosures.

    A diagram is not a single left-to-right line. No OCR, rewriting, or
    semantic guesses: only closed native drawing boxes containing one marker.
    """
    if len(controls)<2:return []
    drawings=page.get_drawings(); boxes=[]
    for c in controls:
        possible=[list(d['rect']) for d in drawings if d['rect'].height>25 and d['rect'].width>50 and _inside(c['bbox'],list(d['rect']))]
        if not possible:return []
        box=min(possible,key=lambda b:(b[2]-b[0])*(b[3]-b[1]))
        if sum(_inside(x['bbox'],box) for x in controls)!=1:return []
        boxes.append(box)
    if len({tuple(round(v) for v in b) for b in boxes})!=len(boxes):return []
    result=[]
    for control,box in zip(controls,boxes):
        ranges=[]; offset=0
        for fragment in sorted(fragments,key=lambda f:(round(f['bbox'][1],1),f['bbox'][0])):
            chars=fragment['chars']; left=0
            while left<len(chars) and chars[left]['text'].isspace():left+=1
            trimmed=chars[left:]
            for i,char in enumerate(trimmed[:len(fragment['text'])]):
                # Unlocated collapsed text remains in the source ledger; it
                # cannot prove membership of an option. Tagged vector text
                # has already received verified MCID geometry above.
                visible = char['bbox'][2]-char['bbox'][0] > .05 and char['bbox'][3]-char['bbox'][1] > .05
                if visible and char['text'] not in '□☐☑☒✓✔' and _inside(char['bbox'],box):
                    at=offset+i
                    if ranges and ranges[-1]['end']==at:ranges[-1]['end']=at+1
                    else:ranges.append({'start':at,'end':at+1})
            offset+=len(fragment['text'])+1
        if not ranges:return []
        result.append({'ranges':ranges,'control_start':control['start'],'control_end':control['end'],'selected':control['selected'],'bbox':box})
    return result


def _ocr_image_regions(page, native_lines, dpi):
    """Existing local OCR enhancement, only called after explicit enablement."""
    found, seen = [], set()
    page_area = max(1.0, page.rect.width * page.rect.height)
    minimum_ratio = float(os.getenv("QSR_OCR_IMAGE_MIN_PAGE_RATIO", "0.03"))
    for info in page.get_images(full=True):
        for rect in page.get_image_rects(info[0]):
            key = tuple(round(v, 2) for v in rect)
            if key in seen:
                continue
            seen.add(key)
            if rect.width < 80 or rect.height < 50 or rect.width*rect.height < page_area*minimum_ratio:
                continue
            pix = page.get_pixmap(dpi=dpi, alpha=False, clip=rect)
            rendered = Image.open(io.BytesIO(pix.tobytes("png")))
            sx, sy = rect.width/rendered.width, rect.height/rendered.height
            for item in ocr_recognize(rendered):
                box = [rect.x0+item["bbox"][0]*sx, rect.y0+item["bbox"][1]*sy, rect.x0+item["bbox"][2]*sx, rect.y0+item["bbox"][3]*sy]
                if any(_intersection_ratio(box, line["bbox"]) > .55 for line in native_lines):
                    continue
                found.append({**item, "bbox": box, "size": 0, "extraction": "ocr_region"})
    return found


def _scan_background(page, dpi):
    """Render a large scan background without its separate native answer layer.

    Work on an in-memory copy. Removing native text only (no images/drawings,
    no white redaction fill) prevents OCR from joining a printed question to
    its typed answer. Source coordinates and the original PDF remain intact.
    """
    with fitz.open() as scratch:
        scratch.insert_pdf(page.parent, from_page=page.number, to_page=page.number, annots=False)
        copied = scratch[0]
        copied.add_redact_annot(copied.rect, fill=False, cross_out=False)
        copied.apply_redactions(images=0, graphics=0, text=0)
        pix = copied.get_pixmap(dpi=dpi, alpha=False, annots=False)
        return Image.open(io.BytesIO(pix.tobytes('png'))).convert('RGB')


def _attach_native_overlay(regions, native_blocks, visible_image=None, page_width=None, background_blocks=None):
    """Attach exact native glyphs/values only inside one physical raster slot.

    Ink density is never used to guess a checked answer. A native check mark
    must overlap a unique detected checkbox. Other text requires a bounded
    response line/box and remains source-referenced for pairing validation.
    """
    for block in native_blocks:
        if not block.text.strip(): continue
        box = block.location.get('bbox', [0,0,0,0])
        if block.text.strip() in {'√', '✓', '✔', '☑', '☒'}:
            candidates = [r for r in regions if r.structure.get('scan_region_kind') == 'checkbox'
                and _intersection_ratio(box, r.location['bbox']) > .45]
            if len(candidates) == 1:
                region = candidates[0]
                if visible_image is not None:
                    # PDF editors may leave a white/invisible old tick in the
                    # text layer when changing Yes to No. Require actual dark
                    # pixels inside this exact box as corroboration; density
                    # alone never creates a selected state.
                    scale = visible_image.width / page_width
                    rb = region.location['bbox']; pad = 1.3
                    crop = visible_image.convert('L').crop((int((rb[0]+pad)*scale), int((rb[1]+pad)*scale),
                        int((rb[2]-pad)*scale), int((rb[3]-pad)*scale)))
                    histogram = crop.histogram()
                    if crop.width <= 0 or crop.height <= 0 or sum(histogram[:120]) / (crop.width*crop.height) < .025:
                        block.structure['resolved_layout_role'] = 'invisible_native_mark'
                        continue
                region.controls[0].update(selected=True, source='native_mark_in_scan_rectangle',
                    value_block_id=block.block_id)
                region.structure.update(blank_state='filled', content_state='text',
                    overlay_value_block_ids=[block.block_id], native_selection_verified=True)
                block.structure['resolved_layout_role'] = 'checkbox_symbol'
            continue
        candidates = [r for r in regions if r.structure.get('scan_region_kind') in {'write_line','text_box'}
            and r.location['bbox'][1]-5 <= (box[1]+box[3])/2 <= r.location['bbox'][3]+2
            and r.location['bbox'][0]-2 <= box[0] < r.location['bbox'][2]
            and (box[2] <= r.location['bbox'][2]+2 or (background_blocks is not None and
                not any(_intersection_ratio(box, item['bbox']) > .12 for item in background_blocks) and
                not any(other is not r and other.location['bbox'][0] > r.location['bbox'][0]+3 and
                    box[0] < other.location['bbox'][0] < box[2] and
                    other.location['bbox'][1]-2 <= (box[1]+box[3])/2 <= other.location['bbox'][3]+2 for other in regions)))]
        if len(candidates) == 1:
            region = candidates[0]
            region.structure.setdefault('overlay_value_block_ids', []).append(block.block_id)
            region.structure.update(blank_state='filled', content_state='text')
            block.structure['native_overlay_answer_region'] = region.block_id
            if box[2] > region.location['bbox'][2]+2:
                block.structure['native_overlay_overflow_evidence'] = {'value_bbox':box,'region_bbox':region.location['bbox'],
                    'background_overlap_checked':True,'adjacent_response_checked':True}


def _scan_layout_blocks(image, recognized, location_base, page_width, page_height):
    """One persisted evidence ledger for fresh OCR and checkpoint recovery."""
    sx,sy=page_width/image.width,page_height/image.height
    blocks=[]
    for region in scan_form_regions(image,recognized,page_width):
        box=[region['bbox'][0]*sx,region['bbox'][1]*sy,region['bbox'][2]*sx,region['bbox'][3]*sy]
        controls=[{'type':'checkbox','source':region.get('selection_source','scan_rectangle'),'selected':region['selected'],'bbox':box,'mark_evidence':region.get('selection_mark')}] if region['kind']=='checkbox' else []
        state='filled' if region.get('selected') is True else ('unreadable' if region.get('unreadable_ink') else ('blank' if region.get('selected') is False or (region['kind'] in {'write_line','text_box','blank_lane'} and not region.get('text_overlap')) else 'unresolved'))
        proof=region.get('closed_region_evidence')
        if proof:
            proof={**proof,'polygon':[[px*sx,py*sy] for px,py in proof['polygon']],
                'coordinate_system':'PDF points, top-left origin'}
            for key in ('seed_cell_bbox','observed_horizontal_rule'):
                if key in proof:proof[key]=[v*(sx if i%2==0 else sy) for i,v in enumerate(proof[key])]
        content_state='control' if controls and isinstance(region.get('selected'),bool) else 'empty' if state=='blank' else 'unreadable'
        blocks.append(make_block('ocr_text','',0,{**location_base,'bbox':box,'extraction':'ocr'},structure={'scan_region_kind':region['kind'],'scan_candidate':True,'source_method':'raster_geometry','blank_state':state,'content_state':content_state,'write_requires_association':True,'text_overlap':region.get('text_overlap',False),'interior_ink_ratio':region.get('interior_ink_ratio'),'line_style':region.get('line_style'),'closed_region_evidence':proof},controls=controls))
    return blocks


def extract_pdf(path: Path, source_hash: str, document_id: str, password: str = "", progress=None, ocr_enabled: bool | None = None) -> ExtractedDocument:
    blocks, warnings, unread, native_pages, ocr_pages, ocr_regions, metadata_tables = [], [], [], [], [], [], []
    unusable_native_pages, encoding_diagnostics = [], []
    ocr_background_ledgers = {}
    minimum = int(os.getenv("QSR_PDF_NATIVE_MIN_CHARS", "20"))
    dpi = int(os.getenv("QSR_OCR_DPI", "220"))
    if ocr_enabled is None:
        ocr_enabled = os.getenv("QSR_OCR_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
    ocr_ready = ocr_enabled and ocr_available()
    with fitz.open(path) as doc:
        if doc.needs_pass and (not password or not doc.authenticate(password)):
            raise PermissionError("PDF 需要正确的打开密码")
        if not len(doc):
            raise ValueError("PDF 没有页面")
        if len(doc) > int(os.getenv("QSR_MAX_PDF_PAGES", "300")):
            raise ValueError("PDF 页数超过配置限制")
        with pdfplumber.open(path, password=password or None) as plumber:
            for page_number, page in enumerate(doc, 1):
                if progress:
                    progress(page_number, len(doc), "reading")
                lines = _native_lines(page, plumber.pages[page_number-1])
                usable_chars = len("".join(line["text"] for line in lines).strip())
                encoding = native_text_encoding_evidence(lines)
                native = not encoding["unusable"] and (usable_chars >= minimum or (usable_chars > 0 and not ocr_ready))
                location_base = {"type": "pdf", "page": page_number, "coordinate_system": "PDF points, top-left origin"}
                if encoding["unusable"]:
                    unusable_native_pages.append(page_number)
                    encoding_diagnostics.append({"page": page_number, **encoding})
                    warnings.append({"code": "unusable_native_text_encoding",
                        "message": f"第 {page_number} 页原生文字映射包含异常字符，不能用于题目或答案。" + ("已改用本地 OCR，请核对原页。" if ocr_ready else "请查看原页；需明确启用本地 OCR 后重试。"),
                        "location": location_base, "diagnostic": encoding})
                page_blocks = []
                image_infos, seen_images = [], set()
                for info in page.get_images(full=True):
                    for rect in page.get_image_rects(info[0]):
                        key = (info[0], *(round(v, 3) for v in rect))
                        if key not in seen_images:
                            seen_images.add(key)
                            image_infos.append({"xref": info[0], "bbox": list(rect)})
                if native:
                    native_pages.append(page_number)
                    tables = _physical_tables(plumber.pages[page_number-1])
                    regions = _cell_regions(tables, page_number)
                    outside = _route_characters(lines, regions)
                    for region in regions:
                        fragments=region.pop('fragments')
                        text, raw_text, controls = _fragment_text(fragments)
                        structure = {k: v for k, v in region.items() if k not in {"bbox", "table_bbox", "table"}}
                        structure.update(blank_state="filled" if text else "blank", content_state="text" if text else "empty",
                                         raw_text=raw_text, source_text_normalized=raw_text != text)
                        span_offset=0;structure['native_text_spans']=[]
                        for fragment in sorted(fragments,key=lambda f:(round(f['bbox'][1],1),f['bbox'][0])):
                            structure['native_text_spans'].append({'start':span_offset,'end':span_offset+len(fragment['text']),'bbox':fragment['bbox']})
                            span_offset+=len(fragment['text'])+1
                        glyphs=[c for f in fragments for c in f['chars'] if c['text'].strip()]
                        if glyphs:
                            structure['native_bold_fraction']=round(sum('bold' in c['font'].lower() for c in glyphs)/len(glyphs),3)
                        tagged_sources = {(tuple(c['native_mcid']), tuple(c['bbox'])) for f in fragments for c in f['chars'] if c.get('native_mcid')}
                        if tagged_sources:
                            structure['native_actual_text_sources'] = [{'mcids': list(m), 'bbox': list(b), 'coordinate_precision': 'actual_text_span'} for m,b in sorted(tagged_sources)]
                        groups=_diagram_option_groups(page,fragments,controls) if region['bbox'][3]-region['bbox'][1]>90 else []
                        if groups:structure['native_option_groups']=groups
                        location = {**location_base, "table": region["table"], "row": region["row"], "cell": region["column"],
                                    "bbox": region["bbox"], "table_bbox": region["table_bbox"], "extraction": "native"}
                        page_blocks.append(make_block("table_cell", text, 0, location, structure=structure, controls=controls))
                    for line in outside:
                        text, raw_text, controls = _fragment_text([line])
                        location = {**location_base, "bbox": line["bbox"], "rotation_normalized_from": page.rotation, "extraction": "native"}
                        page_blocks.append(make_block("paragraph", text, 0, location, style={"font_size": line["size"], "raw_text": raw_text}, controls=controls))
                    for ti, table in enumerate(tables, 1):
                        metadata_tables.append({"table_id": f"p{page_number}-t{ti}", "page": page_number, "bbox": list(table.bbox),
                                                "physical_cells": sum(r["table"] == ti for r in regions), "geometry_method": "visible_vector_borders"})
                    if ocr_ready and image_infos:
                        if progress:
                            progress(page_number, len(doc), "ocr")
                        try:
                            large_scan = any(_intersection_ratio(list(page.rect), info['bbox']) > .5 for info in image_infos)
                            if large_scan:
                                image = _scan_background(page, dpi)
                                raw_recognized = ocr_recognize(image)
                                sx, sy = page.rect.width/image.width, page.rect.height/image.height
                                recognized = []
                                for item in raw_recognized:
                                    box = [item['bbox'][0]*sx, item['bbox'][1]*sy, item['bbox'][2]*sx, item['bbox'][3]*sy]
                                    if any(_intersection_ratio(box, line['bbox']) > .55 for line in lines): continue
                                    recognized.append({**item, 'bbox': box})
                                physical = _scan_layout_blocks(image, raw_recognized, location_base, page.rect.width, page.rect.height)
                                visible_pix = page.get_pixmap(dpi=dpi, alpha=False)
                                visible_image = Image.open(io.BytesIO(visible_pix.tobytes('png')))
                                background = [{**item,'bbox':[item['bbox'][0]*sx,item['bbox'][1]*sy,item['bbox'][2]*sx,item['bbox'][3]*sy]} for item in raw_recognized]
                                ocr_background_ledgers[str(page_number)] = background
                                _attach_native_overlay(physical, page_blocks, visible_image, page.rect.width, background)
                                page_blocks.extend(physical)
                                for block in page_blocks: block.structure['scan_page_context'] = True
                            else:
                                recognized = _ocr_image_regions(page, lines, dpi)
                            if recognized:
                                ocr_pages.append(page_number)
                                ocr_regions.append({"page": page_number, "line_count": len(recognized)})
                            for item in recognized:
                                page_blocks.append(make_block("ocr_text", item["text"], 0,
                                    {**location_base, "bbox": item["bbox"], "extraction": "ocr_region"},
                                    structure={'scan_page_context': large_scan, 'source_text_is_ocr':True},
                                    style={"ocr_confidence": item.get("confidence")}))
                        except Exception as exc:
                            warnings.append({"code": "ocr_region_failed", "message": f"第 {page_number} 页图片区域 OCR 失败。", "location": location_base, "diagnostic": type(exc).__name__})
                    for widget in list(page.widgets() or []):
                        value = "" if widget.field_value is None else str(widget.field_value)
                        kind = str(widget.field_type_string or "field")
                        selected = value.lower() not in {"", "off", "false", "0", "none"} if kind.lower() in {"checkbox", "radiobutton"} else None
                        page_blocks.append(make_block("form_field", value, 0,
                            {**location_base, "bbox": list(widget.rect), "field_name": widget.field_name, "extraction": "native"},
                            controls=[{"type": kind, "selected": selected, "value": value}]))
                elif ocr_ready:
                    if progress:
                        progress(page_number, len(doc), "ocr")
                    try:
                        pix = page.get_pixmap(dpi=dpi, alpha=False)
                        image = Image.open(io.BytesIO(pix.tobytes("png")))
                        recognized = ocr_recognize(image)
                        sx, sy = page.rect.width/image.width, page.rect.height/image.height
                        for item in recognized:
                            box = [item["bbox"][0]*sx, item["bbox"][1]*sy, item["bbox"][2]*sx, item["bbox"][3]*sy]
                            page_blocks.append(make_block("ocr_text", item["text"], 0, {**location_base, "bbox": box, "extraction": "ocr"}, style={"ocr_confidence": item.get("confidence")}, structure={"source_text_is_ocr":True,"rotation":item.get('rotation',0)}))
                        # Blank marks have no text to OCR. Preserve their own
                        # physical identities instead of attaching the next
                        # nonempty recognized label as an existing answer.
                        page_blocks.extend(_scan_layout_blocks(image,recognized,location_base,page.rect.width,page.rect.height))
                        uncertain=[b for b in page_blocks if b.style.get('ocr_confidence',1)<.85 or b.structure.get('blank_state')=='unresolved']
                        if uncertain:
                            warnings.append({'code':'ocr_content_needs_review','message':f'第 {page_number} 页有 {len(uncertain)} 处文字或选择状态需要原页核对。','location':location_base,'block_ids':[b.block_id for b in uncertain]})
                        if recognized:
                            ocr_pages.append(page_number)
                    except Exception as exc:
                        warnings.append({"code": "ocr_failed", "message": f"第 {page_number} 页本地 OCR 失败。", "location": location_base, "diagnostic": type(exc).__name__})
                if not native and page_number not in ocr_pages:
                    reason = "OCR 已关闭，页面没有可用原生文字；可查看原页或在设置中明确启用本地 OCR。" if not ocr_enabled else "没有可用原生文字，且本地 OCR 未就绪或未读到文字。"
                    unread.append({"kind": "page", "location": location_base, "reason": reason})
                    warnings.append({"code": "ocr_disabled" if not ocr_enabled else "unreadable_page", "message": f"第 {page_number} 页：{reason}", "location": location_base})
                if image_infos and not ocr_ready:
                    # Unknown images are retained and summarized, never silently
                    # declared decorative because they are small or textless.
                    for info in image_infos:
                        location = {**location_base, **info, "extraction": "native_image_reference"}
                        unread.append({"kind": "image", "location": location, "reason": "图像内容未读取（OCR已关闭或未就绪），请在原文中核对其用途。"})
                        page_blocks.append(make_block("image", "", 0, location, structure={"content_state": "unreadable", "image_xref": info["xref"]}))
                    warnings.append({"code": "unread_image_content", "message": f"第 {page_number} 页有 {len(image_infos)} 个未读取的图像区域，请查看原文核对。", "location": location_base, "count": len(image_infos)})
                # Full-page OCR already sorts in its corrected reading frame.
                # Its bbox is mapped back to the original rotated page for
                # source navigation, and is therefore not a reading-order key.
                # Sorting those coordinates again reverses/permutes paragraphs.
                if native or not ocr_ready or (page_blocks and not any(b.structure.get('rotation') for b in page_blocks)):
                    page_blocks.sort(key=lambda b: (round(b.location.get("bbox", [0, 0])[1], 2), b.location.get("bbox", [0])[0], 0 if b.kind == "table_cell" else 1))
                blocks.extend(page_blocks)
    for order, block in enumerate(blocks, 1):
        block.order = order
    status = "failed" if not any(b.text for b in blocks) else "partial" if unread else "complete"
    return ExtractedDocument(document_id=document_id, source_hash=source_hash, filename=path.name, file_type="PDF",
        parser="native_pdf+local_ocr" if ocr_enabled else "native_pdf", parser_version=PARSER_VERSION,
        extraction_status=status, source_blocks=blocks, warnings=warnings, unread_regions=unread,
        metadata={"coordinate_system": "PDF points, top-left origin", "ocr_enabled": ocr_enabled,
                  "ocr_engine": "RapidOCR PP-OCRv4 local" if ocr_enabled else None, "ocr_pages": sorted(set(ocr_pages)),
                  "ocr_regions": ocr_regions, "native_text_pages": native_pages, "tables": metadata_tables,
                  "unusable_native_text_pages": unusable_native_pages, "native_text_encoding_diagnostics": encoding_diagnostics,
                  "raster_geometry_version": RASTER_GEOMETRY_VERSION,
                  "ocr_background_ledgers": ocr_background_ledgers,
                  "ocr_policy": "explicitly enabled local OCR" if ocr_enabled else "disabled: native content and image references only"})
