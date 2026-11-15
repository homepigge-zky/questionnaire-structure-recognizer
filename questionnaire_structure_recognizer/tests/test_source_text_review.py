from copy import deepcopy
import pytest

from questionnaire_structure_recognizer.source_text_review import (
    apply_source_text_reviews, create_source_text_review, _digest,
)


SOURCE = '1' * 64


def blocks():
    return [{"block_id": "src-label", "kind": "ocr_text", "text": "公司名籍 CompanyName",
             "location": {"type": "pdf", "extraction": "ocr", "page": 1, "bbox": [1, 2, 90, 20]},
             "structure": {"source_text_is_ocr": True, "logical_slots": [{"start": 0, "end": 5}],
                           "protected_label_ranges": [{"start": 0, "end": 5}]},
             "controls": []},
            {"block_id": "src-blank", "kind": "ocr_text", "text": "",
             "location": {"type": "pdf", "extraction": "ocr", "page": 1, "bbox": [90, 2, 190, 20]},
             "structure": {"scan_candidate": True, "blank_state": "blank"}, "controls": []}]


def evidence():
    return {"method": "source_image_visual_review", "note": "Original local page opened and checked.",
            "pages": [{"page": 1, "image_sha256": '2' * 64}]}


def review(items=None):
    items = items or blocks()
    return create_source_text_review(SOURCE, items, [{"block_id": "src-label", "before": items[0]['text'],
                                      "after": "公司名稱 Company Name"}], reviewer="local-user", evidence=evidence())


def test_review_creates_new_text_version_and_preserves_physical_region_and_raw_extraction():
    original = blocks()
    saved = deepcopy(original)
    r = review(original)
    actual, audit = apply_source_text_reviews(SOURCE, original, [r])
    assert original == saved
    assert actual[0]['text'] == '公司名稱 Company Name'
    assert actual[0]['block_id'] == original[0]['block_id']
    assert actual[0]['location'] == original[0]['location']
    assert actual[1] == original[1]
    assert 'logical_slots' not in actual[0]['structure']
    assert 'protected_label_ranges' not in actual[0]['structure']
    provenance = actual[0]['structure']['source_text_review']
    assert provenance['original_extracted_text'] == original[0]['text']
    assert provenance['knowledge_approval'] is False
    assert audit['requires_structure_reanalysis'] and audit['reviewed_blocks'] == 1


@pytest.mark.parametrize('change', ['text', 'position', 'source', 'record', 'duplicate'])
def test_stale_or_tampered_reviews_fail_without_mutation(change):
    original = blocks()
    r = review(original)
    source = SOURCE
    reviews = [r]
    if change == 'text': original[0]['text'] += ' changed'
    elif change == 'position': original[0]['location']['bbox'][0] += 1
    elif change == 'source': source = '3' * 64
    elif change == 'record': r['edits'][0]['after'] = 'Invented changed text'
    elif change == 'duplicate': reviews.append(r)
    before = deepcopy(original)
    with pytest.raises(ValueError):
        apply_source_text_reviews(source, original, reviews)
    assert original == before


def test_new_review_of_corrected_text_retains_original_and_chain_and_invalidates_cache_digest():
    original = blocks()
    first = review(original)
    once, first_audit = apply_source_text_reviews(SOURCE, original, [first])
    second = create_source_text_review(SOURCE, once, [{"block_id": "src-label", "before": once[0]['text'],
                                      "after": "公司名稱 Company Name:"}], reviewer="second-reviewer", evidence=evidence())
    twice, audit = apply_source_text_reviews(SOURCE, original, [first, second])
    assert twice[0]['structure']['source_text_review']['original_extracted_text'] == original[0]['text']
    assert len(twice[0]['structure']['source_text_review']['review_chain']) == 2
    assert audit['review_digest'] != first_audit['review_digest']


@pytest.mark.parametrize('invalid', ['blank', 'control', 'native', 'missing_page', 'no_actor', 'wrong_before', 'duplicate_edit'])
def test_correction_cannot_create_values_in_blank_regions_or_change_control_state(invalid):
    original = blocks()
    edits = [{"block_id": "src-label", "before": original[0]['text'], "after": "公司名稱 Company Name"}]
    actor, proof = 'local-user', evidence()
    if invalid == 'blank': edits = [{"block_id": "src-blank", "before": "", "after": "INVENTED ANSWER"}]
    elif invalid == 'control': original[0]['controls'] = [{'type': 'checkbox', 'selected': None}]
    elif invalid == 'native': original[0]['location']['extraction'] = 'native'
    elif invalid == 'missing_page': proof['pages'][0]['page'] = 2
    elif invalid == 'no_actor': actor = ''
    elif invalid == 'wrong_before': edits[0]['before'] = 'other'
    elif invalid == 'duplicate_edit': edits.append(deepcopy(edits[0]))
    with pytest.raises(ValueError):
        create_source_text_review(SOURCE, original, edits, reviewer=actor, evidence=proof)


def test_no_review_is_deep_copy_and_never_auto_approval():
    original = blocks()
    result, audit = apply_source_text_reviews(SOURCE, original, [])
    assert result == original and result is not original
    assert audit['requires_structure_reanalysis'] is False
    assert audit['knowledge_approval'] is False and audit['reviewed_blocks'] == 0


@pytest.mark.parametrize('bad', [None, [{}], [{'block_id': 'src-label'}]])
def test_malformed_persisted_edit_records_have_a_controlled_validation_error(bad):
    r = review()
    r['edits'] = bad
    r['review_digest'] = _digest({k:v for k,v in r.items() if k != 'review_digest'})
    with pytest.raises(ValueError):
        apply_source_text_reviews(SOURCE, blocks(), [r])


