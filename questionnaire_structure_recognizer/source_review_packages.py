"""Explicitly registered original-file transcription reviews, never knowledge.

An operator can approve a source-image review chain for the exact source bytes.
New, OCR-authorized tasks may bind it once. Existing tasks, manual reviews and
other versions of a template remain untouched. The recognizer replays every
text/geometry/control check. Only the exact already-validated result approved
by the package may be reused, under the current pipeline and OCR authorization.
"""
from copy import deepcopy
import json
from pathlib import Path
import uuid

from .source_text_review import VERSION as TEXT_VERSION, _digest, _sha
from .source_choice_review import VERSION as CHOICE_VERSION

VERSION = 'exact-source-review-package-v1'
SCOPE = 'identical_original_transcription_only'
TABLES = {'reviewed_ocr_transcription_only': 'source_text_reviews',
          'reviewed_source_selection_only': 'source_choice_reviews'}


def validated_analysis_package(db, source_hash, recognition_sha256, result):
    """Return the latest exact authorization, without binding or mutating data.

    Matching a chain alone is insufficient: the operator approved an immutable
    fully assembled result, including its source relations. Any changed bytes
    require actual pipeline replay. This never grants knowledge authorization.
    """
    from .source_text_review import verified_reviewed_text
    from .source_choice_review import verified_reviewed_choice
    package = db.execute('SELECT id,package_digest,payload_json FROM source_review_packages WHERE source_hash=? ORDER BY rowid DESC LIMIT 1',
                         (source_hash,)).fetchone()
    if not package:return None
    try:
        payload=json.loads(package[2])
        if (payload.get('schema_version')!=VERSION or payload.get('scope')!=SCOPE
                or payload.get('source_hash')!=source_hash or result.get('source_hash')!=source_hash
                or payload.get('knowledge_approval') is not False or payload.get('enable_ocr') is not False
                or _digest(payload)!=package[1] or payload.get('recognition_sha256')!=recognition_sha256):return None
        records=_records(source_hash,payload.get('records'))
        lookup={b['block_id']:b for b in result.get('source_blocks',[])}
        if len(lookup)!=len(result.get('source_blocks',[])):return None
        for kind,scope,version in [('text','reviewed_ocr_transcription_only',TEXT_VERSION),
                                   ('choice','reviewed_source_selection_only',CHOICE_VERSION)]:
            expected=[r for r in records if r['scope']==scope]
            if result.get(f'source_{kind}_review_records',[])!=expected:return None
            audit=result.get(f'source_{kind}_review') or {}
            digests=[r['review_digest'] for r in expected]
            ids=sorted({e['block_id'] for r in expected for e in r['edits']})
            if not expected:
                if audit.get('review_digests') or audit.get('reviewed_blocks'):return None
                continue
            if (audit.get('version')!=version or audit.get('source_hash')!=source_hash
                    or audit.get('review_digests')!=digests or audit.get('reviewed_block_ids')!=ids
                    or audit.get('reviewed_blocks')!=len(ids) or audit.get('knowledge_approval') is not False
                    or audit.get('original_extraction_unchanged') is not True
                    or audit.get('requires_structure_reanalysis') is not True
                    or audit.get('review_digest')!=_digest({'version':version,'source_hash':source_hash,'reviews':digests})):return None
            final={((e['block_id'],e['control_index']) if kind=='choice' else e['block_id']):e
                   for r in expected for e in r['edits']}
            for key,edit in final.items():
                block=lookup.get(edit['block_id'],{})
                if kind=='text':
                    if (block.get('text')!=edit['after'] or block.get('location')!=edit['location']
                            or (edit['after'] and not verified_reviewed_text(block))):return None
                elif (not verified_reviewed_choice(block,key[1])
                        or block['controls'][key[1]]['selected'] is not edit['after']):return None
        return {'package_id':package[0],'package_digest':package[1],
                'recognition_sha256':recognition_sha256,'source_hash':source_hash}
    except (ValueError,KeyError,TypeError,IndexError):return None


def initialize_package_schema(db):
    db.executescript((Path(__file__).parent/'migrations/007_source_review_packages.sql').read_text('utf8'))


