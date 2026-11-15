"""Exact physical record identity does not require a name synonym dictionary."""
import json

from docx import Document

from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.semantic_fields import classify_field
from questionnaire_structure_recognizer.tests.test_import_source_context import (
    approve_history, materialized, register, store_at,
)


def make_records(tmp_path, name, filled):
    document = Document()
    document.add_heading('Production', 1)
    document.add_paragraph('To be completed by supplier')
    table = document.add_table(rows=4, cols=5)
    for cell, value in zip(table.rows[0].cells, ['', 'AREA', 'NAME', 'PHONE/FAX', 'EMAIL']): cell.text = value
    for index, area in enumerate(['Specifications', 'Manufacturing', 'Logistic'], 1):
        table.cell(index, 1).text = area
        table.cell(index, 2).text = f'FICTIONAL_PERSON_{index}' if filled else ''
        table.cell(index, 3).text = f'FICTIONAL_PHONE_{index}' if filled else ''
        table.cell(index, 4).text = f'fictional{index}@invalid.test' if filled else ''
    path = tmp_path / f'{name}.docx'; document.save(path)
    result = validate_and_assemble(extract_document(path).model_dump(), [], {'provider': 'native-fixture'})
    return path, result


def test_names_reuse_only_the_same_independently_identified_record(tmp_path):
    store = store_at(tmp_path)
    history_path, history = make_records(tmp_path, 'history', True)
    blank_path, blank = make_records(tmp_path, 'blank', False)
    old_task = register(store, history_path, history, 'history')
    new_task = register(store, blank_path, blank, 'blank')
    approve_history(store, old_task, history)
    questions = materialized(store, new_task, blank)
    names = [q for q in questions if q['question_text'] == 'NAME']
    assert len(names) == 3
    for question in names:
        definition = json.loads(question['form_definition_json'])
        index = ['Specifications', 'Manufacturing', 'Logistic'].index(definition['semantic_context']) + 1
        assert question['answer_text'] == f'FICTIONAL_PERSON_{index}'
    # Losing a historical record identity cannot be repaired by the next row
    # or another person's matching generic column name.
    with store.connect() as db:
        db.execute("UPDATE knowledge_records SET active=0 WHERE answer='FICTIONAL_PERSON_2'")
        from questionnaire_structure_recognizer.workflow import match_questionnaire, task_snapshot
        match_questionnaire(db, new_task['id'], replace_auto=True)
        after = task_snapshot(db, new_task['id'])
    names = [q for q in after['questions'] if q['question_text'] == 'NAME']
    assert [q['answer_text'] for q in names] == ['FICTIONAL_PERSON_1', '', 'FICTIONAL_PERSON_3']


def test_explicit_production_address_is_distinct_from_registered_and_unspecified():
    for label in ['生产地址', '生产基地地址', '公司生产区英文地址', 'Manufacturing address', 'Production site address']:
        annotation = classify_field(label)
        assert annotation['canonical_field_key'] == 'address.full'
        assert annotation['entity_type'] == 'site'
        assert annotation['qualifiers']['address_use'] == 'manufacturing_site'
    assert classify_field('注册地址')['qualifiers']['address_use'] == 'registered'
    assert classify_field('Address')['qualifiers']['address_use'] == 'unspecified'
