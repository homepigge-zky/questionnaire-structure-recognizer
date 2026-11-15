"""Source responsibility and conditional scope survive import approval."""
import json

from docx import Document

from questionnaire_structure_recognizer.form_positions import enrich_result_form_positions
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.workflow import restore_source_reuse_constraints
from questionnaire_structure_recognizer.tests.test_import_source_context import (
    approve_history, materialized, register, store_at,
)


def fixture(tmp_path, name, *, filled):
    doc = Document()
    doc.add_paragraph('Supplier Questionnaire')
    sections = [('', 'Supplier name', 'FICTIONAL_COMPANY'),
                ('Part B (Applicable for suppliers having storage facility)', 'Supplier name', 'N/A'),
                ('Supplier Commitment', 'Name', 'FICTIONAL_SIGNATORY')]
    for section, label, value in sections:
        if section: doc.add_heading(section, 1)
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text = 'Question'; table.cell(0, 1).text = 'Answer'
        table.cell(1, 0).text = label
        table.cell(1, 1).text = value if filled else ''
    path = tmp_path / f'{name}.docx'; doc.save(path)
    result = validate_and_assemble(extract_document(path).model_dump(), [], {'provider': 'native-fixture'})
    enrich_result_form_positions(result)
    # The fixture stands for a source-verified signatory association. Its role
    # may not be lost when the source is converted into review/knowledge rows.
    signatory = next(f for f in result['questions'] if f['question_text_original'] == 'Name')
    signatory['form_definition']['requires_signatory_confirmation'] = True
    signatory['form_definition']['auto_fill_eligible'] = False
    return path, result


def test_conditional_na_and_signatory_do_not_conflict_with_company_name(tmp_path):
    store = store_at(tmp_path)
    path, result = fixture(tmp_path, 'history', filled=True)
    task = register(store, path, result, 'history')
    drafts = approve_history(store, task, result)
    assert len(drafts) == 3
    with store.connect() as db:
        signatory = db.execute("SELECT * FROM knowledge_records WHERE answer='FICTIONAL_SIGNATORY'").fetchone()
        assert signatory['review_status'] == 'approved' and not signatory['allow_auto_reuse']
        assert json.loads(signatory['source_location_json'])['reuse_restrictions'] == ['source_signatory_requires_current_confirmation']
        # Even a stale UI/client attempting to enable it cannot defeat the
        # source-side restriction in the actual matcher.
        db.execute('UPDATE knowledge_records SET allow_auto_reuse=1 WHERE id=?', (signatory['id'],)); db.commit()
    blank_path, blank = fixture(tmp_path, 'blank', filled=False)
    blank_task = register(store, blank_path, blank, 'blank')
    questions = materialized(store, blank_task, blank)
    company = next(q for q in questions if q['question_text'] == 'Supplier name' and 'Part B' not in q['section'])
    conditional = next(q for q in questions if q['question_text'] == 'Supplier name' and 'Part B' in q['section'])
    assert company['answer_text'] == 'FICTIONAL_COMPANY' and company['origin'] == 'reviewed_knowledge'
    # A historical N/A is not proof that this submission's storage condition
    # is false. This source has only the conditional title and no parent or
    # verified route. Preserve the value as a visible candidate, not an answer.
    assert conditional['answer_text'] == '' and conditional['origin'] == 'blank'
    with store.connect() as db:
        candidates = [dict(row) for row in db.execute(
            'SELECT m.status,m.reason,m.coverage_complete,k.answer FROM matches m '
            'JOIN knowledge_records k ON k.id=m.knowledge_id WHERE m.question_id=?',
            (conditional['id'],))]
        held = [row for row in candidates if row['answer'] == 'N/A']
        assert len(held) == 1 and held[0]['status'] == 'candidate'
        assert not held[0]['coverage_complete'] and '条件分部' in held[0]['reason']
        assert not any(row['status'] == 'auto_filled' for row in candidates)
    assert next(q for q in questions if q['question_text'] == 'Name')['answer_text'] == ''


def test_customer_source_cannot_become_our_automatic_answer_after_bulk_approval(tmp_path):
    store = store_at(tmp_path)
    path, result = fixture(tmp_path, 'customer', filled=True)
    company = next(f for f in result['questions'] if f['question_text_original'] == 'Supplier name')
    company['form_definition']['fill_responsibility'] = 'customer'
    task = register(store, path, result, 'history')
    approve_history(store, task, result)
    with store.connect() as db:
        company = db.execute("SELECT * FROM knowledge_records WHERE answer='FICTIONAL_COMPANY'").fetchone()
        assert company['review_status'] == 'approved' and not company['allow_auto_reuse']
        assert 'source_not_exclusively_our_responsibility' in json.loads(company['source_location_json'])['reuse_restrictions']


def test_existing_knowledge_recovers_source_constraints_without_changing_answer_or_review(tmp_path):
    store = store_at(tmp_path)
    path, result = fixture(tmp_path, 'history', filled=True)
    task = register(store, path, result, 'history')
    store.save_version(task['id'], result, 'model')
    approve_history(store, task, result)
    with store.connect() as db:
        prior = dict(db.execute("SELECT * FROM knowledge_records WHERE answer='FICTIONAL_SIGNATORY'").fetchone())
        source = json.loads(prior['source_location_json']); source.pop('reuse_restrictions')
        db.execute('UPDATE knowledge_records SET source_location_json=?,allow_auto_reuse=1 WHERE id=?', (json.dumps(source), prior['id']))
        assert restore_source_reuse_constraints(db) == 1
        row = db.execute('SELECT * FROM knowledge_records WHERE id=?', (prior['id'],)).fetchone()
        assert not row['allow_auto_reuse']
        for key in ('answer', 'review_status', 'active', 'answer_version'):
            assert row[key] == prior[key]
        assert restore_source_reuse_constraints(db) == 0


