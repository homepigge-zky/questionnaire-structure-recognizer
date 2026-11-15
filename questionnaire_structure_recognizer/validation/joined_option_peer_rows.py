"""Resolve a letter contour only when an adjacent printed row proves the layout."""
import re
from copy import deepcopy
from .field_boundaries import ref


def box(b):
    return b['location']['bbox']


def midpoint(b):
    b=box(b)
    return (b[1]+b[3]) / 2


def profile(row, text):
    if len(row) not in (2,3):
        return None
    y = sum(midpoint(b) for b in row) / len(row)
    captions = [b for b in text if abs(midpoint(b)-y)<=5 and re.fullmatch(r'\s*yes\s+no\s*', b['text'], re.I)
                and all(box(b)[0]-2<=box(s)[0] and box(s)[2]<=box(b)[2]+2 for s in row)]
    if len(captions)!=1:
        return None
    caption=captions[0]
    numbered=[]
    for candidate in text:
        if abs(midpoint(candidate)-y)>6 or box(candidate)[2]>box(caption)[0]-4:
            continue
        match=re.match(r'^\s*(?:(\d+(?:\.\d+)+)|([A-Z])\s*[-–]\s*(\d+))\s*(?=[^\d.])',candidate['text'])
        if match and len(candidate['text'][match.end():].strip())>5:
            number=tuple(int(i) for i in match[1].split('.')) if match[1] else (match[2],int(match[3]))
            numbered.append((candidate,number))
    return (caption,*numbered[0]) if len(numbered)==1 else None


def exclude_verified_caption_contours(groups,text):
    records=[(sorted(row,key=lambda b:box(b)[0]),profile(row,text)) for row in groups]
    excluded=set()
    for shapes, info in records:
        if len(shapes)!=3 or not info:
            continue
        caption,question,number=info
        decisions={}
        for pair, other in records:
            if len(pair)!=2 or not other:
                continue
            sample, peer, number2 = other
            if (number[:-1]!=number2[:-1] or abs(number[-1]-number2[-1])!=1
                    or abs(midpoint(question)-midpoint(peer))>116
                    or abs(box(question)[0]-box(peer)[0])>4
                    or any(abs(box(caption)[k]-box(sample)[k])>4 for k in (0,2))):
                continue
            matching=[]
            for guide in pair:
                candidates=[b for b in shapes if all(abs(box(guide)[k]-box(b)[k])<=2.5 for k in (0,2))
                            and abs((box(guide)[3]-box(guide)[1])-(box(b)[3]-box(b)[1]))<=2
                            and abs((midpoint(b)-midpoint(caption))-(midpoint(guide)-midpoint(sample)))<=2]
                if len(candidates)!=1:
                    break
                matching.append(candidates[0])
            if len(matching)!=2 or matching[0] is matching[1]:
                continue
            glyph,=[s for s in shapes if s not in matching]
            g, first, c=box(glyph),box(matching[0]),box(caption)
            if (not glyph.get('controls') or any(ctrl.get('selected') is not None for ctrl in glyph['controls'])
                    or glyph.get('structure',{}).get('native_selection_verified')
                    or not c[0]<=g[0]<g[2]<=first[0]-4 or not c[1]<=g[1]<g[3]<=c[3]
                    or g[2]-g[0]>.65*(first[2]-first[0]) or g[3]-g[1]>.65*(first[3]-first[1])):
                continue
            decisions[glyph['block_id']] = (glyph, matching, peer, pair)
        if len(decisions)==1:
            glyph, matching, peer, pair=next(iter(decisions.values()))
            proof={'question_ref':ref(question),'peer_question_ref':ref(peer),'caption_ref':ref(caption),
                   'region_ids':[b['block_id'] for b in matching],'peer_region_ids':[b['block_id'] for b in pair]}
            glyph.setdefault('structure',{}).update(resolved_layout_role='option_caption_glyph',scan_peer_caption_evidence=proof)
            for b in matching:b.setdefault('structure',{})['scan_numbered_peer_layout']=deepcopy(proof)
            excluded.add(glyph['block_id'])
    return excluded


def bind_verified_condition(field,lookup):
    regions=[lookup[r['block_id']] for r in field.get('answer_region_refs',[]) if r['block_id'] in lookup]
    if (len(regions)!=2 or not all(r.get('structure',{}).get('scan_numbered_peer_layout') for r in regions)
            or field.get('manual_override') or field.get('answer_origin') in {'manual','knowledge'}):
        return
    for child in field.get('children',[]):
        condition=child.get('explicit_condition') or {}
        if condition.get('parent_field_id')!=field['field_id'] or child.get('manual_override'):
            continue
        match=re.match(r'^\s*if\s*(yes|no)\s*[:：]', child.get('question_text_original',''),re.I)
        if not match:continue
        choices=[o for o in field.get('options',[]) if o['text_original'].strip().lower()==match[1].lower()]
        if len(choices)==1:
            condition.update(equals_option=choices[0]['text_original'],source_refs=deepcopy(child['label_refs']))


def verified_peer_choice(field, lookup):
    """Keep physical state only with the program's exact source association."""
    from .selection_evidence import selected_scan_caption
    options=field.get('options',[])
    if len(options)!=2:
        return False
    used=[]
    for index, option in enumerate(options):
        refs=option.get('refs',[])
        targets=option.get('answer_region_refs',[])
        if len(refs)!=1 or len(targets)!=1 or targets[0] not in field.get('answer_region_refs',[]):
            return False
        region=lookup.get(targets[0].get('block_id'),{})
        structure=region.get('structure',{})
        proof=structure.get('scan_numbered_peer_layout')
        if not proof or proof['question_ref'] not in field.get('label_refs',[]):
            return False
        caption=lookup.get(proof['caption_ref']['block_id'],{})
        spans=list(re.finditer(r'Yes|No',caption.get('text',''),re.I))
        if len(spans)!=2 or proof['region_ids'][index]!=region.get('block_id'):
            return False
        expected=ref(caption,spans[index].start(),spans[index].end())
        if refs[0]!=expected or refs[0] not in structure.get('scan_option_caption_refs',[]):
            return False
        controls=region.get('controls',[])
        if len(controls)!=1 or option.get('selected') is not controls[0].get('selected'):
            return False
        if option.get('selected') is True and not selected_scan_caption(refs[0],field,lookup):
            return False
        used.append(region['block_id'])
    return len(set(used))==2