def _records(source_hash, records):
    _sha(source_hash, 'Original source')
    if not isinstance(records, list) or not 1 <= len(records) <= 100:
        raise ValueError('A package requires between 1 and 100 explicit source reviews')
    seen = set()
    for review in records:
        if not isinstance(review, dict):
            raise ValueError('Invalid source review')
        scope = review.get('scope')
        expected = TEXT_VERSION if scope == 'reviewed_ocr_transcription_only' else CHOICE_VERSION
        if (scope not in TABLES or review.get('source_hash') != source_hash
                or review.get('schema_version') != expected
                or review.get('knowledge_approval') is not False
                or review.get('requires_structure_reanalysis') is not True
                or not str(review.get('reviewer', '')).strip()
                or not isinstance(review.get('edits'), list) or not 1 <= len(review['edits']) <= 1000
                or not (review.get('evidence') or {}).get('pages')):
            raise ValueError('Package must contain only explicit reviews of the same original; no knowledge approval')
        digest = _sha(review.get('review_digest'), 'Source review')
        if digest in seen or digest != _digest({k:v for k,v in review.items() if k != 'review_digest'}):
            raise ValueError('Changed or duplicate source review')
        seen.add(digest)
    return deepcopy(records)


def register_package(db, source_hash, records, reviewer, recognition_sha256):
    """Called only by the explicit local installer, not by documents or models."""
    if not isinstance(reviewer,str) or not reviewer.strip():
        raise ValueError('Explicit local reviewer required')
    _sha(recognition_sha256, 'Verified recognition result')
    payload = {'schema_version': VERSION, 'scope': SCOPE, 'source_hash': source_hash,
               'records': _records(source_hash,records), 'approved_by': reviewer.strip(),
               'recognition_sha256': recognition_sha256, 'knowledge_approval': False,
               'enable_ocr': False}
    digest = _digest(payload)
    existing = db.execute('SELECT id FROM source_review_packages WHERE package_digest=?',(digest,)).fetchone()
    if existing:
        return {'status':'already_registered','id':existing[0], 'review_count':len(records)}
    identifier = uuid.uuid4().hex
    db.execute('INSERT INTO source_review_packages(id,source_hash,package_digest,payload_json,approved_by) VALUES(?,?,?,?,?)',
               (identifier, source_hash, digest, json.dumps(payload,ensure_ascii=False,sort_keys=True), reviewer.strip()))
    return {'status':'registered','id':identifier,'review_count':len(records)}


def bind_package(db, task_id, actual_source_hash, ocr_enabled):
    """Atomically bind a package once to a new task. Caller owns transaction."""
    _sha(actual_source_hash,'Uploaded original')
    task = db.execute('SELECT source_hash,current_version FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not task or task[0] != actual_source_hash:
        raise ValueError('Uploaded source fingerprint differs from the task')
    if ocr_enabled is not True:
        return {'status':'ocr_not_authorized','bound_count':0}
    existing = db.execute('SELECT package_id FROM source_review_package_bindings WHERE task_id=?',(task_id,)).fetchone()
    if existing:
        return {'status':'already_bound','bound_count':0,'package_id':existing[0]}
    if task[1] is not None or any(db.execute(f'SELECT 1 FROM {table} WHERE task_id=? LIMIT 1',(task_id,)).fetchone()
                                  for table in TABLES.values()):
        return {'status':'existing_task_or_reviews_preserved','bound_count':0}
    package = db.execute('SELECT id,package_digest,payload_json FROM source_review_packages WHERE source_hash=? ORDER BY rowid DESC LIMIT 1',
                         (actual_source_hash,)).fetchone()
    if not package:
        return {'status':'no_exact_source_package','bound_count':0}
    payload = json.loads(package[2])
    if (payload.get('schema_version') != VERSION or payload.get('scope') != SCOPE
            or payload.get('source_hash') != actual_source_hash or payload.get('knowledge_approval') is not False
            or payload.get('enable_ocr') is not False or _digest(payload) != package[1]):
        raise ValueError('Registered source-review package changed or has invalid authority')
    records = _records(actual_source_hash,payload.get('records'))
    counters = {table:0 for table in TABLES.values()}
    # Validate the complete package before inserting any task-local review.
    for review in records:
        table = TABLES[review['scope']]; counters[table] += 1
        db.execute(f'INSERT INTO {table}(id,task_id,sequence,base_version,source_hash,review_digest,payload_json) VALUES(?,?,?,?,?,?,?)',
                   (uuid.uuid4().hex, task_id, counters[table], 0, actual_source_hash, review['review_digest'],
                    json.dumps(review,ensure_ascii=False,sort_keys=True)))
    db.execute('INSERT INTO source_review_package_bindings(task_id,package_id) VALUES(?,?)',(task_id,package[0]))
    return {'status':'bound','bound_count':len(records),'package_id':package[0]}
