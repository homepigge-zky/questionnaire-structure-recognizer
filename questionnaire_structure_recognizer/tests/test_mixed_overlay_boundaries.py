from copy import deepcopy

from questionnaire_structure_recognizer.validation.field_boundaries import ref
from questionnaire_structure_recognizer.validation.form_completion import new_field
from questionnaire_structure_recognizer.validation.mixed_overlay_boundaries import restore_mixed_overlay_boundaries


def block(bid, text, bbox, extraction='ocr_region', **structure):
    return {'block_id': bid, 'kind': 'paragraph' if extraction == 'native' else 'ocr_text',
            'text': text, 'order': 0, 'location': {'type': 'pdf', 'page': 1, 'bbox': bbox, 'extraction': extraction},
            'structure': structure}


def lane(bid, value, x, y, width):
    region = block(bid, '', [x, y+2, x+width, y+12], 'ocr', scan_region_kind='write_line',
                   blank_state='filled', overlay_value_block_ids=[bid+'-value'])
    val = block(bid+'-value', value, [x+2, y, x+min(width-2, 100), y+12], 'native', native_overlay_answer_region=bid)
    return region, val


def physical(label, region, value):
    field = new_field(label, ref(label), [ref(region)])
    field.update(boundary_evidence='scan_physical_form', answer_value_refs=[ref(value)], answer_refs=[ref(value)],
                 answer_text_original=value['text'], answer_status='filled')
    region['structure'].update(scan_association_valid=True, scan_field_id=field['field_id'])
    return field


def multiline():
    label = block('label', 'Describe the fictional ownership:', [40, 60, 240, 74])
    context = block('context', '(Fictional name and relationship)', [40, 78, 250, 90])
    r1, v1 = lane('r1', 'FIRST_TEST_VALUE', 40, 100, 500)
    r2, v2 = lane('r2', 'SECOND_TEST_VALUE', 40, 118, 500)
    r3, v3 = lane('r3', 'THIRD_TEST_VALUE', 40, 136, 500)
    field = physical(label, r1, v1); field['context_refs'] = [ref(context)]
    return [label, context, r1, v1, r2, v2, r3, v3], field


def test_native_filled_continuation_lines_and_context_remnant_are_reconciled():
    blocks, field = multiline(); lookup = {b['block_id']: b for b in blocks}
    old = new_field(lookup['context'], ref(lookup['context']))
    old['answer_value_refs'] = [ref(lookup['r1-value'])]
    fields = [field, old]
    audit = restore_mixed_overlay_boundaries(fields, blocks, lookup)
    assert fields == [field]
    assert field['answer_text_original'] == 'FIRST_TEST_VALUE\nSECOND_TEST_VALUE\nTHIRD_TEST_VALUE'
    assert [r['block_id'] for r in field['answer_region_refs']] == ['r1', 'r2', 'r3']
    assert audit['retired_model_groups'][0]['field_id'] == old['field_id']
    assert lookup['context']['text'] == '(Fictional name and relationship)'


def test_continuation_stops_at_new_printed_field_and_preserves_manual_values():
    blocks, field = multiline(); lookup = {b['block_id']: b for b in blocks}
    new_label = block('other-question', 'Different fictional field:', [40, 120, 240, 134])
    blocks.append(new_label); lookup[new_label['block_id']] = new_label
    restore_mixed_overlay_boundaries([field], blocks, lookup)
    assert field['answer_text_original'] == 'FIRST_TEST_VALUE'
    original = deepcopy(field); field['manual_override'] = True; original['manual_override'] = True
    restore_mixed_overlay_boundaries([field], blocks, lookup)
    assert field == original
    blocks, child = multiline(); lookup = {b['block_id']: b for b in blocks}
    parent = new_field(lookup['label'], ref(lookup['label'])); parent['field_id'] = 'manual-parent'
    parent.update(manual_override=True, children=[child]); before = deepcopy(parent)
    restore_mixed_overlay_boundaries([parent], blocks, lookup)
    assert parent == before


def test_repeated_right_column_retains_each_row_identity_and_values_that_look_like_labels():
    header = block('header', 'Fictional assessment', [430, 75, 530, 87]); blocks = [header]; fields = []
    for n, y in enumerate([100, 120]):
        label = block(f'label-{n}', f'Distinct role {n}', [40, y, 190, y+12])
        region, value = lane(f'left-{n}', f'TEST_PERSON_{n}', 200, y, 210)
        right, right_value = lane(f'right-{n}', 'Fictional assessment', 430, y, 110)
        blocks += [label, region, value, right, right_value]
        fields.append(physical(label, region, value))
    lookup = {b['block_id']: b for b in blocks}
    wrong = new_field(lookup['right-1-value'], ref(lookup['right-1-value']))
    fields.append(wrong)
    audit = restore_mixed_overlay_boundaries(fields, blocks, lookup)
    assert len(fields) == 2 and len(audit['record_children']) == 2
    for n, field in enumerate(fields):
        child, = field['children']
        assert child['boundary_evidence'] == 'mixed_overlay_record_lane'
        assert child['boundary_version'] == audit['version']
        assert child['answer_text_original'] == 'Fictional assessment'
        assert child['context_refs'] == field['label_refs']
        assert child['answer_region_refs'][0]['block_id'] == f'right-{n}'
    assert len({f['children'][0]['field_id'] for f in fields}) == 2
    before = deepcopy(fields)
    restore_mixed_overlay_boundaries(fields, blocks, lookup)
    assert fields == before


def test_unverified_overlay_and_unaccounted_model_text_are_never_silently_dropped():
    blocks, field = multiline(); lookup = {b['block_id']: b for b in blocks}
    lookup['r2-value']['structure']['native_overlay_answer_region'] = 'wrong-region'
    unknown = block('unknown', 'Independent question with no established target?', [40, 200, 400, 214])
    blocks.append(unknown); lookup['unknown'] = unknown
    mixed = new_field(unknown, ref(unknown)); mixed['label_refs'].append(ref(lookup['r1-value']))
    manual = new_field(lookup['r1-value'], ref(lookup['r1-value'])); manual['manual_override'] = True
    unknown_answer = new_field(lookup['r1-value'], ref(lookup['r1-value']))
    unknown_answer['answer_value_refs'] = [ref(unknown)]
    fields = [field, mixed, manual, unknown_answer]
    restore_mixed_overlay_boundaries(fields, blocks, lookup)
    assert field['answer_text_original'] == 'FIRST_TEST_VALUE'
    assert mixed in fields and manual in fields and unknown_answer in fields
