"""Reconstruct two typographic joins from source-native PDF text runs."""
from __future__ import annotations

import re
from copy import deepcopy

VERSION = 'native-overlay-value-typography-v1'
SUPERSCRIPT = str.maketrans('0123456789', '⁰¹²³⁴⁵⁶⁷⁸⁹')
SUBSCRIPT = str.maketrans('0123456789', '₀₁₂₃₄₅₆₇₈₉')


def _runs(block):
    location = block.get('location', {})
    if location.get('type') != 'pdf' or location.get('extraction') != 'native':
        return []
    runs = block.get('structure', {}).get('native_pdf_font_runs') or []
    if not runs:
        return []
    cursor = 0
    for run in runs:
        box = run.get('bbox') or []
        if (run.get('start') != cursor or not isinstance(run.get('end'), int)
                or run['end'] <= cursor or len(box) != 4 or box[3] <= box[1]
                or not run.get('font') or not isinstance(run.get('paint_seqno'), int)
                or not isinstance(run.get('baseline'), (float, int))):
            return []
        cursor = run['end']
    return runs if cursor == len(block.get('text', '')) else []


def _full_ref(block):
    return {'block_id': block['block_id'], 'start': 0, 'end': len(block['text']), 'slot_id': ''}


def _full_blocks(refs, lookup):
    blocks = []
    for ref in refs:
        block = lookup.get(ref.get('block_id'), {})
        if not _runs(block) or ref.get('start') != 0 or ref.get('end') != len(block['text']):
            return []
        blocks.append(block)
    return blocks


def _height(run):
    return run['bbox'][3] - run['bbox'][1]


def _script_join(prefix, suffix):
    """Confirm same baseline after the small, raised or lowered digit run."""
    body, tail = _runs(prefix)[-1], _runs(suffix)
    normal_height = _height(body)
    previous, fragments = body, []
    for run in tail:
        if (run['font'] != body['font'] or run['paint_seqno'] != previous['paint_seqno'] + 1
                or abs(run['bbox'][0] - previous['bbox'][2]) > .6):
            return None
        chars = suffix['text'][run['start']:run['end']]
        offset = (body['baseline'] - run['baseline']) / normal_height
        ratio = _height(run) / normal_height
        if .35 <= ratio <= .78 and re.fullmatch(r'[0-9]+', chars) and .18 <= abs(offset) <= .8:
            fragments.append(chars.translate(SUPERSCRIPT if offset > 0 else SUBSCRIPT))
        elif fragments and abs(offset) <= .08 and .92 <= ratio <= 1.08:
            fragments.append(chars)
        else:
            return None
        previous = run
    return prefix['text'] + ''.join(fragments)


def _has_obstruction(stop, region, last_run, lookup):
    """A new physical response or printed question between baselines owns its own line."""
    point = _runs(stop)[0]
    x, low, high = point['bbox'][0], last_run['baseline'] + 1, point['baseline']
    for other in lookup.values():
        loc = other.get('location', {})
        box = loc.get('bbox') or []
        if (other['block_id'] in {region['block_id'],stop['block_id']} or len(box)!=4
                or loc.get('page') != stop['location']['page'] or loc.get('type') != 'pdf'):
            continue
        structured = other.get('structure', {})
        is_region = structured.get('scan_region_kind') in {'write_line','text_box','checkbox'}
        printed = bool(other.get('text','').strip()) and loc.get('extraction') in {'ocr','ocr_region'}
        if not is_region and not printed:
            continue
        if low <= (box[1]+box[3])/2 <= high and box[0]-2 <= x <= box[2]+2:
            return True
        if printed and low <= (box[1]+box[3])/2 <= high and box[2] <= x and x-box[2] < 38:
            return True
    return False


def wraps_final_stop(stop, region, lookup):
    """Validate a wrapped full stop from glyphs and the already verified response."""
    if stop.get('text') not in {'.','。'} or region.get('structure',{}).get('scan_region_kind')!='write_line':
        return False
    runs = _runs(stop)
    if len(runs)!=1 or stop['location']['page'] != region['location']['page']:
        return False
    # A glyph already attached to another physical response cannot be moved.
    if stop.get('structure',{}).get('native_overlay_answer_region') not in (None,region['block_id']):
        return False
    previous_ids = [bid for bid in region.get('structure',{}).get('overlay_value_block_ids', [])
                    if bid != stop['block_id']]
    if len(previous_ids) != 1:
        return False
    block = lookup.get(previous_ids[0], {})
    before = _runs(block)
    if not before or not block['text'][-1].isalnum():
        return False
    from .pdf_source_relations import native_overlay_value_in_region
    if not native_overlay_value_in_region(_full_ref(block),[{'block_id':region['block_id']}],lookup):
        return False
    first, last, point = before[0], before[-1], runs[0]
    height = _height(last)
    return (last['font'] == point['font'] and point['paint_seqno'] == last['paint_seqno']+1
            and abs(_height(point)-height) <= .4
            and abs(first['bbox'][0]-point['bbox'][0]) <= .5
            and .9 <= (point['baseline']-last['baseline'])/height <= 1.35
            and not _has_obstruction(stop,region,last,lookup))


