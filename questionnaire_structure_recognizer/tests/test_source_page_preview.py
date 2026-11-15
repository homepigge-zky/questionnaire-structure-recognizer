"""Source images and block positions remain local and permission checked."""
import hashlib

import fitz
from fastapi.testclient import TestClient

from questionnaire_structure_recognizer.api.app import create_app
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.workflow import materialize_questionnaire, save_snapshot


def test_native_pdf_preview_is_original_and_authenticated(tmp_path):
    source = tmp_path / 'FICTIONAL.pdf'
    with fitz.open() as pdf:
        page = pdf.new_page(width=400, height=500)
        page.insert_text((30, 40), 'FICTIONAL ONLY - Company name:')
        page.draw_rect(fitz.Rect(30, 60, 350, 100))
        pdf.save(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    extracted = extract_document(source, ocr_enabled=False).model_dump()
    result = {**extracted, 'questions': [], 'structure_status': 'original_only'}
    app = create_app(tmp_path / 'isolated', 'preview-test', auth_mode='key')
    store = app.state.store
    (store.files / source.name).write_bytes(source.read_bytes())
    with store.connect() as db:
        db.execute('INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,purpose,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)',
            ('preview', source.name, source.name, digest, source.stat().st_size, 'needs_review', 'test', 'questionnaire', '{}'))
    store.save_version('preview', result, 'model')
    with store.connect() as db:
        materialize_questionnaire(db, store.task('preview'), result, {'questionnaire_name': 'FICTIONAL'})
        save_snapshot(db, store.data, 'preview', 'test')
    headers = {'X-QSR-Key': 'preview-test'}
    with TestClient(app) as client:
        assert client.get('/api/questionnaires/preview/source-pages/1.png').status_code in {401, 403}
        detail = client.get('/api/questionnaires/preview/fill-regions', headers=headers).json()
        page = detail['pages'][0]
        assert (page['width'], page['height']) == (400, 500)
        assert page['regions'][0]['location']['bbox'] == extracted['source_blocks'][0]['location']['bbox']
        image = client.get(page['image_url'], headers=headers)
        assert image.status_code == 200 and image.content.startswith(b'\x89PNG\r\n\x1a\n')
        assert client.get('/api/questionnaires/preview/source-pages/2.png', headers=headers).status_code == 404
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
