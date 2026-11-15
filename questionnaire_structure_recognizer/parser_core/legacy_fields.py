"""Bounded legacy FORMTEXT result access; never evaluates field instructions."""
from __future__ import annotations

from copy import deepcopy
from lxml import etree

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
def tag(name): return '{' + W + '}' + name


def _parts(begin):
    paragraph = begin
    while paragraph is not None and paragraph.tag != tag('p'):
        paragraph = paragraph.getparent()
    if paragraph is None or begin.get(tag('fldCharType')) != 'begin': return None
    nodes = list(paragraph.iter())
    start = nodes.index(begin)
    separate, end, instructions = None, None, []
    # Nested fields and multi-paragraph fields require a different writer.
    for node in nodes[start + 1:]:
        if node.tag == tag('fldChar'):
            kind = node.get(tag('fldCharType'))
            if kind == 'begin': return None
            if kind == 'separate':
                if separate is not None: return None
                separate = node
            if kind == 'end':
                end = node
                break
        elif node.tag == tag('instrText') and separate is None:
            instructions.append(node.text or '')
    if separate is None or end is None or ''.join(instructions).strip().upper() != 'FORMTEXT': return None
    middle = nodes[nodes.index(separate) + 1:nodes.index(end)]
    # A result containing controls/images/links must not be flattened.
    if any(n.tag in {tag(x) for x in ('sdt', 'drawing', 'pict', 'object', 'hyperlink', 'tab')} for n in middle): return None
    # A regular FORMTEXT value may contain line breaks inside the same
    # paragraph. Page/column breaks remain layout instructions, never text.
    if any(n.tag == tag('br') and n.get(tag('type'), 'textWrapping') != 'textWrapping' for n in middle): return None
    texts = [n for n in middle if n.tag in {tag('t'), tag('br'), tag('cr')}]
    if any(n.getparent().tag != tag('r') for n in texts): return None
    return texts, end


def inspect_legacy_text(begin):
    data = begin.find(tag('ffData'))
    inp = data.find(tag('textInput')) if data is not None else None
    if inp is None: return {}
    parts = _parts(begin)
    kind = inp.find(tag('type'))
    formatting = inp.find(tag('format'))
    text_format = formatting.get(tag('val'), '') if formatting is not None else ''
    enabled = data.find(tag('enabled'))
    maximum = inp.find(tag('maxLength'))
    raw_limit = maximum.get(tag('val'), '') if maximum is not None else ''
    value = ''.join((n.text or '') if n.tag == tag('t') else '\n' for n in parts[0]) if parts else ''
    supported = bool(parts) and (kind is None or kind.get(tag('val'), 'regular') == 'regular') and text_format in {'', 'UPPERCASE', 'LOWERCASE'}
    supported = supported and (enabled is None or enabled.get(tag('val'), '1') not in {'0', 'false', 'off'})
    return {'legacy_result_supported': supported, 'current_value': value,
            'placeholder': not value.strip(), 'max_length': int(raw_limit) if raw_limit.isdigit() else None,
            'legacy_text_format': text_format,
            'legacy_text_type': kind.get(tag('val')) if kind is not None else 'regular'}


def set_legacy_text(begin, value: str, expected: dict) -> bool:
    current = inspect_legacy_text(begin)
    if not current.get('legacy_result_supported') or not expected.get('legacy_result_supported'): return False
    if current.get('current_value') != expected.get('current_value') or current.get('max_length') != expected.get('max_length'): return False
    if current.get('legacy_text_format', '') != expected.get('legacy_text_format', ''): return False
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    # Preserve the native control's explicit format without silently changing a
    # saved answer during export. Non-conforming values must be resolved before
    # export; later Word field updates then cannot change the saved answer.
    text_format = current.get('legacy_text_format')
    if text_format:
        from ..native_text_constraints import normalized_native_case
        try: formatted = normalized_native_case(value, text_format)
        except ValueError: return False
        if value != formatted: return False
    limit = current.get('max_length')
    if limit and len(value) > limit: return False
    texts, end = _parts(begin)
    if texts:
        anchor = texts[0]
        if anchor.tag != tag('t'):
            text = etree.Element(tag('t')); anchor.addprevious(text)
        else: text = anchor
        # Remove only the bounded old result tokens, retaining all run
        # formatting, field delimiters, bookmarks, and neighboring controls.
        for node in texts:
            if node is not text: node.getparent().remove(node)
    else:
        end_run = end.getparent()
        if end_run.tag != tag('r') or end_run.getparent().tag != tag('p'): return False
        run = etree.Element(tag('r'))
        props = end_run.find(tag('rPr'))
        if props is not None: run.append(deepcopy(props))
        text = etree.SubElement(run, tag('t'))
        end_run.addprevious(run)
    lines = value.split('\n')
    text.text = lines[0]
    text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    previous = text
    for line in lines[1:]:
        br = etree.Element(tag('br')); previous.addnext(br)
        following = etree.Element(tag('t')); following.text = line
        following.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        br.addnext(following); previous = following
    return True
