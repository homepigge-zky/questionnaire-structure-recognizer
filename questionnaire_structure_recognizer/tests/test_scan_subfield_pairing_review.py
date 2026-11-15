"""Physical subfield pairing and OCR approval are independent decisions."""
from copy import deepcopy
import pytest

from questionnaire_structure_recognizer.validation.scanned_subfields import restore_scanned_subfields
from questionnaire_structure_recognizer.validation.validator import _apply_pairing_validation


def source(identifier, text, box, kind=None):
    result = {'block_id': identifier, 'text': text, 'order': box[1], 'kind': 'ocr_text',
            'location': {'type': 'pdf', 'extraction': 'ocr', 'page': 1, 'bbox': box},
            'style': {'ocr_confidence': .99}, 'controls': [],
            'structure': {'scan_region_kind': kind, 'source_method': 'raster_geometry'} if kind else {}}
    if kind == 'text_box':
        x0, y0, x1, y1 = box
        result['structure']['closed_region_evidence'] = {'method': 'observed_four_sided_polygon_v1',
            'polygon': [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], 'edge_ink_coverage': [1, 1, 1, 1]}
    return result


def bounded_answer():
    blocks = [source('cell', '', [0, 0, 500, 100], 'text_box'),
              source('prompt', 'Please provide the fictional process description.', [5, 10, 490, 25]),
              source('value', 'Ans: FIRST_TEST_LINE', [5, 30, 490, 45]),
              source('tail', 'SECOND_TEST_LINE', [5, 47, 490, 60])]
    lookup = {b['block_id']: b for b in blocks}
    fields = []
    restore_scanned_subfields(fields, blocks, lookup)
    assert len(fields) == 1
    return fields[0], lookup


def test_exact_subfield_location_does_not_approve_ocr_transcription():
    field, lookup = bounded_answer()
    _apply_pairing_validation([field], lookup)
    assert field['pairing_validation'] == 'valid'
    assert field['answer_presence'] == 'present'
    assert field['answer_text_original'] == 'FIRST_TEST_LINE\nSECOND_TEST_LINE'
    assert field['review_status'] == 'needs_review'
    assert field['source_observation']['status'] == 'pending_review'
    assert field['source_review_reasons']
    assert not field.get('allow_auto_reuse')


@pytest.mark.parametrize('origin', ['none', 'source_document'])
def test_verified_location_is_still_an_ambiguous_pending_import(origin):
    from questionnaire_structure_recognizer.knowledge import extract_pending_entries
    field, lookup = bounded_answer()
    _apply_pairing_validation([field], lookup)
    field['answer_origin'] = origin
    drafts = extract_pending_entries({'id': 'fictional-subfield', 'filename': 'FICTIONAL.pdf'}, 1,
                                     {'questions': [field], 'source_blocks': list(lookup.values())})
    draft, = drafts
    assert draft['ambiguous'] and not draft['active'] and draft['review'] == 'pending'
    assert draft['review_reasons']
    assert draft['answer'] == 'FIRST_TEST_LINE\nSECOND_TEST_LINE'


@pytest.mark.parametrize('damage', [
    'wrong_label', 'cross_page', 'missing_container', 'empty_range',
    'moved_rule_region', 'forged_slot_text', 'duplicate_slot', 'wrong_value',
    'extra_region', 'forged_answer_text', 'extra_structural_reason',
    'missing_polygon', 'degenerate_polygon', 'expanded_box', 'missing_edge', 'wrong_polygon_method',
])
def test_location_proof_is_rechecked_not_trusted_as_a_flag(damage):
    field, lookup = bounded_answer()
    first = field['answer_region_refs'][0]
    slot = lookup['value']['structure']['logical_slots'][0]
    if damage == 'wrong_label':
        other = source('other', 'Another field?', [5, 65, 200, 80])
        lookup['other'] = other
        field['label_refs'] = [{'block_id': 'other', 'start': 0, 'end': len(other['text'])}]
    elif damage == 'cross_page':
        lookup['tail']['location']['page'] = 2
    elif damage == 'missing_container':
        slot['container_ref']['block_id'] = 'absent'
    elif damage == 'empty_range':
        first['end'] = first['start']
    elif damage == 'moved_rule_region':
        lookup['cell']['location']['bbox'] = [0, 150, 500, 200]
    elif damage == 'forged_slot_text':
        slot['expected_source_text'] = 'CHANGED_SOURCE'
    elif damage == 'duplicate_slot':
        lookup['value']['structure']['logical_slots'].append(deepcopy(slot))
    elif damage == 'wrong_value':
        field['answer_value_refs'][0]['start'] = 0  # Includes the Ans marker.
    elif damage == 'extra_region':
        field['answer_region_refs'].append({'block_id': 'cell', 'start': 0, 'end': 0})
    elif damage == 'forged_answer_text':
        field['answer_text_original'] = 'NOT_IN_SOURCE'
    elif damage == 'extra_structural_reason':
        field['review_reasons'].append('A second unrelated label requires review.')
    elif damage == 'missing_polygon':
        lookup['cell']['structure'].pop('closed_region_evidence')
    elif damage == 'degenerate_polygon':
        lookup['cell']['structure']['closed_region_evidence']['polygon'] = [[0, 0]] * 4
    elif damage == 'expanded_box':
        lookup['cell']['location']['bbox'][3] += 30
    elif damage == 'missing_edge':
        lookup['cell']['structure']['closed_region_evidence']['edge_ink_coverage'][2] = .1
    elif damage == 'wrong_polygon_method':
        lookup['cell']['structure']['closed_region_evidence']['method'] = 'model_guessed_rectangle'
    _apply_pairing_validation([field], lookup)
    assert field['pairing_validation'] != 'valid'
    assert field['review_status'] == 'needs_review'
    assert field['source_observation']['status'] == 'pending_review'