def verified_value(field, lookup):
    """Return a plain-text rendition only if every original reference still validates."""
    refs = field.get('answer_value_refs') or []
    regions = field.get('answer_region_refs') or []
    if len(refs)!=2 or len(regions)!=1 or field.get('options'):
        return None
    blocks = _full_blocks(refs,lookup)
    if len(blocks)!=2:
        return None
    from .pdf_source_relations import native_overlay_value_in_region
    if not all(native_overlay_value_in_region(ref,regions,lookup) for ref in refs):
        return None
    first, second = blocks
    region = lookup.get(regions[0]['block_id'],{})
    if wraps_final_stop(second,region,lookup):
        return first['text']+second['text']
    return _script_join(first,second)


def restore_native_value_typography(questions,lookup):
    owners = []
    def walk(items,guarded=False):
        for field in items:
            guarded_field = guarded or field.get('manual_override') or field.get('review_status')=='human_corrected' or any(
                field.get(k) in {'manual','knowledge','reviewed_knowledge'} for k in ('answer_origin','answer_source'))
            owners.append((field, bool(guarded_field)))
            walk(field.get('children') or [], guarded_field)
    walk(questions)
    from collections import Counter
    region_owners = Counter(r['block_id'] for f, _ in owners for r in f.get('answer_region_refs', []))
    retired, changes = set(), []
    stops = [b for b in lookup.values() if b.get('text') in {'.','。'} and _runs(b)]
    for field, guarded in owners:
        refs, regions = field.get('answer_value_refs',[]),field.get('answer_region_refs',[])
        if (guarded or field.get('answer_status')!='filled' or field.get('options') or len(regions)!=1
                or not refs or not _full_blocks(refs,lookup)):
            continue
        region = lookup.get(regions[0]['block_id'],{})
        if region_owners[regions[0]['block_id']] != 1 or region.get('structure',{}).get('scan_region_kind')!='write_line':
            continue
        if len(refs)==1:
            # Find an unambiguous punctuation-only model remnant, with no other field roles.
            possible=[b for b in stops if wraps_final_stop(b,region,lookup)]
            if len(possible)!=1:
                continue
            mark=possible[0];bid=mark['block_id']; consumed=[]
            for item, protected in owners:
                roles=[r.get('block_id') for key in ('question_refs','label_refs','answer_region_refs','answer_value_refs','context_refs','section_refs','instruction_refs','attachment_refs','unit_refs')
                       for r in item.get(key) or []]
                option_refs=[r.get('block_id') for o in item.get('options',[]) for r in o.get('refs',[])]
                if bid not in roles+option_refs:
                    continue
                if (protected or {r.get('block_id') for r in item.get('label_refs',[])}!={bid} or item.get('children') or item.get('answer_region_refs')
                        or item.get('answer_value_refs') or item.get('options') or item.get('boundary_evidence')):
                    consumed=None;break
                consumed.append(item['field_id'])
            if consumed is None:
                continue
            src=_full_ref(mark)
            s=region['structure'];previous=list(s['overlay_value_block_ids'])
            s['overlay_value_block_ids']=[*previous,bid]
            updated={**field,'answer_value_refs':[*refs,src]}
            rendered=verified_value(updated,lookup)
            if rendered is None:
                s['overlay_value_block_ids']=previous;continue
            field['answer_value_refs']=updated['answer_value_refs']
            field['answer_refs']=deepcopy(field['answer_value_refs'])
            retired.update(consumed)
            mark['structure']['resolved_layout_role']='answer_value'
            field.setdefault('source_locations',{})['answer']=[deepcopy(lookup[r['block_id']]['location']) for r in field['answer_value_refs']]
        else:
            rendered=verified_value(field,lookup)
            if rendered is None:
                continue
        field['answer_text_original']=rendered
        field['native_value_typography']=VERSION
        changes.append(field['field_id'])
    def discard(items):
        items[:]=[f for f in items if f['field_id'] not in retired]
        for f in items:
            discard(f.get('children',[]))
    discard(questions)
    return {'version':VERSION,'fields':changes,'retired_punctuation_fields':sorted(retired)}
