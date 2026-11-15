"""Source-aligned paragraph slots in composite Word cells, no model calls."""
from docx import Document

from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble
from questionnaire_structure_recognizer.exporter import fill_workflow_docx_template


def compound(tmp_path, labels, right_extra=0):
    path = tmp_path / "fictional_composite.docx"
    document = Document(); table = document.add_table(rows=1, cols=2)
    left, right = table.rows[0].cells
    left.paragraphs[0].text = "1.2"
    right.paragraphs[0].text = "Contact person for quality issues"
    for label in labels:
        left.add_paragraph(); left.add_paragraph(label + ":")
        right.add_paragraph(); right.add_paragraph()
    for _ in range(right_extra): right.add_paragraph()
    document.save(path)
    extracted = extract_document(path).model_dump()
    cells = [b for b in extracted["source_blocks"] if b["kind"] == "table_cell"]
    classifications = [{"block_id": b["block_id"], "start": 0, "end": len(b["text"]), "semantic_role": "question", "group_id": "wrong-whole-cell", "relation_status": "proposed"} for b in cells]
    result = validate_and_assemble(extracted, [{"classifications": classifications}], {"provider": "fixture"})
    return path, result


def test_composite_right_title_does_not_hide_three_blank_paragraph_slots(tmp_path):
    path, result = compound(tmp_path, ["Name", "Title", "Function"])
    assert len(result["questions"]) == 1
    parent = result["questions"][0]
    assert [f["question_text_original"] for f in parent["children"]] == ["Name", "Title", "Function"]
    for index, child in enumerate(parent["children"]):
        assert child["answer_text_original"] == "" and child["answer_status"] == "blank"
        assert child["answer_region_refs"][0]["slot_id"].endswith(f":p{index*2+3}:blank")
        child.update(manual_override=True, answer_origin="manual", answer_text_original=f"TEST_{index}")
    destination = tmp_path / "filled_composite.docx"
    updates, warnings = fill_workflow_docx_template(path, result, destination)
    assert len(updates) == 3 and not warnings
    paragraphs = Document(destination).tables[0].cell(0, 1).paragraphs
    assert [p.text for p in paragraphs] == ["Contact person for quality issues", "", "TEST_0", "", "TEST_1", "", "TEST_2"]


def test_mismatched_composite_keeps_all_six_labels_without_guessing_targets(tmp_path):
    labels = ["Name", "Manufacturing site address", "Business Address (if different)", "Phone No.", "e-Mail", "Website"]
    _, result = compound(tmp_path, labels, right_extra=1)
    parent = result["questions"][0]
    assert [f["question_text_original"] for f in parent["children"]] == labels
    for child in parent["children"]:
        assert child["answer_text_original"] == ""
        assert child["answer_status"] == "not_located"
        assert child["answer_region_refs"] == []
        assert child["review_reasons"]
