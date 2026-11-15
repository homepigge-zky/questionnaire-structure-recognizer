"""Fictional source-backed ownership regressions; no customer files or models.

The document's actual instruction may authorize ordinary response fields across
sections.  That is independent of an answer's truth, requiredness, and local
customer/reviewer/signing restrictions.
"""
from copy import deepcopy

import pytest

from questionnaire_structure_recognizer.form_positions import (
    build_form_definition,
    infer_responsibility,
)
from questionnaire_structure_recognizer.source_ownership import non_authorizing_statement


@pytest.mark.parametrize("caption", [
    "Example Buyer use only:",
    "To be filled by CQA – Example Pharmaceuticals Ltd",
    "Example Buyer Evaluation Signature",
])
def test_example_in_named_organization_is_not_a_sample_directive(caption):
    assert not non_authorizing_statement(caption)


@pytest.mark.parametrize("caption", [
    "Example wording: This questionnaire is to be completed by the supplier.",
    "For example, this questionnaire is completed by the supplier.",
    "Example: Supplier to complete",
    "Example 2: Supplier to complete",
    "Sample wording: Supplier to complete",
    "如果供应商填写，应提交审核。",
    "Does your procedure state Supplier to complete?",
])
def test_explicit_example_question_or_condition_cannot_authorize(caption):
    assert non_authorizing_statement(caption)


def source_block(key, text, order, *, kind="paragraph", page=None, row=None,
                 column=1, end_column=None, table="form", y=200):
    location = {"type": "docx", "part": "word/document.xml", "paragraph": order + 1}
    structure = {}
    if page is not None:
        location = {"type": "pdf", "page": page, "bbox": [30, y, 570, y + 18]}
        if kind not in {"heading", "title", "table_cell", "form_field"}:
            kind = "ocr_text"
    if row is not None:
        structure.update(table_id=table, cell_id=key, row_start=row, row_end=row,
                         grid_col_start=column, grid_col_end=end_column or column,
                         blank_state="blank" if not text else "filled")
        kind = "table_cell"
    return {"block_id": key, "text": text, "order": order, "kind": kind,
            "location": location, "structure": structure, "controls": []}


def fixture(instruction=None, *, title="Site information form", pdf=False,
            label="Number of production lines", local_owner=None):
    page = 1 if pdf else None
    blocks = [source_block("title", title, 0, kind="title", page=page, y=40)]
    if instruction is not None:
        blocks.append(source_block("instruction", instruction, 1, page=page, y=80))
    # A neutral heading is not a change of respondent. Deliberately beyond the
    # old first-20/first-50-block windows to prove structural, not index, scope.
    blocks.extend(source_block(f"padding-{n}", "", n + 2, page=page) for n in range(55))
    field_page = 4 if pdf else None
    blocks.append(source_block("section", "Production facilities", 60,
                               kind="heading", page=field_page, y=60))
    if local_owner:
        blocks.append(source_block("local-owner", local_owner, 61,
                                   kind="heading", page=field_page, y=90))
    blocks.append(source_block("label", label, 62, page=field_page,
                               row=None if pdf else 2, y=150))
    answer = source_block("answer", "", 63, page=field_page,
                          row=None if pdf else 2, column=2, y=190)
    if pdf:
        answer["kind"] = "form_field"
        answer["structure"] = {"scan_candidate": True, "scan_association_valid": True,
                               "blank_state": "blank"}
    blocks.append(answer)
    refs = lambda key: [{"block_id": key, "start": 0,
                        "end": len(next(b["text"] for b in blocks if b["block_id"] == key))}]
    field = {"field_id": "fictional-field", "question_id": "fictional-field",
             "question_text_original": label, "label_refs": refs("label"),
             "section_refs": refs("local-owner" if local_owner else "section"),
             "context_refs": [], "answer_region_refs": [{"block_id": "answer"}],
             "answer_value_refs": [], "answer_text_original": "",
             "answer_status": "blank", "original_answer_status": "blank",
             "pairing_validation": "valid", "options": [], "children": []}
    return blocks, field


def definition(blocks, field):
    return build_form_definition(deepcopy(field), {b["block_id"]: b for b in blocks},
                                 "fictional-source-sha", "fictional-native-layout")


