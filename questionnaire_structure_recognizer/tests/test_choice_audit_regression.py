"""Choice semantics regressions; fictional inputs, no model/database."""
from copy import deepcopy


def fixture(text, *, kind="table_cell", controls=None, start=0, end=None):
    block = {"block_id": "answer", "kind": kind, "text": text,
             "location": {"type": "pdf" if kind == "paragraph" else "docx"},
             "structure": {"cell_id": "c-answer"}, "controls": controls or []}
    region = {"block_id": "answer", "start": start, "end": len(text) if end is None else end, "slot_id": ""}
    field = {"field_id": "f", "question_text_original": "Fictional eligibility?", "label_refs": [],
             "answer_region_refs": [region], "answer_value_refs": [region], "answer_refs": [region],
             "answer_text_original": text, "answer_status": "filled", "options": [], "children": []}
    return [field], {"answer": block}


def normalize(fields, lookup):
    from questionnaire_structure_recognizer.validation.native_choices import normalize_choice_fields
    normalize_choice_fields(fields, lookup)
    return fields[0]


def test_unchecked_word_options_are_blank_not_existing_answer():
    fields, lookup = fixture("□ Yes  □ No  □ N/A")
    field = normalize(fields, lookup)
    assert field["answer_text_original"] == "" and field["answer_status"] == "blank"
    assert [x["text_original"] for x in field["options"]] == ["Yes", "No", "N/A"]
    assert not field["answer_value_refs"]
    assert all(lookup[r["block_id"]]["text"][r["start"]:r["end"]] == "□" for r in field["answer_region_refs"])


def test_pdf_suffix_boxes_preserve_selected_label():
    fields, lookup = fixture("Yes □   No ☒   N/A □", kind="paragraph")
    field = normalize(fields, lookup)
    assert field["answer_text_original"] == "No"
    assert [x["selected"] for x in field["options"]] == [False, True, False]
    assert [x["text_original"] for x in field["options"]] == ["Yes", "No", "N/A"]


def test_selected_registration_value_excludes_other_choices_and_translation():
    fields, lookup = fixture("☑Registration No.: FICTIONAL-2026    □None     □Not Applicable\n登録番号:                 なし     対象外")
    field = normalize(fields, lookup)
    assert field["answer_text_original"] == "Registration No.: FICTIONAL-2026"
    assert [x["text_original"] for x in field["options"]] == ["Registration No.: FICTIONAL-2026", "None", "Not Applicable"]


def test_three_unchecked_inspection_options_not_one_option():
    fields, lookup = fixture("□Completed   □Corrective actions required   □Uncompleted\n原文の対照表現")
    field = normalize(fields, lookup)
    assert len(field["options"]) == 3
    assert not field["answer_text_original"]


def test_legacy_checkbox_false_state_clears_raw_yes_no_answer():
    controls = [{"type": "checkbox", "control_id": f"box{i}", "selected": False, "source": "legacy_form_field"} for i in range(3)]
    fields, lookup = fixture("Yes      No    N/A", controls=controls)
    field = normalize(fields, lookup)
    assert field["answer_status"] == "blank" and not field["answer_text_original"]
    assert [x["selected"] for x in field["options"]] == [False, False, False]
    assert [r["control_id"] for r in field["answer_region_refs"]] == ["box0", "box1", "box2"]


def test_unknown_control_selection_is_unresolved_not_false():
    fields, lookup = fixture("Yes   No", controls=[{"type": "checkbox", "control_id": "a", "selected": None}, {"type": "checkbox", "control_id": "b", "selected": False}])
    field = normalize(fields, lookup)
    assert field["answer_status"] == "unresolved" and not field["answer_value_refs"]
    assert field["options"][0]["selected"] is None


def test_zero_width_region_does_not_expand_to_full_option_block():
    fields, lookup = fixture("☑ Yes  □ No", start=0, end=0)
    before = deepcopy(fields)
    normalize(fields, lookup)
    assert fields == before


def test_child_fields_are_normalized_recursively():
    fields, lookup = fixture("Yes □ No □", kind="paragraph")
    parent = {"field_id": "parent", "children": fields, "answer_region_refs": []}
    normalize([parent], lookup)
    assert parent["children"][0]["answer_status"] == "blank"


