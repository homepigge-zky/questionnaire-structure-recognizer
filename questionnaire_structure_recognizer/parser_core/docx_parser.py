from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path

from lxml import etree

from .models import ExtractedDocument, make_block


PARSER_VERSION = "docx-native-v28-bounded-text-control-results"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
NS = {"w": W, "r": R, "w14": W14}


def _xml(data: bytes):
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("文档包含不允许的 XML 实体")
    return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))


def _attr(element, name, namespace=W):
    return element.get(f"{{{namespace}}}{name}") if element is not None else None


def _control_metadata(sdt):
    props = sdt.find("w:sdtPr", NS)
    if props is None:
        return None
    alias, tag, identifier = props.find("w:alias", NS), props.find("w:tag", NS), props.find("w:id", NS)
    binding, lock = props.find("w:dataBinding", NS), props.find("w:lock", NS)
    checkbox = props.find("w14:checkbox", NS)
    dropdown, combo, date = props.find("w:dropDownList", NS), props.find("w:comboBox", NS), props.find("w:date", NS)
    if checkbox is not None:
        ctype = "checkbox"
    elif dropdown is not None:
        ctype = "dropdown"
    elif combo is not None:
        ctype = "combo_box"
    elif date is not None:
        ctype = "date"
    else:
        ctype = "text"
    options = []
    for node in (dropdown, combo):
        if node is not None:
            options.extend({"display": _attr(x, "displayText") or _attr(x, "value") or "", "value": _attr(x, "value") or ""} for x in node.findall("w:listItem", NS))
    checked = checkbox.find("w14:checked", NS) if checkbox is not None else None
    current = "".join(sdt.xpath("./w:sdtContent//w:t/text()", namespaces=NS)).strip()
    return {
        "type": ctype,
        "control_id": _attr(identifier, "val"),
        "name": _attr(alias, "val"),
        "tag": _attr(tag, "val"),
        "options": options,
        "selected": (_attr(checked, "val", W14) or "0") in {"1", "true", "on"} if checked is not None else None,
        "current_value": current,
        "placeholder": props.find("w:showingPlcHdr", NS) is not None or current in {"Click or tap here to enter text.", "点击或点击此处输入文字。", "请选择"},
        "data_binding": ({"xpath": _attr(binding, "xpath"), "store_item_id": _attr(binding, "storeItemID")} if binding is not None else None),
        "lock": _attr(lock, "val"),
        "source": "content_control",
    }


def _logical_slots(text, target_id):
    slots = []
    # A completed "caption: value" cell must retain the same local boundary
    # that its blank "caption:" template exposed. Store only a candidate;
    # the structural layer requires neighboring independent in-cell prompts
    # before treating a row as this layout. URLs, sentences and option lists
    # do not create a caption candidate.
    inline = re.match(r"^\s*([^:：\n?？。;；]{1,65})[:：]", text)
    if inline:
        caption = inline.group(1).strip()
        start = inline.end()
        while start < len(text) and text[start].isspace(): start += 1
        end = len(text.rstrip())
        if (caption and start < end and not re.fullmatch(r"https?|ftp|mailto", caption, re.I)
                and not re.search(r"[□☐☑☒]", caption)
                and not re.search(r"[ \t\u3000]{2,}[^:：\n]{1,65}[:：]", text[inline.end():])
                and not re.search(r"\n\s*[^:：\n]{1,65}[:：]", text[inline.end():])):
            slots.append({"slot_id": f"{target_id}:value-slot:1", "kind": "inline_value_candidate",
                          "start": start, "end": end, "offset": start,
                          "label_start": inline.start(1), "label_end": inline.start(1) + len(inline.group(1).rstrip()),
                          "prompt_text": caption})
    candidates=list(re.finditer(r"_{2,}|＿{2,}", text))
    for match in re.finditer(r'[.．…·]{4,}',text):
        before=text[text.rfind('\n',0,match.start())+1:match.start()]
        after=text[match.end():].split('\n',1)[0]
        if before.rstrip().endswith((':','：')) and not after.strip():candidates.append(match)
    for index, match in enumerate(sorted(candidates,key=lambda m:m.start()), 1):
        prefix = text[:match.start()]
        label_match = re.search(r"([A-Za-z0-9\u4e00-\u9fff][^:：;；_＿\n]{0,160})[:：;；]\s*$", prefix)
        slots.append({
            "slot_id": f"{target_id}:slot:{index}:inline-placeholder",
            "start": match.start(), "end": match.end(), "offset": match.start(),
            "kind": "inline_placeholder", "placeholder_text": match.group(0),
            "label_start": label_match.start(1) if label_match else None,
            "label_end": label_match.end(1) if label_match else None,
        })
    # A populated response cell can contain both fixed option wording and
    # one or more empty write-in positions, for example
    # ``□ No, Cause:`` or ``Regulatory authority:``.  These positions have no
    # placeholder characters, so retain a zero-width physical slot directly
    # after the prompt.  This is only source extraction: the validator still
    # decides whether the slot belongs to a real field.
    prompt_index = 0
    line_start = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        delimiters = list(re.finditer(r"[:：]", content))
        # Semicolons are used as colons in some forms. Only expose a candidate
        # when a short line ends at that delimiter, never split prose on ';'.
        if not delimiters and re.fullmatch(r"[^;；\n]{1,65}[;；]\s*", content):
            delimiters = list(re.finditer(r"[;；]", content))
        previous_end = 0
        for delimiter_index, colon in enumerate(delimiters):
            after = content[colon.end():]
            next_label_start = None
            if delimiter_index + 1 < len(delimiters):
                segment = content[colon.end():delimiters[delimiter_index + 1].start()]
                gap = re.match(r"[ \t\u3000]{2,}", segment)
                if gap and segment[gap.end():].strip():
                    next_label_start = colon.end() + gap.end()
            if after.strip() and next_label_start is None:
                previous_end = colon.end()
                continue
            before = content[:colon.start()]
            # For ``□ No, Cause:`` the write-in label starts after the last
            # option delimiter.  For a plain ``Date of Audit:`` line it starts
            # at the beginning of the line.
            delimiter = max(before.rfind("，"), before.rfind(","), before.rfind("；"), before.rfind(";"))
            label_start = max(delimiter + 1 if delimiter >= 0 else 0, previous_end)
            while label_start < len(before) and before[label_start].isspace():
                label_start += 1
            marker = re.match(r"[□☐☑☒✓✔]\s*", before[label_start:])
            if marker:
                label_start += marker.end()
            label_end = len(before.rstrip())
            label_text = before[label_start:label_end].strip()
            if not label_text or re.fullmatch(r"[□☐☑☒✓✔]?\s*(?:是|否|Y|N|YES|NO|N/?A)[\s/]*", label_text, re.I):
                continue
            following = text[line_start + len(line):].lstrip()
            # An instruction such as “If yes, indicate which country:” is
            # followed by the actual checkbox choices; the colon itself is
            # not a write-in slot.  Keep prompts like “Shelf life:” because
            # those carry a concrete value even when another option follows.
            if re.match(r"(?:please\s+(?:indicate|choose|select)|请(?:选择|指出))", label_text, re.I) and re.match(r"[□☐☑☒✓✔]", following):
                continue
            prompt_index += 1
            offset = line_start + colon.end()
            slots.append({
                "slot_id": f"{target_id}:prompt-slot:{prompt_index}",
                "start": offset, "end": line_start + next_label_start if next_label_start is not None else offset, "offset": offset,
                "kind": "inline_prompt_candidate",
                "label_start": line_start + label_start,
                "label_end": line_start + label_end,
                "prompt_text": label_text,
            })
            previous_end = next_label_start if next_label_start is not None else colon.end()
        # Some templates end a response instruction without punctuation,
        # e.g. ``If yes, please indicate PDE``.  Preserve one trailing slot
        # when the line itself explicitly asks for additional content.
        stripped = content.strip()
        if (
            stripped and not stripped.endswith((":", "："))
            and re.search(r"(?:please\s+(?:indicate|specify|describe|provide|explain)|请(?:说明|注明|列出|填写|陈述|提供))\b", stripped, re.I)
        ):
            prompt_index += 1
            offset = line_start + len(content)
            slots.append({
                "slot_id": f"{target_id}:prompt-slot:{prompt_index}",
                "start": offset, "end": offset, "offset": offset,
                "kind": "inline_prompt_candidate",
                "label_start": line_start,
                "label_end": line_start + len(content.rstrip()),
                "prompt_text": stripped,
            })
        line_start += len(line)
    return slots


