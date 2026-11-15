"""A physical signing group's Date is this signature's date, not reusable data."""
import pytest
from docx import Document

from questionnaire_structure_recognizer.form_positions import enrich_result_form_positions
from questionnaire_structure_recognizer.parser_core import extract_document
from questionnaire_structure_recognizer.validation import validate_and_assemble


def read_form(tmp_path, *, signer_caption="Signature", interruption=None):
    document = Document()
    document.add_paragraph("This questionnaire is to be completed by the supplier.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "Supplier"
    rows = [("Name", "FICTIONAL_SIGNER"), (signer_caption, "")]
    if interruption:
        rows.append((interruption, ""))
    rows.append(("Date", ""))
    for label, value in rows:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = label, value
    document.add_paragraph("Production record")
    business = document.add_table(rows=2, cols=2)
    business.cell(0, 0).text, business.cell(0, 1).text = "Manufacturing date", ""
    business.cell(1, 0).text, business.cell(1, 1).text = "Date", ""
    path = tmp_path / "fictional-supplier-signing.docx"
    document.save(path)
    result = validate_and_assemble(extract_document(path).model_dump(), [], {"provider": "native-fixture"})
    enrich_result_form_positions(result)
    lookup = {block["block_id"]: block for block in result["source_blocks"]}
    fields = {(lookup[field["label_refs"][0]["block_id"]]["location"].get("table"), field["question_text_original"]): field
              for field in result["questions"]}
    return result, fields


def test_supplier_signing_date_is_protected_even_without_completed_by_sentence(tmp_path):
    result, fields = read_form(tmp_path)
    date = fields[(1, "Date")]
    form = date["form_definition"]
    assert form["fill_targets"][0]["writable"]
    assert form["requires_signatory_confirmation"]
    assert not form["auto_fill_eligible"]
    assert not form["auto_fill_eligible_if_condition_met"]
    lookup = {block["block_id"]: block for block in result["source_blocks"]}
    assert any(lookup[ref["block_id"]]["text"] == "Signature" for ref in form["signatory_evidence_refs"])
    for label in ("Manufacturing date", "Date"):
        assert fields[(2, label)]["form_definition"]["auto_fill_eligible"]
        assert not fields[(2, label)]["form_definition"]["requires_signatory_confirmation"]


@pytest.mark.parametrize("caption,interruption", [
    ("Does the supplier verify signatures?", None),
    ("Signature", "Manufacturing batch"),
])
def test_signature_question_or_new_business_region_does_not_protect_unrelated_date(tmp_path, caption, interruption):
    _, fields = read_form(tmp_path, signer_caption=caption, interruption=interruption)
    form = fields[(1, "Date")]["form_definition"]
    assert form["auto_fill_eligible"]
    assert not form["requires_signatory_confirmation"]


def test_parallel_signing_cells_preserve_all_three_prompt_positions(tmp_path):
    document = Document()
    document.add_paragraph('This questionnaire is to be completed by the supplier.')
    table = document.add_table(rows=1, cols=2)
    labels = [('Completed by (Name)', 'Department/Title', 'Signature/Date'),
              ('Reviewer by Quality Unit (Name)', 'Department/Title', 'Signature/Date')]
    for cell, prompts in zip(table.rows[0].cells, labels):
        cell.text = '\n'.join(label + ':' for label in prompts)
    path = tmp_path / 'fictional-parallel-signing.docx'; document.save(path)
    native = extract_document(path).model_dump()
    result = validate_and_assemble(native, [], {'provider': 'native-fixture'})
    enrich_result_form_positions(result)
    assert len(result['questions']) == 6
    lookup = {b['block_id']: b for b in result['source_blocks']}
    for col, prompts in enumerate(labels, 1):
        group = [f for f in result['questions'] if lookup[f['label_refs'][0]['block_id']]['location']['cell'] == col]
        assert [f['question_text_original'].rstrip(':') for f in group] == list(prompts)
        for field in group:
            form = field['form_definition']
            assert field['answer_text_original'] == '' and field['answer_status'] == 'blank'
            assert form['fill_targets'] and all(t['writable'] for t in form['fill_targets'])
            assert form['requires_signatory_confirmation'] and form['signatory_evidence_refs']
            assert not form['auto_fill_eligible']
            assert not form['auto_fill_eligible_if_condition_met']
            # Every field writes into its own actual colon prompt, never the
            # neighbour's cell or the final Signature/Date slot by default.
            region = field['answer_region_refs'][0]
            assert region['block_id'] == field['label_refs'][0]['block_id']
            block = lookup[region['block_id']]
            assert region['start'] == region['end']
            assert block['text'][field['label_refs'][0]['end']:region['start']].strip() in {'', ':'}
