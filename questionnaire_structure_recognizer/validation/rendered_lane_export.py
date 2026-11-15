"""Preserve native parallel answer paragraphs when a replacement wraps less.

Only validated answer paragraphs receive extra spacing. Labels and their XML
remain untouched. Expansion beyond the verified band refuses template output
instead of compressing text or silently shifting an answer beside another label.
"""
from copy import deepcopy


def spacing_adjustments(lanes, positions):
    changes, errors, cumulative = [], [], 0.0
    for lane in lanes:
        if lane.get('answer_is_empty'):
            continue
        label = positions.get(lane['label_paragraph'])
        pid = lane['answer_paragraphs'][0]
        answer = positions.get(pid)
        original = lane.get('answer_origins', {}).get(pid)
        if not label or not answer or not original or label[0] != answer[0]:
            errors.append(pid + ': missing or cross-page paragraph alignment')
            continue
        expected = original[2] - lane['baseline']
        needed = expected - (answer[2] - label[2]) - cumulative
        if needed < -1.0:
            errors.append(pid + ': replacement exceeds the source answer band')
        elif needed > 1.0:
            changes.append((pid, float(needed)))
            cumulative += needed
    return changes, errors


def _paragraphs(document, blocks, wanted):
    from ..exporter import locate_docx_target
    found = {}
    for block in blocks.values():
        spans = [p for p in block.get('structure', {}).get('paragraph_spans', [])
                 if p['paragraph_id'] in wanted]
        if not spans:
            continue
        container = locate_docx_target(document, {'location': block['location'], 'structure': block['structure']})
        if not container or container[1] != 'cell':
            continue
        for span in spans:
            index = span['paragraph_index'] - 1
            if 0 <= index < len(container[0].paragraphs):
                found[span['paragraph_id']] = container[0].paragraphs[index]
    return found


def _positions(document, blocks, wanted):
    from ..parser_core.native_layout import document_paragraph_positions
    return document_paragraph_positions(document, blocks, wanted)


def preserve_rendered_answer_bands(document, source, result, changed_targets):
    from docx import Document
    from docx.shared import Pt
    from ..exporter import TemplateFillError
    blocks = {b['block_id']: b for b in result.get('source_blocks', [])}
    groups = []
    for bid in changed_targets:
        lanes = deepcopy(blocks.get(bid, {}).get('structure', {}).get('filled_lane_alignment', []))
        if lanes:
            from ..parser_core.filled_word_layout import label_groups
            for lane in lanes:
                group = next((g for g in label_groups(blocks[lane['label_block']])
                              if g['start'] == lane['label_start'] and g['end'] == lane['label_end']), None)
                lane['label_candidates'] = group.get('paragraph_ids', [group['paragraph_id']]) if group else [lane['label_paragraph']]
            groups.append(lanes)
    if not groups:
        return []
    wanted = {p for lanes in groups for lane in lanes
              for p in [*lane['label_candidates'], *lane['answer_paragraphs']]}
    paragraphs = _paragraphs(document, blocks, wanted)
    from docx.oxml.ns import qn
    for lanes in groups:
        for lane in lanes:
            for pid in lane['answer_paragraphs']:
                paragraph=paragraphs.get(pid)
                if paragraph is None:
                    raise TemplateFillError('并排答复段落已变化，无法定位原模板；请重新核对或使用标准 Word。')
                rows=paragraph._p.xpath('ancestor::w:tr[1]')
                if rows and any(h.get(qn('w:hRule'))=='exact' for h in rows[0].xpath('./w:trPr/w:trHeight')):
                    raise TemplateFillError('原模板并排填写区域使用固定行高，无法保证修改后的内容完整可见；请使用标准 Word 导出。')
            lane['answer_is_empty']=all(not paragraphs[pid].text.strip() for pid in lane['answer_paragraphs'])
    if any(not lane.get('answer_origins') or lane.get('layout_measurement_version') != 'filled-paragraph-lanes-v7-unmodified-native-render'
           for lanes in groups for lane in lanes):
        original = _positions(Document(source), blocks, wanted)
        for lanes in groups:
            for lane in lanes:
                first_answer=original.get(lane['answer_paragraphs'][0])
                anchor=next((pid for pid in lane['label_candidates'] if pid in original and first_answer
                             and original[pid][0]==first_answer[0]),lane['label_paragraph'])
                lane['label_paragraph']=anchor
                origin = original.get(anchor)
                if origin:
                    lane['baseline'] = origin[2]
                lane['answer_origins'] = {pid: original[pid] for pid in lane['answer_paragraphs'] if pid in original}
    adjustments = []
    # A second pass verifies the actual exported geometry after bounded padding.
    for attempt in range(3):
        positions = _positions(document, blocks, wanted)
        pending, errors = [], []
        for lanes in groups:
            changes, failures = spacing_adjustments(lanes, positions)
            pending.extend(changes); errors.extend(failures)
            # Matching a first baseline alone does not prove that a long final
            # paragraph or later part of a multi-paragraph answer is visible.
            for lane in lanes:
                errors.extend(pid+': complete paragraph text not uniquely visible in rendering'
                    for pid in lane['answer_paragraphs'] if paragraphs[pid].text.strip() and pid not in positions)
        if not pending:
            if errors:
                error = TemplateFillError('并排多字段的新答案超出原有段落布局，无法保证标签与答复对齐；请使用标准 Word 导出，内容不会截断。')
                error.layout_diagnostics = {'errors': errors, 'positions': positions, 'lanes': groups, 'adjustments': adjustments}
                raise error
            return adjustments
        if attempt == 2:
            break
        for pid, amount in pending:
            paragraph = paragraphs[pid]
            before = paragraph.paragraph_format.space_before
            if before is None:
                style = paragraph.style
                while style is not None and before is None:
                    before = style.paragraph_format.space_before
                    style = style.base_style
            old = before.pt if before is not None else 0.0
            paragraph.paragraph_format.space_before = Pt(old + amount)
            adjustments.append({'kind': 'native_answer_lane_minimum_spacing', 'paragraph_id': pid,
                'space_before_points_before': old, 'space_before_points_after': old + amount,
                'changed_properties': ['answer_paragraph/pPr/spacing/before'],
                'reason': '短答案减少自然换行；仅增加已验证答复段前距，保持与原标签对齐，不改标签或答案文字。'})
    raise TemplateFillError('并排字段排版复核未收敛，未输出错位模板；请使用标准 Word 导出。')
