"""Apply only explicit native text-control formatting before persistence.

Knowledge/source text is immutable. Exports validate the already-saved value;
they never silently change it. Locale-sensitive Unicode casing is unsupported.
"""
import hashlib
import json
import uuid

VERSION = 'native-text-case-constraints-v1'
SCHEMA = """
CREATE TABLE IF NOT EXISTS answer_format_applications(
 id TEXT PRIMARY KEY, question_id TEXT NOT NULL, created_at TEXT NOT NULL,
 evidence_json TEXT NOT NULL, FOREIGN KEY(question_id) REFERENCES questions(id));
CREATE INDEX IF NOT EXISTS ix_answer_format_question
 ON answer_format_applications(question_id,created_at);
"""


def text_constraints(definition: dict) -> list[dict]:
    constraints = []
    for target in definition.get('fill_targets') or []:
        control = target.get('control') or {}
        if control.get('source') != 'legacy_form_field' or control.get('type') != 'text':
            continue
        formatting = control.get('legacy_text_format', '')
        if not formatting:
            continue
        constraints.append({'format': formatting, 'max_length': control.get('max_length'),
            'control_name': control.get('name') or control.get('control_id'),
            'target_id': target.get('target_id'), 'block_id': target.get('block_id'),
            'source_hash': target.get('source_hash'),
            'supported': bool(control.get('legacy_result_supported') and formatting in {'UPPERCASE', 'LOWERCASE'})})
    return constraints


def normalized_native_case(value: str, formatting: str) -> str:
    if formatting not in {'UPPERCASE', 'LOWERCASE'}:
        raise ValueError('原 Word 文本域含未支持的格式要求。')
    if any(ord(char) > 127 and char.lower() != char.upper() for char in value):
        raise ValueError('此 Word 文本域要求字母大小写格式；含非 ASCII 大小写字符，当前不能保证与 Word 的语言规则一致，请核对并使用标准 Word。')
    return value.upper() if formatting == 'UPPERCASE' else value.lower()


def prepare_saved_text(value: str, definition: dict) -> tuple[str, dict | None]:
    constraints = text_constraints(definition)
    if not constraints:
        return value, None
    formats = {item['format'] for item in constraints}
    if len(formats) != 1 or not all(item['supported'] for item in constraints):
        # Unsupported template controls must not prevent manual editing and
        # standard DOCX export. Their original target remains non-writable.
        return value, None
    formatting = next(iter(formats))
    saved = normalized_native_case(value, formatting)
    for item in constraints:
        limit = item.get('max_length')
        if isinstance(limit, int) and limit > 0 and len(saved) > limit:
            raise ValueError(f'按原 Word 文本域格式保存后的内容超过 {limit} 字符限制；请缩短内容，系统不会截断。')
    digest = lambda text: hashlib.sha256(text.encode('utf8')).hexdigest()
    return saved, {'policy_version': VERSION, 'format': formatting, 'constraints': constraints,
        'changed': saved != value, 'input_sha256': digest(value), 'saved_sha256': digest(saved)}


def record_application(db, question_id: str, evidence: dict | None, origin: str,
                       stamp: str, knowledge_id=None, knowledge_version=None):
    if evidence is None:
        return
    record = {**evidence, 'answer_origin': origin,
              'knowledge_id': knowledge_id, 'knowledge_version': knowledge_version}
    db.execute('INSERT INTO answer_format_applications VALUES(?,?,?,?)',
               (uuid.uuid4().hex, question_id, stamp, json.dumps(record, ensure_ascii=False)))


def latest_application(db, question_id: str, current_value: str, answer_updated_at: str, origin: str, definition: dict) -> dict | None:
    row = db.execute('SELECT id,evidence_json,created_at FROM answer_format_applications WHERE question_id=? ORDER BY rowid DESC LIMIT 1',
                     (question_id,)).fetchone()
    if not row:
        return None
    evidence = json.loads(row['evidence_json'])
    # A later clear/restoration must not inherit the earlier format application.
    if (evidence.get('saved_sha256') != hashlib.sha256(current_value.encode('utf8')).hexdigest()
            or row['created_at'] != answer_updated_at or evidence.get('answer_origin') != origin
            or evidence.get('constraints') != text_constraints(definition)):
        return None
    return {**evidence, 'application_id': row['id'], 'answer_updated_at': answer_updated_at}
