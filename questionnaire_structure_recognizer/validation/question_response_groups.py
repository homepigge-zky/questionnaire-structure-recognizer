"""Bound explicit response groups without interpreting contours inside prose.

A raster region inside a question is retained as undetermined source content.
It must not become an extra option merely because its baseline matches two
independently captioned response boxes. Size alone is never an exclusion.
"""
from copy import deepcopy
import re


def _box(block):return block['location']['bbox']
def _cy(block):
    b=_box(block);return (b[1]+b[3])/2


def bounded_question_response_rows(groups, decoded, text_blocks, inline_labels):
    output=[]
    for original in groups:
        working=dict(decoded)
        physical=[b for b in original if len(b.get('controls',[]))==1
            and b['controls'][0].get('selected') in (True,False)
            and b['controls'][0].get('selected') is not None
            and b.get('structure',{}).get('blank_state') in {'blank','filled'}]
        physical.sort(key=lambda b:_box(b)[0])
        # OCR may join the entire question and the two terminal captions in
        # one source line. Its right-hand physical pair, terminal word order,
        # shared baseline and exact source containment establish a local group.
        # A third independently observed box prevents this interpretation.
        if len(physical)==2 and any(b['block_id'] not in decoded for b in physical):
            candidates=[]
            for source in text_blocks:
                match=re.fullmatch(r'(.+[?？])\s*(Yes)\s*[□☐口]*\s*(No)\s*[□☐口]*\s*',source.get('text',''),re.I)
                sb=_box(source)
                if not match or abs(_cy(source)-sum(_cy(b) for b in physical)/2)>7:continue
                if (all(sb[0]<=_box(b)[0]<_box(b)[2]<=sb[2]+2 for b in physical)
                    and 0<=sb[2]-_box(physical[-1])[2]<12
                    and 12<_box(physical[-1])[0]-_box(physical[0])[2]<65
                    and _box(physical[0])[0]>sb[0]+.7*(sb[2]-sb[0])):
                    candidates.append((source,match))
            if len(candidates)==1:
                source,match=candidates[0]
                for b,group in zip(physical,(2,3)):
                    working[b['block_id']]=(source,{'block_id':source['block_id'],'start':match.start(group),'end':match.end(group),'slot_id':''})
        known=[b for b in physical if b['block_id'] in working]
        known.sort(key=lambda b:_box(b)[0])
        if len(known) not in (2,3) or len(known)==len(original):
            output.append(original);continue
        captions=[working[b['block_id']] for b in known]
        words=[source['text'][r['start']:r['end']].strip().casefold() for source,r in captions]
        if len(set(words))!=len(words) or set(words) not in ({'yes','no'},{'yes','no','n/a'}):
            output.append(original);continue
        # A source question must have a terminal explicit choice suffix. A
        # condition such as "If Yes" inside ordinary prose does not qualify.
        questions={}
        for source,_ in captions:
            refs=sorted([r for b,r in captions if b['block_id']==source['block_id']],key=lambda r:r['start'])
            first=refs[0]['start'];prefix=source['text'][:first].rstrip()
            suffix=source['text'][first:]
            for r in reversed(refs):suffix=suffix[:r['start']-first]+suffix[r['end']-first:]
            if (len(prefix)>12 and re.search(r'[?？]',prefix) and
                    not suffix.strip(' \t\r\n:;□☐O口()/|') and
                    not re.match(r'^\s*if\s+(?:yes|no)\b',prefix,re.I)):
                questions[source['block_id']]=(source,{'block_id':source['block_id'],'start':0,'end':len(prefix),'slot_id':''})
        if len(questions)!=1:
            output.append(original);continue
        question,question_ref=next(iter(questions.values()));qb=_box(question)
        width=min(_box(b)[2]-_box(b)[0] for b in known)
        height=min(_box(b)[3]-_box(b)[1] for b in known)
        left=min(_box(b)[0] for b in known)
        if any(abs(_cy(b)-_cy(question))>7 or
               not .75<=(_box(b)[3]-_box(b)[1])/height<=1.33 for b in known):
            output.append(original);continue
        extras=[b for b in original if b not in known]
        valid=True
        for b in extras:
            bb=_box(b)
            # Keep any marked, full-sized, independent or response-lane region
            # in the unresolved group. This only scopes smaller ambiguous
            # contours enclosed by this source question's printed line.
            if (not b.get('controls') or any(c.get('selected') is not None for c in b['controls'])
                or b.get('structure',{}).get('native_selection_verified')
                or not (qb[0]<=bb[0]<bb[2]<=min(qb[2],left-8)
                        and qb[1]-1<=bb[1]<bb[3]<=qb[3]+1)
                or bb[2]-bb[0]>.75*width or bb[3]-bb[1]>.75*height):
                valid=False;break
        if not valid:
            output.append(original);continue
        proof={'kind':'bounded_explicit_response_group','question_ref':deepcopy(question_ref),
            'question_text':question['text'],'question_bbox':deepcopy(qb),
            'page':question['location'].get('page'),
            'response_region_ids':[b['block_id'] for b in known],
            'response_region_bboxes':[deepcopy(_box(b)) for b in known],
            'caption_refs':[deepcopy(r) for _,r in captions],
            'caption_texts':[source['text'] for source,_ in captions],
            'caption_bboxes':[deepcopy(_box(source)) for source,_ in captions],
            'undetermined_source_contour_ids':[b['block_id'] for b in extras]}
        for b in known:b.setdefault('structure',{})['bounded_question_response_group']=deepcopy(proof)
        decoded.update({b['block_id']:working[b['block_id']] for b in known})
        inline_labels[known[0]['block_id']]=(question,deepcopy(question_ref))
        for b in extras:
            b.setdefault('structure',{})['scan_question_contour_evidence']={
                **deepcopy(proof),'status':'undetermined_source_content',
                'action':'excluded_from_this_response_group_only',
                'reason':'候选位于题干文字区域内，未判定其用途；保留原始图像位置，不作为右侧已定位选项。'}
        output.append(known)
    from .separate_response_groups import scope_printed_response_groups
    return scope_printed_response_groups(output,decoded,text_blocks,inline_labels)


