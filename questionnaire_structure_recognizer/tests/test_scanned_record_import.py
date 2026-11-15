"""A physically shared record-column header is not a duplicated question."""
from copy import deepcopy

import pytest

from questionnaire_structure_recognizer.knowledge import extract_pending_entries


def fixture():
    ref = lambda bid, end=0: {'block_id': bid, 'start': 0, 'end': end}
    header = {'block_id': 'header', 'text': 'Product name', 'location': {'type': 'pdf', 'page': 1, 'bbox': [10, 10, 90, 20]},
              'structure': {'resolved_layout_role': 'record_column_header'}}
    blocks, fields = [header], []
    regions = [ref('cell-0'), ref('cell-1')]
    for i, answer in enumerate(['FICTIONAL_PRODUCT_A', 'FICTIONAL_PRODUCT_B']):
        fid = f'field-{i}'; bid = f'value-{i}'; value_ref = ref(bid, len(answer))
        blocks += [
            {'block_id': bid, 'text': answer, 'structure': {},
             'location': {'type': 'pdf', 'page': 1, 'bbox': [15, 30 + 20*i, 85, 40 + 20*i]}},
            {'block_id': f'cell-{i}', 'text': '',
             'location': {'type': 'pdf', 'page': 1, 'bbox': [10, 25 + 20*i, 90, 45 + 20*i]},
             'structure': {'scan_region_kind': 'text_box', 'source_method': 'raster_geometry',
                 'scan_association_valid': True, 'scan_field_id': fid, 'ocr_value_refs': [value_ref],
                 'record_header_ref': ref('header', len(header['text'])), 'record_column_region_refs': deepcopy(regions)}}]
        fields.append({'field_id': fid, 'question_text_original': header['text'],
            'label_refs': [ref('header', len(header['text']))], 'answer_region_refs': [deepcopy(regions[i])],
            'answer_value_refs': [value_ref], 'answer_text_original': answer, 'answer_status': 'filled',
            'answer_origin': 'none', 'pairing_validation': 'needs_review',
            'source_observation': {'origin': 'source_document', 'status': 'pending_review', 'answer_value_refs': [deepcopy(value_ref)]}})
    return {'source_blocks': blocks, 'questions': fields}


def drafts(result):
    return extract_pending_entries({'id': 'fictional-scan-record', 'filename': 'FICTIONAL.pdf'}, 1, result)


def test_shared_physical_record_header_preserves_both_observed_drafts_without_authorizing():
    result = fixture(); before = deepcopy(result)
    items = drafts(result)
    assert [i['answer'] for i in items] == ['FICTIONAL_PRODUCT_A', 'FICTIONAL_PRODUCT_B']
    assert all(i['ambiguous'] and i['pairing_validation'] == 'needs_review' for i in items)
    assert all(not i['active'] and i['review'] == 'pending' for i in items)
    assert result == before


@pytest.mark.parametrize('change', ['wrong_page', 'wrong_column', 'wrong_header', 'unlisted_region', 'wrong_owner', 'unverified', 'value_outside_region', 'different_observed_value'])
def test_shared_header_requires_exact_physical_source_relation(change):
    result = fixture(); region = result['source_blocks'][2]
    if change == 'wrong_page': region['location']['page'] = 2
    elif change == 'wrong_column': region['location']['bbox'] = [120, 25, 180, 45]
    elif change == 'wrong_header': region['structure']['record_header_ref']['end'] -= 1
    elif change == 'unlisted_region': region['structure']['record_column_region_refs'] = [region['structure']['record_column_region_refs'][1]]
    elif change == 'wrong_owner': region['structure']['scan_field_id'] = 'different-field'
    elif change == 'unverified': region['structure']['scan_association_valid'] = False
    elif change == 'value_outside_region': result['source_blocks'][1]['location']['bbox'] = [15, 50, 85, 60]
    elif change == 'different_observed_value': region['structure']['ocr_value_refs'] = result['questions'][1]['answer_value_refs']
    assert not any(d['source']['field_id'] == 'field-0' for d in drafts(result))