@pytest.mark.parametrize("pdf", [False, True], ids=["word", "pdf"])
@pytest.mark.parametrize("instruction", [
    "This questionnaire is to be completed by the supplier.",
    "The manufacturer shall complete this questionnaire and return it to the purchaser.",
    "本问卷由供应商填写，完成后交采购方审核。",
])
def test_document_instruction_authorizes_ordinary_fields_across_neutral_sections(instruction, pdf):
    blocks, field = fixture(instruction, pdf=pdf)
    form = definition(blocks, field)
    assert form["fill_responsibility"] == "our_side"
    assert form["auto_fill_eligible"], form["validation_reasons"]
    # Authorization is traceable to the same version of source text, not to
    # filename, model confidence, or a hardcoded default organization.
    source_refs = form.get("responsibility_source_refs", [])
    assert any(r["block_id"] == "instruction" for r in source_refs)
    lookup = {b["block_id"]: b for b in blocks}
    for ref in source_refs:
        text = lookup[ref["block_id"]]["text"]
        assert 0 <= ref["start"] < ref["end"] <= len(text)
    assert form["required_status"] == "unknown"


@pytest.mark.parametrize("pdf", [False, True], ids=["word", "pdf"])
@pytest.mark.parametrize(("local_owner", "expected"), [
    ("For customer use only", "customer"),
    ("To be completed by reviewer", "reviewer"),
    ("客户填写", "customer"),
    ("审核人员填写", "reviewer"),
])
def test_local_other_party_overrides_document_supplier_instruction(pdf, local_owner, expected):
    blocks, field = fixture("This questionnaire is to be completed by the supplier.",
                            pdf=pdf, local_owner=local_owner)
    form = definition(blocks, field)
    # customer/reviewer are distinct roles but old generic 'customer use only'
    # aliases may conservatively produce reviewer; both exclude auto fill.
    assert form["fill_responsibility"] in ({"customer", "reviewer"} if expected == "customer" else {expected})
    assert not form["auto_fill_eligible"]


@pytest.mark.parametrize("title", ["Vendor Audit Checklist", "Supplier Audit Report",
                                    "采购方现场审核记录", "Supplier approval decision"])
def test_audit_document_title_does_not_authorize_supplier_answers(title):
    blocks, field = fixture(title=title)
    form = definition(blocks, field)
    assert form["fill_responsibility"] != "our_side"
    assert not form["auto_fill_eligible"]


def test_file_name_and_model_owner_hint_do_not_authorize_answer_regions():
    blocks, field = fixture()
    for block in blocks:
        block["location"]["filename"] = "Supplier Questionnaire - to be completed by supplier.docx"
    field["model_hints"] = {"responsibility_hint": "our_side", "confidence": 0.999}
    form = definition(blocks, field)
    assert form["fill_responsibility"] == "unknown"
    assert not form["auto_fill_eligible"]


@pytest.mark.parametrize("quoted_instruction", [
    "Does your procedure state that a questionnaire should be completed by the supplier?",
    "Example wording: This questionnaire is to be completed by the supplier.",
    "Do you require questionnaires to be completed by suppliers during vendor qualification?",
])
def test_business_questions_and_examples_are_not_document_authorization(quoted_instruction):
    blocks, field = fixture(quoted_instruction)
    form = definition(blocks, field)
    assert form["fill_responsibility"] != "our_side"
    assert not form["auto_fill_eligible"]


def test_policy_question_in_same_section_cannot_own_next_field():
    blocks, field = fixture()
    blocks.insert(-2, source_block("unrelated-question",
        "Should audit questionnaires be completed by the supplier?", 61))
    assert infer_responsibility(field, {b["block_id"]: b for b in blocks})[0] != "our_side"


def test_example_instruction_in_same_section_does_not_authorize_following_field():
    blocks, field = fixture()
    blocks.insert(-2, source_block("example",
        "Example wording: This questionnaire is to be completed by the supplier.", 61))
    assert infer_responsibility(field, {b["block_id"]: b for b in blocks})[0] != "our_side"


@pytest.mark.parametrize("caption", [
    "Example wording: Supplier use only",
    "Example 2: Vendor to complete",
    "Does the procedure require supplier use only?",
    "In case of self-assessment, vendor to complete",
    "例如：供应商填写",
    "如果供应商填写，应提交审核。",
])
def test_context_alias_fallback_cannot_authorize_example_question_or_condition(caption):
    blocks, field = fixture(local_owner=caption)
    form = definition(blocks, field)
    assert form["fill_responsibility"] != "our_side"
    assert not form["auto_fill_eligible"]


def test_question_mentioning_supplier_questionnaire_is_not_a_document_title():
    blocks, field = fixture("Does your company use a supplier questionnaire?")
    assert infer_responsibility(field, {b["block_id"]: b for b in blocks})[0] != "our_side"


def manufacturing_company_fixture(*, local_owner=None):
    blocks, field = fixture(title='（原料药部分）公司问卷', local_owner=local_owner)
    blocks.extend([
        source_block('manufacturing-address-label', '公司生产区英文地址', 2, row=2, table='intro'),
        source_block('manufacturing-count-label', 'Total Number of Manufacturing blocks:', 3, row=7, table='intro'),
    ])
    return blocks, field


