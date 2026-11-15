from copy import deepcopy
import pytest
from questionnaire_structure_recognizer.api.app import analysis_is_current
from questionnaire_structure_recognizer.engine import ENGINE_VERSION
from questionnaire_structure_recognizer.pipeline_revision import PIPELINE_REVISION
from questionnaire_structure_recognizer.form_positions import PDF_OWNER_SCOPE_VERSION
from questionnaire_structure_recognizer.parser_core.pdf_parser import PARSER_VERSION
from questionnaire_structure_recognizer.parser_core.docx_parser import PARSER_VERSION as WORD_PARSER_VERSION
from questionnaire_structure_recognizer.validation.scanned_forms import VERSION as SCAN_VERSION
from questionnaire_structure_recognizer.form_positions import LOCAL_PERSONNEL_SCOPE_VERSION


@pytest.mark.parametrize('file_type',['pdf','PDF'])
def test_pdf_result_reuse_checks_local_boundaries_and_ownership_without_mutation(file_type):
    current={'engine_version':ENGINE_VERSION,'pipeline_revision':PIPELINE_REVISION,'file_type':file_type,'parser_version':PARSER_VERSION,
        'questions':[{'boundary_evidence':'scan_physical_form','boundary_version':SCAN_VERSION,
            'form_definition':{'pdf_owner_scope_version':PDF_OWNER_SCOPE_VERSION},'children':[]}]}
    assert analysis_is_current(current)
    old=deepcopy(current);old['questions'][0]['boundary_version']='old-pairing'
    before=deepcopy(old);assert not analysis_is_current(old) and old==before
    old=deepcopy(current);old['questions'][0]['form_definition']['pdf_owner_scope_version']='old-owner'
    assert not analysis_is_current(old)
    child=deepcopy(old['questions'][0]);old=deepcopy(current);old['questions'][0]['children']=[child]
    assert not analysis_is_current(old)
    old=deepcopy(current);field=old['questions'][0]
    field['label_refs']=[{'block_id':'line','start':0,'end':20}]
    field['options']=[{'refs':[{'block_id':'line','start':17,'end':20}]}]
    assert not analysis_is_current(old)
    field['label_refs'][0]['end']=16
    assert analysis_is_current(old)
    word={'engine_version':ENGINE_VERSION,'pipeline_revision':PIPELINE_REVISION,'file_type':'docx','parser_version':WORD_PARSER_VERSION,'questions':[]}
    assert analysis_is_current(word)


def test_old_word_person_context_reuses_only_after_current_source_guard():
    def cell(key,text,row):
        return {'block_id':key,'text':text,'kind':'table_cell','order':row,
                'location':{'type':'docx','part':'word/document.xml'},
                'structure':{'table_id':'t','row_start':row,'grid_col_start':1,'grid_col_end':1}}
    result={'engine_version':ENGINE_VERSION,'pipeline_revision':PIPELINE_REVISION,'file_type':'docx','parser_version':WORD_PARSER_VERSION,
            'source_blocks':[cell('heading','Primary Quality Assurance Contact',1),cell('label','Name',2)],
            'questions':[{'label_refs':[{'block_id':'label','start':0,'end':4}], 'form_definition':{}}]}
    assert not analysis_is_current(result)
    result['questions'][0]['form_definition']['local_personnel_scope_version']=LOCAL_PERSONNEL_SCOPE_VERSION
    assert analysis_is_current(result)
