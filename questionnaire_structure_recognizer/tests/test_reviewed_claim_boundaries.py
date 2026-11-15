"""A historical document can describe an attachment or qualify its Yes/No."""
import pytest
from fastapi.testclient import TestClient

from questionnaire_structure_recognizer.knowledge import answer_value_consistency_reasons, source_reuse_restrictions
from questionnaire_structure_recognizer.source_pair_guards import reviewed_value_restrictions
from questionnaire_structure_recognizer.tests.test_scoped_equivalence_review import setup


def test_attachment_acknowledgement_in_existing_knowledge_requires_current_check(tmp_path):
    app, _ = setup(tmp_path)
    headers = {"X-QSR-Key": "test"}
    with TestClient(app) as client:
        url = "/api/questionnaires/t"
        question = client.get(url, headers=headers).json()["questions"][0]
        claim = "APIs, and the product list is attached."
        response = client.post("/api/knowledge/manual", headers=headers, json={
            "canonical_question": question["question_text"], "section": question["section"],
            "context": question["form_definition"].get("semantic_context", ""),
            "answer": claim, "approved": True})
        assert response.is_success
        client.post(url + "/rematch", headers=headers).raise_for_status()
        field = client.get(url, headers=headers).json()["questions"][0]
        assert field["answer_text"] == ""
        assert any(m["status"] == "candidate" and "附件" in m["reason"] for m in field["matches"])
        assert any(m["answer"] == claim for m in field["matches"])


@pytest.mark.parametrize("answer", [
    "A product list is maintained on site.", "Our report is attached to each shipment.",
    "The equipment is attached to a wall.", "Please attach the product list.",
])
def test_process_and_instruction_are_not_claims_of_a_current_attachment(answer):
    assert reviewed_value_restrictions(answer) == []


def test_qualification_and_attachment_declared_by_source_are_retained_separately():
    field = {"answer_text_original": "Our certificate is enclosed.",
             "answer_qualification_refs": [{"block_id": "qualification", "start": 0, "end": 25}]}
    assert set(source_reuse_restrictions(field)) == {
        "source_choice_requires_qualification", "source_attachment_requires_current_confirmation"}


def test_continent_slot_does_not_accept_inspection_agency_names_as_geography():
    assert answer_value_consistency_reasons("Certifying body from (continent):", "NMPA, BJV, etc.")
    assert not answer_value_consistency_reasons("Inspection authority:", "NMPA, BJV, etc.")
    assert not answer_value_consistency_reasons("Certifying body from (continent):", "Asia (NMPA)")
    assert not answer_value_consistency_reasons("Certifying body from (continent):", "EU")


def test_complete_capability_and_incomplete_detail_answers_are_distinguished():
    assert answer_value_consistency_reasons('Name the functions responsible for batch release.', 'Yes')
    assert answer_value_consistency_reasons('Do you use water? If yes please describe how it is purified.', 'Yes')
    assert not answer_value_consistency_reasons('Can you provide Method of analysis and specifications?', 'Yes')
    assert not answer_value_consistency_reasons('Is certification pending? If yes which standard?', 'N/A')
    assert not answer_value_consistency_reasons('Please specify whether water is used.', 'No')


def test_relative_windows_and_current_enclosures_are_separate_from_static_capability():
    assert 'source_relative_period_requires_confirmation' in reviewed_value_restrictions('No', question='Have there been any recalls in the last two years?')
    assert not reviewed_value_restrictions('Yes', question='Are records kept for at least two years?')
    assert 'source_attachment_requires_current_confirmation' in reviewed_value_restrictions('Yes', question='Attach a list of all products. Attached:')
    assert not reviewed_value_restrictions('Yes', question='Can you provide a reference standard?')