def test_company_questionnaire_requires_independent_source_manufacturing_context():
    blocks, field = manufacturing_company_fixture()
    form = definition(blocks, field)
    assert form['fill_responsibility'] == 'our_side' and form['auto_fill_eligible']
    assert {ref['block_id'] for ref in form['responsibility_source_refs']} == {
        'title', 'manufacturing-address-label', 'manufacturing-count-label'}
    assert form['required_status'] == 'unknown'


@pytest.mark.parametrize('missing', ['manufacturing-address-label', 'manufacturing-count-label'])
def test_company_title_with_only_one_manufacturing_cue_is_not_authority(missing):
    blocks, field = manufacturing_company_fixture()
    blocks = [block for block in blocks if block['block_id'] != missing]
    assert definition(blocks, field)['fill_responsibility'] != 'our_side'


def test_company_title_does_not_authorize_using_an_unrelated_later_table():
    blocks, field = manufacturing_company_fixture()
    next(b for b in blocks if b['block_id'] == 'manufacturing-count-label')['structure']['table_id'] = 'unrelated'
    assert definition(blocks, field)['fill_responsibility'] != 'our_side'


@pytest.mark.parametrize('local_owner', ['客户填写', 'To be completed by reviewer'])
def test_company_manufacturing_context_never_overrides_local_customer_review(local_owner):
    blocks, field = manufacturing_company_fixture(local_owner=local_owner)
    form = definition(blocks, field)
    assert form['fill_responsibility'] in {'customer', 'reviewer'}
    assert not form['auto_fill_eligible']


@pytest.mark.parametrize("title", [
    "MANUFACTURER ASSESSMENT QUESTIONNAIRE FOR API",
    "Manufacturer's Assessment Questionnaire",
])
def test_native_manufacturer_assessment_title_authorizes_ordinary_responses(title):
    blocks, field = fixture(title=title)
    blocks[0]["kind"] = "header"
    blocks[0]["location"]["part"] = "word/header1.xml"
    form = definition(blocks, field)
    assert form["fill_responsibility"] == "our_side"
    assert form["auto_fill_eligible"]
    assert form["responsibility_source_refs"][0]["block_id"] == "title"


@pytest.mark.parametrize('title', [
    'Vendor Re-qualification for Raw materials (API / Excipients)',
    'VENDORRE-QUALIFICATIONFORM',
    'Supplier Requalification Form for Raw Materials',
])
def test_source_requalification_form_title_authorizes_ordinary_supplier_fields(title):
    blocks, field = fixture(title=title, pdf=True)
    form = definition(blocks, field)
    assert form['fill_responsibility'] == 'our_side'
    assert form['auto_fill_eligible']
    assert form['responsibility_source_refs'] == [{'block_id': 'title', 'start': 0, 'end': len(title)}]


@pytest.mark.parametrize('title', [
    'Vendor Re-qualification audit report',
    'SOP Vendor Periodic Review and Periodic Requalification',
    'Vendor Requalification Form review procedure',
    'Example: Vendor Requalification Form',
    'Does the vendor requalification form contain your quality review?',
])
def test_requalification_procedure_or_mention_is_not_supplier_authorization(title):
    blocks, field = fixture(title=title, pdf=True)
    assert not definition(blocks, field)['auto_fill_eligible']


def test_named_quality_assessment_scope_survives_missing_model_section_and_keeps_source():
    blocks, field = fixture(title='Vendor Requalification Form', pdf=True,
                            local_owner='Assessment byExampleVendor QualityManagement')
    field['section_refs'] = []
    form = definition(blocks, field)
    assert form['fill_responsibility'] == 'reviewer'
    assert not form['auto_fill_eligible']
    assert [r['block_id'] for r in form['responsibility_source_refs']] == ['local-owner']
    # The later assessment panel cannot take over an earlier supplier field.
    lookup = {b['block_id']: b for b in blocks}
    lookup['local-owner']['order'] = 70
    lookup['local-owner']['location']['page'] = 5
    assert infer_responsibility(deepcopy(field), lookup)[0] == 'our_side'


@pytest.mark.parametrize('heading', [
    'Is assessment by Example Vendor Quality Management required?',
    'Is assessment by Example Vendor Quality Management required',
    'Example wording: Assessment by Example Vendor Quality Management',
])
def test_assessment_process_question_cannot_create_customer_section(heading):
    blocks, field = fixture(title='Vendor Requalification Form', pdf=True, local_owner=heading)
    assert definition(blocks, field)['fill_responsibility'] == 'our_side'


