import pytest

from questionnaire_structure_recognizer.form_positions import _target


def source_fixture():
    cell = {"block_id": "physical-cell", "text": "", "location": {
        "type": "pdf", "page": 1, "bbox": [10, 10, 200, 60]},
        "structure": {"scan_region_kind": "text_box", "source_method": "raster_geometry"}}
    block = {"block_id": "source-line", "text": "Date: 06/2024", "location": {
        "type": "pdf", "page": 1, "bbox": [20, 20, 180, 35], "extraction": "ocr"},
        "structure": {}}
    slot = {"slot_id": "source-slot", "kind": "scan_bounded_source_value", "start": 6, "end": 13,
        "expected_source_text": block["text"], "container_ref": {"block_id": cell["block_id"]},
        "label_refs": [{"block_id": block["block_id"], "start": 0, "end": 5}],
        "evidence": "explicit_prompt_and_value_in_closed_response"}
    block["structure"]["logical_slots"] = [slot]
    ref = {"block_id": block["block_id"], "slot_id": slot["slot_id"], "start": 6, "end": 13}
    return ref, {block["block_id"]: block, cell["block_id"]: cell}


def test_bounded_scan_value_is_standard_only_target():
    ref, lookup = source_fixture()
    target = _target(ref, lookup, "fixture-hash", "fixture-parser")
    assert target["writable"]
    assert target["write_method"] == "standard_pdf_response"
    assert target["output_modes"] == ["standard"]


@pytest.mark.parametrize("change", ["cross_page", "out_of_cell", "label_overlap", "stale_text", "ref_range"])
def test_unverified_scan_value_cannot_be_target(change):
    ref, lookup = source_fixture()
    block = lookup["source-line"]
    slot = block["structure"]["logical_slots"][0]
    if change == "cross_page": block["location"]["page"] = 2
    if change == "out_of_cell": block["location"]["bbox"] = [210, 20, 300, 35]
    if change == "label_overlap": slot["label_refs"][0]["end"] = 9
    if change == "stale_text": block["text"] = "Date: 07/2024"
    if change == "ref_range": ref["start"] = 0
    target = _target(ref, lookup, "fixture-hash", "fixture-parser")
    assert target is None or not target["writable"]
