"""Real fictional DOCX history -> review -> blank form, using source context."""
import hashlib
import importlib
import json

import pytest
from docx import Document

from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.form_positions import enrich_result_form_positions
from questionnaire_structure_recognizer.workflow import (
    approve_import_items, import_context_by_field, import_tabular, initialize_schema,
    materialize_import, materialize_questionnaire,
)


def questionnaire(tmp_path, name, *, filled, section="Manufacturing process", labels=None):
    labels = labels or ["Describe fictional purge handling", "Explain fictional rinse handling"]
    path = tmp_path / f"{name}.docx"
    document = Document()
    document.add_paragraph("Supplier Questionnaire")
    document.add_heading(section, 1)
    table = document.add_table(rows=len(labels) + 1, cols=2)
    table.cell(0, 0).text = "Question"
    table.cell(0, 1).text = "Answer"
    for number, label in enumerate(labels, 1):
        table.cell(number, 0).text = label
        table.cell(number, 1).text = f"FICTIONAL_VALUE_{number}" if filled else ""
    document.save(path)
    result = validate_and_assemble(extract_document(path).model_dump(), [], {"provider": "native-fixture"})
    enrich_result_form_positions(result)
    return path, result


def register(store, path, result, identifier, *, metadata=None):
    payload = path.read_bytes()
    with store.connect() as db:
        db.execute("INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,purpose,metadata_json) "
                   "VALUES(?,?,?,?,?,?,?,?,?)", (identifier, path.name, path.name,
                   hashlib.sha256(payload).hexdigest(), len(payload), "ready", "fixture",
                   "questionnaire", json.dumps(metadata or {})))
        db.commit()
    return store.task(identifier)


def store_at(tmp_path):
    api = importlib.import_module("questionnaire_structure_recognizer.api.app")
    return api.create_app(tmp_path / "data", "fixture-key", auth_mode="key").state.store


def approve_history(store, task, result):
    with store.connect() as db:
        batch = materialize_import(db, task, 1, result)
        rows = [dict(row) for row in db.execute("SELECT * FROM import_items WHERE batch_id=?", (batch,))]
        assert rows
        assert all(row["pairing_validation"] == "valid" and not row["ambiguous"] for row in rows)
        response = approve_import_items(db, batch, [row["id"] for row in rows], "fictional-source-review")
        assert response["approved"] == len(rows), response
    return rows


def materialized(store, task, result, metadata=None):
    with store.connect() as db:
        materialize_questionnaire(db, task, result, metadata or {})
        return [dict(row) for row in db.execute(
            "SELECT q.*,a.answer_text,a.origin FROM questions q JOIN answers a ON a.question_id=q.id WHERE q.task_id=? ORDER BY q.ordinal", (task["id"],))]


def test_reviewed_docx_answers_reuse_without_neighbor_answer_context(tmp_path):
    store = store_at(tmp_path)
    history_path, history = questionnaire(tmp_path, "fictional-answered", filled=True)
    blank_path, blank = questionnaire(tmp_path, "fictional-blank", filled=False)
    history_task = register(store, history_path, history, "history")
    blank_task = register(store, blank_path, blank, "blank")
    rows = approve_history(store, history_task, history)
    questions = materialized(store, blank_task, blank)
    assert len(rows) == len(questions) == 2
    assert [q["answer_text"] for q in questions] == ["FICTIONAL_VALUE_1", "FICTIONAL_VALUE_2"]
    assert all(q["origin"] == "reviewed_knowledge" for q in questions)
    fields = {field["field_id"]: field for field in history["questions"]}
    for item in rows:
        source = json.loads(item["source_location_json"])
        expected = fields[source["field_id"]]["form_definition"]["semantic_context"]
        assert item["context"] == expected
        assert "FICTIONAL_VALUE" not in item["context"]
        assert source["context_policy"] == "source-field-context-v1"
        assert source["context_refs"] == fields[source["field_id"]].get("context_refs", [])
        assert source["source_hash"] == history["source_hash"]