def test_supplier_requalification_designation_and_company_seal_remain_manual():
    blocks, field = fixture(title='Vendor Requalification Form', pdf=True,
                            label='Designation\nCompany Seal')
    form = definition(blocks, field)
    assert form['requires_signatory_confirmation']
    assert not form['auto_fill_eligible']
    refs = form['signatory_evidence_refs']
    assert len(refs) == 1
    assert refs[0]['block_id'] == 'label'
    assert field['question_text_original'][refs[0]['start']:refs[0]['end']] == 'Company Seal'


@pytest.mark.parametrize("title", [
    "Do you use a manufacturer assessment questionnaire?",
    "Example wording: Manufacturer Assessment Questionnaire",
    "If needed, Manufacturer Assessment Questionnaire",
    "Manufacturer Assessment Audit Report",
])
def test_mention_or_audit_report_is_not_a_manufacturer_questionnaire_title(title):
    blocks, field = fixture(title=title)
    form = definition(blocks, field)
    assert form["fill_responsibility"] != "our_side"
    assert not form["auto_fill_eligible"]


@pytest.mark.parametrize("local_owner", ["For customer use only", "To be completed by reviewer"])
def test_manufacturer_title_cannot_authorize_local_customer_or_review_area(local_owner):
    blocks, field = fixture(title="MANUFACTURER ASSESSMENT QUESTIONNAIRE FOR API", local_owner=local_owner)
    form = definition(blocks, field)
    assert form["fill_responsibility"] in {"customer", "reviewer"}
    assert not form["auto_fill_eligible"]


def test_manufacturer_title_does_not_authorize_completed_signatory_person():
    blocks, field = fixture(title="MANUFACTURER ASSESSMENT QUESTIONNAIRE FOR API", label="Signee Name:")
    form = definition(blocks, field)
    assert form["requires_signatory_confirmation"]
    assert not form["auto_fill_eligible"]


def test_company_name_requested_as_rubber_stamp_is_not_an_auto_name_field():
    blocks, field = fixture(title="MANUFACTURER ASSESSMENT QUESTIONNAIRE FOR API",
                            label="Company Name (Company Rubber Stamp)")
    form = definition(blocks, field)
    assert form["fill_targets"][0]["writable"]
    assert form["requires_signatory_confirmation"]
    assert not form["auto_fill_eligible"]
    from questionnaire_structure_recognizer.form_positions import upgrade_responsibility_definition
    previous = deepcopy(form)
    previous.update(responsibility_policy_version='previous-guard', requires_signatory_confirmation=False,
                    signatory_evidence_refs=[], auto_fill_eligible=True)
    upgraded = upgrade_responsibility_definition(field, previous, {b['block_id']: b for b in blocks}, 'fictional-source-sha')
    assert upgraded['requires_signatory_confirmation']
    assert not upgraded['auto_fill_eligible']
    assert upgraded['fill_targets'] == previous['fill_targets']


@pytest.mark.parametrize("label", ["Name:", "Job title:", "Date:", "© Contact No.:", "© Email"])
def test_adjacent_completed_by_signature_table_protects_all_metadata_even_when_writable(label):
    blocks, field = fixture(title="MANUFACTURER ASSESSMENT QUESTIONNAIRE FOR API", label=label)
    blocks = [b for b in blocks if not b['block_id'].startswith('padding-')]
    blocks.append(source_block('completion', 'The questionnaire should be completed and signed by Quality Personnel.', 61))
    blocks.append(source_block('signature', 'Signature:', 64, row=3))
    blocks.append(source_block('signature-answer', '', 65, row=3, column=2))
    form = definition(blocks, field)
    assert form['fill_targets'][0]['writable']
    assert form['requires_current_person_confirmation']
    assert not form['auto_fill_eligible']
    assert {r['block_id'] for r in form['current_person_evidence_refs']} == {'completion', 'signature'}


@pytest.mark.parametrize('interruption', ['another_table', 'new_section', 'business_row'])
def test_completion_caption_does_not_own_unrelated_contact_table(interruption):
    blocks, field = fixture(title="MANUFACTURER ASSESSMENT QUESTIONNAIRE FOR API", label="Name:")
    blocks.append(source_block('completion', 'The questionnaire should be completed and signed by Quality Personnel.', 58))
    if interruption == 'another_table':
        blocks.append(source_block('prior-signature', 'Signature:', 59, row=1, table='completion-table'))
    elif interruption == 'new_section':
        blocks.append(source_block('contact-section', 'Primary Quality Assurance Contact', 61, kind='heading'))
    else:
        blocks.append(source_block('business-row', 'Manufacturing address', 64, row=3))
        blocks.append(source_block('signature', 'Signature:', 65, row=4))
    form = definition(blocks, field)
    assert not form['requires_current_person_confirmation']