def verified_bounded_response(field, lookup):
    """Later normalizers cannot replace exact physical boxes by caption text."""
    from .separate_response_groups import verified_separate_response
    if verified_separate_response(field,lookup):return True
    from .selection_evidence import selected_scan_caption
    options=field.get('options',[])
    if len(options) not in (2,3):return False
    seen=[]
    for index,option in enumerate(options):
        refs=option.get('refs',[]);targets=option.get('answer_region_refs',[])
        if len(refs)!=1 or len(targets)!=1:return False
        target=lookup.get(targets[0].get('block_id'),{});s=target.get('structure',{})
        proof=s.get('bounded_question_response_group') or {}
        if proof.get('kind')!='bounded_explicit_response_group':return False
        ids=proof.get('response_region_ids',[]);bbs=proof.get('response_region_bboxes',[])
        captions=proof.get('caption_refs',[]);texts=proof.get('caption_texts',[]);caption_bboxes=proof.get('caption_bboxes',[])
        if not all(len(x)==len(options) for x in (ids,bbs,captions,texts,caption_bboxes)):return False
        if targets[0] not in field.get('answer_region_refs',[]) or ids[index]!=target.get('block_id') or bbs[index]!=_box(target):return False
        if target.get('location',{}).get('page')!=proof.get('page'):return False
        question=lookup.get(proof.get('question_ref',{}).get('block_id'),{})
        if (proof.get('question_ref') not in field.get('label_refs',[]) or
                question.get('text')!=proof.get('question_text') or
                question.get('location',{}).get('page')!=proof.get('page') or
                question.get('location',{}).get('bbox')!=proof.get('question_bbox')):return False
        caption=lookup.get(refs[0].get('block_id'),{});a,z=refs[0].get('start'),refs[0].get('end')
        if (refs[0]!=captions[index] or caption.get('text')!=texts[index] or
                caption.get('location',{}).get('bbox')!=caption_bboxes[index] or
                caption.get('location',{}).get('page')!=proof.get('page') or
                not isinstance(a,int) or not isinstance(z,int) or not 0<=a<z<=len(caption.get('text','')) or
                caption['text'][a:z]!=option.get('text_original') or
                refs[0] not in s.get('scan_option_caption_refs',[])):return False
        controls=target.get('controls',[])
        if len(controls)!=1 or option.get('selected') is not controls[0].get('selected'):return False
        if option.get('selected') is True and not selected_scan_caption(refs[0],field,lookup):return False
        seen.append(target['block_id'])
    return len(set(seen))==len(options) and len(field.get('answer_region_refs',[]))==len(options)
