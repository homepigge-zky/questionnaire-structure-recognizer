"""A verified physical pair must not advertise a contradictory value as safe."""
from copy import deepcopy
from questionnaire_structure_recognizer.tests.test_source_value_consistency import source_document
from questionnaire_structure_recognizer.form_positions import enrich_result_form_positions, refresh_result_responsibility


def test_source_conflict_is_separate_from_location_and_retains_original(tmp_path):
    rows = [('Measured range of CFU/g', '<25EU/mg'),
            ('Company / Site Registration # (if any)', '18 Fictional Avenue, Test District, Sample City'),
            ('Registered address', '18 Fictional Avenue, Test District, Sample City')]
    _, result = source_document(tmp_path, rows)
    fields = {f['question_text_original']: f for f in result['questions']}
    for label, answer in rows[:2]:
        field = fields[label]
        assert field['answer_text_original'] == answer
        assert field['source_value_validation'] == 'conflict'
        assert field['source_value_reasons']
        assert field['form_definition']['source_value_validation'] == 'conflict'
        assert not field['form_definition']['auto_fill_eligible']
        assert not field['form_definition']['auto_fill_eligible_if_condition_met']
        assert field['form_definition']['fill_targets']
    valid = fields[rows[2][0]]
    assert valid['source_value_validation'] == 'no_detected_conflict'
    before = deepcopy(result['source_blocks'])
    refresh = refresh_result_responsibility(result)
    assert refresh['source_blocks'] == before
    assert all(not f['form_definition']['auto_fill_eligible'] for f in refresh['questions'][:2])


def test_recheck_does_not_keep_stale_conflict_after_explicit_value_correction(tmp_path):
    _, result = source_document(tmp_path, [('Measured range of CFU/g', '<25EU/mg')])
    field = result['questions'][0]
    assert field['source_value_validation'] == 'conflict'
    field['answer_text_original'] = '<25CFU/g'
    enrich_result_form_positions(result)
    assert field['source_value_validation'] == 'no_detected_conflict'
    assert field['source_value_reasons'] == []
