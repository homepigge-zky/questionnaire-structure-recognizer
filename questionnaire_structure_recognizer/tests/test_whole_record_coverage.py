from copy import deepcopy
import json
import pytest
from questionnaire_structure_recognizer.workflow import whole_record_coverage_reason


def record():
    requirement={'requirement_text':'Contact Person(s) Name / Contact No. / Fax No. / Email address\nFor technical matter',
                 'object_context':'Manufacturing site',
                 'label_refs':[{'block_id':'FICTIONAL-shared-label','start':0,'end':12}],
                 'context_refs':[{'block_id':'FICTIONAL-column','start':0,'end':8}]}
    return {'whole_record_requirement':requirement},{'original_question':requirement['requirement_text'],
        'canonical_question':requirement['requirement_text'],'source_location_json':json.dumps({'whole_record_requirement':deepcopy(requirement)})}


def test_complete_source_requirement_and_same_object_can_pass_coverage_gate():
    definition,entry=record()
    assert whole_record_coverage_reason(definition,entry)==''
    assert whole_record_coverage_reason({},entry)==''


@pytest.mark.parametrize('change',['short_name','short_phone','other_object','other_purpose','no_source_requirement','invalid_metadata'])
def test_partial_or_different_record_never_becomes_complete_via_a_canonical_key_or_alias(change):
    definition,entry=record();source=json.loads(entry['source_location_json'])
    if change=='short_name':entry['original_question']='Name'
    elif change=='short_phone':entry['original_question']='Contact No.'
    elif change=='other_object':source['whole_record_requirement']['object_context']='Head office'
    elif change=='other_purpose':source['whole_record_requirement']['requirement_text']=entry['original_question'].replace('technical','commercial')
    elif change=='no_source_requirement':source={}
    else:source=None
    # These matching hints do not substitute for complete preserved source.
    entry['canonical_question']=definition['whole_record_requirement']['requirement_text']
    entry['confirmed_alias']=True;entry['canonical_field_key']='person.name'
    entry['source_location_json']=json.dumps(source)
    assert whole_record_coverage_reason(definition,entry)