@pytest.mark.parametrize("title", ["生产商问卷调查表", "制造商调查问卷", "生产厂家调查表"])
def test_native_chinese_manufacturer_titles_and_local_review_records_remain_separate(title):
    blocks, field = fixture(title=title)
    assert definition(blocks, field)["fill_responsibility"] == "our_side"
    protected, review = fixture(title=title, local_owner="供应商问卷调查情况审核记录")
    form = definition(protected, review)
    assert form["fill_responsibility"] == "reviewer"
    assert not form["auto_fill_eligible"]


def test_future_local_instruction_does_not_authorize_previous_section():
    blocks, field = fixture()
    blocks.append(source_block("future", "Part 2: To be completed by supplier", 80, kind="heading"))
    assert infer_responsibility(field, {b["block_id"]: b for b in blocks})[0] != "our_side"


@pytest.mark.parametrize("label", ["Signature", "Company seal", "签字及盖章", "Signee Name:"])
def test_supplier_instruction_does_not_authorize_signing_or_signatory_metadata(label):
    blocks, field = fixture("This questionnaire is to be completed by the supplier.", label=label)
    form = definition(blocks, field)
    assert not form["auto_fill_eligible"]
    assert form["field_type"] == "manual_signature" or form["requires_signatory_confirmation"]


def test_document_instruction_cannot_cross_customer_column():
    blocks, field = fixture("This questionnaire is to be completed by the supplier.")
    blocks.extend([
        source_block("supplier-column", "Supplier to complete", 57, row=1, column=2),
        source_block("customer-column", "Customer use only", 58, row=1, column=3),
        source_block("customer-answer", "", 64, row=2, column=3),
    ])
    supplier = definition(blocks, field)
    field["answer_region_refs"] = [{"block_id": "customer-answer"}]
    customer = definition(blocks, field)
    assert supplier["fill_responsibility"] == "our_side"
    assert supplier["auto_fill_eligible"]
    assert customer["fill_responsibility"] == "customer"
    assert not customer["auto_fill_eligible"]


def test_owning_instruction_is_never_copied_to_question_or_answer():
    blocks, field = fixture("This questionnaire is to be completed by the supplier.")
    before = deepcopy(field)
    definition(blocks, field)
    assert field == before
    assert field["answer_text_original"] == ""
    assert field["question_text_original"] == "Number of production lines"


@pytest.mark.parametrize("label", ["Auditor(s):", "*Approved:"])
def test_same_cell_audit_signature_approval_caption_never_uses_historical_person(label):
    blocks, field = fixture("This questionnaire is to be completed by the supplier.")
    text = "Audit report Signature Approval\nAuditor(s): ______\n*Approved: ______\nHead-QA"
    source = source_block("signing-cell", text, 62, row=8)
    spans = []
    cursor = 0
    for index, line in enumerate(text.splitlines(), 1):
        spans.append({"start": cursor, "end": cursor + len(line), "text": line,
                      "paragraph_index": index, "paragraph_id": f"signing-p-{index}"})
        cursor += len(line) + 1
    start = text.index(label)
    value_start = text.index("______", start)
    slot_id = f"signing-cell:{start}:inline-placeholder"
    source["structure"].update(paragraph_spans=spans, logical_slots=[{
        "slot_id": slot_id, "kind": "inline_placeholder", "start": value_start,
        "end": value_start + 6, "placeholder_text": "______",
        "label_start": start, "label_end": start + len(label),
    }])
    blocks = [b for b in blocks if b["block_id"] not in {"label", "answer"}] + [source]
    field.update(question_text_original=label,
                 label_refs=[{"block_id": "signing-cell", "start": start, "end": start + len(label)}],
                 answer_region_refs=[{"block_id": "signing-cell", "start": value_start,
                                      "end": value_start + 6, "slot_id": slot_id}])
    form = definition(blocks, field)
    assert form["fill_responsibility"] == "our_side"
    assert form["fill_targets"][0]["writable"]
    assert form["requires_current_person_confirmation"] or form["requires_signatory_confirmation"]
    assert not form["auto_fill_eligible"]


@pytest.mark.parametrize("pdf", [False, True], ids=["word", "pdf"])
@pytest.mark.parametrize("owner", ["For customer use only", "To be completed by reviewer"])
def test_neutral_subheading_does_not_end_protected_owner_when_model_omits_scope(pdf, owner):
    blocks, field = fixture("This questionnaire is to be completed by the supplier.", pdf=pdf)
    page = 4 if pdf else None
    # The persisted model kept only the subsection title as section_ref, so
    # ownership must still use the actual source order/hierarchy conservatively.
    blocks = [b for b in blocks if b["block_id"] != "section"]
    blocks.extend([source_block("protected-section", owner, 60, kind="heading", page=page, y=55),
                   source_block("section", "A. Administrative details", 61, kind="heading", page=page, y=90)])
    blocks.sort(key=lambda b: b["order"])
    form = definition(blocks, field)
    assert form["fill_responsibility"] != "our_side"
    assert not form["auto_fill_eligible"]


