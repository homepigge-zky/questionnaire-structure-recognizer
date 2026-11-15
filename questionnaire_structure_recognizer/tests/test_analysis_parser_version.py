from copy import deepcopy
from docx import Document

from questionnaire_structure_recognizer.api.app import analysis_is_current
from questionnaire_structure_recognizer.engine import ENGINE_VERSION
from questionnaire_structure_recognizer.pipeline_revision import PIPELINE_REVISION
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble


def test_word_result_reuse_requires_current_native_parser(tmp_path):
    source = tmp_path / 'fictional.docx'
    doc = Document()
    row = doc.add_table(rows=1, cols=2).rows[0].cells
    row[0].text = 'Fictional site address'
    doc.save(source)
    result = validate_and_assemble(extract_document(source).model_dump(), [], {'provider': 'test'})
    result['engine_version'] = ENGINE_VERSION
    result['pipeline_revision'] = PIPELINE_REVISION
    assert analysis_is_current(result)
    old = deepcopy(result)
    old['parser_version'] = 'docx-native-obsolete-test'
    assert not analysis_is_current(old)
    assert result['questions'] == old['questions']
