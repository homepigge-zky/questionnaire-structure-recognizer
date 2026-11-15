"""Source citations do not establish that a source's answer is appropriate."""
import json
import pytest

from docx import Document

from questionnaire_structure_recognizer.form_positions import enrich_result_form_positions
from questionnaire_structure_recognizer.knowledge import answer_value_consistency_reasons
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.workflow import approve_import_items, materialize_import
from questionnaire_structure_recognizer.tests.test_import_source_context import register, store_at


def source_document(tmp_path, rows):
    path = tmp_path / "fictional-source-conflicts.docx"
    document = Document()
    document.add_paragraph("Supplier Questionnaire — fictional test only")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Question", "Answer"
    for question, answer in rows:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = question, answer
    document.save(path)
    result = validate_and_assemble(extract_document(path).model_dump(), [], {"provider": "native-fixture"})
    enrich_result_form_positions(result)
    return path, result


def test_source_conflicts_stay_visible_and_block_automatic_knowledge(tmp_path):
    original = [
        ("Company / Site Registration # (if any)", "18 Fictional Avenue, Sample District, Example City, Test Country"),
        ("If yes, what is the range of CFU/g obtained over a period (trend)?", "<25EU/mg"),
    ]
    path, result = source_document(tmp_path, original)
    store = store_at(tmp_path)
    task = register(store, path, result, "fictional-history")
    with store.connect() as db:
        batch = materialize_import(db, task, 1, result)
        items = [dict(row) for row in db.execute("SELECT * FROM import_items WHERE batch_id=?", (batch,))]
        assert len(items) == 2
        assert {item["answer"] for item in items} == {answer for _, answer in original}
        assert all(item["answer_presence"] == "present" for item in items)
        assert all(item["ambiguous"] and not item["allow_auto_reuse"] for item in items)
        assert all("原文答案" in " ".join(json.loads(item["validation_reasons_json"])) for item in items)
        outcome = approve_import_items(db, batch, [item["id"] for item in items], "explicit-test-review")
        assert outcome["approved"] == 0
        assert db.execute("SELECT COUNT(*) FROM knowledge_records").fetchone()[0] == 0


def test_approval_rechecks_current_value_and_retains_explicit_correction(tmp_path):
    path, result = source_document(tmp_path, [("Measured range of CFU/g", "<25EU/mg")])
    store = store_at(tmp_path)
    task = register(store, path, result, "fictional-history")
    with store.connect() as db:
        batch = materialize_import(db, task, 1, result)
        item = dict(db.execute("SELECT * FROM import_items WHERE batch_id=?", (batch,)).fetchone())
        # A pairing confirmation cannot alone make contradictory source units valid.
        db.execute("UPDATE import_items SET pairing_validation='valid',ambiguous=0 WHERE id=?", (item["id"],))
        assert approve_import_items(db, batch, [item["id"]], "reviewed-source-position")["approved"] == 0
        db.execute("UPDATE import_items SET answer='<25CFU/g',pairing_validation='valid',ambiguous=0 WHERE id=?", (item["id"],))
        assert approve_import_items(db, batch, [item["id"]], "explicit-corrected-value-review")["approved"] == 1
        assert db.execute("SELECT answer FROM knowledge_records").fetchone()[0] == "<25CFU/g"
        # Repeated approval does not revisit or undo already approved human values.
        assert approve_import_items(db, batch, [item["id"]], "repeat")["approved"] == 0
        assert db.execute("SELECT answer FROM knowledge_records").fetchone()[0] == "<25CFU/g"


@pytest.mark.parametrize("question,answer", [
    ("Company / Site Registration # (if any)", "REG-12345-EXAMPLE"),
    ("Registered address", "18 Fictional Avenue, Sample District, Example City, Test Country"),
    ("Please provide the registration number and registered address", "18 Fictional Avenue, Sample District, Example City, Test Country"),
    ("Batch number", "123456"),
    ("Company registration #", "18-ROAD-123"),
    ("Measured range of CFU/g", "<25CFU/g"),
    ("CFU/g and EU/mg test results", "<25EU/mg"),
    ("Measured range of CFU/g", "CFU/g was not tested. A separate endotoxin measurement was <25EU/mg."),
    ("Test result", "<25EU/mg"),
    ("Endotoxin EU/mg", "<25EU/mg"),
])
def test_not_a_general_number_address_or_unit_blacklist(question, answer):
    assert answer_value_consistency_reasons(question, answer) == []