@pytest.mark.parametrize("changed", ["section", "product"])
def test_same_wording_in_different_business_scope_does_not_reuse(tmp_path, changed):
    store = store_at(tmp_path)
    history_path, history = questionnaire(tmp_path, "history", filled=True)
    blank_path, blank = questionnaire(tmp_path, "blank", filled=False,
                                       section="Packaging process" if changed == "section" else "Manufacturing process")
    old_task = register(store, history_path, history, "history")
    task = register(store, blank_path, blank, "blank")
    approve_history(store, old_task, history)
    if changed == "product":
        with store.connect() as db:
            db.execute("UPDATE knowledge_records SET applicable_scope_json=?", (json.dumps({"product": "FICTIONAL_A"}),))
            db.commit()
    questions = materialized(store, task, blank, {"product": "FICTIONAL_B"})
    assert all(q["answer_text"] == "" for q in questions)


def test_empty_field_context_is_not_repopulated_from_neighbor_answers_on_startup(tmp_path):
    store = store_at(tmp_path)
    path, result = questionnaire(tmp_path, "names", filled=True, labels=["Name", "Describe fictional handling"])
    task = register(store, path, result, "history")
    rows = approve_history(store, task, result)
    name = next(row for row in rows if row["canonical_question"] == "Name")
    assert name["context"] == ""
    with store.connect() as db:
        initialize_schema(db)
        after = db.execute("SELECT * FROM import_items WHERE id=?", (name["id"],)).fetchone()
        knowledge = db.execute("SELECT * FROM knowledge_records WHERE canonical_question='Name'").fetchone()
        assert after["context"] == knowledge["context"] == ""


def test_different_contact_roles_do_not_share_same_name_answer(tmp_path):
    store = store_at(tmp_path)
    path, history = questionnaire(tmp_path, "quality-history", filled=True,
                                  section="Quality Assurance Contact", labels=["Name"])
    blank_path, blank = questionnaire(tmp_path, "site-contact", filled=False,
                                      section="Site operations lead", labels=["Name"])
    approve_history(store, register(store, path, history, "history"), history)
    questions = materialized(store, register(store, blank_path, blank, "blank"), blank)
    assert len(questions) == 1
    assert questions[0]["answer_text"] == ""


def test_validated_context_reference_fallback_keeps_only_its_business_role(tmp_path):
    path, result = questionnaire(tmp_path, "context-fallback", filled=True)
    field = result["questions"][0]
    section = next(b for b in result["source_blocks"] if b["text"] == "Manufacturing process")
    ref = {"block_id": section["block_id"], "start": 0, "end": len(section["text"])}
    field["context_refs"] = [ref, {**ref, "end": 0}, {"block_id": "missing", "start": 0, "end": 5}]
    field["form_definition"].pop("semantic_context")
    context = import_context_by_field(result)[field["field_id"]]
    assert context["context_text"] == "Manufacturing process"
    assert context["context_refs"] == [ref]
    assert context["context_origin"] == "validated_context_refs"


def test_csv_and_reviewed_manual_context_survive_schema_upgrade(tmp_path):
    store = store_at(tmp_path)
    with store.connect() as db:
        batch, count = import_tabular(db, "fictional.csv", b"question,answer,context\nName,FICTIONAL_NAME,Quality Assurance Contact\n")
        item = db.execute("SELECT * FROM import_items WHERE batch_id=?", (batch,)).fetchone()
        assert count == 1 and item["context"] == "Quality Assurance Contact"
        approved = approve_import_items(db, batch, [item["id"]], "explicit-review")
        assert approved["approved"] == 1
        db.execute("UPDATE knowledge_records SET context='MANUALLY_REVIEWED_CONTEXT'")
        initialize_schema(db)
        assert db.execute("SELECT context FROM knowledge_records").fetchone()[0] == "MANUALLY_REVIEWED_CONTEXT"


def test_reviewer_cleared_context_is_not_recreated_from_provenance(tmp_path):
    store = store_at(tmp_path)
    path, result = questionnaire(tmp_path, "edited-context", filled=True, labels=["Name"])
    # Preserve a real verified source context, then explicitly clear it after review.
    section = next(b for b in result["source_blocks"] if b["text"] == "Manufacturing process")
    result["questions"][0]["context_refs"] = [{"block_id": section["block_id"], "start": 0, "end": len(section["text"])}]
    enrich_result_form_positions(result)
    items = approve_history(store, register(store, path, result, "history"), result)
    assert items[0]["context"] == "Manufacturing process"
    with store.connect() as db:
        db.execute("UPDATE knowledge_records SET context=''")
        db.execute("UPDATE import_items SET context=''")
        initialize_schema(db)
        assert db.execute("SELECT context FROM knowledge_records").fetchone()[0] == ""
        assert db.execute("SELECT context FROM import_items").fetchone()[0] == ""
