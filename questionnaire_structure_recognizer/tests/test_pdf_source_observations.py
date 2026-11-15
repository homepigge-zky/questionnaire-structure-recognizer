from copy import deepcopy

import pytest

from questionnaire_structure_recognizer.parser_core.models import make_block
from questionnaire_structure_recognizer.validation import validate_and_assemble


def drawing(order, y):
    return make_block('ocr_text', '', order, {
        'type': 'pdf', 'page': 2, 'bbox': [40, y, 440, y + 10], 'extraction': 'ocr',
    }, structure={'scan_region_kind': 'write_line', 'source_method': 'raster_geometry',
                  'blank_state': 'blank'}).model_dump()


def assemble(blocks):
    return validate_and_assemble({'document_id': 'fictional', 'source_hash': 'fictional-hash',
                                  'file_type': 'pdf', 'source_blocks': blocks}, [], None)


def test_coincident_drawings_preserve_one_region_and_all_observation_orders():
    first, second_line = drawing(6, 50), drawing(9, 75)
    same_line = {**deepcopy(first), 'order': 8}
    result = assemble([first, same_line, second_line])
    assert [b['block_id'] for b in result['source_blocks']] == [first['block_id'], second_line['block_id']]
    assert result['extraction_metadata']['coincident_source_observations'] == {first['block_id']: [6, 8]}


@pytest.mark.parametrize('change', [
    {'text': 'different evidence'},
    {'controls': [{'type': 'checkbox', 'selected': True}]},
    {'location': {'type': 'pdf', 'page': 3, 'bbox': [40, 50, 440, 60], 'extraction': 'ocr'}},
])
def test_conflicting_sources_with_the_same_id_are_never_silently_combined(change):
    first = drawing(6, 50)
    conflicting = {**deepcopy(first), 'order': 7, **change}
    with pytest.raises(ValueError, match='来源标识'):
        assemble([first, conflicting])
