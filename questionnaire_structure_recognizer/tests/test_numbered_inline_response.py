"""Numbering must not consume a separately located inline response field."""
import pytest

from questionnaire_structure_recognizer.validation.scanned_forms import restore_scanned_fields


def block(bid, text, box, **structure):
    return {'block_id': bid, 'kind': 'ocr_text', 'text': text, 'order': 0,
            'location': {'type': 'pdf', 'page': 1, 'bbox': box, 'extraction': 'ocr'},
            'structure': structure}


@pytest.mark.parametrize('value', ['', 'TEST_DATE'])
def test_numbered_prompt_preserves_separate_right_hand_label_and_response(value):
    blocks = [block('number-1', '1.1', [35, 100, 55, 112]),
              block('prompt', 'Has the fictional facility been inspected?', [75, 100, 300, 112]),
              block('date-label', 'Inspection date:', [335, 100, 415, 112]),
              block('date-region', '', [425, 102, 550, 112], scan_region_kind='write_line',
                    blank_state='filled' if value else 'blank',
                    overlay_value_block_ids=['value'] if value else []),
              block('number-2', '1.2', [35, 160, 55, 172]),
              block('prompt-2', 'Describe the fictional process?', [75, 160, 300, 172]),
              block('number-3', '1.3', [35, 220, 55, 232]),
              block('prompt-3', 'Is the fictional process documented?', [75, 220, 300, 232])]
    if value:
        blocks.append(block('value', value, [427, 100, 500, 112], native_overlay_answer_region='date-region'))
    lookup = {b['block_id']: b for b in blocks}
    fields = restore_scanned_fields([], blocks, lookup)
    date = [f for f in fields if any(r['block_id'] == 'date-label' for r in f['label_refs'])]
    assert len(date) == 1
    assert date[0]['question_text_original'] == 'Inspection date:'
    assert [r['block_id'] for r in date[0]['answer_region_refs']] == ['date-region']
    assert date[0]['answer_text_original'] == value
    assert date[0]['answer_status'] == ('filled' if value else 'blank')
    prompt, = [f for f in fields if any(r['block_id'] == 'prompt' for r in f['label_refs'])]
    assert [(r['block_id'], r['start'], r['end']) for r in prompt['label_refs']] == [('prompt', 0, len(blocks[1]['text']))]
    assert all(r['block_id'] != 'date-region' for r in prompt['answer_region_refs'])
