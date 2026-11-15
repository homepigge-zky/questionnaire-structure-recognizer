"""Explicit, source-bound transcription reviews applied before structural analysis.

This is an audited correction to an OCR reading, not an OCR confidence bypass,
an answer generator or an approval for knowledge reuse. Native extraction cache
objects are never mutated. Model references must be regenerated from the returned
blocks: references into the previous text version must not be reused.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import math
import re


VERSION = "source-text-review-v1-source-bound-transcription"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_DERIVED_RANGES = {
    "logical_slots", "protected_label_ranges", "label_list", "native_text_spans",
    "section_path_block_ids", "resolved_layout_role", "ocr_answer_region",
    "source_text_review",
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf8")).hexdigest()


def _sha(value, name):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(f"{name} must be a SHA256 fingerprint")
    return value


def _block_identity(block):
    return {key: deepcopy(block.get(key)) for key in ("block_id", "kind", "text", "location")}


def _lookup(blocks):
    if not isinstance(blocks, list):
        raise ValueError("Source blocks must be a list")
    by_id = {}
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("block_id"), str):
            raise ValueError("Invalid source block identity")
        if block["block_id"] in by_id:
            raise ValueError("Duplicate source block identity")
        by_id[block["block_id"]] = block
    return by_id


def _editable(block):
    loc = block.get("location") or {}
    bbox = loc.get("bbox")
    if (block.get("kind") != "ocr_text" or loc.get("type") != "pdf"
            or loc.get("extraction") != "ocr"
            or not (block.get("structure") or {}).get("source_text_is_ocr")
            or block.get("controls")):
        raise ValueError("Only located OCR text is editable; regions and controls need separate review")
    if (type(loc.get("page")) is not int or loc["page"] < 1 or not isinstance(bbox, list)
            or len(bbox) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in bbox)
            or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
        raise ValueError("OCR text needs a valid page and physical bounding box")
    original = (block.get("structure") or {}).get("source_text_review") or {}
    if (not isinstance(block.get("text"), str)
            or (not block["text"] and not original.get("original_extracted_text"))):
        raise ValueError("Empty regions cannot be converted into guessed text")


def _validate_evidence(evidence):
    if (not isinstance(evidence, dict) or set(evidence) != {"method", "note", "pages"}
            or evidence.get("method") != "source_image_visual_review"
            or not isinstance(evidence.get("note"), str) or not evidence["note"].strip()
            or len(evidence["note"]) > 8000 or not isinstance(evidence.get("pages"), list)):
        raise ValueError("Explicit visual source review evidence is required")
    seen = set()
    for page in evidence["pages"]:
        if (not isinstance(page, dict) or set(page) != {"page", "image_sha256"}
                or type(page.get("page")) is not int or page["page"] < 1 or page["page"] in seen):
            raise ValueError("Review images need unique page identities")
        _sha(page.get("image_sha256"), "Reviewed image")
        seen.add(page["page"])
    if not seen:
        raise ValueError("At least one reviewed source image is required")
    return seen


def create_source_text_review(source_hash, source_blocks, edits, *, reviewer, evidence):
    """Build an immutable review from a server-supplied source version.

    ``edits`` contains block_id, before, after only. Evidence images must have
    been read/rendered by the authenticated review flow; the API must not accept
    arbitrary local image paths. It supplies its actor as ``reviewer``.
    """
    _sha(source_hash, "Original source")
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 200:
        raise ValueError("A review actor is required")
    pages = _validate_evidence(evidence)
    lookup = _lookup(source_blocks)
    if not isinstance(edits, list) or not 1 <= len(edits) <= 1000:
        raise ValueError("A review needs between 1 and 1000 edits")
    reviewed, seen = [], set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"block_id", "before", "after"}:
            raise ValueError("Each text edit requires block_id, before and after only")
        block_id = edit["block_id"]
        if not isinstance(block_id, str) or block_id in seen or block_id not in lookup:
            raise ValueError("Unknown or duplicated edited source block")
        seen.add(block_id)
        block = lookup[block_id]
        _editable(block)
        if block["location"]["page"] not in pages:
            raise ValueError("Edited block has no reviewed source-page evidence")
        if not isinstance(edit["before"], str) or edit["before"] != block["text"]:
            raise ValueError("Source text has changed; review its current version")
        after = edit["after"]
        if not isinstance(after, str) or len(after) > 20000 or "\x00" in after:
            raise ValueError("Invalid reviewed transcription")
        if after == edit["before"]:
            raise ValueError("Unchanged text is not a correction")
        reviewed.append({**deepcopy(edit), "location": deepcopy(block["location"]),
                         "before_block_sha256": _digest(_block_identity(block))})
    record = {"schema_version": VERSION, "source_hash": source_hash, "reviewer": reviewer,
              "created_at": datetime.now(timezone.utc).isoformat(), "evidence": deepcopy(evidence),
              "edits": reviewed, "scope": "reviewed_ocr_transcription_only",
              "requires_structure_reanalysis": True, "knowledge_approval": False}
    record["review_digest"] = _digest(record)
    return record


def _map_range(before, after, start, end, *, region=False):
    if type(start) is not int or type(end) is not int:
        raise ValueError("Source reference offsets are not integers")
    if start == end == 0 and region:
        return 0, 0  # A physical-region identity, never an existing text value.
    if not 0 <= start < end <= len(before):
        raise ValueError("Source value reference is empty or outside its original text")
    if start == 0 and end == len(before) and after:
        return 0, len(after)
    fragment = before[start:end]
    # A unique, literally unchanged substring in one equal sequence is safe to
    # shift. A range crossing a correction is not guessed from nearby text.
    if before.count(fragment) != 1 or after.count(fragment) != 1:
        raise ValueError("Partial source reference cannot be uniquely preserved")
    for tag, a, b, c, _ in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag == 'equal' and a <= start < end <= b:
            return c + start - a, c + end - a
    raise ValueError("Partial source reference crosses changed text and requires reassociation")


def _rebase_source_relations(blocks, edits, digest):
    """Repair pre-analysis geometry references, not previous model output.

    Invalidating the complete owning relation prevents a remaining half-answer
    from being mistaken for the original complete answer. Raw cache is retained
    separately; this only affects the deep-copied new extraction version.
    """
    changes = {edit['block_id']: edit for edit in edits}
    counts = {'checked': 0, 'rebased': 0, 'invalidated': []}

    def visit(value, path):
        if isinstance(value, dict):
            ref_id = value.get('block_id')
            if isinstance(ref_id, str) and ref_id in changes and ('start' in value or 'end' in value):
                counts['checked'] += 1
                edit = changes[ref_id]
                region = any('region' in component for component in path) and not any('value' in component for component in path)
                start, end = _map_range(edit['before'], edit['after'], value.get('start'), value.get('end'), region=region)
                if (start, end) != (value.get('start'), value.get('end')):
                    counts['rebased'] += 1
                value['start'], value['end'] = start, end
                return
            for key, child in value.items():
                visit(child, path + (str(key),))
        elif isinstance(value, list):
            for child in value:
                visit(child, path)

    for block in blocks:
        structure = block.setdefault('structure', {})
        for key in list(structure):
            if key.startswith('source_text_review'):
                continue
            relation = deepcopy(structure[key])
            try:
                visit(relation, (key,))
            except ValueError as exc:
                details = {'source_block_id': block['block_id'], 'relation': key,
                           'reason': str(exc), 'review_digest': digest}
                structure.pop(key)
                structure['source_text_review_requires_reassociation'] = True
                structure.setdefault('source_text_review_invalidated_relations', []).append(details)
                counts['invalidated'].append(details)
            else:
                structure[key] = relation
        # A native control must never be silently discarded or have its selected
        # state guessed just because a referenced text label was corrected.
        controls = deepcopy(block.get('controls') or [])
        try:
            visit(controls, ('controls',))
        except ValueError as exc:
            raise ValueError('A native control has a changed text range; separate control review is required') from exc
        if 'controls' in block:
            block['controls'] = controls
    return counts


def apply_source_text_reviews(source_hash, source_blocks, reviews):
    """Return corrected deep copies plus audit; never mutate raw extraction.

    Reviews are applied in saved order and fail closed on stale source, changed
    position or changed text. The digest in the audit must participate in every
    downstream structural/model cache key. An error invalidates the whole review
    application instead of silently applying a subset.
    """
    _sha(source_hash, "Original source")
    if not isinstance(reviews, list):
        raise ValueError("Saved source reviews must be a list")
    result = deepcopy(source_blocks)
    lookup = _lookup(result)
    expected_keys = {"schema_version", "source_hash", "reviewer", "created_at", "evidence", "edits",
                     "scope", "requires_structure_reanalysis", "knowledge_approval", "review_digest"}
    digests, edited = [], set()
    reference_audits = []
    for review in reviews:
        if (not isinstance(review, dict) or set(review) != expected_keys
                or review.get("schema_version") != VERSION or review.get("source_hash") != source_hash
                or review.get("scope") != "reviewed_ocr_transcription_only"
                or review.get("requires_structure_reanalysis") is not True
                or review.get("knowledge_approval") is not False):
            raise ValueError("Unsupported source review or different original document")
        digest = _sha(review.get("review_digest"), "Review")
        if digest != _digest({k: v for k, v in review.items() if k != "review_digest"}) or digest in digests:
            raise ValueError("Saved review was changed or repeated")
        # Re-run all public-request checks on this current source version.
        if (not isinstance(review["edits"], list) or not 1 <= len(review["edits"]) <= 1000
                or any(not isinstance(edit, dict) or set(edit) !=
                       {"block_id", "before", "after", "location", "before_block_sha256"}
                       for edit in review["edits"])):
            raise ValueError("Invalid saved text edits")
        sanitized = [{k: e[k] for k in ("block_id", "before", "after")} for e in review["edits"]]
        create_source_text_review(source_hash, result, sanitized, reviewer=review["reviewer"], evidence=review["evidence"])
        # Verify every edit before re-associating a different block's metadata.
        for edit in review["edits"]:
            if set(edit) != {"block_id", "before", "after", "location", "before_block_sha256"}:
                raise ValueError("Invalid saved text edit")
            block = lookup[edit["block_id"]]
            if (edit["location"] != block["location"]
                    or edit["before_block_sha256"] != _digest(_block_identity(block))):
                raise ValueError("Reviewed source position or text version changed")
        reference_audits.append(_rebase_source_relations(result, review['edits'], digest))
        for edit in review['edits']:
            block = lookup[edit['block_id']]
            previous = deepcopy((block.get("structure") or {}).get("source_text_review"))
            original_text = previous["original_extracted_text"] if previous else block["text"]
            for key in _DERIVED_RANGES:
                block.setdefault("structure", {}).pop(key, None)
            block["text"] = edit["after"]
            block["structure"]["source_text_review"] = {
                "version": VERSION, "source_hash": source_hash, "original_extracted_text": original_text,
                "before": edit["before"], "review_digest": digest, "reviewer": review["reviewer"],
                "review_chain": (previous.get("review_chain", []) if previous else []) + [digest],
                "source_image_sha256": next(p["image_sha256"] for p in review["evidence"]["pages"]
                                             if p["page"] == block["location"]["page"]),
                "requires_structure_reanalysis": True, "knowledge_approval": False,
                "before_block_sha256": edit["before_block_sha256"],
                "current_block_sha256": _digest(_block_identity(block)),
            }
            proof = block["structure"]["source_text_review"]
            proof["proof_digest"] = _digest(proof)
            edited.add(edit["block_id"])
        digests.append(digest)
    audit = {"version": VERSION, "source_hash": source_hash, "review_digests": digests,
             "review_digest": _digest({"version": VERSION, "source_hash": source_hash, "reviews": digests}),
             "reviewed_block_ids": sorted(edited), "reviewed_blocks": len(edited),
             "original_extraction_unchanged": True, "requires_structure_reanalysis": bool(reviews),
             "reference_rebase_version": "reviewed-source-references-v1-full-or-unique-literal-span",
             "checked_source_references": sum(a['checked'] for a in reference_audits),
             "rebased_source_references": sum(a['rebased'] for a in reference_audits),
             "invalidated_source_relations": sum(len(a['invalidated']) for a in reference_audits),
             "invalidated_relation_details": [item for a in reference_audits for item in a['invalidated']],
             "knowledge_approval": False}
    return result, audit


def verified_reviewed_text(block, ref=None):
    """Verify the source-bound proof produced by the trusted review application.

    This establishes only that the current text was explicitly checked against
    the source image. It does not establish pairing, responsibility or knowledge
    approval, and never changes the OCR confidence. Callers still validate those
    separately. Source blocks and proofs must be server-owned extraction data,
    never a model response or a client-supplied replacement for native blocks.
    """
    try:
        _editable(block)
        if not block['text']:
            return False  # A verified deletion supplies no existing answer value.
        proof = (block.get('structure') or {}).get('source_text_review') or {}
        if (proof.get('version') != VERSION or proof.get('knowledge_approval') is not False
                or proof.get('requires_structure_reanalysis') is not True
                or not isinstance(proof.get('reviewer'), str) or not proof['reviewer'].strip()
                or not isinstance(proof.get('before'), str)
                or not isinstance(proof.get('original_extracted_text'), str)
                or not proof['original_extracted_text']):
            return False
        for key in ('source_hash', 'review_digest', 'source_image_sha256',
                    'before_block_sha256', 'current_block_sha256', 'proof_digest'):
            _sha(proof.get(key), key)
        if proof['proof_digest'] != _digest({k: v for k, v in proof.items() if k != 'proof_digest'}):
            return False
        chain = proof.get('review_chain')
        if not isinstance(chain, list) or not chain or len(set(chain)) != len(chain) or chain[-1] != proof['review_digest']:
            return False
        for digest in chain:
            _sha(digest, 'Review chain')
        identity = _block_identity(block)
        if proof['current_block_sha256'] != _digest(identity):
            return False
        identity['text'] = proof['before']
        if proof['before_block_sha256'] != _digest(identity):
            return False
        if ref is not None:
            if not isinstance(ref, dict) or ref.get('block_id') != block['block_id']:
                return False
            start, end = ref.get('start'), ref.get('end')
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(block['text']):
                return False
        return True
    except (ValueError, KeyError, TypeError):
        return False
