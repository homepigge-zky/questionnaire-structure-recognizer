from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .form_part_routes import (alternate_entity_condition, clear_inactive_derived_answers, dependency_order,
                             different_contact_scope, explicit_followup_polarity, historical_route, incompatible_historical_route,
                             restore_existing_routes, route_status, source_part_routes)
from .source_pair_guards import reviewed_value_restrictions, restriction_reason
from .knowledge import answer_value_consistency_reasons, extract_pending_entries, source_reuse_restrictions
from .semantic_fields import annotation, normalized_text, upsert_annotation
from .source_pair_guards import (shared_response_targets, REUSE_CONSTRAINT_POLICY,
                                 SHARED_TARGET_RESTRICTION, SHARED_TARGET_REASON)


IMPORT_CONTEXT_POLICY = "source-field-context-v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(value: str) -> str:
    value = (value or "").strip().lower()
    value = value.translate(str.maketrans({"：": ":", "？": "?", "（": "(", "）": ")", "　": " "}))
    return re.sub(r"[\s\-_/.,;:!?()\[\]{}'\"，。；：！？、]+", "", value)


def json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return deepcopy(default)
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return deepcopy(default)


def _neighborhood_context(blocks: list[dict], refs: list[dict]) -> str:
    by_id = {block.get("block_id"): block for block in blocks}
    target = next((by_id.get(ref.get("block_id")) for ref in refs if by_id.get(ref.get("block_id"))), None)
    if not target:
        return ""
    structure = target.get("structure") or {}
    table_id, row = structure.get("table_id"), structure.get("row_start", structure.get("row"))
    if not table_id or row is None:
        return ""
    nearby = []
    for block in blocks:
        current = block.get("structure") or {}
        current_row = current.get("row_start", current.get("row"))
        if current.get("table_id") == table_id and current_row is not None and abs(int(current_row) - int(row)) <= 2 and str(block.get("text") or "").strip():
            nearby.append((int(block.get("order") or 0), str(block["text"]).strip()))
    seen, values = set(), []
    for _, text in sorted(nearby):
        if text not in seen:
            seen.add(text); values.append(text)
    return "\n".join(values[:12])


def _stored_neighborhood_context(db: sqlite3.Connection, source_location: Any) -> str:
    source = json_value(source_location, {})
    if source.get("context_policy"):
        # New imports already chose a field-specific context, including an
        # intentionally empty one. Never recreate surrounding answer text on
        # restart or undo a reviewer's later decision to clear that context.
        return ""
    task_id, refs = source.get("task_id"), source.get("question_refs") or []
    if not task_id or not refs:
        return ""
    blocks = []
    for row in db.execute("SELECT block_id,kind,order_no,text,location_json,structure_json FROM document_blocks WHERE task_id=? ORDER BY order_no", (task_id,)):
        blocks.append({"block_id": row["block_id"], "kind": row["kind"], "order": row["order_no"], "text": row["text"], "location": json_value(row["location_json"], {}), "structure": json_value(row["structure_json"], {})})
    return _neighborhood_context(blocks, refs)


def import_context_by_field(result: dict) -> dict[str, dict]:
    """Use the same source-derived context as questionnaire materialization.

    The field identity supplies the link; neighboring rows, answer arrays and
    similar labels never do. Source refs remain separate from readable context.
    """
    lookup = {block["block_id"]: block for block in result.get("source_blocks") or []}
    contexts = {}
    for field, _ in _all_fields(result):
        field_id = str(field.get("field_id") or field.get("question_id") or "")
        if not field_id:
            continue
        refs = []
        for ref in field.get("context_refs") or []:
            text = str(lookup.get(ref.get("block_id"), {}).get("text") or "")
            start, end = ref.get("start"), ref.get("end")
            if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text):
                refs.append(deepcopy(ref))
        form = field.get("form_definition") or {}
        has_semantic = isinstance(form.get("semantic_context"), str)
        context = form["semantic_context"] if has_semantic else "\n".join(
            lookup[ref["block_id"]]["text"][ref["start"]:ref["end"]] for ref in refs)
        contexts[field_id] = {
            "context_policy": IMPORT_CONTEXT_POLICY,
            "context_origin": "form_definition.semantic_context" if has_semantic else "validated_context_refs",
            "context_text": context,
            "context_refs": refs,
            "source_hash": str(result.get("source_hash") or ""),
            "source_section_scope": deepcopy(form.get('source_section_scope')),
            "record_identity": ({"status": form.get('record_identity_status'), "context": context}
                                if form.get('record_identity_status') else None),
        }
    return contexts


