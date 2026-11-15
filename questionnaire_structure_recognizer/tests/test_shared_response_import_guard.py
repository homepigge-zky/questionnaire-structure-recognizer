from copy import deepcopy
import json

from fastapi.testclient import TestClient

from questionnaire_structure_recognizer.knowledge import extract_pending_entries
from questionnaire_structure_recognizer.source_pair_guards import shared_response_targets, SHARED_TARGET_RESTRICTION
from questionnaire_structure_recognizer.tests.test_source_value_consistency import source_document
from questionnaire_structure_recognizer.tests.test_import_source_context import store_at, register, materialized
from questionnaire_structure_recognizer.workflow import materialize_import, approve_import_items, restore_source_reuse_constraints


def fixture(tmp_path):
    path, result = source_document(tmp_path, [('Describe fictional cleaning', 'TEST_CLEAN'), ('Describe fictional packaging', 'TEST_PACK')])
    first, second = result['questions']
    second['form_definition']['fill_targets'] = deepcopy(first['form_definition']['fill_targets'])
    return path, result


def test_shared_answer_target_stays_visible_but_cannot_be_bulk_approved(tmp_path):
    path, result = fixture(tmp_path); before = deepcopy(result)
    items = extract_pending_entries({'id': 'shared-source', 'filename': path.name}, 1, result)
    assert len(items) == 2
    assert all(i['ambiguous'] and i['pairing_validation'] == 'needs_review' for i in items)
    assert all(SHARED_TARGET_RESTRICTION in i['source']['reuse_restrictions'] for i in items)
    assert result == before
    store = store_at(tmp_path); task = register(store, path, result, 'shared-source')
    store.save_version(task['id'], result, 'model')
    with store.connect() as db:
        batch = materialize_import(db, task, 1, result)
        ids = [r['id'] for r in db.execute('SELECT id FROM import_items WHERE batch_id=?', (batch,))]
        # A generic "I checked" or old client flag cannot undo a hard shared
        # physical source boundary; it needs an actually corrected mapping.
        db.execute("UPDATE import_items SET pairing_validation='valid',ambiguous=0 WHERE batch_id=?", (batch,))
        response = approve_import_items(db, batch, ids, 'TEST_ONLY')
        assert response['approved'] == 0 and db.execute('SELECT COUNT(*) FROM knowledge_records').fetchone()[0] == 0


def test_distinct_subslots_and_empty_group_do_not_create_shared_target_conflicts(tmp_path):
    _, result = fixture(tmp_path)
    first, second = result['questions']
    first['form_definition']['fill_targets'][0]['ref'].update(slot_id='fragment-1', start=0, end=4)
    second['form_definition']['fill_targets'][0]['ref'].update(slot_id='fragment-2', start=5, end=9)
    group = deepcopy(first); group.update(field_id='group', answer_text_original='', options=[], children=result['questions'])
    result['questions'] = [group]
    assert shared_response_targets(result) == {}


def test_blank_neighbor_cannot_share_an_existing_answer_target(tmp_path):
    _, result = fixture(tmp_path)
    result['questions'][1].update(answer_text_original='', answer_status='blank')
    assert len(shared_response_targets(result)) == 2


def test_generic_pair_confirmation_cannot_waive_shared_source_position(tmp_path):
    from questionnaire_structure_recognizer.api.app import create_app
    path, result = fixture(tmp_path)
    app = create_app(tmp_path/'data', 'key', auth_mode='key'); store = app.state.store
    task = register(store, path, result, 'shared-source')
    with store.connect() as db:
        batch = materialize_import(db, task, 1, result)
        item_id = db.execute('SELECT id FROM import_items WHERE batch_id=?', (batch,)).fetchone()[0]
    with TestClient(app) as client:
        response = client.post(f'/api/import-items/{item_id}/confirm-pairing', headers={'X-QSR-Key':'key'})
        assert response.status_code == 409


def test_old_knowledge_is_quarantined_from_matching_without_losing_human_answer(tmp_path):
    from questionnaire_structure_recognizer.api.app import create_app
    path, result = fixture(tmp_path)
    app = create_app(tmp_path/'data', 'key', auth_mode='key'); store = app.state.store
    task = register(store, path, result, 'historical-shared-source')
    store.save_version(task['id'], result, 'model')
    with TestClient(app) as client:
        response = client.post('/api/knowledge/manual', headers={'X-QSR-Key':'key'}, json={
            'canonical_question': result['questions'][0]['question_text_original'],
            'answer': 'HUMAN_REVISED_HISTORY_VALUE', 'approved': True})
        assert response.status_code == 200
    with store.connect() as db:
        old = dict(db.execute('SELECT * FROM knowledge_records').fetchone())
        source = {'task_id':task['id'], 'task_version':1, 'field_id':result['questions'][0]['field_id'],
                  'source_hash':result['source_hash'], 'reuse_restrictions':[], 'record_identity':None}
        db.execute('UPDATE knowledge_records SET source_location_json=?,allow_auto_reuse=1 WHERE id=?',
                   (json.dumps(source), old['id']))
        restore_source_reuse_constraints(db)
        updated = dict(db.execute('SELECT * FROM knowledge_records').fetchone())
        assert not updated['allow_auto_reuse']
        assert SHARED_TARGET_RESTRICTION in json.loads(updated['source_location_json'])['reuse_restrictions']
        for key in ('answer','answer_version','review_status','active'): assert updated[key] == old[key]
        # A stale client toggling only the reuse boolean is still unable to
        # bypass source constraints in the backend candidate resolver.
        db.execute('UPDATE knowledge_records SET allow_auto_reuse=1 WHERE id=?', (old['id'],)); db.commit()
    blank = deepcopy(result)
    for field in blank['questions']:
        field.update(answer_origin='none', answer_status='blank', answer_text_original='', answer_value_refs=[])
    newtask = register(store, path, blank, 'new-blank')
    assert all(q['origin'] != 'reviewed_knowledge' for q in materialized(store, newtask, blank))