def _visible_text(element):
    controls = []
    symbol_index = 0

    def walk(node, source_offset=0):
        nonlocal symbol_index
        local = etree.QName(node).localname
        if local in {"del", "delText", "instrText", "commentRangeStart", "commentRangeEnd", "commentReference", "sdtPr", "ffData"}:
            return ""
        if local == "t":
            return node.text or ""
        if local in {"br", "cr"}:
            return "\n"
        if local == "tab":
            return "\t"
        if local == 'sym':
            symbol_index += 1
            font, code = _attr(node,'font') or '', (_attr(node,'char') or '').upper()
            # Preserve physical symbol identity independently of text offsets.
            # Only mappings verified against source rendering establish state.
            known = {('wingdings 2','F035'):False,('segoe ui symbol','2610'):False,('segoe ui symbol','2611'):True,
                     ('wingdings 2','0052'):True,('wingdings 2','F052'):True,
                     ('wingdings','00FE'):True,('wingdings','F0FE'):True,
                     ('wingdings','F06F'):False,('symbol','F0F0'):False}
            state = known.get((font.lower(),code))
            controls.append({'type':'checkbox' if (font.lower(),code) in known else 'unknown_symbol',
                'source':'word_symbol','symbol_index':symbol_index,'symbol_font':font,'symbol_char':code,
                'raw_text_offset': source_offset,
                'selected':state,'control_id':f'symbol:{symbol_index}','placeholder':False,'data_binding':None,
                'state_evidence':'verified_symbol_font' if state is not None else 'unrecognized_symbol_font'})
            return ''
        pieces = []
        position = source_offset
        for child in node:
            piece = walk(child, position)
            pieces.append(piece)
            position += len(piece)
        text = "".join(pieces)
        if local == "r":
            vertical = node.find("w:rPr/w:vertAlign", NS)
            if vertical is not None and vertical.get(f"{{{W}}}val") == "superscript":
                text = text.translate(str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾"))
        if local == "sdt":
            metadata = _control_metadata(node)
            if metadata:
                controls.append(metadata)
                if metadata["type"] == "checkbox":
                    text = ("☑ " if metadata["selected"] else "☐ ") + text.lstrip("☐☑☒✓✔ ")
        if local == "fldChar":
            data = node.find("w:ffData", NS)
            if data is not None:
                name = data.find("w:name", NS)
                kind = "checkbox" if data.find("w:checkBox", NS) is not None else "dropdown" if data.find("w:ddList", NS) is not None else "text"
                metadata = {"type": kind, "control_id": _attr(name, "val"), "name": _attr(name, "val"), "source": "legacy_form_field", "placeholder": False, "data_binding": None, "selected": None, "options": [], "current_value": None}
                checkbox = data.find("w:checkBox", NS)
                dropdown = data.find("w:ddList", NS)
                if checkbox is not None:
                    state = checkbox.find("w:checked", NS)
                    evidence = "checked"
                    if state is None:
                        state = checkbox.find("w:default", NS)
                        evidence = "default"
                    if state is not None:
                        metadata["selected"] = (_attr(state, "val") or "1").lower() in {"1", "true", "on"}
                        metadata["state_evidence"] = evidence
                    else:
                        # ISO/IEC 29500-1 17.16.12: omitted w:default is
                        # unchecked. Missing w:checked inherits that default;
                        # this is native schema evidence, not a guessed state.
                        metadata['selected'] = False
                        metadata['state_evidence'] = 'ooxml_default_unchecked'
                if dropdown is not None:
                    metadata["options"] = [{"display": _attr(x, "val") or "", "value": _attr(x, "val") or ""} for x in dropdown.findall("w:listEntry", NS)]
                    selected = dropdown.find("w:result", NS)
                    if selected is None: selected = dropdown.find("w:default", NS)
                    selected_index = _attr(selected, "val")
                    if selected_index is not None and selected_index.isdigit() and int(selected_index) < len(metadata["options"]):
                        metadata["selected_index"] = int(selected_index)
                        metadata["current_value"] = metadata["options"][int(selected_index)]["value"]
                if kind == 'text':
                    from .legacy_fields import inspect_legacy_text
                    metadata.update(inspect_legacy_text(node))
                    metadata['raw_result_start'] = source_offset
                controls.append(metadata)
        return text

    raw = walk(element)
    text = raw.strip()
    leading = len(raw) - len(raw.lstrip())
    # Native symbol offsets follow the exact same source-character stream.
    # Synthetic checkbox text added for content controls is a different
    # representation; do not certify symbol offsets in such mixed paragraphs.
    if not element.xpath('.//w:sdt', namespaces=NS):
        for control in controls:
            if control.get('source') == 'word_symbol':
                control['text_offset'] = min(len(text), max(0, control['raw_text_offset'] - leading))
                control['offset_evidence'] = 'native_xml_character_order'
            if control.get('source') == 'legacy_form_field' and control.get('legacy_result_supported'):
                start=control.get('raw_result_start');value=control.get('current_value','')
                if isinstance(start,int) and raw[start:start+len(value)]==value:
                    control['result_start']=min(len(text),max(0,start-leading))
                    control['result_end']=min(len(text),max(0,start+len(value)-leading))
                    control['offset_evidence']='native_xml_character_order'
    return text, controls


def _relationships(zf, part):
    path = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if path not in zf.namelist():
        return {}
    return {x.get("Id"): x for x in _xml(zf.read(path))}


class _Numbering:
    """Resolve OOXML list markers as metadata; never inject them into source text."""
    def __init__(self, zf):
        names = set(zf.namelist())
        self.styles = {}
        if "word/styles.xml" in names:
            self.styles = {_attr(x, "styleId"): x for x in _xml(zf.read("word/styles.xml")).findall("w:style", NS)}
        self.instances, self.abstracts, self.counts = {}, {}, {}
        if "word/numbering.xml" in names:
            root = _xml(zf.read("word/numbering.xml"))
            self.instances = {_attr(x, "numId"): x for x in root.findall("w:num", NS)}
            self.abstracts = {_attr(x, "abstractNumId"): x for x in root.findall("w:abstractNum", NS)}

    @staticmethod
    def _format(value, fmt):
        if fmt in {"lowerLetter", "upperLetter"}:
            letters, n = "", value
            while n > 0: n, r = divmod(n - 1, 26); letters = chr(65 + r) + letters
            return letters.lower() if fmt == "lowerLetter" else letters
        if fmt in {"lowerRoman", "upperRoman"} and 0 < value < 4000:
            text, n = "", value
            for amount, symbol in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
                while n >= amount: text += symbol; n -= amount
            return text.lower() if fmt == "lowerRoman" else text
        return f"{value:02d}" if fmt == "decimalZero" else str(value)

    def describe(self, p, part):
        ppr = p.find("w:pPr", NS)
        style = {}
        pstyle = ppr.find("w:pStyle", NS) if ppr is not None else None
        style_id = _attr(pstyle, "val")
        if style_id: style["paragraph_style"] = style_id
        direct = ppr.find("w:numPr", NS) if ppr is not None else None
        num = direct
        seen = set()
        while style_id and style_id not in seen:
            seen.add(style_id)
            inherited = self.styles.get(style_id)
            if inherited is None: break
            if num is None: num = inherited.find("w:pPr/w:numPr", NS)
            style_id = _attr(inherited.find("w:basedOn", NS), "val")
        if num is None: return style
        num_id = _attr(num.find("w:numId", NS), "val")
        level = int(_attr(num.find("w:ilvl", NS), "val") or "0")
        if num_id == "0": return style
        info = {"num_id": num_id, "level": str(level), "source": "direct" if direct is not None else "paragraph_style", "marker": None}
        style["numbering"] = info
        instance = self.instances.get(num_id)
        if instance is None: return style
        abstract = self.abstracts.get(_attr(instance.find("w:abstractNumId", NS), "val"))
        if abstract is None: return style
        levels = {int(_attr(x, "ilvl") or 0): x for x in abstract.findall("w:lvl", NS)}
        overrides = {int(_attr(x, "ilvl") or 0): x for x in instance.findall("w:lvlOverride", NS)}
        for i, override in overrides.items():
            overridden = override.find("w:lvl", NS)
            if overridden is not None: levels[i] = overridden
        definition = levels.get(level)
        if definition is None: return style
        start = int(_attr(definition.find("w:start", NS), "val") or 1)
        override = overrides.get(level)
        if override is not None:
            start = int(_attr(override.find("w:startOverride", NS), "val") or start)
        counters = self.counts.setdefault((part, num_id), {})
        counters[level] = counters.get(level, start - 1) + 1
        for key in list(counters):
            if key > level:
                restart = _attr(levels[key].find("w:lvlRestart", NS), "val") if key in levels else None
                if restart != "0": counters.pop(key)
        pattern = _attr(definition.find("w:lvlText", NS), "val") or "%1."
        def replacement(match):
            i = int(match.group(1)) - 1
            entry = levels.get(i)
            value = counters.get(i, int(_attr(entry.find("w:start", NS), "val") or 1) if entry is not None else 1)
            fmt = _attr(entry.find("w:numFmt", NS), "val") if entry is not None else "decimal"
            return self._format(value, fmt)
        info.update(marker=re.sub(r"%([1-9])", replacement, pattern), format=_attr(definition.find("w:numFmt", NS), "val"), value=counters[level])
        return style


def _paragraph_evidence(p, text, target_id, index):
    """Keep physical empty paragraphs and formatting without changing old offsets."""
    raw_text = "".join(p.xpath(".//w:t[not(ancestor::w:del)]/text()", namespaces=NS))
    evidence = {
        "paragraph_id": f"{target_id}:p{index}", "paragraph_index": index,
        "raw_text": raw_text, "text": text, "blank": not text,
        "has_underlined_runs": bool(p.xpath("./w:r/w:rPr/w:u[not(@w:val='none')]", namespaces=NS)),
        "has_tabs": bool(p.xpath(".//w:tab", namespaces=NS)),
        "tab_stops": [{"position": _attr(x, "pos"), "alignment": _attr(x, "val"), "leader": _attr(x, "leader")} for x in p.xpath("./w:pPr/w:tabs/w:tab", namespaces=NS)],
        "indent": dict(p.find("w:pPr/w:ind", NS).attrib) if p.find("w:pPr/w:ind", NS) is not None else {},
    }
    visible=p.xpath('.//w:t[not(ancestor::w:del)]',namespaces=NS)
    if visible and any((node.text or '').strip() for node in visible) and all(
            not (node.text or '').strip() or node.xpath('ancestor::w:ins',namespaces=NS) for node in visible):
        # Source fact only. A tracked insertion can be a revised question;
        # downstream logic must also establish the completed prompt and the
        # inserted paragraph's role before treating it as a source note.
        evidence['fully_tracked_insertion']=True
    return evidence


def extract_docx(path: Path, source_hash: str, document_id: str) -> ExtractedDocument:
    blocks, warnings, unread, table_metadata = [], [], [], []
    order = 0
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if "word/document.xml" not in names:
            raise ValueError("Word 容器缺少 word/document.xml")
        numbering = _Numbering(zf)
        if "word/comments.xml" in names:
            warnings.append({"code": "comments_not_body", "message": "批注未混入现行正文；可在原文件中核对。"})

        def add(kind, text, location, **extra):
            nonlocal order
            order += 1
            block = make_block(kind, text, order, location, **extra)
            blocks.append(block)
            return block

        def paragraph(p, location, kind="paragraph", parent_id=None):
            text, controls = _visible_text(p)
            for control_index, control in enumerate(controls,1):
                control.update(paragraph_index=1,control_index=control_index)
            style = numbering.describe(p, location.get("part"))
            target_id = f"{location.get('part')}:{kind}:{location.get('paragraph')}"
            evidence = _paragraph_evidence(p, text, target_id, 1)
            evidence.update(start=0, end=len(text))
            structure = {"logical_slots": _logical_slots(text, target_id), "paragraph_spans": [evidence], "blank_state": "filled" if text else "control" if controls else "blank", "content_state": "text" if text else "control" if controls else "blank"}
            style_node=numbering.styles.get(style.get('paragraph_style'))
            style_name=_attr(style_node.find('w:name',NS),'val') if style_node is not None else ''
            if re.fullmatch(r'(?:toc|目录)\s*\d+',style_name or '',re.I):structure['resolved_layout_role']='table_of_contents'
            # Empty paragraphs are source positions, not necessarily questions
            # or mandatory fields. Keep them for downstream relationship checks.
            add(kind, text, location, parent_id=parent_id, style=style, structure=structure, controls=controls)
            drawings = p.xpath(".//*[local-name()='drawing' or local-name()='pict']")
            for image_index, drawing in enumerate(drawings, 1):
                has_textbox = bool(drawing.xpath(".//*[local-name()='txbxContent']"))
                if has_textbox:
                    for pi, inner in enumerate(drawing.xpath(".//*[local-name()='txbxContent']/*[local-name()='p']"), 1):
                        inner_text, inner_controls = _visible_text(inner)
                        if inner_text:
                            add("paragraph", inner_text, {**location, "textbox": image_index, "paragraph": pi}, controls=inner_controls)
                embeds = drawing.xpath(".//@r:embed | .//@r:id", namespaces=NS)
                if embeds and not has_textbox:
                    image_location = {**location, "image": image_index, "relationship_id": embeds[0]}
                    add("image", "", image_location, warnings=["图像文字未启用 OCR，无法读取该部分文字"])
                    unread.append({"kind": "image", "location": image_location, "reason": "OCR disabled"})

        def table(tbl, table_no, part, parent_id=None, nested_level=0, preceding_block_ids=None, section_path_block_ids=None):
            preceding_block_ids = preceding_block_ids or []
            section_path_block_ids = section_path_block_ids or []
            rows = tbl.xpath("./w:tr", namespaces=NS)
            table_id = f"{part}:table:{table_no}:level:{nested_level}"
            grid_widths = [int(_attr(x, 'w') or 0) for x in tbl.xpath('./w:tblGrid/w:gridCol', namespaces=NS)]
            declared_columns = len(grid_widths)
            records, max_column = [], 0
            for ri, tr in enumerate(rows, 1):
                cells = tr.xpath("./w:tc", namespaces=NS)
                logical_col = 1 + int(_attr(tr.find("w:trPr/w:gridBefore", NS), "val") or 0)
                for ci, tc in enumerate(cells, 1):
                    tcpr = tc.find("w:tcPr", NS)
                    span, vmerge = 1, None
                    if tcpr is not None:
                        grid = tcpr.find("w:gridSpan", NS)
                        merge = tcpr.find("w:vMerge", NS)
                        if grid is not None:
                            span = int(grid.get(f"{{{W}}}val", "1"))
                        if merge is not None:
                            vmerge = merge.get(f"{{{W}}}val", "continue")
                    cell_id = f"{table_id}:r{ri}:c{logical_col}"
                    text_parts, controls, paragraph_spans = [], [], []
                    text_length = 0
                    for pi, p in enumerate(tc.xpath("./w:p", namespaces=NS), 1):
                        value, found = _visible_text(p)
                        for control_index, control in enumerate(found, 1):
                            control.update(paragraph_index=pi, paragraph_id=f"{cell_id}:p{pi}", control_index=control_index)
                        controls.extend(found)
                        evidence = _paragraph_evidence(p, value, cell_id, pi)
                        evidence["style"] = numbering.describe(p, part)
                        start = text_length + (1 if value and text_parts else 0)
                        for control in found:
                            if control.get('offset_evidence')=='native_xml_character_order' and 'result_start' in control:
                                control['result_start']+=start;control['result_end']+=start
                        evidence.update(start=start, end=start + len(value))
                        paragraph_spans.append(evidence)
                        if value:
                            text_parts.append(value)
                            text_length = start + len(value)
                    text = "\n".join(text_parts)
                    raw_text = "".join(tc.xpath(".//w:t/text()", namespaces=NS))
                    logical_slots = _logical_slots(text, cell_id)
                    label_list = []
                    for slot in logical_slots:
                        offset=slot.get('label_start') if slot.get('label_start') is not None else slot['start']
                        paragraph_span = next((p for p in paragraph_spans if not p["blank"] and p["start"] <= offset < p["end"]), None)
                        if paragraph_span is None: continue
                        # A label can be above its underline in the next
                        # paragraph. Bind the write target to the actual
                        # response range, retaining the label's own paragraph
                        # separately in label_list. Never write through its
                        # caption paragraph just because that supplied context.
                        a, z = slot.get('start'), slot.get('end')
                        target_span = next((p for p in paragraph_spans if not p['blank']
                            and isinstance(a, int) and isinstance(z, int)
                            and p['start'] <= a <= z <= p['end']), None)
                        if target_span is not None:
                            slot["paragraph_index"] = target_span["paragraph_index"]
                            slot["paragraph_id"] = target_span["paragraph_id"]
                        if slot.get('label_start') is None: continue
                        # This is a list of source candidates, not an assertion
                        # that every colon/blank is a required business field.
                        label_list.append({"label_start": slot["label_start"], "label_end": slot["label_end"], "paragraph_index": paragraph_span["paragraph_index"], "paragraph_id": paragraph_span["paragraph_id"], "slot_id": slot["slot_id"], "evidence": "explicit_delimiter"})
                    for evidence in paragraph_spans:
                        if evidence["blank"]:
                            logical_slots.append({"slot_id": evidence["paragraph_id"] + ":blank", "kind": "blank_paragraph_candidate", "start": evidence["start"], "end": evidence["end"], "offset": evidence["start"], "paragraph_id": evidence["paragraph_id"], "paragraph_index": evidence["paragraph_index"]})
                    if text:
                        if raw_text[:1].isspace(): logical_slots.append({"slot_id": cell_id + ":before-content", "offset": 0, "kind": "leading_blank_candidate"})
                        logical_slots.append({"slot_id": cell_id + ":after-content", "offset": len(text), "kind": "trailing_fill_candidate"})
                    records.append({"tc": tc, "row": ri, "physical_cell": ci, "column_start": logical_col, "column_end": logical_col + span - 1, "column_span": span, "vertical_merge": vmerge, "text": text, "controls": controls, "cell_id": cell_id, "row_end": ri, "anchor_cell_id": None, "logical_slots": logical_slots, "paragraph_spans": paragraph_spans, "label_list": label_list})
                    max_column = max(max_column, logical_col + span - 1); logical_col += span

            active = {}
            for record in records:
                covered_columns = range(record["column_start"], record["column_end"] + 1)
                if record["vertical_merge"] == "restart":
                    record["anchor_cell_id"] = record["cell_id"]
                    for column in covered_columns: active[column] = record
                elif record["vertical_merge"] == "continue":
                    anchor = active.get(record["column_start"])
                    if anchor:
                        record["anchor_cell_id"] = anchor["cell_id"]; anchor["row_end"] = record["row"]
                    else:
                        warnings.append({"code": "orphan_vertical_merge", "message": "发现没有起始单元格的纵向合并延续位置。", "location": {"type": "docx", "part": part, "table": table_no, "row": record["row"], "logical_column": record["column_start"]}})
                else:
                    for column in covered_columns: active.pop(column, None)

            column_count = max(declared_columns, max_column)
            row_groups = {row: [x for x in records if x["row"] == row and x["vertical_merge"] != "continue"] for row in range(1, len(rows) + 1)}
            horizontal_pair_rows = set()
            horizontal_signatures = set()
            def possible_pair_label(item):
                # A choice list in the second half of a row is a response,
                # never another independent label merely because it is not
                # empty. This matters in reviewer matrix tables.
                return bool(item["text"] and not item["controls"]
                            and len(re.findall(r"[□☐☑☒]", item["text"])) < 2)
            for row, items in row_groups.items():
                ordered = sorted(items, key=lambda item: item["column_start"])
                contiguous = all(
                    left["column_end"] + 1 == right["column_start"]
                    for left, right in zip(ordered, ordered[1:])
                )
                # Strong local geometry only: at least two label/response
                # pairs, labels present, and at least one physical blank
                # response. A visible Word cell may span several grid columns;
                # that is normal layout geometry and must not disqualify the
                # label/response pattern. Fully populated rows still need
                # model role evidence because they may be data headers/rows.
                if (
                    len(ordered) >= 4 and len(ordered) % 2 == 0 and contiguous
                    and all(possible_pair_label(ordered[index]) for index in range(0, len(ordered), 2))
                    and any(not ordered[index]["text"] and not ordered[index]["controls"] for index in range(1, len(ordered), 2))
                ):
                    horizontal_pair_rows.add(row)
                    horizontal_signatures.add(tuple((item["column_start"], item["column_end"]) for item in ordered))
            # A populated row in the same local region has no EMPTY marker of
            # its own. Reuse only an identical physical grid signature already
            # proven by a neighbouring row with blank response cells.
            for row, items in row_groups.items():
                ordered = sorted(items, key=lambda item: item["column_start"])
                signature = tuple((item["column_start"], item["column_end"]) for item in ordered)
                if signature in horizontal_signatures and all(possible_pair_label(ordered[index]) for index in range(0, len(ordered), 2)):
                    horizontal_pair_rows.add(row)
            # Detect local layouts inside a larger mixed Word table. A table
            # can contain title rows, horizontal key/value rows, a record
            # area, and a Yes/No matrix at different row ranges.
            row_signatures = {
                row: tuple((item["column_start"], item["column_end"]) for item in sorted(items, key=lambda x: x["column_start"]))
                for row, items in row_groups.items()
            }
            qa_header_rows, qa_rows, qa_meta = set(), set(), {}
            for row, items in row_groups.items():
                ordered = sorted(items, key=lambda x: x["column_start"])
                question_labels = [x for x in ordered if re.match(r"^(?:questions?\b|问题(?:\s|$)|题目(?:\s|$)|问项(?:\s|$)|(?:调查|检查|审核)(?:内容|项目)(?:\s|$))", x["text"].strip(), re.I)]
                answer_labels = [x for x in ordered if re.match(r"^(?:answers?\b|responses?\b|yes\s*[,/]\s*no\b|回答|答复|答案|结论(?:\s|$)|结果(?:\s|$))", x["text"].strip(), re.I)]
                if len(question_labels) != 1 or not answer_labels:
                    continue
                number = next((x for x in ordered if re.fullmatch(r"(?:no\.?|number|#|序号|编号)", x["text"].strip(), re.I)), None)
                qa_header_rows.add(row)
                meta = {"question_header_row": row, "number_column": number["column_start"] if number else None,
                        "label_column": question_labels[0]["column_start"], "answer_columns": [x["column_start"] for x in answer_labels],
                        "question_header_cell_id": question_labels[0]["cell_id"], "answer_header_cell_ids": [x["cell_id"] for x in answer_labels]}
                qa_meta[row] = meta
                for later in range(row + 1, len(rows) + 1):
                    if row_signatures.get(later) != row_signatures[row]:
                        # A vertically merged question may have an entirely
                        # empty continuation row. It is still in the same local
                        # answer table; do not discard all later numbered rows.
                        later_items = row_groups[later]
                        if all(not x["text"] and not x["controls"] for x in later_items):
                            qa_rows.add(later); qa_meta[later] = meta
                            continue
                        break
                    # The next explicit header starts another local region.
                    later_items = row_groups[later]
                    if number is not None and any(x["column_start"] == number["column_start"] and re.fullmatch(r"(?:no\.?|序号|编号)", x["text"].strip(), re.I) for x in later_items): break
                    qa_rows.add(later); qa_meta[later] = meta
            # A three-column numbered checklist can use a domain-specific
            # heading instead of the literal word "Question". The explicit
            # serial heading and consecutive physical numbers establish the
            # number/label/response columns; neither header is an answer.
            for row, items in row_groups.items():
                ordered = sorted(items, key=lambda x:x['column_start'])
                if len(ordered)!=3 or row in qa_header_rows|qa_rows:
                    continue
                if not re.fullmatch(r'(?:no\.?|s(?:r)?\.?\s*no\.?|序号|编号)',ordered[0]['text'].strip(),re.I):
                    continue
                numbered_rows=[]; previous=None
                for later in range(row+1,len(rows)+1):
                    if row_signatures.get(later)!=row_signatures[row]:break
                    later_cells=sorted(row_groups[later],key=lambda x:x['column_start'])
                    raw=later_cells[0]['text'].strip().rstrip('.)')
                    if not raw.isdigit() or not later_cells[1]['text'].strip():break
                    number=int(raw)
                    if previous is not None and number!=previous+1:break
                    numbered_rows.append(later); previous=number
                if len(numbered_rows)<2:continue
                meta={'question_header_row':row,'number_column':ordered[0]['column_start'],
                      'label_column':ordered[1]['column_start'],'answer_columns':[ordered[2]['column_start']],
                      'number_source':'explicit_serial_header_and_consecutive_rows',
                      'question_header_cell_id':ordered[1]['cell_id'],'answer_header_cell_ids':[ordered[2]['cell_id']]}
                qa_header_rows.add(row); qa_meta[row]=meta
                for later in numbered_rows:qa_rows.add(later);qa_meta[later]=meta
            # Word list numbering is not w:t text. A dedicated numbered cell
            # can therefore look empty while visibly displaying "4.2.". Use
            # that physical numbering evidence with a separate label and
            # response cell; do not fabricate the number in the raw text.
            literal_letter_rows = {}
            for row, items in row_groups.items():
                ordered = sorted(items, key=lambda x: x["column_start"])
                if len(ordered) >= 3 and ordered[1]["text"].strip() and re.fullmatch(r"[a-zA-Z][.)]", ordered[0]["text"].strip()):
                    literal_letter_rows[row] = (ord(ordered[0]["text"].strip()[0].lower()), ordered)
            for row, (letter, ordered) in literal_letter_rows.items():
                if row in qa_rows or row in qa_header_rows: continue
                neighbors = [other for other in (row-1, row+1) if other in literal_letter_rows
                             and literal_letter_rows[other][0] == letter + other-row
                             and row_signatures[other] == row_signatures[row]]
                if not neighbors: continue
                qa_rows.add(row)
                qa_meta[row] = {"question_header_row": None, "number_column": ordered[0]["column_start"],
                               "label_column": ordered[1]["column_start"], "answer_columns": [x["column_start"] for x in ordered[2:]],
                               "number_source": "consecutive_native_literal_letters", "question_number": ordered[0]["text"].strip(),
                               "number_cell_id": ordered[0]["cell_id"]}
            for row, items in row_groups.items():
                if row in qa_rows or row in qa_header_rows: continue
                ordered = sorted(items, key=lambda x: x["column_start"])
                if len(ordered) < 3 or not ordered[1]["text"].strip(): continue
                number_cell = ordered[0]
                literal_parenthesized = re.fullmatch(r'\((?:[a-zA-Z]|[ivxlcdmIVXLCDM]{2,6})\)',number_cell['text'].strip())
                literal_numeric = len(ordered)==3 and re.fullmatch(r'\d+(?:\.\d+)*[.)]?',number_cell['text'].strip())
                explicit_control = (len(ordered)==3 and not number_cell['text'].strip() and not number_cell['controls']
                                    and not ordered[1]['controls'] and len(ordered[2]['controls'])==1
                                    and ordered[2]['controls'][0].get('type')=='text'
                                    and ordered[2]['controls'][0].get('legacy_result_supported'))
                if literal_parenthesized or literal_numeric or explicit_control:
                    qa_rows.add(row)
                    qa_meta[row]={'question_header_row':None,'number_column':number_cell['column_start'] if literal_parenthesized or literal_numeric else None,
                                  'label_column':ordered[1]['column_start'],'answer_columns':[x['column_start'] for x in ordered[2:]],
                                  'number_source':'parenthesized_source_enumeration' if literal_parenthesized else 'literal_source_number' if literal_numeric else 'separate_native_text_control',
                                  'question_number':number_cell['text'].strip(),'number_cell_id':number_cell['cell_id']}
                    continue
                if number_cell["text"].strip() and not re.fullmatch(r"\d+(?:\.\d+)*[.)]?", number_cell["text"].strip()): continue
                numbered = [p for p in number_cell["paragraph_spans"] if p.get("style", {}).get("numbering", {}).get("marker")]
                if len(numbered) != 1: continue
                number = numbered[0]["style"]["numbering"]
                if not re.fullmatch(r"\s*\d+(?:\.\d+)*[.)]?\s*", number["marker"]): continue
                qa_rows.add(row)
                qa_meta[row] = {"question_header_row": None, "number_column": number_cell["column_start"],
                                "label_column": ordered[1]["column_start"], "answer_columns": [x["column_start"] for x in ordered[2:]],
                                "number_source": "native_numbering", "number_paragraph_id": numbered[0]["paragraph_id"],
                                "question_number": number["marker"], "number_cell_id": number_cell["cell_id"]}
            # Follow-up rows often leave the dedicated number column empty.
            # Keep their label/response relation when an explicit answer
            # control is present, or a filled response follows the same
            # verified numbered physical grid. Never borrow a later value.
            verified_qa_signatures={row_signatures[row] for row in qa_rows if qa_meta[row].get('number_column') is not None}
            for row,items in row_groups.items():
                if row in qa_rows|qa_header_rows:continue
                ordered=sorted(items,key=lambda x:x['column_start'])
                if len(ordered)!=3 or ordered[0]['text'].strip() or ordered[0]['controls'] or not ordered[1]['text'].strip():continue
                target=ordered[2]
                controlled=bool(target['controls']) and all(c.get('type') in {'checkbox','legacy_checkbox'} or
                    (c.get('type')=='text' and c.get('legacy_result_supported')) for c in target['controls'])
                printed_options=len(re.findall(r'[☐□☑☒]',target['text']))>=2
                verified_filled=bool(target['text'].strip()) and row_signatures[row] in verified_qa_signatures
                if not (controlled or printed_options or verified_filled):continue
                qa_rows.add(row);qa_meta[row]={'question_header_row':None,'number_column':None,
                    'label_column':ordered[1]['column_start'],'answer_columns':[target['column_start']],
                    'number_source':'unnumbered_followup_with_native_response_evidence'}
            choice_header_rows, choice_rows, choice_meta = set(), set(), {}
            choice_tokens = {"是", "否", "不适用", "yes", "no", "n/a", "na", "y", "n", "attached", "not attached"}
            def option_word(item):
                value=re.sub(r'^(?:answer|response)\s*[:：]?\s*','',item['text'].strip(),flags=re.I)
                value=re.sub(r'[☐☑☒□✓✔\uf031\uf0f0]','',value).strip()
                return re.sub(r'\s+',' ',value).lower() if value.lower() in choice_tokens else None
            for row, items in row_groups.items():
                ordered = sorted(items, key=lambda x: x["column_start"])
                option_cells=[item for item in ordered if option_word(item)]
                if len(option_cells)<2 or len({option_word(item) for item in option_cells})<2: continue
                for item in option_cells:
                    # The Yes/No column relation identifies a selection target,
                    # but an unsupported font still cannot prove its old state.
                    for control in item['controls']:
                        if control.get('source')=='word_symbol' and control.get('type')=='unknown_symbol':
                            control['type']='checkbox';control['role_evidence']='verified_choice_column'
                labels=[item for item in ordered if item['column_end']<option_cells[0]['column_start'] and item['text'].strip() and not re.fullmatch(r'(?:\d+[.)]?|s\.?\s*no\.?|no\.?|#|序号)',item['text'].strip(),re.I)]
                if not labels: continue
                label=labels[-1]
                header=bool(re.fullmatch(r'(?:questions?|check\s*points?|items?|问题|题目|问项|name\s+of\s+the\s+document)',label['text'].strip(),re.I))
                if not header and row==1 and not re.search(r'[?？]|^(?:do|does|is|are|have|has|if|please)\b',label['text'].strip(),re.I):
                    aligned=[sorted(row_groups[later],key=lambda x:x['column_start']) for later in range(row+1,len(rows)+1)]
                    header=bool(aligned) and all(any(x['column_start']==label['column_start'] and x['text'].strip() for x in later) and all(not x['text'].strip() for x in later if x['column_start'] in {o['column_start'] for o in option_cells}) for later in aligned)
                options=[{'text':re.sub(r'^(?:answer|response)\s*[:：]?\s*','',item['text'].strip(),flags=re.I),'column_start':item['column_start'],'column_end':item['column_end'],'header_cell_id':item['cell_id']} for item in option_cells]
                meta={'header_row':row if header else None,'options':options,'choice_label_column':label['column_start'],'choice_comment_columns':[x['column_start'] for x in ordered if re.match(r'^(?:remarks?|comments?|备注)',x['text'].strip(),re.I)]}
                if header:
                    choice_header_rows.add(row)
                    for later in range(row + 1, len(rows) + 1):
                        later_items = sorted(row_groups.get(later, []), key=lambda x: x["column_start"])
                        later_label=next((x for x in later_items if x['column_start']==label['column_start']),None)
                        if not later_label or not later_label['text'].strip(): continue
                        present={x['column_start'] for x in later_items}
                        if not all(o['column_start'] in present for o in options):
                            if len(later_items)!=len(ordered):continue
                            adjusted=[{**o,'column_start':later_items[ordered.index(next(x for x in ordered if x['cell_id']==o['header_cell_id']))]['column_start']} for o in options]
                            choice_rows.add(later);choice_meta[later]={**meta,'options':adjusted};continue
                        choice_rows.add(later); choice_meta[later]=meta
                else:
                    choice_rows.add(row); choice_meta[row]=meta
            # Choice columns are stronger than native numbering or alternating
            # populated/blank geometry. Number cells remain metadata only.
            qa_header_rows-=choice_header_rows;qa_rows-=choice_rows
            horizontal_pair_rows-=choice_rows|choice_header_rows
            record_header_rows, record_rows, record_meta = set(), set(), {}
            record_spacer_cells, record_context_cells = set(), set()
            for row, items in row_groups.items():
                ordered = sorted(items, key=lambda x: x["column_start"])
                signature = row_signatures[row]
                contact_columns=[item for item in ordered if item['text'].strip()]
                contact_header=(len(contact_columns)>=3
                    and any(re.fullmatch(r'(?:name|姓名)',item['text'].strip(),re.I) for item in contact_columns)
                    and sum(bool(re.fullmatch(r'(?:phone(?:\s*/\s*fax)?|telephone|e[- ]?mail|电话|邮箱|邮件)',item['text'].strip(),re.I)) for item in contact_columns)>=2
                    and all(item['text'].strip() or (item is ordered[0] and not item['controls']) for item in ordered))
                if row in choice_header_rows | choice_rows | qa_header_rows | qa_rows | record_rows | horizontal_pair_rows or len(ordered) < 2 or (not all(item["text"] for item in ordered) and not contact_header):
                    continue
                # A populated question or first record is not a new header.
                # Header evidence is local and conservative, not "a blank
                # occurs somewhere later in this table".
                if re.fullmatch(r"\s*\d+[.)]?\s*", ordered[0]["text"]):
                    continue
                following = []
                for later in range(row + 1, len(rows) + 1):
                    if row_signatures.get(later) != signature or later in qa_header_rows | qa_rows | choice_header_rows | choice_rows | horizontal_pair_rows:
                        break
                    following.append(later)
                if not following:
                    continue
                if contact_header and not ordered[0]['text'].strip():
                    gutter=ordered[0]['column_start']
                    if any(any(x['text'].strip() or x['controls'] for x in row_groups[later] if x['column_start']==gutter) for later in following):
                        contact_header=False
                    if not contact_header:continue
                    for rn in [row,*following]:
                        record_spacer_cells.update(x['cell_id'] for x in row_groups[rn] if x['column_start']==gutter)
                    ordered=contact_columns
                if contact_header:
                    role_column=next((x['column_start'] for x in ordered if re.fullmatch(r'(?:area|role|responsibilit(?:y|ies)|department|职责|部门|范围)',x['text'].strip(),re.I)),None)
                    if role_column is not None:
                        for later in following:
                            record_context_cells.update(x['cell_id'] for x in row_groups[later] if x['column_start']==role_column)
                if (len(ordered) == 2 and ordered[0]["text"].rstrip().endswith((":", "："))
                        and not ordered[1]["text"].rstrip().endswith((":", "："))
                        and any(all(not x["text"].strip() for x in row_groups[later]) for later in following)
                        and sum(sorted(row_groups[later],key=lambda x:x["column_start"])[0]["text"].rstrip().endswith((":", "："))
                                for later in following) >= 2):
                    # Repeated caption/value pairs separated by empty spacer
                    # rows supply stronger local form evidence than the mere
                    # presence of multiple blanks. Colons in actual column
                    # headings do not by themselves suppress record tables.
                    continue
                # Long, descriptive column labels (including a contact
                # permission question) are valid record headers when multiple
                # aligned reserved rows establish that local record grid.
                reserved_rows = [later for later in following if sum(not item["text"].strip() for item in row_groups[later]) >= max(2, len(ordered) - 1)]
                # Answered record tables often have their unused reserve rows
                # removed. Retain the two-column record relation only with
                # independent heading evidence: an explicit serial-number
                # column, or a plural record caption with a distinctly centered
                # heading row above noncentered records. Being the first row
                # (or being bold) alone is not sufficient.
                serial_header = bool(re.fullmatch(r"(?:s(?:r)?\.?\s*no\.?|serial\s*(?:no\.?|number)|序号|编号)", ordered[0]["text"].strip(), re.I))
                plural_record_caption = bool(re.fullmatch(r"(?:products|materials|items|components|facilities|sites|locations|产品列表|物料列表|场所列表)", ordered[0]["text"].strip(), re.I))
                def all_centered(items):
                    return all(item["tc"].xpath("./w:p/w:pPr/w:jc[@w:val='center']", namespaces=NS) for item in items)
                aligned_heading = (plural_record_caption and all_centered(ordered)
                                   and not all_centered(row_groups[following[0]]))
                explicit_completed_record = (len(ordered) == 2 and (serial_header or aligned_heading)) or contact_header
                # In a completely filled certificate/history grid, a date
                # caption above repeated actual dates is independent column
                # evidence. Require the repeated physical grid and multiple
                # date values; a date word or a populated first row alone is
                # insufficient. This also works after blank reserve rows were
                # removed from the answered copy.
                typed_date_columns=[]
                if len(ordered)>=3 and len(following)>=2:
                    for column,item in enumerate(ordered):
                        if not re.search(r'\b(?:date|issued|expiry|expiration)\b|日期|有效期|到期',item['text'],re.I):continue
                        values=[next(x['text'].strip() for x in row_groups[later] if x['column_start']==item['column_start']) for later in following]
                        date_values=sum(bool(re.search(r'(?<!\d)(?:19|20)\d{2}[.\-/年]\d{1,2}[.\-/月]\d{1,2}(?:日)?(?!\d)',value)) for value in values)
                        if date_values>=2:typed_date_columns.append(item['column_start'])
                typed_completed_record=bool(typed_date_columns)
                if len(ordered)==2 and len(reserved_rows)<2 and not explicit_completed_record: continue
                if len(reserved_rows) < 2 and not typed_completed_record and any(len(x["text"]) > 100 or re.search(r"[?？]", x["text"]) for x in ordered):
                    continue
                # A repeated grid beneath a populated multi-column row is a
                # record area. Empty reserved rows remain physical positions,
                # but are not interpreted as mandatory records.
                if any(any(not item["text"] for item in row_groups[later]) for later in following) or len(ordered) >= 5 or explicit_completed_record or typed_completed_record:
                    record_header_rows.add(row)
                    columns = [{"text": item["text"], "column_start": item["column_start"], "column_end": item["column_end"], "header_cell_id": item["cell_id"]} for item in ordered]
                    for record_index, later in enumerate(following, 1):
                        record_rows.add(later); record_meta[later] = {"header_row": row, "record_index": record_index, "columns": columns}
            label_rows = [items for items in row_groups.values() if any(x["column_start"] == 1 and x["text"] for x in items) and any(x["column_start"] > 1 or x["column_end"] > 1 for x in items)]
            blank_right = sum(any((x["column_start"] > 1 or x["column_end"] > 1) and not x["text"] for x in items) for items in label_rows)
            first_row_cells = row_groups.get(1, []); first_row_all_filled = len(first_row_cells) >= 2 and all(x["text"] for x in first_row_cells)
            layout_hint = ("numbered_question_answer_candidate" if qa_rows and any(x.get("number_column") is not None for x in qa_meta.values()) else "data_table_candidate" if qa_rows or record_rows else ("label_response_form_candidate" if label_rows and len(label_rows) >= max(2, len(rows) // 3) else "mixed_or_data_table"))
            occupied = {row: set() for row in range(1, len(rows) + 1)}
            for record in records:
                occupied[record["row"]].update(range(record["column_start"], record["column_end"] + 1))
            absent = [{"row": row, "column": column} for row in occupied for column in range(1, column_count + 1) if column not in occupied[row]]
            table_metadata.append({"table_id": table_id, "part": part, "table": table_no, "row_count": len(rows), "column_count": column_count, "nested_level": nested_level, "preceding_block_ids": preceding_block_ids, "section_path_block_ids": section_path_block_ids, "layout_hint": layout_hint, "horizontal_pair_rows": sorted(horizontal_pair_rows), "label_row_count": len(label_rows), "blank_response_row_count": blank_right, "absent_grid_positions": absent})

            for record in records:
                location = {"type": "docx", "part": part, "table": table_no, "row": record["row"], "cell": record["physical_cell"], "logical_column": record["column_start"], "cell_id": record["cell_id"]}
                continuation = record["vertical_merge"] == "continue"
                empty_text_control = (len(record["controls"]) == 1 and record["controls"][0].get("type") == "text"
                                      and record["controls"][0].get("legacy_result_supported")
                                      and record["controls"][0].get("placeholder") and not record["text"].strip())
                blank_state = "merge_continuation" if continuation else ("blank" if empty_text_control else "filled" if record["text"] or record["controls"] else "blank")
                content_state = "merge_continuation" if continuation else ("text" if record["text"] else "control" if record["controls"] else "blank")
                if record["row"] in qa_header_rows: local_layout = "question_answer_header"
                elif record["row"] in qa_rows: local_layout = "numbered_question_answer" if qa_meta[record["row"]]["number_column"] is not None else "question_answer"
                elif record["row"] in choice_header_rows: local_layout = "choice_matrix_header"
                elif record["row"] in choice_rows: local_layout = "choice_matrix"
                elif record["row"] in record_header_rows: local_layout = "record_table_header"
                elif record["row"] in record_rows: local_layout = "record_table"
                else:
                    local_items = sorted(row_groups[record["row"]], key=lambda item: item["column_start"])
                    explicit_text_form = (len(local_items) == 2 and local_items[0]["text"].strip() and not local_items[0]["controls"]
                        and local_items[0]["column_end"] + 1 == local_items[1]["column_start"]
                        and len(local_items[1]["controls"]) == 1 and local_items[1]["controls"][0].get("type") == "text"
                        and local_items[1]["controls"][0].get("legacy_result_supported"))
                    local_layout = ("horizontal_key_value_form" if record["row"] in horizontal_pair_rows
                        else "label_response_form" if explicit_text_form
                        else "choice_matrix" if any(c.get("type") in {"checkbox", "legacy_checkbox"} for c in record["controls"]) or re.search(r"[☐☑☒□].{0,20}[☐☑☒□]", record["text"])
                        else "label_response_form" if layout_hint == "label_response_form_candidate" else "mixed")
                special = choice_meta.get(record["row"], record_meta.get(record["row"], {}))
                structure = {"table_id": table_id, "table_layout_hint": layout_hint, "local_layout_type": local_layout, "row_layout_hint": "horizontal_key_value_candidate" if record["row"] in horizontal_pair_rows else "", "potential_column_header": record["row"] in choice_header_rows or record["row"] in record_header_rows or (len(rows) > 1 and first_row_all_filled and record["row"] == 1), "table_row_count": len(rows), "table_column_count": column_count, "cell_id": record["cell_id"], "row": record["row"], "column": record["column_start"], "row_start": record["row"], "row_end": record["row_end"], "column_start": record["column_start"], "column_end": record["column_end"], "grid_col_start": record["column_start"], "grid_col_end": record["column_end"], "row_span": record["row_end"] - record["row"] + 1, "column_span": record["column_span"], "grid_span": record["column_span"], "vertical_merge": record["vertical_merge"], "is_merge_continuation": continuation, "anchor_cell_id": record["anchor_cell_id"], "merge_anchor_id": record["anchor_cell_id"], "blank_state": blank_state, "content_state": content_state, "logical_slots": [] if continuation else record["logical_slots"], "nested_level": nested_level, "preceding_block_ids": preceding_block_ids, "section_path_block_ids": section_path_block_ids, "preceding_block_id": preceding_block_ids[-1] if preceding_block_ids else None, "choice_header_row": special.get("header_row") if record["row"] in choice_rows else None, "choice_options": special.get("options", []) if record["row"] in choice_rows else [], "record_header_row": special.get("header_row") if record["row"] in record_rows else None, "record_index": special.get("record_index") if record["row"] in record_rows else None, "record_columns": special.get("columns", []) if record["row"] in record_rows else []}
                structure["potential_column_header"] = record["row"] in choice_header_rows | record_header_rows | qa_header_rows
                if record['cell_id'] in record_spacer_cells:
                    structure.update(blank_state='structural_spacer',layout_evidence='empty_column_before_verified_contact_record_grid')
                if record['cell_id'] in record_context_cells:
                    structure['fixed_record_context']=True
                structure["paragraph_spans"] = record["paragraph_spans"]
                structure["label_list"] = record["label_list"] if not continuation else []
                structure["is_label_list_candidate"] = len(record["label_list"]) >= 2 and not continuation
                structure.update(qa_meta.get(record["row"], {}))
                if record['row'] in choice_rows:
                    structure.update(choice_label_column=choice_meta[record['row']]['choice_label_column'],choice_comment_columns=choice_meta[record['row']].get('choice_comment_columns',[]))
                if len(grid_widths)>=record['column_end'] and all(w>0 for w in grid_widths):
                    structure['grid_x_twips']=[sum(grid_widths[:record['column_start']-1]),sum(grid_widths[:record['column_end']])]
                width=record['tc'].find('w:tcPr/w:tcW',NS)
                borders=record['tc'].find('w:tcPr/w:tcBorders',NS)
                if borders is not None:
                    structure['native_cell_borders']={etree.QName(edge).localname:_attr(edge,'val') for edge in borders}
                row_items=sorted(row_groups[record['row']],key=lambda x:x['column_start'])
                if width is not None and (_attr(width,'type') or 'dxa')=='dxa' and 0<int(_attr(width,'w') or 0)<240 and record is row_items[-1] and not record['text'] and not record['controls'] and len(row_items)>2 and re.search(r'_{3,}',row_items[-2]['text']):
                    structure.update(blank_state='structural_spacer',layout_evidence='narrow_empty_tail_after_explicit_response_lines',cell_width_twips=int(_attr(width,'w')))
                add("table_cell", record["text"] if not continuation else "", location, parent_id=parent_id, structure=structure, controls=record["controls"])
                for nested_no, nested in enumerate(record["tc"].xpath("./w:tbl", namespaces=NS), 1):
                    table(nested, f"{table_no}.{record['row']}.{record['physical_cell']}.{nested_no}", part, table_id, nested_level + 1, preceding_block_ids, section_path_block_ids)

        def parse_part(part, semantic_kind=None):
            root = _xml(zf.read(part))
            body = root.find("w:body", NS) if part == "word/document.xml" else root
            p_no = t_no = 0
            pending_paragraph_ids, active_context_ids, active_parent_ids = [], [], []

            def block_text(block_id):
                return next((item.text for item in reversed(blocks) if item.block_id == block_id), "")

            def section_path(ids):
                nonlocal active_parent_ids
                headings=[i for i,bid in enumerate(ids) if re.match(r'^\s*\d+(?:\.\d+)*[.)]?\s+\S',block_text(bid)) and not re.search(r'\t\s*\d+\s*$',block_text(bid))]
                if headings and headings[-1]>0 and any(re.search(r'\t\s*\d+\s*$',block_text(bid)) for bid in ids[:headings[-1]]):
                    # TOC entries and intervening notes are source content,
                    # not a combined section title for the following table.
                    active_parent_ids=[]
                    return [list(ids[headings[-1]:])]
                numbered = next((index for index, block_id in enumerate(ids) if re.match(r"^\s*\d+(?:\.\d+)*[.)]?\s+\S", block_text(block_id))), None)
                if numbered is not None:
                    if ids[:numbered]:
                        active_parent_ids = list(ids[:numbered])
                    leaf = list(ids[numbered:])
                    return [group for group in (list(active_parent_ids), leaf) if group]
                if active_parent_ids:
                    return [list(active_parent_ids), list(ids)] if ids else [list(active_parent_ids)]
                return [list(ids)] if ids else []
            for child in body:
                local = etree.QName(child).localname
                if local == "p":
                    p_no += 1
                    before = len(blocks)
                    paragraph(child, {"type": "docx", "part": part, "paragraph": p_no}, semantic_kind or "paragraph")
                    if len(blocks) > before and blocks[-1].text:
                        pending_paragraph_ids = (pending_paragraph_ids + [blocks[-1].block_id])[-4:]
                elif local == "tbl":
                    t_no += 1
                    if pending_paragraph_ids: active_context_ids = list(pending_paragraph_ids)
                    path_ids = section_path(list(active_context_ids))
                    table(child, t_no, part, preceding_block_ids=list(active_context_ids), section_path_block_ids=path_ids)
                    pending_paragraph_ids = []

        parse_part("word/document.xml")
        for part in sorted(name for name in names if name.startswith("word/header") and name.endswith(".xml")):
            parse_part(part, "header")
        for part in sorted(name for name in names if name.startswith("word/footer") and name.endswith(".xml")):
            parse_part(part, "footer")
        for name in names:
            if any(token in name.lower() for token in ("embeddings/", "activex/", "vbaproject")):
                location = {"type": "docx", "part": name}
                unread.append({"kind": "embedded_object", "location": location, "reason": "active or embedded content not executed"})
                warnings.append({"code": "active_content", "message": "宏或嵌入对象未执行，内容可能需要在原文件中核对。", "location": location})
    status = "partial" if unread else "complete"
    return ExtractedDocument(
        document_id=document_id,
        source_hash=source_hash,
        filename=path.name,
        file_type=path.suffix.lower().lstrip(".").upper(),
        parser="native_ooxml",
        parser_version=PARSER_VERSION,
        extraction_status=status,
        source_blocks=blocks,
        warnings=warnings,
        unread_regions=unread,
        metadata={"coordinate_system": None, "docx_pages_available": False, "deleted_revision_text_included": False, "tables": table_metadata},
    )