def test_manual_value_is_preserved():
    fields, lookup = fixture("□ Yes □ No")
    fields[0].update(manual_override=True, answer_text_original="Manual reviewed value", answer_origin="manual")
    normalize(fields, lookup)
    assert fields[0]["answer_text_original"] == "Manual reviewed value"


def test_ordinary_text_with_no_native_choice_evidence_is_unchanged():
    fields, lookup = fixture("Yes, the fictional process is reviewed annually.")
    before = deepcopy(fields)
    normalize(fields, lookup)
    assert fields == before


def test_pdf_suffix_choice_with_trailing_comment_keeps_correct_selected_value():
    fields, lookup = fixture("Yes ☒   No □   N/A □   Comment: ____", kind="paragraph")
    field = normalize(fields, lookup)
    assert field["answer_text_original"] == "Yes"
    assert [o["text_original"] for o in field["options"]] == ["Yes", "No", "N/A"]


def test_selected_control_requires_native_state_and_source_label():
    fields, lookup = fixture("Yes   No", controls=[{"type": "checkbox", "control_id": "a", "selected": False}, {"type": "checkbox", "control_id": "b", "selected": True}])
    source = deepcopy(lookup)
    field = normalize(fields, lookup)
    assert field["answer_text_original"] == "No"
    assert lookup == source
    normalize(fields, lookup)
    assert field["answer_text_original"] == "No"


def test_control_display_disagreement_cannot_create_selected_answer():
    fields, lookup = fixture("☒ Yes  □ No", controls=[{"type": "checkbox", "control_id": "a", "selected": False}, {"type": "checkbox", "control_id": "b", "selected": False}])
    field = normalize(fields, lookup)
    assert field["answer_status"] == "unresolved" and not field["answer_text_original"]
    assert field["review_reasons"]


def test_one_control_per_native_paragraph_keeps_multiline_choice_labels():
    fields, lookup = fixture("Acceptable\nNon-Acceptable", controls=[{"type": "checkbox", "control_id": "a", "selected": False, "paragraph_index": 2}, {"type": "checkbox", "control_id": "b", "selected": False, "paragraph_index": 6}])
    lookup["answer"]["structure"]["paragraph_spans"] = [
        {"paragraph_index": 2, "start": 0, "end": 10, "text": "Acceptable", "blank": False},
        {"paragraph_index": 6, "start": 11, "end": 25, "text": "Non-Acceptable", "blank": False}]
    field = normalize(fields, lookup)
    assert [o["text_original"] for o in field["options"]] == ["Acceptable", "Non-Acceptable"]
    assert field["answer_status"] == "blank" and not field["answer_text_original"]


def test_dedicated_pdf_choice_cell_recovers_scope_cut_by_prefix_interpreter():
    controls = [{"type": "checkbox", "selected": False, "start": 4, "end": 5}, {"type": "checkbox", "selected": False, "start": 9, "end": 10}]
    fields, lookup = fixture("Yes □ No □", controls=controls, start=4, end=5)
    fields[0]["options"] = [{"refs": [{"block_id": "answer", "start": 6, "end": 8}], "answer_region_refs": [{"block_id": "answer", "start": 4, "end": 5}], "selected": False, "text_original": "No"}]
    field = normalize(fields, lookup)
    assert [o["text_original"] for o in field["options"]] == ["Yes", "No"]
    assert [r["start"] for r in field["answer_region_refs"]] == [4, 9]
    assert field["answer_status"] == "blank"


def test_inline_question_cell_does_not_expand_clipped_answer_scope():
    fields, lookup = fixture("Was review done? Yes ☒ No □", start=23, end=26)
    fields[0]["label_refs"] = [{"block_id": "answer", "start": 0, "end": 16}]
    normalize(fields, lookup)
    assert not any(o.get("text_original") == "Yes" for o in fields[0]["options"])


def test_unnamed_legacy_controls_have_distinct_physical_slot_ids():
    controls = [{"type": "checkbox", "control_id": None, "paragraph_index": 1, "control_index": i, "selected": False} for i in (1, 2, 3)]
    fields, lookup = fixture("Yes No N/A", controls=controls)
    field = normalize(fields, lookup)
    slots = [r["slot_id"] for r in field["answer_region_refs"]]
    assert len(set(slots)) == 3
    assert all(r["control_id"] is None for r in field["answer_region_refs"])