def test_explicit_new_supplier_section_resets_prior_customer_region():
    blocks, field = fixture("This questionnaire is to be completed by the supplier.")
    blocks = [b for b in blocks if b["block_id"] != "section"]
    blocks.extend([source_block("protected-section", "For customer use only", 59, kind="heading"),
                   source_block("section", "To be completed by supplier", 60, kind="heading"),
                   source_block("neutral-subsection", "Administrative details", 61, kind="heading")])
    blocks.sort(key=lambda b: b["order"])
    form = definition(blocks, field)
    assert form["fill_responsibility"] == "our_side"
    assert form["auto_fill_eligible"]


@pytest.mark.parametrize("opening", [
    "Are product records reviewed by quality personnel before the release of the",
    "12.4 Are inspection reports reviewed by the customer before",
    "是否由审核人员填写并复核每次生产记录",
])
def test_wrapped_business_question_cannot_open_reviewer_section(opening):
    blocks, field = fixture("This questionnaire is to be completed by the supplier.",
                            pdf=True, label="Name of material")
    blocks.append(source_block("prior-question", opening, 58, page=3, y=650))
    blocks.append(source_block("prior-question-continuation", "material?", 59, page=3, y=675))
    blocks.sort(key=lambda b: b["order"])
    form = definition(blocks, field)
    assert form["fill_responsibility"] == "our_side"
    assert all(r["block_id"] != "prior-question" for r in form["responsibility_source_refs"])


def test_review_signature_caption_still_starts_protected_scope_without_question_mark():
    blocks, field = fixture("This questionnaire is to be completed by the supplier.", pdf=True)
    blocks.append(source_block("review-caption", "Questionnaire reviewed by: ______", 59, page=3, y=650))
    blocks.sort(key=lambda b: b["order"])
    assert definition(blocks, field)["fill_responsibility"] == "reviewer"


@pytest.mark.parametrize("label", [
    "实验室记录是否由分析人员和审核人签名？",
    "实验室记录是否由分析人员和审核人签名",
    "Are laboratory records signed by the analyst and reviewed by a reviewer?",
    "Are laboratory records signed by the analyst and reviewed by a reviewer",
])
@pytest.mark.parametrize("protected", [False, True])
def test_question_about_review_process_does_not_assign_current_form_to_reviewer(label, protected):
    blocks, field = fixture("This questionnaire is to be completed by the supplier.",
                            label=label, local_owner="审核人员填写" if protected else None)
    form = definition(blocks, field)
    assert form["fill_responsibility"] == ("reviewer" if protected else "our_side")


@pytest.mark.parametrize("label", ["审核人", "审核人填写", "Reviewed by:"])
def test_bare_reviewer_field_is_still_protected(label):
    blocks, field = fixture("This questionnaire is to be completed by the supplier.", label=label)
    assert definition(blocks, field)["fill_responsibility"] == "reviewer"


def test_named_party_section_never_inherits_supplier_default_without_identity_evidence():
    blocks,field=fixture("This questionnaire is to be completed by the supplier.",pdf=True,label="Comments")
    blocks.append(source_block("party-section","D.SECTION FOR FICTIONAL BUYER LIMITED:",59,page=4,y=40))
    blocks.sort(key=lambda b:b['order'])
    form=definition(blocks,field)
    assert form['fill_responsibility']=='unknown' and not form['auto_fill_eligible']
    assert any(r['block_id']=='party-section' for r in form['responsibility_source_refs'])


@pytest.mark.parametrize("instruction", [
    "To be completed by vendor in case of self-assessment",
    "By vendor in case of self-assessment",
])
def test_conditional_vendor_self_assessment_is_not_unconditional_permission(instruction):
    blocks, field = fixture(instruction)
    form = definition(blocks, field)
    assert form["fill_responsibility"] != "our_side"
    assert not form["auto_fill_eligible"]