def test_explicit_removed_ocr_can_be_restored_without_turning_physical_blanks_into_values():
    original = blocks()
    first = create_source_text_review(SOURCE, original,
        [{'block_id':'src-label','before':original[0]['text'],'after':''}], reviewer='local-user', evidence=evidence())
    once, _ = apply_source_text_reviews(SOURCE, original, [first])
    second = create_source_text_review(SOURCE, once,
        [{'block_id':'src-label','before':'','after':'公司名稱'}], reviewer='local-user', evidence=evidence())
    twice, _ = apply_source_text_reviews(SOURCE, original, [first, second])
    assert twice[0]['text'] == '公司名稱'
    assert twice[0]['structure']['source_text_review']['original_extracted_text'] == original[0]['text']
    assert twice[1] == original[1]


def test_cross_block_full_value_ref_is_rebased_without_old_length_truncation():
    original = blocks()
    original[1]['structure']['ocr_value_refs'] = [{'block_id':'src-label','start':0,'end':len(original[0]['text'])}]
    result, audit = apply_source_text_reviews(SOURCE, original, [review(original)])
    assert result[1]['structure']['ocr_value_refs'] == [{'block_id':'src-label','start':0,'end':len(result[0]['text'])}]
    assert audit['rebased_source_references'] == 1


def test_cross_block_partial_ref_into_changed_characters_is_invalidated_not_moved_to_nearby_text():
    original = blocks()
    original[1]['structure']['ocr_value_refs'] = [{'block_id':'src-label','start':2,'end':4}]
    result, audit = apply_source_text_reviews(SOURCE, original, [review(original)])
    assert 'ocr_value_refs' not in result[1]['structure']
    assert result[1]['structure']['source_text_review_requires_reassociation'] is True
    assert audit['invalidated_source_relations'] == 1


def test_cross_block_unique_unchanged_partial_ref_tracks_its_exact_literal_range():
    original = blocks()
    original[0]['text'] = 'Fax: 01234'
    original[1]['structure']['ocr_value_refs'] = [{'block_id':'src-label','start':5,'end':10}]
    r = create_source_text_review(SOURCE, original, [{'block_id':'src-label','before':'Fax: 01234','after':'Fax number: 01234'}],
                                  reviewer='local-user',evidence=evidence())
    result, audit = apply_source_text_reviews(SOURCE, original, [r])
    ref=result[1]['structure']['ocr_value_refs'][0]
    assert ref['start']==12 and ref['end']==17
    assert result[0]['text'][ref['start']:ref['end']]=='01234'


def test_zero_length_value_ref_never_becomes_whole_corrected_text():
    original = blocks()
    original[1]['structure']['ocr_value_refs'] = [{'block_id':'src-label','start':0,'end':0}]
    result, audit = apply_source_text_reviews(SOURCE, original, [review(original)])
    assert 'ocr_value_refs' not in result[1]['structure']
    assert audit['invalidated_source_relations'] == 1


def test_verified_reviewed_text_is_source_bound_and_does_not_change_ocr_confidence():
    from questionnaire_structure_recognizer.source_text_review import verified_reviewed_text
    original = blocks()
    original[0]['structure']['ocr_confidence'] = 0.72
    actual, _ = apply_source_text_reviews(SOURCE, original, [review(original)])
    block = actual[0]
    assert verified_reviewed_text(block)
    assert block['structure']['ocr_confidence'] == 0.72
    assert verified_reviewed_text(block, {'block_id':block['block_id'], 'start':0, 'end':2})
    assert not verified_reviewed_text(block, {'block_id':block['block_id'], 'start':0, 'end':0})
    assert not verified_reviewed_text(block, {'block_id':'another', 'start':0, 'end':2})
    assert not verified_reviewed_text(original[0])


@pytest.mark.parametrize('changed', ['text','location','kind','block_id','before','source_hash','review_digest','reviewer','image','proof','approval'])
def test_reviewed_text_proof_rejects_drift_and_client_flags(changed):
    from questionnaire_structure_recognizer.source_text_review import verified_reviewed_text
    actual, _ = apply_source_text_reviews(SOURCE, blocks(), [review()])
    block = actual[0]
    proof = block['structure']['source_text_review']
    if changed in ('text','kind','block_id'): block[changed] += 'changed'
    elif changed == 'location': block['location']['bbox'][0] += 0.1
    elif changed == 'before': proof['before'] += 'changed'
    elif changed == 'source_hash': proof['source_hash'] = '3'*64
    elif changed == 'review_digest': proof['review_digest'] = '3'*64
    elif changed == 'reviewer': proof['reviewer'] = 'different'
    elif changed == 'image': proof['source_image_sha256'] = '3'*64
    elif changed == 'proof': proof['proof_digest'] = '3'*64
    elif changed == 'approval': proof['knowledge_approval'] = True
    assert not verified_reviewed_text(block)


def test_reviewed_transcription_proof_tracks_latest_review_and_cannot_answer_an_empty_deletion():
    from questionnaire_structure_recognizer.source_text_review import verified_reviewed_text
    once, _ = apply_source_text_reviews(SOURCE, blocks(), [review()])
    second = create_source_text_review(SOURCE, once,
        [{'block_id':'src-label','before':once[0]['text'],'after':''}], reviewer='reviewer',evidence=evidence())
    twice, _ = apply_source_text_reviews(SOURCE, blocks(), [review(), second])
    assert not verified_reviewed_text(twice[0])