def test_rechecking_unchanged_source_does_not_duplicate_logical_slots():
    field, lookup = bounded_answer()
    fields = [field]
    before = {k: deepcopy(b['structure'].get('logical_slots', [])) for k, b in lookup.items()}
    restore_scanned_subfields(fields, list(lookup.values()), lookup)
    _apply_pairing_validation(fields, lookup)
    assert len(fields) == 1 and fields[0]['pairing_validation'] == 'valid'
    assert {k: b['structure'].get('logical_slots', []) for k, b in lookup.items()} == before


def test_later_slot_conflict_does_not_leave_partial_source_mutations():
    from questionnaire_structure_recognizer.validation.scanned_subfields import _emit
    field, lookup = bounded_answer()
    lookup['value']['structure']['logical_slots'] = []
    lookup['tail']['structure']['logical_slots'][0]['expected_source_text'] = 'STALE_VERSION'
    before = deepcopy(lookup)
    fields = []
    _emit(fields, list(lookup.values()), lookup, lookup['cell'],
          field['label_refs'], field['answer_value_refs'])
    assert fields == [] and lookup == before


def test_same_line_label_and_value_use_disjoint_original_character_ranges():
    blocks = [source('cell', '', [300, 0, 500, 60], 'text_box'),
              source('question', 'Was a fictional inspection performed?', [0, 5, 280, 20]),
              source('yes', 'Yes, Date: 06/2024', [310, 5, 480, 20]),
              source('no', 'No', [310, 35, 340, 50])]
    lookup = {b['block_id']: b for b in blocks}
    fields = [{'field_id': 'parent', 'question_id': 'parent',
               'label_refs': [{'block_id': 'question', 'start': 0, 'end': len(blocks[1]['text'])}],
               'options': [{'refs': [{'block_id': 'yes', 'start': 0, 'end': 3}]}],
               'physical_response_region_ref': {'block_id': 'cell', 'start': 0, 'end': 0}}]
    restore_scanned_subfields(fields, blocks, lookup)
    child = next(f for f in fields if f.get('boundary_evidence') == 'closed_scan_source_subfield')
    _apply_pairing_validation([child], lookup)
    assert child['pairing_validation'] == 'valid'
    assert child['answer_text_original'] == '06/2024'
    assert child['source_observation']['status'] == 'pending_review'


def test_unreadable_inline_value_stays_unresolved_and_empty():
    blocks = [source('cell', '', [30, 100, 450, 150], 'text_box'),
              source('prompt', 'Which specification?', [35, 110, 190, 127]),
              source('value', 'UNCERTAIN_SCAN', [202, 106, 320, 123]),
              source('rule', '', [193, 114, 410, 128], 'write_line')]
    blocks[2]['style']['ocr_confidence'] = .7
    fields = []
    lookup = {b['block_id']: b for b in blocks}
    restore_scanned_subfields(fields, blocks, lookup)
    _apply_pairing_validation(fields, lookup)
    field, = fields
    assert field['answer_status'] == 'unresolved'
    assert field['answer_text_original'] == '' and field['answer_value_refs'] == []
    assert field['pairing_validation'] != 'valid'
    assert field['observed_answer_text'] == 'UNCERTAIN_SCAN'