@pytest.mark.parametrize("instruction,expected", [
    ("TO BE FILLED IN BY API MANUFACTURER IF ANY COMMENTS NEED TO BE MADE", "our_side"),
    ("TO BE FILED IN BY API MANUFACTURER IF ANY COMMENTS NEED TO BE MADE", "our_side"),
    ("TO BE FILLED IN BY API MANUFACTURER IF ANY BE COMMENTS NEED TO BE MADE", "our_side"),
    ("To be completed by supplier if remarks are required", "our_side"),
    ("To be filled in by supplier in case of self-assessment", "unknown"),
    ("Example: To be filled in by supplier if any comments need to be made", "unknown"),
    ("Is this to be filled in by supplier if any comments need to be made?", "unknown"),
])
def test_local_optional_comment_instruction_does_not_become_global_condition_permission(instruction, expected):
    blocks, field = fixture(label="Comments")
    local = source_block("optional-comments", instruction, 64, row=2, column=3)
    blocks.append(local)
    field["instruction_refs"] = [{"block_id": local["block_id"], "start": 0, "end": len(instruction)}]
    form = definition(blocks, field)
    assert form["fill_responsibility"] == expected
    if expected == "our_side":
        assert form["responsibility_source_refs"] == field["instruction_refs"]
    else:
        assert not form["auto_fill_eligible"]


@pytest.mark.parametrize('label,instruction,other_row', [
    ('Comments', 'TO BE FILED IN BY API MANUFACTURER', False),
    ('Comments', 'TO BE FILED IN BY CUSTOMER IF ANY COMMENTS NEED TO BE MADE', False),
    ('Comments', 'Example: TO BE FILED IN BY API MANUFACTURER IF ANY COMMENTS NEED TO BE MADE', False),
    ('Comments', 'TO BE FILED IN BY API MANUFACTURER IF ANY COMMENTS NEED TO BE MADE?', False),
    ('Production output', 'TO BE FILED IN BY API MANUFACTURER IF ANY COMMENTS NEED TO BE MADE', False),
    ('Comments', 'TO BE FILED IN BY API MANUFACTURER IF ANY COMMENTS NEED TO BE MADE', True),
])
def test_filed_spelling_requires_explicit_local_comment_instruction(label, instruction, other_row):
    blocks, field = fixture(label=label)
    local = source_block('filed-instruction', instruction, 64, row=3 if other_row else 2, column=3)
    blocks.append(local)
    field['instruction_refs'] = [{'block_id': local['block_id'], 'start': 0, 'end': len(instruction)}]
    form = definition(blocks, field)
    assert form['fill_responsibility'] != 'our_side'
    assert not form['auto_fill_eligible']


