from copy import deepcopy
from questionnaire_structure_recognizer.pipeline_revision import fingerprint, PIPELINE_REVISION


def test_native_conditional_scope_changes_invalidate_complete_results(tmp_path):
    module=tmp_path/'local_condition_scopes.py'
    module.write_text('VERSION = 1',encoding='utf8')
    before=fingerprint(tmp_path)
    module.write_text('VERSION = 2',encoding='utf8')
    assert fingerprint(tmp_path)!=before


def test_matrix_review_geometry_changes_invalidate_complete_results(tmp_path):
    module=tmp_path/'source_matrix_choices.py'
    module.write_text('VERSION = 1',encoding='utf8')
    before=fingerprint(tmp_path)
    module.write_text('VERSION = 2',encoding='utf8')
    assert fingerprint(tmp_path)!=before
from questionnaire_structure_recognizer.api.app import analysis_is_current
from questionnaire_structure_recognizer.engine import ENGINE_VERSION
from questionnaire_structure_recognizer.parser_core.docx_parser import PARSER_VERSION


def test_complete_result_without_current_postprocessing_is_not_reused():
    result = {'engine_version': ENGINE_VERSION, 'parser_version': PARSER_VERSION,
              'file_type': 'DOCX', 'questions': []}
    before = deepcopy(result)
    assert not analysis_is_current(result) and result == before
    result['pipeline_revision'] = PIPELINE_REVISION
    assert analysis_is_current(result)
    result['pipeline_revision'] = 'previous-validation-rules'
    assert not analysis_is_current(result)


def test_validator_changes_invalidate_results_without_hashing_runtime_data(tmp_path):
    (tmp_path / 'validation').mkdir()
    source = tmp_path / 'validation/pairs.py'
    source.write_text('PAIR_VERSION = 1')
    original = fingerprint(tmp_path)
    (tmp_path / 'runtime-data.json').write_text('a different task')
    assert fingerprint(tmp_path) == original
    source.write_text('PAIR_VERSION = 2')
    assert fingerprint(tmp_path) != original