def restore_source_reuse_constraints(db: sqlite3.Connection) -> int:
    """Recover omitted safety metadata from the exact immutable source version.

    No source/answer/review version is rewritten. Only protected-source reuse
    authorization is disabled; ordinary historical values remain unchanged.
    """
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'versions', 'tasks', 'knowledge_records', 'import_items'} <= tables:
        return 0
    filename = next((row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main'), '')
    if not filename:
        return 0
    base, cache, changed = Path(filename).resolve().parent, {}, 0
    for table in ('knowledge_records', 'import_items'):
        for row in db.execute(f'SELECT id,source_location_json FROM {table}').fetchall():
            source = json_value(row['source_location_json'], {})
            if ('reuse_restrictions' in source and 'record_identity' in source
                and source.get('reuse_constraint_policy') == REUSE_CONSTRAINT_POLICY) or not source.get('field_id'):
                continue
            key = (source.get('task_id'), source.get('task_version'))
            if not all(key):
                continue
            if key not in cache:
                cache[key] = {}
                version = db.execute('SELECT v.result_file,t.source_hash FROM versions v JOIN tasks t ON t.id=v.task_id WHERE v.task_id=? AND v.version=?', key).fetchone()
                if version:
                    path = (base / version['result_file']).resolve()
                    try:
                        if path.is_relative_to(base) and path.is_file():
                            result = json.loads(path.read_text(encoding='utf8'))
                            if result.get('source_hash') == version['source_hash']:
                                cache[key] = {'fields': {field['field_id']: field for field, _ in _all_fields(result)},
                                              'shared': shared_response_targets(result),
                                              'blocks': {b['block_id']: b for b in result.get('source_blocks', [])}}
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
            field = cache[key].get('fields', {}).get(source['field_id'])
            if field is None:
                continue
            shared = cache[key].get('shared', {}).get(source['field_id'], [])
            restrictions = source_reuse_restrictions(field, shared, cache[key].get('blocks', {}))
            source['reuse_restrictions'] = restrictions
            source['reuse_constraint_policy'] = REUSE_CONSTRAINT_POLICY
            source['shared_response_targets'] = shared
            form = field.get('form_definition') or {}
            source['record_identity'] = ({'status': form.get('record_identity_status'), 'context': form.get('semantic_context', '')}
                                         if form.get('record_identity_status') else None)
            db.execute(f'UPDATE {table} SET source_location_json=? WHERE id=?',
                       (json.dumps(source, ensure_ascii=False), row['id']))
            if restrictions:
                db.execute(f'UPDATE {table} SET allow_auto_reuse=0 WHERE id=?', (row['id'],))
            changed += 1
    return changed


def quarantine_source_role_overlaps(db: sqlite3.Connection) -> dict[str, int]:
    """Isolate values overlapping another label in the same source version.

    One physical cell can contain several labels and response slots. Sharing
    its block ID does not make disjoint, validated text ranges a bad pair.
    Legacy references with missing/invalid ranges remain conservatively held.
    """
    def identity(source, ref):
        return (source.get("task_id"), source.get("task_version"), ref.get("block_id"))

    def overlaps(left, right):
        bounds = [left.get('start'), left.get('end'), right.get('start'), right.get('end')]
        if not (all(type(value) is int for value in bounds)
                and 0 <= bounds[0] < bounds[1] and 0 <= bounds[2] < bounds[3]):
            return True
        return max(bounds[0], bounds[2]) < min(bounds[1], bounds[3])

    def conflicts(owners, source, ref, identifier):
        if not ref.get('block_id'):
            return False
        return any(other_id != identifier and overlaps(ref, label)
                   for other_id, label in owners.get(identity(source, ref), []))

    knowledge = [(dict(row), json_value(row["source_location_json"], {})) for row in db.execute("SELECT * FROM knowledge_records")]
    label_owners = {}
    for row, source in knowledge:
        for ref in source.get("question_refs") or []:
            label_owners.setdefault(identity(source, ref), []).append((row["id"], ref))
    knowledge_ids = set()
    for row, source in knowledge:
        for ref in source.get("answer_refs") or []:
            if conflicts(label_owners, source, ref, row["id"]):
                knowledge_ids.add(row["id"])
    stamp = now()
    if knowledge_ids:
        db.executemany(
            "UPDATE knowledge_records SET review_status='pending',active=0,allow_auto_reuse=0,updated_at=? WHERE id=?",
            [(stamp, identifier) for identifier in knowledge_ids],
        )

    imports = [(dict(row), json_value(row["source_location_json"], {})) for row in db.execute("SELECT * FROM import_items")]
    import_labels = {}
    for row, source in imports:
        for ref in source.get("question_refs") or []:
            import_labels.setdefault(identity(source, ref), []).append((row["id"], ref))
    import_ids = set()
    reason = "已有答案引用了同批次另一个字段的标签，已隔离并等待新版重新解析。"
    for row, source in imports:
        if any(conflicts(import_labels, source, ref, row["id"]) for ref in source.get("answer_refs") or []):
            import_ids.add(row["id"])
    if import_ids:
        db.executemany(
            "UPDATE import_items SET pairing_validation='invalid',ambiguous=1,allow_auto_reuse=0,validation_reasons_json=?,updated_at=? WHERE id=?",
            [(json.dumps([reason], ensure_ascii=False), stamp, identifier) for identifier in import_ids],
        )
    return {"knowledge": len(knowledge_ids), "imports": len(import_ids)}


def initialize_schema(db: sqlite3.Connection) -> None:
    from .scoped_aliases import SCHEMA as SCOPED_ALIAS_SCHEMA
    from .native_text_constraints import SCHEMA as NATIVE_TEXT_SCHEMA
    from .local_condition_scopes import SCHEMA as LOCAL_CONDITION_SCHEMA
    db.executescript("""
    CREATE TABLE IF NOT EXISTS documents(
      id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL, original_filename TEXT NOT NULL,
      storage_path TEXT NOT NULL, file_type TEXT NOT NULL, source_hash TEXT NOT NULL,
      purpose TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS questionnaire_tasks(
      id TEXT PRIMARY KEY, document_id TEXT NOT NULL, customer_name TEXT NOT NULL DEFAULT '',
      questionnaire_name TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '',
      factory TEXT NOT NULL DEFAULT '', product TEXT NOT NULL DEFAULT '',
      business_scope TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
      workflow_status TEXT NOT NULL DEFAULT 'uploaded', current_edit_version INTEGER NOT NULL DEFAULT 0,
      save_status TEXT NOT NULL DEFAULT 'saved', last_saved_at TEXT, created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL, FOREIGN KEY(document_id) REFERENCES documents(id));
    CREATE TABLE IF NOT EXISTS document_blocks(
      task_id TEXT NOT NULL, block_id TEXT NOT NULL, kind TEXT NOT NULL, order_no INTEGER NOT NULL,
      text TEXT NOT NULL, location_json TEXT NOT NULL, structure_json TEXT NOT NULL,
      controls_json TEXT NOT NULL DEFAULT '[]', style_json TEXT NOT NULL DEFAULT '{}',
      PRIMARY KEY(task_id,block_id));
    CREATE TABLE IF NOT EXISTS questions(
      id TEXT PRIMARY KEY, task_id TEXT NOT NULL, parent_id TEXT, field_id TEXT NOT NULL,
      ordinal INTEGER NOT NULL, section TEXT NOT NULL DEFAULT '', original_text TEXT NOT NULL,
      question_text TEXT NOT NULL, input_type TEXT NOT NULL DEFAULT 'text',
      source_refs_json TEXT NOT NULL, answer_regions_json TEXT NOT NULL,
      form_definition_json TEXT NOT NULL DEFAULT '{}',
      review_status TEXT NOT NULL DEFAULT 'ready', updated_at TEXT NOT NULL,
      UNIQUE(task_id,field_id));
    CREATE TABLE IF NOT EXISTS answers(
      question_id TEXT PRIMARY KEY, answer_text TEXT NOT NULL DEFAULT '', selected_options_json TEXT NOT NULL DEFAULT '[]',
      notes TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL DEFAULT 'blank', manual_override INTEGER NOT NULL DEFAULT 0,
      match_id TEXT, confirmed INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
      FOREIGN KEY(question_id) REFERENCES questions(id));
    CREATE TABLE IF NOT EXISTS answer_options(
      id TEXT PRIMARY KEY, question_id TEXT NOT NULL, option_order INTEGER NOT NULL, text TEXT NOT NULL,
      value TEXT NOT NULL DEFAULT '', selected INTEGER NOT NULL DEFAULT 0,
      FOREIGN KEY(question_id) REFERENCES questions(id));
    CREATE TABLE IF NOT EXISTS knowledge_records(
      id TEXT PRIMARY KEY, canonical_question TEXT NOT NULL, original_question TEXT NOT NULL,
      answer TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', section TEXT NOT NULL DEFAULT '',
      context TEXT NOT NULL DEFAULT '', applicable_scope_json TEXT NOT NULL DEFAULT '{}',
      source_file TEXT NOT NULL DEFAULT '', source_location_json TEXT NOT NULL DEFAULT '{}',
      language TEXT NOT NULL DEFAULT '', review_status TEXT NOT NULL DEFAULT 'pending',
      active INTEGER NOT NULL DEFAULT 0, allow_auto_reuse INTEGER NOT NULL DEFAULT 0,
      answer_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS knowledge_aliases(
      id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL, alias TEXT NOT NULL, context TEXT NOT NULL DEFAULT '',
      applicable_scope_json TEXT NOT NULL DEFAULT '{}', confirmed INTEGER NOT NULL DEFAULT 1,
      FOREIGN KEY(knowledge_id) REFERENCES knowledge_records(id));
    CREATE TABLE IF NOT EXISTS knowledge_versions(
      id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL, version INTEGER NOT NULL, answer TEXT NOT NULL,
      snapshot_json TEXT NOT NULL, reviewer TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
      UNIQUE(knowledge_id,version));
    CREATE TABLE IF NOT EXISTS matches(
      id TEXT PRIMARY KEY, task_id TEXT NOT NULL, question_id TEXT NOT NULL, knowledge_id TEXT,
      knowledge_version INTEGER, method TEXT NOT NULL, status TEXT NOT NULL,
      scope_result TEXT NOT NULL, coverage_complete INTEGER NOT NULL DEFAULT 0,
      reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS import_batches(
      id TEXT PRIMARY KEY, task_id TEXT, filename TEXT NOT NULL, file_type TEXT NOT NULL,
      status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS import_items(
      id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, original_question TEXT NOT NULL,
      canonical_question TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '', section TEXT NOT NULL DEFAULT '',
      category TEXT NOT NULL DEFAULT '', context TEXT NOT NULL DEFAULT '', applicable_scope_json TEXT NOT NULL DEFAULT '{}',
      source_file TEXT NOT NULL, source_location_json TEXT NOT NULL DEFAULT '{}', language TEXT NOT NULL DEFAULT '',
      ambiguous INTEGER NOT NULL DEFAULT 0, empty_answer INTEGER NOT NULL DEFAULT 0,
      allow_auto_reuse INTEGER NOT NULL DEFAULT 0, review_status TEXT NOT NULL DEFAULT 'pending',
      answer_presence TEXT NOT NULL DEFAULT 'blank', pairing_validation TEXT NOT NULL DEFAULT 'unvalidated',
      validation_reasons_json TEXT NOT NULL DEFAULT '[]',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS task_versions(
      task_id TEXT NOT NULL, version INTEGER NOT NULL, snapshot_file TEXT NOT NULL,
      kind TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(task_id,version));
    CREATE TABLE IF NOT EXISTS exports(
      id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_version INTEGER NOT NULL, mode TEXT NOT NULL,
      file_path TEXT NOT NULL, filename TEXT NOT NULL, warnings_json TEXT NOT NULL DEFAULT '[]',
      created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS semantic_annotations(
      owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, normalized_text TEXT NOT NULL DEFAULT '',
      canonical_field_key TEXT NOT NULL DEFAULT '', canonical_label TEXT NOT NULL DEFAULT '',
      entity_type TEXT NOT NULL DEFAULT '', entity_id TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '',
      qualifiers_json TEXT NOT NULL DEFAULT '{}', evidence_json TEXT NOT NULL DEFAULT '{}',
      normalization_version TEXT NOT NULL, PRIMARY KEY(owner_type,owner_id));
    CREATE INDEX IF NOT EXISTS ix_semantic_field ON semantic_annotations(owner_type,canonical_field_key,entity_type,entity_id);
    CREATE INDEX IF NOT EXISTS ix_questions_task ON questions(task_id,ordinal);
    CREATE INDEX IF NOT EXISTS ix_matches_question ON matches(question_id,created_at);
    CREATE INDEX IF NOT EXISTS ix_import_items_batch ON import_items(batch_id,review_status);
    CREATE UNIQUE INDEX IF NOT EXISTS ix_import_task ON import_batches(task_id) WHERE task_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS ix_knowledge_lookup ON knowledge_records(review_status,active,allow_auto_reuse);
    """)
    db.executescript(SCOPED_ALIAS_SCHEMA)
    db.executescript(NATIVE_TEXT_SCHEMA)
    db.executescript(LOCAL_CONDITION_SCHEMA)
    columns = {row[1] for row in db.execute("PRAGMA table_info(import_items)")}
    for name, definition in (
        ("answer_presence", "TEXT NOT NULL DEFAULT 'blank'"),
        ("pairing_validation", "TEXT NOT NULL DEFAULT 'unvalidated'"),
        ("validation_reasons_json", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE import_items ADD COLUMN {name} {definition}")
    block_columns = {row[1] for row in db.execute("PRAGMA table_info(document_blocks)")}
    for name, definition in (("controls_json", "TEXT NOT NULL DEFAULT '[]'"), ("style_json", "TEXT NOT NULL DEFAULT '{}'")):
        if name not in block_columns:
            db.execute(f"ALTER TABLE document_blocks ADD COLUMN {name} {definition}")
    question_columns = {row[1] for row in db.execute("PRAGMA table_info(questions)")}
    if "form_definition_json" not in question_columns:
        db.execute("ALTER TABLE questions ADD COLUMN form_definition_json TEXT NOT NULL DEFAULT '{}'")
    # Rows produced before source-pair validation existed are retained for
    # review but cannot be selected or reused until reanalysis/manual check.
    db.execute("UPDATE import_items SET ambiguous=1,allow_auto_reuse=0,validation_reasons_json=? WHERE pairing_validation='unvalidated' AND source_file!='手工录入'", (json.dumps(["旧解析结果尚未经过字段边界与问答来源校验，请使用新版重新解析或对照原文确认。"], ensure_ascii=False),))
    for suspect in db.execute("SELECT source_file,source_location_json FROM import_items WHERE pairing_validation!='valid'").fetchall():
        db.execute("UPDATE knowledge_records SET review_status='pending',active=0,allow_auto_reuse=0,updated_at=? WHERE source_file=? AND source_location_json=?", (now(), suspect["source_file"], suspect["source_location_json"]))
    # Preserve and normalize knowledge snapshots created by the earlier
    # recognizer build. INSERT OR IGNORE makes the migration restart-safe.
    legacy_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_entries'").fetchone()
    if legacy_exists:
        stamp = now()
        for row in db.execute("SELECT id,version,payload FROM knowledge_entries"):
            item = json_value(row["payload"], {})
            question, answer = str(item.get("canonical_question") or item.get("question") or "").strip(), str(item.get("answer") or "").strip()
            if not question or not answer:
                continue
            scope = item.get("applicable_scope") or {}
            if not scope and item.get("customer"):
                scope = {"customer_name": item["customer"]}
            sources = item.get("sources") or [{}]
            source = sources[0] if sources else {}
            db.execute("INSERT OR IGNORE INTO knowledge_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                row["id"], question, str(item.get("original_question") or item.get("question") or question), answer,
                str(item.get("category") or ""), str(item.get("section") or ""), str(item.get("context") or ""), json.dumps(scope, ensure_ascii=False),
                str(source.get("file") or item.get("source_file") or ""), json.dumps(source.get("location") or item.get("source_location") or {}, ensure_ascii=False),
                str(item.get("language") or ""), "approved" if item.get("review") == "approved" else "pending", 1 if item.get("active") is True else 0,
                1 if item.get("general") is True else 0, int(row["version"] or 1), stamp, stamp,
            ))
            db.execute("INSERT OR IGNORE INTO knowledge_versions VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, row["id"], int(row["version"] or 1), answer, json.dumps(item, ensure_ascii=False), str(item.get("reviewer") or ""), stamp))
            for alias in item.get("aliases") or []:
                value = alias if isinstance(alias, str) else alias.get("text") or alias.get("alias")
                if value:
                    db.execute("INSERT OR IGNORE INTO knowledge_aliases VALUES(?,?,?,?,?,?)", (hashlib.sha256(f"{row['id']}|{value}".encode()).hexdigest()[:32], row["id"], value, str(item.get("context") or ""), json.dumps(scope, ensure_ascii=False), 1))
    quarantine_source_role_overlaps(db)
    restore_source_reuse_constraints(db)
    # Incremental semantic upgrade.  It preserves answers, review state and
    # user scope, and only refreshes the derived registry annotation.
    for row in db.execute("SELECT * FROM knowledge_records").fetchall():
        context = row["context"]
        if not context and normalized_text(row["canonical_question"]) in {"name", "名称"}:
            context = _stored_neighborhood_context(db, row["source_location_json"])
            if context: db.execute("UPDATE knowledge_records SET context=? WHERE id=?", (context, row["id"]))
        upsert_annotation(db, "knowledge", row["id"], row["canonical_question"], row["section"], context, scope=json_value(row["applicable_scope_json"], {}))
    for row in db.execute("SELECT * FROM import_items").fetchall():
        context = row["context"]
        if not context and normalized_text(row["canonical_question"]) in {"name", "名称"}:
            context = _stored_neighborhood_context(db, row["source_location_json"])
            if context: db.execute("UPDATE import_items SET context=? WHERE id=?", (context, row["id"]))
        upsert_annotation(db, "import_item", row["id"], row["canonical_question"], row["section"], context, scope=json_value(row["applicable_scope_json"], {}))
    for row in db.execute("SELECT q.*,qt.company,qt.factory,qt.product FROM questions q JOIN questionnaire_tasks qt ON qt.id=q.task_id").fetchall():
        upsert_annotation(db, "question", row["id"], row["question_text"], row["section"], json_value(row['form_definition_json'],{}).get('semantic_context',''), task=dict(row))
    db.commit()


def ensure_document(db: sqlite3.Connection, task: dict, purpose: str) -> str:
    row = db.execute("SELECT id FROM documents WHERE task_id=?", (task["id"],)).fetchone()
    if row:
        return row["id"]
    identifier = uuid.uuid4().hex
    db.execute(
        "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)",
        (identifier, task["id"], task["filename"], task["storage"], Path(task["filename"]).suffix.lower(), task["source_hash"], purpose, now()),
    )
    return identifier


def persist_blocks(db: sqlite3.Connection, task_id: str, result: dict) -> None:
    db.execute("DELETE FROM document_blocks WHERE task_id=?", (task_id,))
    db.executemany(
        "INSERT INTO document_blocks(task_id,block_id,kind,order_no,text,location_json,structure_json,controls_json,style_json) VALUES(?,?,?,?,?,?,?,?,?)",
        [(task_id, b["block_id"], b["kind"], int(b.get("order") or 0), b.get("text") or "", json.dumps(b.get("location") or {}, ensure_ascii=False), json.dumps(b.get("structure") or {}, ensure_ascii=False), json.dumps(b.get("controls") or [], ensure_ascii=False), json.dumps(b.get("style") or {}, ensure_ascii=False)) for b in result.get("source_blocks") or []],
    )


def _all_fields(result: dict) -> list[tuple[dict, str | None]]:
    output: list[tuple[dict, str | None]] = []
    def visit(field: dict, parent: str | None = None) -> None:
        output.append((field, parent))
        for child in field.get("children") or []:
            visit(child, str(field.get("field_id") or field.get("question_id")))
    for field in result.get("questions") or []:
        visit(field)
    return output


def _input_type(field: dict) -> str:
    options = field.get("options") or []
    text = " ".join(str(x.get("text_original") or x.get("text") or "") for x in options).lower()
    if options:
        if field.get("field_type") == "single_select" or ({"是", "否"} <= {str(x.get("text_original") or x.get("text") or "").strip() for x in options}):
            return "single_select"
        return "multi_select"
    label = str(field.get("question_text_original") or "").lower()
    if any(x in label for x in ("date", "日期", "年月日")):
        return "date"
    if any(x in label for x in ("number", "数量", "人数", "面积")):
        return "number"
    return "textarea"


def materialize_questionnaire(db: sqlite3.Connection, task: dict, result: dict, metadata: dict) -> None:
    document_id = ensure_document(db, task, "questionnaire")
    persist_blocks(db, task["id"], result)
    stamp = now()
    db.execute(
        "INSERT INTO questionnaire_tasks(id,document_id,customer_name,questionnaire_name,company,factory,product,business_scope,notes,workflow_status,created_at,updated_at,last_saved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET workflow_status='ready',updated_at=excluded.updated_at",
        (task["id"], document_id, metadata.get("customer_name", ""), metadata.get("questionnaire_name") or Path(task["filename"]).stem, metadata.get("company", ""), metadata.get("factory", ""), metadata.get("product", ""), metadata.get("business_scope", ""), metadata.get("notes", ""), "ready", stamp, stamp, stamp),
    )
    previous = {r["field_id"]: dict(r) for r in db.execute("SELECT q.field_id,q.id,q.parent_id,q.form_definition_json,a.answer_text,a.selected_options_json,a.notes,a.origin,a.manual_override,a.confirmed FROM questions q JOIN answers a ON a.question_id=q.id WHERE q.task_id=?", (task["id"],))}
    db.execute("DELETE FROM answer_options WHERE question_id IN (SELECT id FROM questions WHERE task_id=?)", (task["id"],))
    db.execute("DELETE FROM answers WHERE question_id IN (SELECT id FROM questions WHERE task_id=?)", (task["id"],))
    db.execute("DELETE FROM questions WHERE task_id=?", (task["id"],))
    all_fields = _all_fields(result)
    from .saved_condition_parents import attach_native_condition_parents
    protected_parent_ids={key for key,old in previous.items()if json_value(old.get('form_definition_json'),{}).get('mapping_review_status')=='human_corrected'}
    all_fields = attach_native_condition_parents(all_fields,result,protected_parent_ids)
    routes = source_part_routes(result["source_blocks"], [field for field,_ in all_fields])
    for ordinal, (field, parent_field_id) in enumerate(all_fields, 1):
        field_id = str(field.get("field_id") or field.get("question_id") or f"field-{ordinal}")
        qid = hashlib.sha256(f"{task['id']}|{field_id}".encode()).hexdigest()[:32]
        parent_id = hashlib.sha256(f"{task['id']}|{parent_field_id}".encode()).hexdigest()[:32] if parent_field_id else None
        refs = field.get("label_refs") or field.get("question_refs") or []
        old = previous.get(field_id)
        form_definition = field.get("form_definition") or {}
        old_definition = json_value(old["form_definition_json"], {}) if old else {}
        if old_definition.get("mapping_review_status") == "human_corrected":
            form_definition = old_definition
            parent_id = old.get('parent_id')
        if field_id in routes:
            form_definition = {**form_definition, "source_part_route":routes[field_id]}
        answer_regions = [x.get("ref") for x in form_definition.get("fill_targets") or []] or field.get("answer_region_refs") or []
        db.execute("INSERT INTO questions(id,task_id,parent_id,field_id,ordinal,section,original_text,question_text,input_type,source_refs_json,answer_regions_json,review_status,updated_at,form_definition_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (qid, task["id"], parent_id, field_id, ordinal, field.get("section_text_original") or "", field.get("question_text_original") or "", field.get("question_text_original") or "", _input_type(field), json.dumps(refs, ensure_ascii=False), json.dumps(answer_regions, ensure_ascii=False), field.get("review_status") or "ready", stamp, json.dumps(form_definition, ensure_ascii=False)))
        upsert_annotation(db, "question", qid, field.get("question_text_original") or "", field.get("section_text_original") or "", form_definition.get('semantic_context',''), task={**metadata, "company": metadata.get("company", ""), "factory": metadata.get("factory", ""), "product": metadata.get("product", "")})
        preserve_manual = bool(old and old["manual_override"])
        verified_original = bool(
            field.get("answer_origin") == "source_document"
            and field.get("pairing_validation") == "valid"
            and field.get("answer_value_refs")
        )
        answer = old["answer_text"] if preserve_manual else (str(field.get("answer_text_original") or "") if verified_original else "")
        origin = old["origin"] if preserve_manual else ("source_document" if verified_original else "blank")
        selected = old["selected_options_json"] if preserve_manual else json.dumps([x.get("value") or x.get("text_original") for x in field.get("options") or [] if x.get("selected")], ensure_ascii=False)
        db.execute("INSERT INTO answers VALUES(?,?,?,?,?,?,?,?,?)", (qid, answer, selected, old["notes"] if preserve_manual else "", origin, 1 if preserve_manual else 0, None, old["confirmed"] if preserve_manual else 0, stamp))
        for option_order, option in enumerate(field.get("options") or []):
            db.execute(
                "INSERT INTO answer_options(id,question_id,option_order,text,value,selected) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, qid, option_order, option.get("text_original") or option.get("text") or "", option.get("value") or "", 1 if option.get("selected") else 0),
            )
    match_questionnaire(db, task["id"], replace_auto=True)
    db.commit()


def materialize_import(db: sqlite3.Connection, task: dict, version: int, result: dict) -> str:
    ensure_document(db, task, "import")
    persist_blocks(db, task["id"], result)
    row = db.execute("SELECT id FROM import_batches WHERE task_id=?", (task["id"],)).fetchone()
    batch_id = row["id"] if row else uuid.uuid4().hex
    stamp = now()
    if not row:
        db.execute("INSERT INTO import_batches VALUES(?,?,?,?,?,?,?)", (batch_id, task["id"], task["filename"], Path(task["filename"]).suffix.lower(), "review", stamp, stamp))
    else:
        db.execute("UPDATE import_batches SET status='review',updated_at=? WHERE id=?", (stamp, batch_id))
    metadata = json_value(task.get("metadata_json"), {})
    category = str(metadata.get("category") or "")
    scope_text = str(metadata.get("applicable_scope") or "")
    # Single-unit deployment: organization ownership is implicit. Optional
    # product/business limits remain separate metadata and are never invented.
    scope: dict[str, str] = {}
    if scope_text: scope["text"] = scope_text
    db.execute("DELETE FROM import_items WHERE batch_id=? AND review_status='pending'", (batch_id,))
    contexts = import_context_by_field(result)
    routes = source_part_routes(result["source_blocks"], [field for field,_ in _all_fields(result)])
    for draft in extract_pending_entries(task, version, result):
        source = draft.get("source") or {}
        context_source = contexts.get(str(source.get("field_id") or ""), {
            "context_policy": IMPORT_CONTEXT_POLICY, "context_origin": "field_not_located",
            "context_text": "", "context_refs": [], "source_hash": str(result.get("source_hash") or ""),
        })
        if source.get("field_id") in routes:
            context_source = {**context_source, "source_part_route":routes[source["field_id"]]}
        if source.get('whole_record_requirement'):
            context_source = {**context_source, 'whole_record_requirement':source['whole_record_requirement']}
        local_context = context_source["context_text"]
        item_id = uuid.uuid4().hex
        db.execute("INSERT INTO import_items(id,batch_id,original_question,canonical_question,answer,section,category,context,applicable_scope_json,source_file,source_location_json,language,ambiguous,empty_answer,allow_auto_reuse,review_status,answer_presence,pairing_validation,validation_reasons_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            item_id, batch_id, draft["question"], draft["question"], draft["answer"], draft.get("section", ""), category, local_context, json.dumps(scope, ensure_ascii=False), task["filename"],
            json.dumps({"batch_id": batch_id, "task_id": task["id"], "task_version": version, "field_id": source.get("field_id"), "question_refs": source.get("question_refs", []), "answer_refs": source.get("answer_refs", []), "locations": source.get("source_locations", {}), "source_observation": source.get("source_observation"), "reuse_restrictions": source.get("reuse_restrictions", []), "reuse_constraint_policy": source.get("reuse_constraint_policy"), "shared_response_targets": source.get("shared_response_targets", []), "value_consistency_policy": source.get("value_consistency_policy"), "value_consistency_reasons": source.get("value_consistency_reasons", []), **context_source}, ensure_ascii=False), "", 1 if draft.get("ambiguous") else 0, 0 if draft["answer"].strip() else 1, 0, "pending", draft.get("answer_presence", "present" if draft["answer"].strip() else "blank"), draft.get("pairing_validation", "unvalidated"), json.dumps(draft.get("review_reasons") or [], ensure_ascii=False), stamp, stamp,
        ))
        upsert_annotation(db, "import_item", item_id, draft["question"], draft.get("section", ""), local_context, scope=scope)
    db.commit()
    return batch_id


def import_tabular(db: sqlite3.Connection, filename: str, payload: bytes) -> tuple[str, int]:
    suffix = Path(filename).suffix.lower()
    rows: list[list[Any]] = []
    if suffix == ".csv":
        decoded = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                decoded = payload.decode(encoding); break
            except UnicodeDecodeError:
                pass
        if decoded is None:
            raise ValueError("CSV 编码无法识别，请使用 UTF-8 或 GB18030")
        rows = list(csv.reader(io.StringIO(decoded)))
    elif suffix == ".xlsx":
        workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
        rows = [["" if value is None else str(value) for value in row] for row in workbook.active.iter_rows(values_only=True)]
    else:
        raise ValueError("表格导入只支持 XLSX 和 CSV")
    if not rows:
        raise ValueError("文件没有可导入内容")
    headers = [normalize(str(x)) for x in rows[0]]
    aliases = {"question": ("问题", "题目", "question", "canonicalquestion"), "answer": ("答案", "回答", "answer"), "section": ("章节", "section"), "context": ("上下文", "context"), "category": ("分类", "category"), "scope": ("适用范围", "scope", "applicablescope"), "aliases": ("同义问法", "aliases", "synonyms")}
    index: dict[str, int] = {}
    for name, names in aliases.items():
        found = next((i for i, header in enumerate(headers) if header in {normalize(x) for x in names}), None)
        if found is not None:
            index[name] = found
    if "question" not in index or "answer" not in index:
        raise ValueError("表格必须包含“问题/Question”和“答案/Answer”列")
    stamp, batch_id = now(), uuid.uuid4().hex
    db.execute("INSERT INTO import_batches VALUES(?,?,?,?,?,?,?)", (batch_id, None, Path(filename).name, suffix, "review", stamp, stamp))
    count = 0
    for row in rows[1:]:
        get = lambda name: str(row[index[name]]).strip() if name in index and index[name] < len(row) else ""
        question, answer = get("question"), get("answer")
        if not question and not answer:
            continue
        # Legacy spreadsheets may contain a company/factory scope column. The
        # single-unit product no longer asks users to maintain that ownership.
        scope: dict[str, str] = {}
        item_id = uuid.uuid4().hex
        db.execute("INSERT INTO import_items(id,batch_id,original_question,canonical_question,answer,section,category,context,applicable_scope_json,source_file,source_location_json,language,ambiguous,empty_answer,allow_auto_reuse,review_status,answer_presence,pairing_validation,validation_reasons_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (item_id, batch_id, question, question, answer, get("section"), get("category"), "", json.dumps(scope, ensure_ascii=False), Path(filename).name, json.dumps({"row": count + 2}, ensure_ascii=False), "", 0, 0 if answer else 1, 0, "pending", "present" if answer else "blank", "valid", "[]", stamp, stamp))
        db.execute('UPDATE import_items SET context=? WHERE id=?', (get('context'), item_id))
        upsert_annotation(db, "import_item", item_id, question, get("section"), get('context'), scope=scope)
        count += 1
    if not count:
        raise ValueError("没有找到非空问答行")
    db.commit()
    return batch_id, count


def approve_import_items(db: sqlite3.Connection, batch_id: str, item_ids: list[str], reviewer: str = "") -> dict:
    restore_source_reuse_constraints(db)
    placeholders = ",".join("?" for _ in item_ids)
    if not item_ids:
        return {"approved": 0, "skipped_empty": 0}
    rows = db.execute(f"SELECT * FROM import_items WHERE batch_id=? AND id IN ({placeholders}) AND review_status='pending'", (batch_id, *item_ids)).fetchall()
    approved = skipped = blocked = source_conflicts = 0
    stamp = now()
    for row in rows:
        item = dict(row)
        if SHARED_TARGET_RESTRICTION in json_value(item.get('source_location_json'), {}).get('reuse_restrictions', []):
            db.execute("UPDATE import_items SET pairing_validation='needs_review',ambiguous=1,allow_auto_reuse=0,validation_reasons_json=? WHERE id=?",
                       (json.dumps([SHARED_TARGET_REASON], ensure_ascii=False), item['id']))
            blocked += 1; continue
        if not item["answer"].strip():
            db.execute("UPDATE import_items SET review_status='rejected',updated_at=? WHERE id=?", (stamp, item["id"])); skipped += 1; continue
        value_reasons = answer_value_consistency_reasons(item["canonical_question"], item["answer"])
        if value_reasons:
            # Recheck current values at the authorization boundary. A generic
            # source-pair confirmation must not waive an explicit unit/type
            # contradiction; previously approved human corrections are not
            # selected by this query and are never migrated or overwritten.
            reasons = list(dict.fromkeys([*json_value(item.get("validation_reasons_json"), []), *value_reasons]))
            db.execute("UPDATE import_items SET ambiguous=1,allow_auto_reuse=0,validation_reasons_json=?,updated_at=? WHERE id=?",
                       (json.dumps(reasons, ensure_ascii=False), stamp, item["id"]))
            blocked += 1; source_conflicts += 1; continue
        if item.get("pairing_validation") != "valid" or item.get("ambiguous"):
            db.execute("UPDATE import_items SET allow_auto_reuse=0,updated_at=? WHERE id=?", (stamp, item["id"])); blocked += 1; continue
        kid = uuid.uuid4().hex
        source_constraints = reviewed_value_restrictions(item['answer'], json_value(item.get('source_location_json'), {}).get('reuse_restrictions'), item['canonical_question'])
        # Selecting a validated item and approving the import is the single
        # explicit authorization step. In single-unit mode no second
        # company/factory reuse confirmation is required.
        db.execute("INSERT INTO knowledge_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (kid, item["canonical_question"], item["original_question"], item["answer"], item["category"], item["section"], item["context"], item["applicable_scope_json"], item["source_file"], item["source_location_json"], item["language"], "approved", 1, 0 if source_constraints else 1, 1, stamp, stamp))
        upsert_annotation(db, "knowledge", kid, item["canonical_question"], item["section"], item["context"], scope=json_value(item["applicable_scope_json"], {}))
        snapshot = {key: item[key] for key in ("canonical_question", "original_question", "answer", "category", "section", "context", "applicable_scope_json", "source_file", "source_location_json", "language")}
        db.execute("INSERT INTO knowledge_versions VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, kid, 1, item["answer"], json.dumps(snapshot, ensure_ascii=False), reviewer, stamp))
        if normalize(item["original_question"]) != normalize(item["canonical_question"]):
            db.execute("INSERT INTO knowledge_aliases VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, kid, item["original_question"], item["context"], item["applicable_scope_json"], 1))
        db.execute("UPDATE import_items SET review_status='approved',updated_at=? WHERE id=?", (stamp, item["id"])); approved += 1
    db.execute("UPDATE import_batches SET status=?,updated_at=? WHERE id=?", ("approved" if approved else "review", stamp, batch_id))
    db.commit()
    return {"approved": approved, "skipped_empty": skipped, "blocked_invalid_pairing": blocked, "blocked_source_value_conflicts": source_conflicts}


def _scope_matches(scope: dict, task: dict) -> tuple[bool, str]:
    # This deployment serves one internal organization. Company and factory
    # ownership are deliberately not task inputs or matching gates. Product,
    # customer and business constraints remain meaningful safety boundaries.
    mappings = {"customer_name": "customer_name", "customer": "customer_name", "product": "product", "business_scope": "business_scope"}
    for key, task_key in mappings.items():
        expected = str(scope.get(key) or "").strip()
        if expected and normalize(expected) != normalize(str(task.get(task_key) or "")):
            return False, f"适用范围 {key} 不一致"
    text_scope = str(scope.get("text") or "").strip()
    if text_scope and normalize(text_scope) not in normalize(" ".join(str(task.get(k) or "") for k in mappings.values())):
        return False, "自由文本适用范围未命中"
    return True, "适用范围通过"


def knowledge_choice_selection(db: sqlite3.Connection, question_id: str, answer: str) -> list[str]:
    """Resolve reviewed literal option values; never infer letters or polarity."""
    options = [dict(r) for r in db.execute('SELECT text,value FROM answer_options WHERE question_id=? ORDER BY option_order', (question_id,))]
    if not options:
        return []
    clean = lambda value: ' '.join(str(value).strip().casefold().split())
    exact = [o for o in options if clean(o['text']) == clean(answer)]
    if len(exact) == 1:
        return [exact[0]['value'] or exact[0]['text']]
    question = db.execute('SELECT input_type FROM questions WHERE id=?', (question_id,)).fetchone()
    parts = [s.strip() for s in re.split(r'[\n;；]+', answer) if s.strip()]
    if question and question['input_type'] == 'multi_select' and len(parts) > 1:
        resolved = [[o for o in options if clean(o['text']) == clean(part)] for part in parts]
        if all(len(matches) == 1 for matches in resolved):
            return list(dict.fromkeys(matches[0]['value'] or matches[0]['text'] for matches in resolved))
    raise ValueError('历史答案无法唯一对应当前选项；保留候选，请明确选择，不按字母或近似文字猜测')


def resolved_applicability(db: sqlite3.Connection, question: dict, definition: dict) -> str:
    from .source_part_constraints import unbound_saved_part_condition
    if unbound_saved_part_condition(db, question, definition):
        return 'unresolved'
    if not definition.get('source_part_route') and alternate_entity_condition(question.get('section','')):
        return 'unresolved'
    status = route_status(db, question, definition.get('source_part_route'))
    if status != 'applicable':return status
    condition=definition.get('applicability_condition')
    local_scope=definition.get('source_local_condition')
    if local_scope and (definition.get('conditional_group') or (condition or {}).get('kind')=='explicit_local_condition_decision'):
        from .local_condition_scopes import decision_status
        return decision_status(db,question,definition)
    if local_scope:
        if (local_scope.get('source_hash')!=definition.get('source_hash')
                or local_scope.get('role')=='condition_header' or not condition):return 'unresolved'
        scoped_parent=db.execute('SELECT source_refs_json,form_definition_json FROM questions WHERE task_id=? AND field_id=?',
            (question.get('task_id'),condition.get('parent_field_id'))).fetchone()
        if not scoped_parent:return 'unresolved'
        labels=json_value(scoped_parent['source_refs_json'],[])
        parent_definition=json_value(scoped_parent['form_definition_json'],{})
        if ((local_scope.get('header_ref') or {}).get('block_id') not in {r.get('block_id')for r in labels}
                or parent_definition.get('validation_status')!='valid'
                or not any(t.get('writable') and t.get('write_method')!='append_after_label'
                           for t in parent_definition.get('fill_targets',[]))):return 'unresolved'
    followup=explicit_followup_polarity(question.get('question_text',''))
    if not condition:return 'unresolved' if followup else 'applicable'
    if followup:
        expected=str(condition.get('equals_option','')).strip().casefold()
        if {'是':'yes','否':'no'}.get(expected,expected)!=followup:return 'unresolved'
    parent_ref=condition.get('parent_field_id'); expected=condition.get('equals_option')
    if not parent_ref or not expected:return 'unresolved'
    parent=db.execute('SELECT q.id,q.question_text,q.original_text,a.selected_options_json FROM questions q JOIN answers a ON a.question_id=q.id WHERE q.task_id=? AND q.field_id=?',
        (question['task_id'],parent_ref)).fetchone()
    if not parent or question.get('parent_id')!=parent['id']:return 'unresolved'
    same_text=lambda a,b: ' '.join(str(a or '').split()) == ' '.join(str(b or '').split())
    # Source associations apply to the printed parent and child meaning. An
    # edited question must not retain the old dependency merely because its
    # database id and selected option did not change. Manual answers survive
    # separately; only derived answers are cleared when saving/rematching.
    if (not parent['original_text'] or not question.get('original_text')
            or not same_text(parent['question_text'],parent['original_text'])
            or not same_text(question.get('question_text'),question.get('original_text'))):return 'unresolved'
    if condition.get('route_version') == 'native-numbered-condition-route-v1':
        if (not same_text(parent['question_text'],condition.get('parent_source_text',parent['original_text']))
                or not same_text(question.get('question_text'),condition.get('child_source_text',question.get('original_text')))):
            return 'unresolved'

    choices=[dict(row) for row in db.execute('SELECT text,value FROM answer_options WHERE question_id=? ORDER BY option_order',(parent['id'],))]
    normalize_option=lambda value: re.sub(r'\s+',' ',str(value)).strip().casefold()
    matches=[i for i,option in enumerate(choices) if normalize_option(option['text'])==normalize_option(expected)]
    selected=json_value(parent['selected_options_json'],[])
    if len(matches)!=1 or not selected:return 'unresolved'
    from .choice_identity import selected_indices
    try:chosen=selected_indices(choices,selected)
    except ValueError:return 'unresolved'
    if followup and len(chosen)!=1:return 'unresolved'
    return 'applicable' if matches[0] in chosen else 'not_applicable'


def refresh_saved_responsibility(db: sqlite3.Connection, task_id: str) -> int:
    """Upgrade only derived ownership; persisted source/manual edits stay intact."""
    from .form_positions import upgrade_responsibility_definition
    from .source_ownership import OWNER_POLICY_VERSION
    task=db.execute('SELECT source_hash FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not task:return 0
    rows=db.execute('SELECT * FROM questions WHERE task_id=?',(task_id,)).fetchall()
    pending=[(dict(q),json_value(q['form_definition_json'],{})) for q in rows]
    pending=[(q,d) for q,d in pending if d and d.get('responsibility_policy_version')!=OWNER_POLICY_VERSION and d.get('mapping_review_status')!='human_corrected']
    if not pending:return 0
    lookup={}
    for row in db.execute('SELECT * FROM document_blocks WHERE task_id=? ORDER BY order_no',(task_id,)):
        block=dict(row);block['order']=block['order_no']
        for key in ('location','structure','controls','style'):
            block[key]=json_value(block.get(key+'_json'),[] if key=='controls' else {})
        lookup[block['block_id']]=block
    changed=0
    for q,definition in pending:
        # An edited question must not silently inherit the old source meaning.
        if q['question_text']!=q['original_text']:continue
        field={'field_id':q['field_id'],'question_text_original':q['original_text'],
            'label_refs':json_value(q['source_refs_json'],[]),'answer_region_refs':json_value(q['answer_regions_json'],[]),
            'section_refs':definition.get('section_refs',[]),'instruction_refs':definition.get('instruction_refs',[])}
        updated=upgrade_responsibility_definition(field,definition,lookup,task['source_hash'])
        if updated==definition:continue
        db.execute('UPDATE questions SET form_definition_json=?,updated_at=? WHERE id=?',
            (json.dumps(updated,ensure_ascii=False),now(),q['id']))
        if not updated.get('auto_fill_eligible') and not updated.get('auto_fill_eligible_if_condition_met'):
            db.execute("UPDATE answers SET answer_text='',selected_options_json='[]',origin='blank',match_id=NULL,confirmed=0,updated_at=? WHERE question_id=? AND manual_override=0 AND origin='reviewed_knowledge'",(now(),q['id']))
        changed+=1
    return changed


def whole_record_coverage_reason(definition: dict, entry: dict) -> str:
    """A shared multi-part label asks for one complete scoped record.

    A Name/Phone canonical key or confirmed alias cannot reduce its coverage.
    This compares the preserved source requirements, not generated answer text.
    The regular review, source-validity and scope gates remain mandatory.
    """
    required = definition.get('whole_record_requirement')
    if not required:
        return ''
    rejected = '此填写格要求同一对象的完整记录；历史条目未证明覆盖完整字段及对象，不能只填姓名、电话或靠同义问法绕过'
    source = json_value(entry.get('source_location_json'),{})
    supplied = source.get('whole_record_requirement') if isinstance(source,dict) else None
    if not isinstance(required,dict) or not isinstance(supplied,dict):
        return rejected
    requirement = normalized_text(required.get('requirement_text',''))
    scope = normalized_text(required.get('object_context',''))
    if (not requirement or not scope or not supplied.get('label_refs') or not supplied.get('context_refs')
            or normalized_text(supplied.get('requirement_text','')) != requirement
            or normalized_text(supplied.get('object_context','')) != scope
            or normalized_text(entry.get('original_question','')) != requirement):
        return rejected
    return ''


def match_questionnaire(db: sqlite3.Connection, task_id: str, replace_auto: bool = False) -> dict:
    restore_source_reuse_constraints(db)
    task_row = db.execute("SELECT * FROM questionnaire_tasks WHERE id=?", (task_id,)).fetchone()
    if not task_row:
        raise ValueError("问卷任务不存在")
    restore_existing_routes(db, task_id)
    responsibility_updated=refresh_saved_responsibility(db,task_id)
    task = dict(task_row)
    knowledge = [dict(x) for x in db.execute("SELECT * FROM knowledge_records")]
    held_sources = {}
    history_routes, route_cache = {}, {}
    for entry in knowledge:
        metadata = json_value(entry.get("source_location_json"), {})
        history_routes[entry["id"]] = historical_route(db,metadata,entry["section"],entry["context"],route_cache)
        restrictions = reviewed_value_restrictions(entry["answer"], metadata.get("reuse_restrictions"), entry["canonical_question"])
        if restrictions:
            held_sources[entry["id"]] = restriction_reason(restrictions)
        else:
            issues = answer_value_consistency_reasons(entry["canonical_question"], entry["answer"])
            if issues:
                held_sources[entry["id"]] = " ".join(issues)
    semantic_rows = {(row["owner_type"], row["owner_id"]): annotation(dict(row)) for row in db.execute("SELECT * FROM semantic_annotations")}
    aliases: dict[str, list[str]] = {}
    from .scoped_aliases import load_scoped_aliases, matches_scope
    scoped_aliases = load_scoped_aliases(db)
    for row in db.execute("SELECT a.knowledge_id,a.alias FROM knowledge_aliases a LEFT JOIN knowledge_alias_scopes s ON s.alias_id=a.id WHERE a.confirmed=1 AND s.alias_id IS NULL"):
        aliases.setdefault(row["knowledge_id"], []).append(row["alias"])
    if replace_auto:
        db.execute(
            "UPDATE answers SET answer_text='',selected_options_json='[]',origin='blank',match_id=NULL,confirmed=0,updated_at=? "
            "WHERE manual_override=0 AND origin='reviewed_knowledge' AND question_id IN (SELECT id FROM questions WHERE task_id=?)",
            (now(), task_id),
        )
    db.execute("DELETE FROM matches WHERE task_id=?", (task_id,))
    counts = {"auto_filled": 0, "candidate": 0, "conflict": 0, "unmatched": 0, "preserved_manual": 0, "responsibility_updated": responsibility_updated}
    questions = db.execute("SELECT q.*,a.answer_text,a.selected_options_json,a.origin,a.manual_override FROM questions q JOIN answers a ON a.question_id=q.id WHERE q.task_id=? ORDER BY q.ordinal", (task_id,)).fetchall()
    parent_ids = {q['parent_id'] for q in questions if q['parent_id']}
    stamp = now()
    for row in dependency_order(questions):
        q = dict(row)
        form_definition = json_value(q.get("form_definition_json"), {})
        applicability=resolved_applicability(db,q,form_definition)
        auto_allowed=bool(form_definition.get('auto_fill_eligible') or (applicability=='applicable' and form_definition.get('auto_fill_eligible_if_condition_met')))
        if q["manual_override"]:
            counts["preserved_manual"] += 1; continue
        if q["origin"] == "source_document" and (q["answer_text"].strip() or json_value(q['selected_options_json'], [])):
            continue
        if q['id'] in parent_ids and not form_definition.get('fill_targets'):
            # Group headings have no shared answer position. Match each child
            # at its own source location instead of proposing one value for
            # the entire Name/Address/Phone or record-table heading.
            continue
        qsemantic = semantic_rows.get(("question", q["id"])) or upsert_annotation(db, "question", q["id"], q["question_text"], q["section"], task=task)
        qnorm = qsemantic.get("normalized_text") or normalize(q["question_text"])
        eligible: list[tuple[dict, str, str]] = []
        visible_candidates: list[tuple[dict, str, str]] = []
        for item in knowledge:
            ksemantic = semantic_rows.get(("knowledge", item["id"])) or upsert_annotation(db, "knowledge", item["id"], item["canonical_question"], item["section"], item["context"], scope=json_value(item["applicable_scope_json"], {}))
            scope_ok, scope_reason = _scope_matches(json_value(item["applicable_scope_json"], {}), task)
            canonical = ksemantic.get("normalized_text") or normalize(item["canonical_question"])
            alias_hit = qnorm in {normalized_text(x) for x in aliases.get(item["id"], [])}
            scoped_alias_hit = matches_scope(scoped_aliases.get(item["id"],[]),item,q,task,form_definition)
            same_field = bool(qsemantic.get("canonical_field_key") and qsemantic.get("canonical_field_key") == ksemantic.get("canonical_field_key"))
            generic_fragment = canonical in {"name", "address", "location", "yes", "no", "n/a"} or qnorm in {"name", "address", "location", "yes", "no", "n/a"}
            source_record = json_value(item.get('source_location_json'), {}).get('record_identity') or {}
            record_exact = bool(qnorm == canonical and form_definition.get('record_identity_status') == 'identified'
                and source_record.get('status') == 'identified'
                and normalized_text(form_definition.get('semantic_context', ''))
                and normalized_text(form_definition.get('semantic_context', '')) == normalized_text(source_record.get('context', '')))
            text_related = bool(qnorm and canonical and not generic_fragment and min(len(qnorm), len(canonical)) >= 6 and (qnorm in canonical or canonical in qnorm))
            method = "confirmed_scoped_alias" if scoped_alias_hit else "record_exact" if record_exact else "" if generic_fragment and not same_field else "canonical_field" if same_field else "canonical_exact" if qnorm == canonical else "confirmed_alias" if alias_hit else "semantic_candidate" if text_related else ""
            if not method: continue
            qqual, kqual = qsemantic.get("qualifiers") or {}, ksemantic.get("qualifiers") or {}
            entity_type_mismatch = bool(
                same_field
                and qsemantic.get("entity_type")
                and ksemantic.get("entity_type")
                and qsemantic["entity_type"] != ksemantic["entity_type"]
            )
            # An unscoped historical address is still useful as a visible
            # candidate, but it is never eligible for automatic filling until
            # its address use and object have been reviewed. Other entity type
            # mismatches are unrelated fields and stay out of the candidate list.
            ambiguous_address_type = bool(
                entity_type_mismatch
                and qsemantic.get("canonical_field_key") == "address.full"
                and "unspecified" in {qqual.get("address_use"), kqual.get("address_use")}
            )
            if entity_type_mismatch and not ambiguous_address_type:
                continue
            reason = ""
            if item["review_status"] != "approved": reason = "历史条目尚未审核通过"
            elif not item["active"]: reason = "历史答案已停用或来源失效"
            elif not item["allow_auto_reuse"]: reason = "历史答案已入库，但尚未允许自动复用"
            elif not scope_ok: reason = scope_reason
            if item["id"] in held_sources:
                reason = held_sources[item["id"]]
            if not reason and ambiguous_address_type: reason = "找到地址候选，但历史地址的用途和所属对象尚未确认"
            if not reason and same_field and qsemantic.get("canonical_field_key") == "address.full":
                quse, kuse = qqual.get("address_use"), kqual.get("address_use")
                if "unspecified" in {quse, kuse}: reason = "找到地址候选，但地址用途尚未确认"
                elif quse and kuse and quse != kuse: reason = "地址用途不一致"
            if not reason and same_field and qsemantic.get("canonical_field_key") in {"person.name", "person.position"}:
                qrole, krole = qqual.get("person_role"), kqual.get("person_role")
                if not qrole or not krole or 'person' in {qrole, krole}: reason = "人员字段缺少具体角色，不能确定是联系人、负责人还是签署人"
                elif qrole != krole: reason = "人员字段对应的角色不一致"
            if not reason and same_field and qsemantic.get("canonical_field_key") in {"contact.phone", "contact.fax", "contact.email"}:
                if qqual.get('contact_role') != kqual.get('contact_role'): reason = "联系方式所属的公司或人员角色不一致"
            if not reason and same_field:
                # A conditional subsection's N/A/company-name value is not
                # the unconditional value of that canonical field. Keep the
                # printed applicability boundary, including for exact names.
                qe = qsemantic.get('normalization_evidence') or {}
                ke = ksemantic.get('normalization_evidence') or {}
                qsection, ksection = normalized_text(qe.get('section', '')), normalized_text(ke.get('section', ''))
                if different_contact_scope(qsection,ksection):
                    reason = "同名字段的原文适用条件或条件章节不同，不能跨条件复用"
            if not reason and form_definition.get('record_identity_status'):
                # A canonical Name/Phone column is shared by many distinct
                # records. Only source-backed business context identifies the
                # row; a physical row number is never a reusable identity.
                record_context=normalized_text(form_definition.get('semantic_context',''))
                if form_definition['record_identity_status']!='identified':
                    reason='重复记录的业务对象尚未唯一确认，需人工选择或填写'
                elif not record_context or record_context!=normalized_text(item.get('context','')):
                    reason='重复记录的职责或业务对象不同，不能复用同名列的答案'
            if not reason and method in {"canonical_exact", "confirmed_alias", "record_exact"}:
                # Exact generic wording (e.g. Comment) is not a license to
                # reuse another section's answer. Shared canonical fields have
                # their own stronger role/use guards above.
                qe, ke = qsemantic.get('normalization_evidence') or {}, ksemantic.get('normalization_evidence') or {}
                qcontext = (normalized_text(qe.get('section', '')), normalized_text(qe.get('context', '')))
                kcontext = (normalized_text(ke.get('section', '')), normalized_text(ke.get('context', '')))
                from .source_section_scope import current_section_key, duplicate_section_context
                qscope=form_definition.get('source_section_scope')
                kscope=json_value(item.get('source_location_json'), {}).get('source_section_scope')
                qsection_key = current_section_key(qe.get('section', ''), qscope)
                ksection_key = current_section_key(ke.get('section', ''), kscope)
                if qsection_key and ksection_key:
                    qcontext = (qsection_key, '' if duplicate_section_context(qe.get('context',''),qscope) else qcontext[1])
                    kcontext = (ksection_key, '' if duplicate_section_context(ke.get('context',''),kscope) else kcontext[1])
                if qcontext != kcontext: reason = "问法相同，但章节或局部上下文不同，需确认完整适用范围"
            if not reason and method == "semantic_candidate": reason = "文本相关，但标准字段尚未可靠归一"
            if not reason and incompatible_historical_route(form_definition.get('source_part_route'),history_routes.get(item['id'])):
                reason='历史答案来自不同的条件分部，需核对当前填写对象'
            if not reason:
                reason = whole_record_coverage_reason(form_definition,item)
            if not reason and applicability!='applicable':reason='当前父题答案不满足此填写条件或尚未确认'
            if not reason and form_definition and not auto_allowed:
                reason = "已找到历史答案，但填写位置或填写责任尚未通过自动填入校验"
            if not reason:
                try: knowledge_choice_selection(db, q['id'], item['answer'])
                except ValueError as exc: reason = str(exc)
            if not reason:
                from .native_text_constraints import prepare_saved_text
                try: prepare_saved_text(item['answer'], form_definition)
                except ValueError as exc: reason = str(exc)
            if reason: visible_candidates.append((item, method, reason))
            else: eligible.append((item, method, scope_reason))
        unique_answers = {item[0]["answer"] for item in eligible}
        if eligible and len(unique_answers) == 1:
            chosen, method, scope_reason = eligible[0]
            match_id = uuid.uuid4().hex
            db.execute("INSERT INTO matches VALUES(?,?,?,?,?,?,?,?,?,?,?)", (match_id, task_id, q["id"], chosen["id"], chosen["answer_version"], method, "auto_filled", scope_reason, 1, "标准字段、对象、限定条件和复用授权均通过；无答案冲突", stamp))
            if replace_auto or not q["answer_text"].strip():
                selected = knowledge_choice_selection(db, q['id'], chosen['answer'])
                from .native_text_constraints import prepare_saved_text, record_application
                saved_answer, format_application = prepare_saved_text(chosen['answer'], form_definition)
                db.execute("UPDATE answers SET answer_text=?,selected_options_json=?,origin='reviewed_knowledge',manual_override=0,match_id=?,confirmed=0,updated_at=? WHERE question_id=?", (saved_answer, json.dumps(selected, ensure_ascii=False), match_id, stamp, q["id"]))
                record_application(db, q['id'], format_application, 'reviewed_knowledge', stamp, chosen['id'], chosen['answer_version'])
            counts["auto_filled"] += 1
        elif eligible and len(unique_answers) > 1:
            for chosen, method, scope_reason in eligible:
                db.execute("INSERT INTO matches VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, task_id, q["id"], chosen["id"], chosen["answer_version"], method, "conflict", scope_reason, 1, "相同问题存在不同有效答案", stamp))
            counts["conflict"] += 1
        elif visible_candidates:
            for chosen, method, reason in visible_candidates:
                db.execute("INSERT INTO matches VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, task_id, q["id"], chosen["id"], chosen["answer_version"], method, "candidate", reason, 0, reason, stamp))
            counts["candidate"] += 1
        else:
            staged = db.execute("SELECT COUNT(*) FROM import_items i JOIN semantic_annotations s ON s.owner_type='import_item' AND s.owner_id=i.id WHERE i.review_status='pending' AND s.canonical_field_key=?", (qsemantic.get("canonical_field_key") or "__none__",)).fetchone()[0]
            reason = "相关历史答案仍在导入审核，尚未进入知识库" if staged else "知识库中没有相同标准字段或相关历史答案"
            db.execute("INSERT INTO matches VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, task_id, q["id"], None, None, "none", "unmatched", reason, 0, reason, stamp))
            counts["unmatched"] += 1
    db.execute("UPDATE questionnaire_tasks SET workflow_status='editing',updated_at=? WHERE id=?", (stamp, task_id))
    db.commit()
    return counts


def task_snapshot(db: sqlite3.Connection, task_id: str) -> dict:
    task = db.execute("SELECT * FROM questionnaire_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        raise ValueError("问卷任务不存在")
    questions = []
    block_lookup = {
        row["block_id"]: {"block_id":row["block_id"],"kind":row["kind"],"order": row['order_no'], "location": json_value(row["location_json"], {}), "structure": json_value(row["structure_json"], {}), "controls": json_value(row["controls_json"], []), "style": json_value(row["style_json"], {}), "text": row["text"]}
        for row in db.execute("SELECT block_id,kind,order_no,location_json,structure_json,controls_json,style_json,text FROM document_blocks WHERE task_id=?", (task_id,))
    }
    for row in db.execute("SELECT q.*,a.answer_text,a.selected_options_json,a.notes,a.origin,a.manual_override,a.match_id,a.confirmed,a.updated_at AS answer_updated_at FROM questions q JOIN answers a ON a.question_id=q.id WHERE q.task_id=? ORDER BY q.ordinal", (task_id,)):
        item = dict(row); item["source_refs"] = json_value(item.pop("source_refs_json"), []); item["answer_regions"] = json_value(item.pop("answer_regions_json"), []); item["form_definition"] = json_value(item.pop("form_definition_json"), {}); item["selected_options"] = json_value(item.pop("selected_options_json"), [])
        from .native_text_constraints import text_constraints, latest_application
        item['native_text_constraints'] = text_constraints(item['form_definition'])
        item['native_format_application'] = latest_application(db, item['id'], item['answer_text'], item['answer_updated_at'], item['origin'], item['form_definition'])
        if item['form_definition'].get('conditional_group'):
            decision=db.execute('SELECT decision,reviewer,updated_at,source_hash,header_ref_json FROM questionnaire_condition_decisions WHERE question_id=?',(item['id'],)).fetchone()
            item['local_condition_decision']=dict(decision) if decision else {'decision':'unknown'}
        item['source_order']=min((block_lookup[ref['block_id']]['order'] for ref in item['source_refs'] if ref.get('block_id') in block_lookup),default=item['ordinal'])
        item["answer_region_details"] = [
            {"block_id": ref.get("block_id"), "location": block_lookup.get(ref.get("block_id"), {}).get("location", {}), "structure": block_lookup.get(ref.get("block_id"), {}).get("structure", {}), "controls": block_lookup.get(ref.get("block_id"), {}).get("controls", []), "text": block_lookup.get(ref.get("block_id"), {}).get("text", "")}
            for ref in item["answer_regions"] if ref.get("block_id") in block_lookup
        ]
        semantic_row = db.execute("SELECT * FROM semantic_annotations WHERE owner_type='question' AND owner_id=?", (item["id"],)).fetchone(); item["semantic"] = annotation(dict(semantic_row)) if semantic_row else {}
        item["matches"] = [dict(x) for x in db.execute("SELECT m.*,k.canonical_question,k.answer,k.source_file,s.canonical_field_key,s.canonical_label,s.entity_type,s.entity_id FROM matches m LEFT JOIN knowledge_records k ON k.id=m.knowledge_id LEFT JOIN semantic_annotations s ON s.owner_type='knowledge' AND s.owner_id=k.id WHERE m.question_id=? ORDER BY m.created_at", (item["id"],))]
        item["options"] = [dict(x) for x in db.execute("SELECT id,option_order,text,value,selected FROM answer_options WHERE question_id=? ORDER BY option_order", (item["id"],))]
        from .scoped_aliases import question_signature
        item["equivalence_review_signature"] = question_signature(item,dict(task),item["form_definition"])
        questions.append(item)
    for item in questions:
        definition=item['form_definition'];status=resolved_applicability(db,item,definition)
        definition['applicability_status']=status
        if status!='applicable':definition['auto_fill_eligible']=False
        elif definition.get('auto_fill_eligible_if_condition_met'):definition['auto_fill_eligible']=True
        # A source reserve slot has no reusable record identity. Explicit task
        # entry activates only this saved value for display/statistics; it does
        # not approve pairing, infer identity, or update the native descriptor.
        if definition.get('record_slot_status') == 'reserved' and item['manual_override'] and (item['answer_text'].strip() or item['selected_options']):
            definition['source_record_slot_status'] = 'reserved'
            definition['record_slot_status'] = 'manual_filled'
            definition['auto_fill_eligible'] = False
            definition['auto_fill_eligible_if_condition_met'] = False
            definition['validation_reasons'] = [reason for reason in definition.get('validation_reasons', [])
                if reason != '空白预留记录尚未启用，不参与自动填报或未回答统计']
    counted = [x for x in questions if x["form_definition"].get("field_type") not in {"repeating_record","conditional_group"} and x["form_definition"].get("record_slot_status") != "reserved" and x['form_definition'].get('applicability_status')!='not_applicable']
    answered = sum(bool(x["answer_text"].strip() or x["selected_options"]) for x in counted)
    stats = {"total": len(counted), "answered": answered, "auto_filled": sum(x["origin"] == "reviewed_knowledge" for x in counted), "manual": sum(bool(x["manual_override"]) for x in counted), "unanswered": len(counted) - answered, "pending": sum(any(m["status"] == "candidate" for m in x["matches"]) for x in counted), "conflicts": sum(any(m["status"] == "conflict" for m in x["matches"]) for x in counted), "reserved_record_slots": sum(x["form_definition"].get("record_slot_status") == "reserved" for x in questions)}
    label_ids={ref['block_id'] for q in questions for ref in q['source_refs'] if ref.get('block_id')}
    section_ids={ref['block_id'] for q in questions for ref in q['form_definition'].get('section_refs',[]) if ref.get('block_id')}
    from .form_positions import _declared_owner
    from .source_notes import is_source_responsibility_notice
    document_content=[{'block_id':bid,'text':block['text'],'order':block['order'],'location':block['location'],
        'role':block['structure'].get('resolved_layout_role') or ('responsibility_notice' if is_source_responsibility_notice(block['text']) else 'section_header')}
        for bid,block in block_lookup.items() if bid not in label_ids|section_ids and block['text'].strip() and
        (block['structure'].get('resolved_layout_role') in {'instruction','attachment_requirement','group_context','conditional_context','section_header','signed_declaration_prose'}
         or is_source_responsibility_notice(block['text'])
         or (len(block['text'])<120 and '\n' not in block['text'].strip() and _declared_owner(block['text']) and
             (not block['structure'].get('table_id') or
              int(block['structure'].get('grid_span') or block['structure'].get('column_span') or 0)>=int(block['structure'].get('table_column_count') or 10000))))]
    attached_notes={}
    # One OCR paragraph can contain the question followed by a disjoint
    # instruction. Protect actual label/section ranges, not the entire block,
    # or the verified conditional suffix vanishes from saved/exported Word.
    protected_note_ranges={}
    for q in questions:
        for ref in q['source_refs']+(q['form_definition'].get('section_refs') or []):
            bid=ref.get('block_id');block=block_lookup.get(bid)
            if not block:continue
            start,end=ref.get('start'),ref.get('end')
            if type(start) is int and type(end) is int and 0<=start<end<=len(block['text']):
                protected_note_ranges.setdefault(bid,[]).append((start,end))
            else:
                protected_note_ranges.setdefault(bid,[]).append((0,len(block['text'])))
    for q in questions:
        for ref in q['form_definition'].get('instruction_refs') or []:
            bid=ref.get('block_id');block=block_lookup.get(bid)
            if not block:continue
            start,end=ref.get('start'),ref.get('end')
            if not isinstance(start,int) or not isinstance(end,int) or not 0<=start<end<=len(block['text']):continue
            if any(max(start,a)<min(end,b) for a,b in protected_note_ranges.get(bid,[])):continue
            identity=(bid,start,end)
            note=attached_notes.setdefault(identity,{'block_id':bid,'text':block['text'][start:end],'order':block['order'],
                'location':block['location'],'role':'instruction','source_ref':ref,'related_field_ids':[]})
            if q['field_id'] not in note['related_field_ids']:note['related_field_ids'].append(q['field_id'])
    attached_blocks={key[0] for key in attached_notes}
    document_content=[note for note in document_content if note['block_id'] not in attached_blocks]
    for note in attached_notes.values():
        if len(note['related_field_ids'])==1:note['anchor_field_id']=note['related_field_ids'][0]
        else:note['anchor_validation']='multiple_source_associations'
        document_content.append(note)
    from .source_notes import group_source_notes, group_readonly_source_tables
    source_document = db.execute("SELECT source_hash FROM documents WHERE id=?", (task['document_id'],)).fetchone()
    readonly_tables = group_readonly_source_tables(block_lookup, source_document['source_hash'] if source_document else '')
    readonly_blocks = {bid for note in readonly_tables for bid in note.get('block_ids', [])}
    document_content = [note for note in document_content if note['block_id'] not in readonly_blocks]
    document_content=group_source_notes(document_content,questions,block_lookup)
    document_content = sorted(document_content + readonly_tables, key=lambda n: (n['order'], n['block_id']))
    return {"task": dict(task), "questions": questions, "document_content":document_content, "fill_position_inventory": [x["form_definition"] for x in questions], "stats": stats}


def save_snapshot(db: sqlite3.Connection, data_dir: Path, task_id: str, kind: str) -> tuple[int, dict]:
    restore_existing_routes(db, task_id)
    clear_inactive_derived_answers(db, task_id, now(), resolved_applicability)
    snapshot = task_snapshot(db, task_id)
    version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM task_versions WHERE task_id=?", (task_id,)).fetchone()[0]
    relative = Path("results") / task_id / f"workflow-v{version}.json"
    target = data_dir / relative; target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    stamp = now()
    db.execute("INSERT INTO task_versions VALUES(?,?,?,?,?)", (task_id, version, str(relative), kind, stamp))
    db.execute("UPDATE questionnaire_tasks SET current_edit_version=?,save_status='saved',last_saved_at=?,updated_at=? WHERE id=?", (version, stamp, stamp, task_id))
    db.commit()
    return version, snapshot


def apply_answers_to_result(result: dict, snapshot: dict) -> dict:
    output = deepcopy(result)
    answers = {x["field_id"]: x for x in snapshot["questions"]}
    def visit(field: dict) -> None:
        current = answers.get(str(field.get("field_id") or field.get("question_id")))
        if current:
            field["question_text_original"] = current["question_text"]
            field["answer_text_original"] = current["answer_text"]
            field["selected_options"] = current["selected_options"]
            field["notes"] = current["notes"]
            field["answer_origin"] = current["origin"]
            field["manual_override"] = bool(current["manual_override"])
            field["answer_status"] = "filled" if current["answer_text"].strip() or current["selected_options"] else "blank"
            field["answer_region_refs"] = current["answer_regions"]
            field["form_definition"] = deepcopy(current.get("form_definition") or field.get("form_definition") or {})
        for child in field.get("children") or []:
            visit(child)
    for field in output.get("questions") or []:
        visit(field)
    output["workflow_snapshot"] = {"version": snapshot["task"]["current_edit_version"], "saved_at": snapshot["task"]["last_saved_at"]}
    return output
