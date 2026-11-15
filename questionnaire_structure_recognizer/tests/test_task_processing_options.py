"""Per-document OCR consent must survive restart without leaking to other jobs."""
import io
import json

from docx import Document
from fastapi.testclient import TestClient

from questionnaire_structure_recognizer.api.app import Store, create_app


def source_bytes():
    doc = Document()
    doc.add_paragraph('FICTIONAL TEST Company name: _____')
    stream = io.BytesIO()
    doc.save(stream)
    return stream.getvalue()


def test_upload_options_are_persisted_and_never_change_process_default(tmp_path, monkeypatch):
    monkeypatch.setenv('QSR_OCR_ENABLED', 'false')
    submitted = []
    monkeypatch.setattr(Store, 'submit', lambda self, identifier, *a, **kw: submitted.append(identifier))
    app = create_app(tmp_path, 'test-key', auth_mode='key')
    with TestClient(app) as client:
        headers = {'X-QSR-Key': 'test-key'}
        for enabled in ('true', 'false'):
            r = client.post('/api/questionnaires', headers=headers,
                files={'file': ('FICTIONAL.docx', source_bytes())}, data={'ocr_enabled': enabled})
            assert r.status_code == 202
            task = app.state.store.task(r.json()['id'])
            assert json.loads(task['metadata_json'])['ocr_enabled'] is (enabled == 'true')
        assert len(set(submitted)) == 2
        assert client.get('/api/capabilities', headers=headers).json()['ocr']['enabled'] is False
        r = client.post('/api/tasks/' + submitted[0] + '/reanalyze', headers=headers, json={'ocr_enabled': False})
        assert r.status_code == 202
        assert json.loads(app.state.store.task(submitted[0])['metadata_json'])['ocr_enabled'] is False
        r = client.post('/api/tasks/' + submitted[0] + '/reanalyze', headers=headers, json={'ocr_enabled': 'yes'})
        assert r.status_code == 422


def test_empty_or_different_ocr_analysis_is_not_reused(tmp_path):
    from questionnaire_structure_recognizer.engine import ENGINE_VERSION
    store = Store(tmp_path, 'test-key')
    with store.connect() as db:
        for identifier, consent in [('source', False), ('target', True)]:
            db.execute('INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,metadata_json) VALUES(?,?,?,?,?,?,?,?)',
                (identifier, 'FICTIONAL.pdf', identifier+'.pdf', 'same-source', 1, 'complete', 'done', json.dumps({'ocr_enabled': consent})))
    result = {'engine_version': ENGINE_VERSION, 'processing_options': {'ocr_enabled': False},
        'extraction_status': 'failed', 'structure_status': 'original_only', 'source_blocks': [], 'questions': []}
    store.save_version('source', result, 'model')
    assert not store.reuse_analysis('target', 'source')
    store.update('target', metadata_json=json.dumps({'ocr_enabled': False}))
    assert not store.reuse_analysis('target', 'source')
    store.pool.shutdown()
