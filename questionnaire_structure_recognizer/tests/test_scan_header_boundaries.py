"""Page furniture must never erase physically located business response lanes."""
from questionnaire_structure_recognizer.validation.scanned_forms import restore_scanned_fields


def block(bid, text, bbox, page, **structure):
    return {'block_id': bid, 'kind': 'ocr_text', 'text': text, 'order': 0,
            'location': {'type': 'pdf', 'page': page, 'bbox': bbox, 'extraction': 'ocr'},
            'structure': structure, 'controls': []}


def field_blocks(page, y=80):
    label = block(f'label-{page}', 'Fictional record identifier:', [40, y, 180, y+12], page)
    value = block(f'value-{page}', f'TEST_{page}', [205, y, 265, y+12], page,
                  native_overlay_answer_region=f'lane-{page}')
    lane = block(f'lane-{page}', '', [200, y+2, 400, y+13], page,
                 scan_candidate=True, scan_region_kind='write_line', blank_state='filled',
                 overlay_value_block_ids=[value['block_id']])
    return [label, value, lane]


def assemble(blocks):
    fields = []
    lookup = {b['block_id']: b for b in blocks}
    restore_scanned_fields(fields, blocks, lookup)
    return fields, lookup


def test_repeated_punctuation_cannot_turn_blank_response_lines_into_headers():
    blocks = []
    for page in range(1, 4):
        blocks.extend(field_blocks(page))
        blocks += [block(f'punctuation-{page}', '.', [500, 760, 503, 772], page),
                   block(f'footer-{page}', 'Fictional version 1', [40, 810, 170, 822], page)]
    fields, lookup = assemble(blocks)
    assert {f['answer_text_original'] for f in fields} == {'TEST_1', 'TEST_2', 'TEST_3'}
    assert all(lookup[f'lane-{p}']['structure'].get('resolved_layout_role') != 'repeated_header_footer' for p in range(1, 4))
    assert all(lookup[f'footer-{p}']['structure']['resolved_layout_role'] == 'repeated_header_footer' for p in range(1, 4))


def test_repeated_heading_does_not_swallow_other_columns_or_higher_business_rows():
    blocks = []
    for page in range(1, 4):
        blocks.extend(field_blocks(page))
        blocks += [block(f'header-{page}', 'Fictional document reference', [430, 145, 570, 157], page),
                   block(f'counter-{page}', f'Page: {page} of 3', [470, 810, 570, 822], page)]
    fields, lookup = assemble(blocks)
    assert len(fields) == 3
    assert all(f['answer_text_original'] == f'TEST_{f["source_locations"]["question"][0]["page"]}' for f in fields)
    assert all(lookup[f'header-{p}']['structure']['resolved_layout_role'] == 'repeated_header_footer' for p in range(1, 4))


def test_repetition_at_different_source_positions_is_not_header_evidence():
    blocks = []
    for page in range(1, 4):
        blocks += [block(f'note-{page}', 'Same business context', [45, 50*page, 190, 50*page+12], page),
                   block(f'counter-{page}', f'Page: {page} of 3', [470, 810, 570, 822], page)]
    _, lookup = assemble(blocks)
    assert all(lookup[f'note-{p}']['structure'].get('resolved_layout_role') != 'repeated_header_footer' for p in range(1, 4))


def test_margin_header_does_not_become_field_due_to_distant_unowned_rule():
    blocks = []
    for page in range(1, 4):
        blocks += [block(f'header-{page}', 'Fictional repeated letterhead', [60, 20, 210, 34], page),
                   block(f'counter-{page}', f'Page: {page} of 3', [470, 810, 570, 822], page)]
    blocks.append(block('distant-rule', '', [380, 23, 520, 34], 2, scan_candidate=True,
                        scan_region_kind='write_line', blank_state='blank'))
    _, lookup = assemble(blocks)
    assert all(lookup[f'header-{p}']['structure']['resolved_layout_role'] == 'repeated_header_footer' for p in range(1, 4))


def test_scanned_header_translation_requires_a_second_verified_source_anchor():
    blocks = []
    # Five physical pages; the last has a translated two-line header. One
    # nearby anchor line remains within the observed source coordinate band.
    for page, shift in [(1, 0), (2, 1), (3, 2), (4, 3), (5, -3)]:
        first_shift = shift-2 if page == 5 else shift
        blocks += [block(f'en-{page}', 'Fictional Company Ltd.', [60+first_shift, 20, 210+first_shift, 34], page),
                   block(f'local-{page}', 'Fictional second letterhead', [60+shift, 36, 210+shift, 50], page),
                   block(f'counter-{page}', f'Page: {page} of 5', [470, 810, 570, 822], page)]
    _, lookup = assemble(blocks)
    assert lookup['en-5']['structure']['resolved_layout_role'] == 'repeated_header_footer'
    assert lookup['en-5']['structure']['scan_header_evidence']['kind'] == 'repeated_text_relative_to_verified_margin_anchor'