def routed_storage_fixture(tmp_path, name, *, filled):
    """Actual DOCX captions, physical marks and two explicit part directives."""
    doc = Document()
    doc.add_paragraph('To be filled by supplier')
    table = doc.add_table(rows=2, cols=3)
    for cell, value in zip(table.rows[0].cells, ['Question', 'Yes', 'No']):
        cell.text = value
    table.cell(1, 0).text = 'Does your company have a storage facility?'
    if filled:
        table.cell(1, 1).text = '☒'
    doc.add_paragraph('If Yes: Please complete Part B.')
    doc.add_paragraph('If No: Please complete Part A.')
    for section, value in [('Part A', 'FICTIONAL_OFFICE'), ('Part B', 'FICTIONAL_STORAGE')]:
        doc.add_heading(section, 1)
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text = 'Question'; table.cell(0, 1).text = 'Answer'
        table.cell(1, 0).text = 'Supplier name'
        table.cell(1, 1).text = value if filled else ''
    path = tmp_path / f'{name}.docx'
    doc.save(path)
    result = validate_and_assemble(extract_document(path).model_dump(), [], {'provider': 'native-fixture'})
    enrich_result_form_positions(result)
    return path, result


def test_source_verified_parent_selection_enables_only_its_part_in_one_match(tmp_path):
    from questionnaire_structure_recognizer.workflow import match_questionnaire, task_snapshot

    store = store_at(tmp_path)
    history_path, history = routed_storage_fixture(tmp_path, 'routed-history', filled=True)
    approve_history(store, register(store, history_path, history, 'history'), history)
    blank_path, blank = routed_storage_fixture(tmp_path, 'routed-blank', filled=False)
    questions = materialized(store, register(store, blank_path, blank, 'blank'), blank)
    parent = next(q for q in questions if q['question_text'].startswith('Does your company'))
    active = next(q for q in questions if q['section'] == 'Part B')
    inactive = next(q for q in questions if q['question_text'] == 'Supplier name' and q['id'] != active['id'])
    # A single materialize/match selects the source-verified Yes first. The
    # dependent part may then reuse its own source, never the other part's name.
    assert parent['answer_text'] == 'Yes' and parent['origin'] == 'reviewed_knowledge'
    assert active['answer_text'] == 'FICTIONAL_STORAGE' and active['origin'] == 'reviewed_knowledge'
    assert inactive['answer_text'] == ''
    with store.connect() as db:
        snapshot = task_snapshot(db, 'blank')
        current = {q['id']: q for q in snapshot['questions']}
        assert current[active['id']]['form_definition']['applicability_status'] == 'applicable'
        assert current[inactive['id']]['form_definition']['applicability_status'] == 'not_applicable'
        # Removing the parent answer is an explicit manual choice. Rematching
        # cannot restore it or use the now-unresolved subordinate answer.
        db.execute("UPDATE answers SET answer_text='',selected_options_json='[]',manual_override=1,origin='manual' WHERE question_id=?", (parent['id'],))
        match_questionnaire(db, 'blank', replace_auto=True)
        after = {q['id']: q for q in task_snapshot(db, 'blank')['questions']}
        assert after[parent['id']]['answer_text'] == '' and after[parent['id']]['manual_override']
        assert after[active['id']]['answer_text'] == ''
        assert after[active['id']]['form_definition']['applicability_status'] == 'unresolved'
        assert any(m['status'] == 'candidate' for m in after[active['id']]['matches'])


def test_native_parent_mark_is_allowed_only_in_its_verified_choice_region(tmp_path):
    from copy import deepcopy
    from questionnaire_structure_recognizer.form_part_routes import source_part_routes

    _, result = routed_storage_fixture(tmp_path, 'physical-parent', filled=True)
    routes = source_part_routes(result['source_blocks'], result['questions'])
    assert len(routes) == 2 and not any(route['unresolved'] for route in routes.values())
    parent = next(f for f in result['questions'] if f.get('options'))
    checked = next(option for option in parent['options'] if option.get('selected'))
    mark_id = checked['answer_region_refs'][0]['block_id']
    # A mark-like token has no authority unless this parent's validated option
    # refers to that physical region. Neighboring marks cannot be borrowed.
    unrelated = deepcopy(result['questions'])
    for field in unrelated:
        for option in field.get('options') or []:
            option['answer_region_refs'] = []
    assert all(route['unresolved'] for route in source_part_routes(result['source_blocks'], unrelated).values())
    # Conversely, a valid region identifier cannot turn unknown prose into a
    # choice state or conceal an intervening instruction.
    unknown = deepcopy(result['source_blocks'])
    next(block for block in unknown if block['block_id'] == mark_id)['text'] = 'Additional approval is required.'
    assert all(route['unresolved'] for route in source_part_routes(unknown, result['questions']).values())