def test_rematch_upgrades_saved_source_ownership_without_reparse_or_overwriting_people(tmp_path, monkeypatch):
    import hashlib
    import importlib
    import json

    from docx import Document
    from fastapi.testclient import TestClient

    from questionnaire_structure_recognizer.form_positions import enrich_result_form_positions
    from questionnaire_structure_recognizer.parser_core import extract_document
    from questionnaire_structure_recognizer.source_ownership import OWNER_POLICY_VERSION
    from questionnaire_structure_recognizer.validation import validate_and_assemble
    from questionnaire_structure_recognizer.workflow import materialize_questionnaire

    api = importlib.import_module("questionnaire_structure_recognizer.api.app")
    source = tmp_path / "FICTIONAL-owner-upgrade.docx"
    document = Document()
    document.add_paragraph("Site information form")
    document.add_paragraph("This questionnaire is to be completed by the supplier.")
    document.add_heading("Production facilities", 1)
    table = document.add_table(rows=5, cols=2)
    rows = [("Question", "Answer"), ("Alpha response", ""), ("Beta response", ""),
            ("Gamma response", ""), ("Delta response", "ORIGINAL_FICTIONAL")]
    for index, (question, answer) in enumerate(rows):
        table.cell(index, 0).text = question
        table.cell(index, 1).text = answer
    document.save(source)
    original = source.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    result = validate_and_assemble(extract_document(source).model_dump(), [], {"provider": "native_fixture"})
    enrich_result_form_positions(result)
    assert len(result["questions"]) == 4
    app = api.create_app(tmp_path / "data", "test", auth_mode="key")
    store = app.state.store
    headers = {"X-QSR-Key": "test"}
    task_id = "fictional-owner-upgrade"
    (store.files / source.name).write_bytes(original)
    with store.connect() as db:
        db.execute("INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,purpose,metadata_json) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (task_id, source.name, source.name, digest, len(original), "ready", "fixture",
                    "questionnaire", "{}"))
        db.commit()
    store.save_version(task_id, result, "fixture")
    with TestClient(app) as client:
        for field in result["questions"]:
            knowledge = client.post("/api/knowledge/manual", headers=headers, json={
                "canonical_question": field["question_text_original"],
                "answer": "KB_" + field["question_text_original"].split()[0].upper(),
                "section": field.get("section_text_original", ""),
                "context": field["form_definition"].get("semantic_context", ""), "approved": True,
            })
            assert knowledge.status_code == 200, knowledge.text
        with store.connect() as db:
            materialize_questionnaire(db, store.task(task_id), result, {})
            records = {row["question_text"]: dict(row) for row in db.execute(
                "SELECT id,question_text,form_definition_json FROM questions WHERE task_id=?", (task_id,))}
            original_blocks = [tuple(row) for row in db.execute(
                "SELECT * FROM document_blocks WHERE task_id=? ORDER BY block_id", (task_id,))]
        beta_id = records["Beta response"]["id"]
        gamma_id = records["Gamma response"]["id"]
        manual = client.patch(f"/api/questionnaires/{task_id}/questions/{beta_id}",
                              headers=headers, json={"answer_text": "MANUAL_BETA", "notes": "keep my correction"})
        assert manual.status_code == 200, manual.text
        corrected = client.patch(f"/api/questionnaires/{task_id}/questions/{gamma_id}/fill-position",
                                 headers=headers, json={"action": "confirm_mapping", "fill_responsibility": "customer"})
        assert corrected.status_code == 200, corrected.text
        human_definition = corrected.json()["form_definition"]
        with store.connect() as db:
            # Simulate a real task saved by the previous policy. Keep the
            # original blocks, physical targets, versions and manual answer.
            for row in db.execute("SELECT id,form_definition_json FROM questions WHERE task_id=?", (task_id,)).fetchall():
                form = json.loads(row["form_definition_json"])
                if row["id"] == gamma_id:
                    continue
                form.update(fill_responsibility="unknown", auto_fill_eligible=False,
                            responsibility_policy_version="old-policy-without-source-inheritance")
                form["responsibility_source_refs"] = []
                db.execute("UPDATE questions SET form_definition_json=? WHERE id=?",
                           (json.dumps(form, ensure_ascii=False), row["id"]))
            db.execute("UPDATE answers SET answer_text='',origin='blank',manual_override=0,confirmed=0 "
                       "WHERE question_id IN (?,?)", (records["Alpha response"]["id"], gamma_id))
            db.commit()

        def forbidden_recognition(*args, **kwargs):
            raise AssertionError("Responsibility refresh must not invoke recognition, OCR or model inference")

        monkeypatch.setattr(api, "recognize", forbidden_recognition)
        response = client.post(f"/api/questionnaires/{task_id}/rematch", headers=headers)
        assert response.status_code == 200, response.text
        snapshot = client.get(f"/api/questionnaires/{task_id}", headers=headers).json()
        questions = {q["question_text"]: q for q in snapshot["questions"]}
        alpha = questions["Alpha response"]
        assert alpha["answer_text"] == "KB_ALPHA"
        assert alpha["origin"] == "reviewed_knowledge"
        assert alpha["form_definition"]["fill_responsibility"] == "our_side"
        assert alpha["form_definition"]["responsibility_policy_version"] == OWNER_POLICY_VERSION
        assert alpha["form_definition"]["responsibility_source_refs"]
        assert questions["Beta response"]["answer_text"] == "MANUAL_BETA"
        assert questions["Beta response"]["manual_override"]
        assert questions["Beta response"]["notes"] == "keep my correction"
        assert questions["Gamma response"]["answer_text"] == ""
        assert {key: value for key, value in questions["Gamma response"]["form_definition"].items()
                if key != "applicability_status"} == human_definition
        assert questions["Delta response"]["answer_text"] == "ORIGINAL_FICTIONAL"
        assert questions["Delta response"]["origin"] == "source_document"
        for name, question in questions.items():
            previous_form = json.loads(records[name]["form_definition_json"])
            assert question["id"] == records[name]["id"]
            assert question["form_definition"]["fill_targets"] == previous_form["fill_targets"]
            assert question["form_definition"]["label_refs"] == previous_form["label_refs"]
        values = {name: (q["answer_text"], q["origin"], q["manual_override"], q["form_definition"])
                  for name, q in questions.items()}
        again = client.post(f"/api/questionnaires/{task_id}/rematch", headers=headers)
        assert again.status_code == 200, again.text
        snapshot2 = client.get(f"/api/questionnaires/{task_id}", headers=headers).json()
        assert values == {q["question_text"]: (q["answer_text"], q["origin"], q["manual_override"], q["form_definition"])
                          for q in snapshot2["questions"]}
        with store.connect() as db:
            assert original_blocks == [tuple(row) for row in db.execute(
                "SELECT * FROM document_blocks WHERE task_id=? ORDER BY block_id", (task_id,))]
            assert json.loads(db.execute("SELECT form_definition_json FROM questions WHERE id=?",
                                         (gamma_id,)).fetchone()[0]) == human_definition
    assert source.read_bytes() == original
    assert (store.files / source.name).read_bytes() == original
