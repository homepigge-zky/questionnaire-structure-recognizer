"""Conservative coordinate-backed fields on local OCR pages.

The OCR text is never rewritten. Empty boxes/lines are separately located by
the raster reader. Only a local label/region relationship is assembled here;
unassociated geometry and uncertain marks remain available for review.
"""
from collections import defaultdict
from copy import deepcopy
import re
from .field_boundaries import ref
from .form_completion import new_field
from ..parser_core.models import make_block

VERSION='scan-local-fields-v26-source-response-owner-boundaries'
LABEL_OPTION_BOUNDARY_VERSION='scan-label-options-v1'


def separate_printed_choice_suffixes(fields, lookup):
    """Separate verified option suffix spans without rewriting OCR source text."""
    changed_blocks=set()
    for field in fields:
        refs=deepcopy(field.get('label_refs',[]));changed=False
        for rr in refs:
            block=lookup.get(rr['block_id'],{});text=block.get('text','')
            start,end=rr.get('start',0),rr.get('end',0)
            spans=[]
            for option in field.get('options',[]):
                for opt in option.get('refs',[]):
                    a,b=opt.get('start',0),opt.get('end',0)
                    if opt.get('block_id')==rr['block_id'] and start<=a<b<=end<=len(text) and text[a:b].strip()==option.get('text_original','').strip():spans.append((a,b))
            if not spans:continue
            spans=sorted(set(spans));cut=spans[0][0]
            residue=text[cut:end]
            for a,b in reversed(spans):residue=residue[:a-cut]+residue[b-cut:]
            prefix=text[start:cut].rstrip()
            # Only a terminal choice list is separable. An option-like word
            # inside a full sentence remains untouched and needs validation.
            if not prefix or residue.strip(' \t\r\n/|,;:□☐☒()'):continue
            rr['end']=start+len(prefix);changed=True;changed_blocks.add(rr['block_id'])
        if changed:
            field.update(label_refs=deepcopy(refs),question_refs=deepcopy(refs),
                question_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in refs),
                label_option_disjoint_version=LABEL_OPTION_BOUNDARY_VERSION)
    for bid in changed_blocks:
        lookup[bid].setdefault('structure',{})['protected_label_ranges']=[{'start':rr['start'],'end':rr['end']} for field in fields for rr in field.get('label_refs',[]) if rr['block_id']==bid]
OPTION=re.compile(r'^(?:yes|no|n\s*/?\s*a|是|否|不适用)\s*[:：]?$' ,re.I)
PLAIN_OPTIONS=re.compile(r'^\s*[:：]?\s*(?:Yes|No|Ycs|N\s*/?\s*A)(?:\s*[/|]\s*(?:Yes|No|Ycs|N\s*/?\s*A))+\s*$',re.I)
NUMBER=re.compile(r'^\d+(?:\.\d+)*[.)]?$')
PAGE_COUNTER=re.compile(r'\s*Page\s*(?:No\.?\s*)?[:：]?\s*\d+\s*(?:of|/)\s*\d+\s*',re.I)
UNIT=re.compile(r'^(?:Sq\.?\s*Mt\.?|m[²2]|%|ppm|ppb|kg|g|mg|m[lL]|[lL]|人|年)\s*$',re.I)

def _box(b):return b.get('location',{}).get('bbox',[0,0,0,0])
def _cy(b):r=_box(b);return (r[1]+r[3])/2
def _text(b):return b.get('text','').strip()
def _kind(b):return b.get('structure',{}).get('scan_region_kind')

def _located_response(b):
    return b.get('structure',{}).get('blank_state') == 'blank' or bool(b.get('structure',{}).get('overlay_value_block_ids'))

def _order_label_refs(refs,lookup):
    """Order already-associated OCR spans; never add, drop or rewrite text.

    Detected boxes on one printed baseline can have slightly different tops.
    Exact-y sorting would put a leading word after the rest of its own line.
    Only substantial vertical overlap and tightly aligned centers form a row;
    neighboring wrapped rows and separate pages remain distinct.
    """
    rows=[]
    for rr in sorted(refs,key=lambda r:(lookup[r['block_id']]['location'].get('page',0),_box(lookup[r['block_id']])[1],_box(lookup[r['block_id']])[0],r.get('start',0))):
        b=lookup[rr['block_id']];bb=_box(b);page=b['location'].get('page',0)
        row=next((row for row in reversed(rows) if row['page']==page and all(
            abs(_cy(b)-_cy(lookup[x['block_id']]))<=3 and
            min(bb[3],_box(lookup[x['block_id']])[3])-max(bb[1],_box(lookup[x['block_id']])[1])>=.6*min(bb[3]-bb[1],_box(lookup[x['block_id']])[3]-_box(lookup[x['block_id']])[1])
            for x in row['refs'])),None)
        if row is None:rows.append({'page':page,'refs':[rr]})
        else:row['refs'].append(rr)
    return [rr for row in rows for rr in sorted(row['refs'],key=lambda r:(_box(lookup[r['block_id']])[0],r.get('start',0)))]

def _unruled_scalar_labels(text):
    """A right margin is not a field. Accept repeated numbered scalar rows.

    Physical lines, boxes and controls use their own evidence. Unruled lanes
    need a short caption inside an explicit sibling-number form group; a
    parent caption only gets its own value when it explicitly requests a total.
    Other whitespace stays an unconfirmed candidate for manual/model review.
    """
    rows=[]
    for number in text:
        if not re.fullmatch(r'\d+(?:\.\d+)+',_text(number)):continue
        labels=[b for b in text if b is not number and not NUMBER.fullmatch(_text(b)) and 0<=_box(b)[0]-_box(number)[2]<35 and abs(_cy(b)-_cy(number))<5]
        if not labels:continue
        label=min(labels,key=lambda b:_box(b)[0]);value=re.sub(r'\s+','',_text(label))
        if len(value)>55 or re.search(r'[?？]',value) or re.match(r'(?:please|are|does|do|is|if|incase|mention|for)',value,re.I):continue
        rows.append((number['text'].split('.'),label))
    allowed=set()
    for parts,label in rows:
        siblings=[b for p,b in rows if len(p)==len(parts) and p[:-1]==parts[:-1] and abs(_box(b)[0]-_box(label)[0])<35 and abs(_cy(b)-_cy(label))<150]
        if len(parts)>=3 and len(siblings)>=2:allowed.add(label['block_id'])
        children=[b for p,b in rows if p[:-1]==parts]
        if len(children)>=2 and re.match(r'(?:total|numberof|quantityof|countof|合计|总|人数)',re.sub(r'\s+','',label['text']),re.I):allowed.add(label['block_id'])
    return allowed

def _repeated_page_furniture(pages):
    """Require nonempty source text at the same margin geometry across pages.

    Punctuation-only text normalizes to an empty key. Such a key must never
    match the physically empty lines/boxes in a form. Repetition is evidence
    for the individual printed block, never for a full-width exclusion band.
    Physical response lanes, their native values, and adjacent captions take
    precedence even when the same blank form is repeated on every page.
    """
    candidates=defaultdict(list)
    for page,items in pages.items():
        height=max((_box(b)[3] for b in items),default=800)
        regions=[b for b in items if _kind(b) in {'write_line','text_box','blank_lane','checkbox'}]
        for b in items:
            structure=b.setdefault('structure',{})
            if structure.get('resolved_layout_role')=='repeated_header_footer':
                structure.pop('resolved_layout_role',None)
            structure.pop('scan_header_evidence',None)
            key=re.sub(r'\W','',b.get('text','')).lower()
            if not key or _kind(b) or structure.get('native_overlay_answer_region'):continue
            bb=_box(b)
            side='top' if bb[3]<height*.28 else 'bottom' if bb[1]>height*.9 else None
            if side is None:continue
            # A caption aligned with a concrete writable region is business
            # content. Blank response regions are just as significant as filled.
            if any(bb[2]<=_box(r)[0]+2 and 0<=_box(r)[0]-bb[2]+2<222
                   and (_box(r)[0]-bb[2]<40 or r.get('structure',{}).get('overlay_value_block_ids') or
                        r.get('structure',{}).get('scan_association_valid') or _kind(r)=='text_box')
                   and _box(r)[1]-4<=_cy(b)<=_box(r)[3]+4 for r in regions):continue
            candidates[(key,side)].append((page,b))
    matches={}
    threshold=max(3,len(pages)//2)
    for (key,side),items in candidates.items():
        for page,b in items:
            bb=_box(b)
            peers=[(other_page,other) for other_page,other in items
                   if all(abs(bb[i]-_box(other)[i])<=4 for i in range(4))]
            if len({p for p,_ in peers})>=threshold:
                matches[b['block_id']]={'kind':'repeated_text_at_same_margin_geometry',
                    'margin':side,'source_block_ids':[other['block_id'] for _,other in peers]}
    # A scanned page can shift slightly horizontally. A second independently
    # repeated header line supplies the source translation; do not widen a
    # global top band or accept repetition without a nearby physical anchor.
    direct=set(matches)
    for (key,side),items in candidates.items():
        for page,b in items:
            if b['block_id'] in matches:continue
            bb=_box(b)
            anchors=[(anchor_key,anchor) for (anchor_key,anchor_side),group in candidates.items()
                if anchor_key!=key and anchor_side==side and len({p for p,a in group if a['block_id'] in direct})>=threshold for p,anchor in group
                if p==page and abs(_box(anchor)[0]-bb[0])<12
                and 8<abs(_cy(anchor)-_cy(b))<35]
            for anchor_key,anchor in anchors:
                support=[];ab=_box(anchor)
                for other_page,other in items:
                    peers=[a for p,a in candidates[(anchor_key,side)] if p==other_page and a['block_id'] in direct]
                    if len(peers)!=1:continue
                    pb=_box(peers[0]);ob=_box(other)
                    if all(abs((bb[i]-ab[i])-(ob[i]-pb[i]))<=4 for i in range(4)):
                        support.append((other_page,other['block_id'],peers[0]['block_id']))
                if len({p for p,_,_ in support})>=threshold:
                    matches[b['block_id']]={'kind':'repeated_text_relative_to_verified_margin_anchor',
                        'margin':side,'anchor_block_id':anchor['block_id'],
                        'source_block_ids':[bid for _,bid,_ in support],
                        'anchor_source_block_ids':[bid for _,_,bid in support]}
                    break
    return matches


def restore_scanned_fields(questions,blocks,lookup):
    pages=defaultdict(list)
    for b in blocks:
        if b.get('location',{}).get('type')=='pdf' and (b.get('location',{}).get('extraction')=='ocr' or b.get('structure',{}).get('scan_page_context')):pages[b['location']['page']].append(b)
    if not pages:return questions
    additions=[];claimed=set();assigned_regions=set();option_headers=set();section_headers=set();fixed=set();verified_values=set()
    repeated=_repeated_page_furniture(pages)
    for page,items in pages.items():
        height=max((_box(b)[3] for b in items),default=800)
        approval_heading=next((b for b in items if re.search(r'document\s*signatory\s*page',b.get('text',''),re.I)),None)
        for b in items:
            repeat=b['block_id'] in repeated or (bool(PAGE_COUNTER.fullmatch(b.get('text',''))) and (_box(b)[1]<height*.1 or _box(b)[1]>height*.85))
            # Yes/No captions recur on almost every page, including at the
            # page top. Recurrence never makes a business option a page header.
            if re.fullmatch(r'(?:(?:yes|no|n/?a|planned|inpreparation)[\s/|□☐口O]*)+',re.sub(r'\s+','',b.get('text','')),re.I):repeat=False
            if (_box(b)[1]>height*.9 and re.match(r'print\s*taken(?:\s*by)?\b',b.get('text',''),re.I)):
                repeat=True
            if repeat or (approval_heading and _box(b)[1]>=_box(approval_heading)[1]):
                fixed.add(b['block_id']);b.setdefault('structure',{})['resolved_layout_role']='fixed_template_approval' if approval_heading else 'repeated_header_footer'
                if b['block_id'] in repeated:b['structure']['scan_header_evidence']=repeated[b['block_id']]
        # Raster contours inside a printed page counter can be letter glyphs
        # (the o in "No."), not actual checkbox controls. Keep their source
        # locations, but do not offer them as business response regions.
        counters=[b for b in items if b['block_id'] in fixed and PAGE_COUNTER.fullmatch(_text(b))]
        for region in items:
            if _kind(region)!='checkbox':continue
            rb=_box(region)
            if any(_box(c)[0]-1<=rb[0] and _box(c)[1]-1<=rb[1] and rb[2]<=_box(c)[2]+1 and rb[3]<=_box(c)[3]+1 for c in counters):
                fixed.add(region['block_id']);region.setdefault('structure',{})['resolved_layout_role']='page_counter_glyph'
    def create(labels,regions,options=None,label_refs=None):
        if not labels or not regions:return None
        f=new_field(labels[0],ref(labels[0]),[ref(r) for r in regions])
        fid='scan-field:'+labels[0]['block_id']
        label_refs=label_refs or [ref(b) for b in labels]
        f.update(field_id=fid,question_id=fid,label_refs=deepcopy(label_refs),question_refs=deepcopy(label_refs),question_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in label_refs),boundary_evidence='scan_physical_form',boundary_version=VERSION)
        if options:f['options']=options
        values=[lookup[bid] for region in regions for bid in region.get('structure',{}).get('overlay_value_block_ids',[])
            if bid in lookup and lookup[bid].get('text','').strip() and bid not in {b['block_id'] for b in labels}]
        if values:
            f.update(answer_status='filled',answer_value_refs=[ref(b) for b in values],answer_refs=[ref(b) for b in values],
                answer_text_original='\n'.join(b['text'] for b in values),answer_pairing_evidence='native_overlay_inside_physical_response')
            verified_values.update(b['block_id'] for b in values)
            for value in values: value.setdefault('structure',{})['resolved_layout_role']='answer_value'
        if any(r.get('structure',{}).get('blank_state') not in {'blank','filled'} for r in regions):
            f.update(answer_status='unresolved',review_status='needs_review',review_reasons=['扫描选择区域存在笔迹或噪点，不能推测已选择的答案，请核对原页。'])
        for label in labels:
            claimed.add(label['block_id']);label.setdefault('structure',{})['protected_label_ranges']=[{'start':r['start'],'end':r['end']} for r in label_refs if r['block_id']==label['block_id']]
        for region in regions:
            assigned_regions.add(region['block_id']);region.setdefault('structure',{}).update(scan_association_valid=True,scan_field_id=fid)
        if options:
            from .selection_evidence import selected_scan_caption
            for option in options:
                for rr in option.get('answer_region_refs',[]):
                    lookup[rr['block_id']].setdefault('structure',{})['scan_option_caption_refs']=deepcopy(option.get('refs',[]))
            captions=[rr for option in options if option.get('selected') is True for rr in option.get('refs',[])]
            if captions and all(selected_scan_caption(rr,f,lookup) for rr in captions):
                f['selection_mark_refs']=deepcopy(f.get('answer_value_refs',[]))
                f.update(answer_status='filled',answer_value_refs=deepcopy(captions),answer_refs=deepcopy(captions),
                    answer_text_original='\n'.join(lookup[rr['block_id']]['text'][rr['start']:rr['end']] for rr in captions),
                    answer_pairing_evidence='physical_selected_box_and_source_caption')
        f['source_locations']={'question':[deepcopy(b['location']) for b in labels],'answer_regions':[deepcopy(b['location']) for b in regions],'answer':[deepcopy(b['location']) for b in values]}
        additions.append(f);return f

    for page,page_blocks in pages.items():
        # A complete imperative explaining how to mark boxes is fixed prose.
        # Keep it and overlapping OCR fragments in the source ledger. Glyph
        # contours wholly inside that printed sentence are not fill targets.
        for instruction in page_blocks:
            value=_text(instruction)
            if (_kind(instruction) or re.search(r'[?？]',value) or
                not re.match(r'^\s*(?:Put|Place|Mark)\b',value,re.I) or
                not re.search(r'(?:square\s+boxes|check\s*boxes)',value,re.I) or
                not re.search(r'(?:appropriate|wherever\s+applicable)',value,re.I)):continue
            ib=_box(instruction);fixed.add(instruction['block_id'])
            instruction.setdefault('structure',{})['resolved_layout_role']='instruction'
            for b in page_blocks:
                bb=_box(b)
                if b is instruction or not (ib[0]<=bb[0] and bb[2]<=ib[2] and ib[1]-1<=bb[1] and bb[3]<=ib[3]+1):continue
                if _kind(b)=='checkbox':
                    fixed.add(b['block_id']);b.setdefault('structure',{}).update(
                        resolved_layout_role='instruction_letter_contour',instruction_source_ref=ref(instruction))
                elif _text(b) and re.sub(r'\s+','',_text(b)).casefold() in re.sub(r'\s+','',value).casefold():
                    fixed.add(b['block_id']);b.setdefault('structure',{}).update(
                        resolved_layout_role='instruction',instruction_source_ref=ref(instruction))
        text=[b for b in page_blocks if _text(b) and not _kind(b) and b['block_id'] not in fixed
            and not b.get('structure',{}).get('native_overlay_answer_region')
            and b.get('structure',{}).get('resolved_layout_role') not in {'checkbox_symbol','invisible_native_mark'}]
        regions=[b for b in page_blocks if _kind(b) and b['block_id'] not in fixed]
        lines=[r for r in regions if _kind(r)=='write_line']
        page_left=min((_box(b)[0] for b in page_blocks),default=0);page_right=max((_box(b)[2] for b in page_blocks),default=595)
        for label in text:
            lb=_box(label);value=label['text']
            if 10<len(value)<90 and re.search('[A-Z]',value) and value.upper()==value and abs((lb[0]+lb[2])/2-(page_left+page_right)/2)<35 and not any(abs(_cy(r)-_cy(label))<8 for r in regions):
                section_headers.add(label['block_id']);label.setdefault('structure',{})['resolved_layout_role']='section_header'
        for number in [b for b in text if re.fullmatch(r'\d+(?:\.0)?',_text(b)) and _box(b)[0]<100]:
            headings=[b for b in text if _box(b)[0]>_box(number)[2]+5 and abs(_cy(b)-_cy(number))<6 and len(b['text'])<80 and not re.search(r'[?？]',b['text'])]
            if len(headings)==1:
                section_headers.add(headings[0]['block_id']);fixed.add(number['block_id'])
        for label in text:
            lb=_box(label)
            upper=[r for r in lines if _box(r)[0]+15<lb[0] and lb[2]<_box(r)[2]-15 and 0<=lb[1]-_box(r)[3]+2<=4]
            lower=[r for r in lines if _box(r)[0]+15<lb[0] and lb[2]<_box(r)[2]-15 and -1<=_box(r)[3]-lb[3]<=4]
            if upper and lower and len(label['text'])<100:
                section_headers.add(label['block_id']);label.setdefault('structure',{})['resolved_layout_role']='section_header'
        unruled_labels=_unruled_scalar_labels(text)
        independent=set()
        for r in regions:
            if _kind(r) not in {'write_line','text_box','blank_lane'} or not _located_response(r):continue
            matches=[label for label in text if _box(label)[2]<=_box(r)[0]+2 and _box(r)[1]-4<=_cy(label)<=_box(r)[3]+4 and _box(r)[0]-_box(label)[2]<220 and label['block_id'] not in section_headers and not NUMBER.fullmatch(_text(label))]
            if _kind(r)=='blank_lane':matches=[b for b in matches if b['block_id'] in unruled_labels]
            if matches:independent.add(max(matches,key=lambda b:_box(b)[2])['block_id'])
        boxes=sorted([b for b in regions if _kind(b)=='checkbox'],key=lambda b:(_cy(b),_box(b)[0]))
        for glyph in text:
            if not re.fullmatch(r'[口□■☐☒☑]',_text(glyph)):continue
            gb=_box(glyph)
            for box in boxes:
                rb=_box(box);intersection=max(0,min(gb[2],rb[2])-max(gb[0],rb[0]))*max(0,min(gb[3],rb[3])-max(gb[1],rb[1]))
                if intersection>=.7*min((gb[2]-gb[0])*(gb[3]-gb[1]),(rb[2]-rb[0])*(rb[3]-rb[1])):
                    option_headers.add(glyph['block_id']);glyph.setdefault('structure',{})['resolved_layout_role']='checkbox_symbol';break
        groups=[]
        for b in boxes:
            row=next((r for r in groups if abs(_cy(r[0])-_cy(b))<4),None)
            if row is None:groups.append([b])
            else:row.append(b)
        from .joined_option_peer_rows import exclude_verified_caption_contours
        redundant=exclude_verified_caption_contours(groups,text)
        if redundant:
            fixed.update(redundant)
            groups=[[b for b in row if b['block_id'] not in redundant] for row in groups]
        # Decode captions using the whole physical option row. A printed
        # caption remains a source span, including when OCR joined it to the
        # question. The small boxes fix order; word-list indices never do.
        token=re.compile(r'(?<![A-Za-z])(?:In preparation|Planned|Yes|Ycs|No|N\s*/?\s*A)(?![A-Za-z])',re.I)
        def option_matches(value):
            # A condition mentions a choice; it is not the choice itself.
            if re.match(r'^\s*(?:If\s*(?:yes|no)\b|若(?:是|否)|如果)',value,re.I):return []
            if re.fullmatch(r'[ZV](?:Yes|No)',value):
                return list(re.finditer(r'Yes|No',value))
            if re.fullmatch(r'\s*(?:(?:Yes|No|N\s*/?\s*A)[\s/|□☐口O()]*)+',value,re.I):
                return list(re.finditer(r'Yes|No|N\s*/?\s*A',value,re.I))
            return list(token.finditer(value))
        decoded={}; inline_labels={}
        for row in groups:
            row.sort(key=lambda b:_box(b)[0]);same_y=sum(_cy(b) for b in row)/len(row)
            for source in text:
                if abs(_cy(source)-same_y)>7:continue
                matches=option_matches(source['text'])
                if len(matches)!=len(row):continue
                if source['text'][matches[-1].end():].strip(' :;□☐O口()') or _box(source)[2]<_box(row[0])[0]-12 or _box(source)[0]>_box(row[-1])[2]+12:continue
                if len({m.group().lower() for m in matches})<len(matches):continue
                for box,m in zip(row,matches):decoded[box['block_id']]=(source,ref(source,m.start(),m.end()))
                prefix=source['text'][:matches[0].start()].strip(' :;□☐O口')
                if len(prefix)>10:inline_labels[row[0]['block_id']]=(source,ref(source,0,source['text'].find(prefix)+len(prefix)))
                break
            # An OCR run can concatenate only the terminal captions, such as
            # NoN/A. Associate the literal source spans with the unique boxes
            # actually enclosed by that run, not estimated character widths.
            for source in text:
                if abs(_cy(source)-same_y)>7:continue
                matches=option_matches(source['text'])
                if len(matches) not in (2,3):continue
                residue=source['text']
                for m in reversed(matches):residue=residue[:m.start()]+residue[m.end():]
                if residue.strip(' \t\r\n:;□☐☑☒O口()/|') or len({m.group().casefold() for m in matches})!=len(matches):continue
                sb=_box(source)
                enclosed=sorted([b for b in row if sb[0]-2<=(_box(b)[0]+_box(b)[2])/2<=sb[2]+2],key=lambda b:_box(b)[0])
                if len(enclosed)!=len(matches):continue
                # Conflicting existing caption ownership is not overwritten.
                proposed=[(b,ref(source,m.start(),m.end())) for b,m in zip(enclosed,matches)]
                if any(b['block_id'] in decoded and decoded[b['block_id']][1]!=rr for b,rr in proposed):continue
                for b,rr in proposed:decoded[b['block_id']]=(source,rr)
            for box in row:
                if box['block_id'] in decoded:continue
                bx=_box(box);choices=[]
                for label in text:
                    lb=_box(label);matches=option_matches(label['text'])
                    if not matches and len(label['text'])<=8:matches=list(re.finditer(r'Yes|No|NA',label['text']))
                    scoped_caption=re.fullmatch(r'\s*(?:N\s*/?\s*A|Yes|No)\s*\([^?]+\)\s*',label['text'],re.I)
                    if scoped_caption and abs(_cy(label)-_cy(box))<7 and -16<bx[0]-lb[2]<20:
                        choices.append((abs(bx[0]-lb[2]),label,ref(label)));continue
                    for m in matches:
                        # Whole caption / last inline caption, or first caption
                        # before an independent supplemental field on that row.
                        end=lb[0]+(lb[2]-lb[0])*m.end()/max(1,len(label['text']))
                        direct=abs(_cy(label)-_cy(box))<6 and -5<bx[0]-end<20
                        if len(matches)==1 and len(label['text'])<=8 and abs(_cy(label)-_cy(box))<7 and -16<bx[0]-end<20:direct=True
                        if len(matches)==1 and '?' in label['text'][:m.start()] and not label['text'][m.end():].strip(' :;□☐O口()') and abs(_cy(label)-_cy(box))<7 and lb[0]+(lb[2]-lb[0])*.55<bx[0]<lb[2]:direct=True
                        if len(matches)==1 and re.fullmatch(r'\s*(?:yes|no|n\s*/?\s*a|planned|in preparation)[\s□口O()]*',label['text'],re.I) and abs(_cy(label)-_cy(box))<7 and lb[0]+(lb[2]-lb[0])*.5<bx[0]<=lb[2] and lb[2]-bx[0]<16:direct=True
                        above=(len(matches)==1 and len(label['text'])<20 and lb[3]<bx[1] and bx[0]-6<=(lb[0]+lb[2])/2<=bx[2]+6)
                        if direct or above:choices.append((abs(bx[0]-end) if direct else 100+bx[1]-lb[3],label,ref(label,m.start(),m.end())))
                    if not matches and len(label['text'])<80 and '?' not in label['text'] and not re.match(r'^\s*If\s*(?:yes|no)\b',label['text'],re.I) and abs(_cy(label)-_cy(box))<5 and 0<=bx[0]-lb[2]<18 and label['block_id'] not in independent|section_headers:
                        choices.append((10000+bx[0]-lb[2],label,ref(label)))
                if choices:
                    _,label,rr=min(choices,key=lambda v:v[0]);decoded[box['block_id']]=(label,rr)
                    prefix=label['text'][:rr['start']].strip(' :;□☐O口')
                    if len(prefix)>10 and '?' in prefix:inline_labels[box['block_id']]=(label,ref(label,0,len(prefix)))
        from .question_response_groups import bounded_question_response_rows
        groups=bounded_question_response_rows(groups,decoded,text,inline_labels)
        # OCR may include the real checkbox in a short caption's bbox while
        # raster tracing also finds the enclosed counter of a printed letter.
        # Do not let both physical contours claim that same caption. A small
        # contour alone is NOT evidence: require another distinct full-size
        # response after the caption, peers on the same option row, and the
        # smaller contour wholly inside the printed caption span.
        for row in groups:
            for glyph in list(row):
                item=decoded.get(glyph['block_id'])
                if not item:
                    gb=_box(glyph);possible={}
                    for other in row:
                        candidate=decoded.get(other['block_id'])
                        if not candidate:continue
                        label,rr=candidate;lb=_box(label)
                        if lb[0]<=gb[0] and gb[2]<=lb[2] and lb[1]-1<=gb[1] and gb[3]<=lb[3]+1:
                            possible[(rr['block_id'],rr['start'],rr['end'])]=candidate
                    if len(possible)!=1:continue
                    item=next(iter(possible.values()))
                label,rr=item;lb=_box(label);gb=_box(glyph)
                if len(label['text'])>12 or rr['end']<=rr['start']:continue
                span_left=lb[0]+(lb[2]-lb[0])*rr['start']/max(1,len(label['text']))
                span_right=lb[0]+(lb[2]-lb[0])*rr['end']/max(1,len(label['text']))
                if not (span_left<=gb[0]<gb[2]<=span_right-2 and lb[1]-1<=gb[1]<gb[3]<=lb[3]+1):continue
                replacements=[]
                for other in row:
                    if other is glyph or decoded.get(other['block_id'],(None,None))[1]!=rr:continue
                    ob=_box(other)
                    if ob[0]<span_right-2 or ob[0]-gb[2]<4:continue
                    if gb[2]-gb[0]>.65*(ob[2]-ob[0]) or gb[3]-gb[1]>.65*(ob[3]-ob[1]):continue
                    peers=[p for p in row if p not in (glyph,other) and p['block_id'] in decoded
                        and decoded[p['block_id']][1]!=rr and .75<=(_box(p)[3]-_box(p)[1])/(ob[3]-ob[1])<=1.33]
                    if peers:replacements.append(other)
                if len(replacements)!=1:continue
                replacement=replacements[0]
                fixed.add(glyph['block_id']);decoded.pop(glyph['block_id'],None)
                glyph.setdefault('structure',{}).update(resolved_layout_role='option_caption_glyph',
                    scan_caption_glyph_evidence={'caption_ref':deepcopy(rr),
                        'distinct_response_block_id':replacement['block_id'],
                        'reason':'smaller enclosed caption contour beside peer-aligned response rectangle'})
            row[:]=[b for b in row if b['block_id'] not in fixed]
        groups=[row for row in groups if row]
        # A continuation contains only a new option caption, with no new
        # question/number between it and the preceding physical option row.
        # This admits wrapped Planned and vertical Yes/No, not unrelated rows.
        merged=[]
        for row in groups:
            if merged and len(row)==1 and row[0]['block_id'] in decoded:
                previous=merged[-1];low=max(_cy(b) for b in previous);high=_cy(row[0]);caption=decoded[row[0]['block_id']][0]
                previous_words=[decoded[b['block_id']][1] for b in previous if b['block_id'] in decoded]
                newword=decoded[row[0]['block_id']][1];word=lookup[newword['block_id']]['text'][newword['start']:newword['end']].lower()
                seen=[lookup[r['block_id']]['text'][r['start']:r['end']].lower() for r in previous_words]
                boundary=any(low+5<_cy(b)<high+4 and b['block_id']!=caption['block_id'] and (NUMBER.fullmatch(_text(b)) or '?' in _text(b)) for b in text)
                if 5<high-low<35 and word not in seen and seen and not boundary and word in {'no','planned','in preparation','n/a','na'}:
                    previous.extend(row);continue
            merged.append(row)
        # An undecoded raster contour (for example the loop in the printed
        # subsection letter) is not an option row. Keep its original block,
        # but never use it to cut the label band of the next verified row.
        groups=[row for row in merged if all(b['block_id'] in decoded for b in row)]
        for i,row in enumerate(groups):
            row.sort(key=lambda b:(_cy(b),_box(b)[0]));cy=min(_cy(b) for b in row);left=min(_box(b)[0] for b in row if abs(_cy(b)-cy)<4)
            if any(b['block_id'] not in decoded for b in row):continue
            below=_cy(groups[i+1][0])-3 if i+1<len(groups) else cy+40
            labels=[b for b in text if _box(b)[2]<left-3 and abs(_cy(b)-cy)<6 and not OPTION.fullmatch(_text(b)) and not NUMBER.fullmatch(_text(b)) and not re.fullmatch(r'\(?[a-z]\)',_text(b)) and b['block_id'] not in claimed|section_headers]
            if labels:
                anchor=min(labels,key=lambda b:abs(_cy(b)-cy));x0=_box(anchor)[0]
            # Wrapped label lines remain left of the first option column and
            # stop before the next physical option row. Notes on the right
            # cannot enter the question or become an answer.
                labels=[b for b in text if _box(b)[2]<left-3 and abs(_box(b)[0]-x0)<8 and cy-7<=_cy(b)<below and not OPTION.fullmatch(_text(b)) and not NUMBER.fullmatch(_text(b)) and not re.fullmatch(r'\(?[a-z]\)',_text(b)) and (b is anchor or (b['block_id'] not in independent and not re.match(r'^\(?\s*(?:If\s*Yes|If\s*No|Please\s*attach)',b['text'],re.I)))]
                # The option boxes commonly sit beside the LAST wrapped line.
                # Recover contiguous preceding lines in the same label lane,
                # even when an earlier long line spans above the option lane.
                lower=max(_cy(groups[i-1][0])+5 if i else 0,cy-80)
                prior=sorted([b for b in text if lower<_cy(b)<_cy(anchor)-5 and b['block_id'] not in claimed|section_headers|independent and not NUMBER.fullmatch(_text(b)) and not re.fullmatch(r'\(?[a-z]\)',_text(b)) and not OPTION.fullmatch(_text(b)) and not PLAIN_OPTIONS.fullmatch(_text(b)) and abs(_box(b)[0]-x0)<18],key=lambda b:_box(b)[1],reverse=True)
                current=anchor
                for b in prior:
                    if _box(current)[1]-_box(b)[3]>9:break
                    labels.insert(0,b);current=b
            labels.sort(key=lambda b:(_box(b)[1],_box(b)[0]))
            # A new numbered prompt below this option row is a hard local
            # boundary; it must never be appended to a wrapped label above.
            next_numbers=[b for b in text if NUMBER.fullmatch(_text(b)) and _box(b)[0]<150 and _cy(b)>cy+4]
            # In vertically centered numbered rows the number can sit below
            # the first option yet still belong to that same physical cell.
            # Only an actual narrow response rectangle supplies this exception.
            row_cells=[r for r in regions if _kind(r)=='text_box' and _box(r)[2]-_box(r)[0]<180 and
                all(_box(r)[0]-1<=_box(mark)[0] and _box(mark)[2]<=_box(r)[2]+1 and
                    _box(r)[1]-1<=_box(mark)[1] and _box(mark)[3]<=_box(r)[3]+1 for mark in row)]
            if len(row_cells)==1:
                next_numbers=[n for n in next_numbers if _cy(n)>_box(row_cells[0])[3]+8]
            if next_numbers:
                stop=min(_cy(b) for b in next_numbers)-6;labels=[b for b in labels if _cy(b)<stop]
            options=[];inline_ref=None;inline_source=None
            for box in row:
                label,choice_ref=decoded[box['block_id']]
                residue=label['text']
                for mark in reversed(option_matches(residue)):residue=residue[:mark.start()]+residue[mark.end():]
                if not residue.strip(' :;□☐O口()') or re.fullmatch(r'[ZV](?:Yes|No)',label['text']):option_headers.add(label['block_id'])
                options.append({'refs':[choice_ref],'text_original':label['text'][choice_ref['start']:choice_ref['end']],'selected':box['controls'][0].get('selected'),'selection_source':box['controls'][0].get('source','scan_empty_rectangle'),'answer_region_refs':[ref(box)]})
                if box['block_id'] in inline_labels:inline_source,inline_ref=inline_labels[box['block_id']]
            inline_refs=None
            if inline_source:
                labels=[inline_source];ib=_box(inline_source);lower=max(_cy(groups[i-1][0])+5 if i else 0,ib[1]-90)
                scoped_response=any(b.get('structure',{}).get('separate_printed_response_group',{}).get('question_ref')==inline_ref for b in row)
                prior=sorted([b for b in text if lower<_cy(b)<_cy(inline_source)-5 and b['block_id'] not in claimed|section_headers|option_headers|independent and not NUMBER.fullmatch(_text(b)) and not re.fullmatch(r'\(?[a-z]\)',_text(b)) and not OPTION.fullmatch(_text(b)) and abs(_box(b)[0]-ib[0])<18],key=lambda b:_box(b)[1],reverse=True)
                current=inline_source
                for previous in prior:
                    if _box(current)[1]-_box(previous)[3]>9:break
                    if scoped_response:
                        # The proof identifies this response owner. A complete
                        # preceding prompt, or its own intervening answer lane,
                        # cannot become a wrapped prefix just because it is near.
                        if previous['text'].rstrip().endswith(('?','？')):break
                        if any(_kind(r) in {'write_line','text_box'} and
                               _box(previous)[3]<=_box(r)[1] and _box(r)[3]<=_box(current)[1] and
                               max(_box(previous)[0],_box(r)[0])<min(_box(previous)[2],_box(r)[2])
                               for r in regions):break
                    labels.insert(0,previous);current=previous
                inline_refs=[ref(b) if b is not inline_source else inline_ref for b in labels]
            choice_blocks={o['refs'][0]['block_id'] for o in options}
            if not inline_source:labels=[b for b in labels if b['block_id'] not in choice_blocks]
            if options and not labels:
                if inline_source:labels=[inline_source]
                else:
                    previous=_cy(groups[i-1][0])+3 if i else cy-55
                    previous=max(previous,cy-55)
                    prior=[b for b in text if b['block_id'] not in claimed|section_headers|option_headers|independent|choice_blocks and previous<_cy(b)<cy+5 and _box(b)[0]<left-15 and not OPTION.fullmatch(_text(b)) and not PLAIN_OPTIONS.fullmatch(_text(b)) and not NUMBER.fullmatch(_text(b)) and not re.fullmatch(r'\(?[a-z]\)',_text(b))]
                    if prior:
                        anchor=max(prior,key=lambda b:(_box(b)[1],-_box(b)[0]));labels=[anchor];current=anchor
                        for b in sorted(prior,key=lambda b:_box(b)[1],reverse=True):
                            if b is anchor or _box(b)[1]>=_box(current)[1]:continue
                            if _box(current)[1]-_box(b)[3]>8 or abs(_box(b)[0]-_box(anchor)[0])>15:break
                            if re.match(r'^\s*(?:[A-Z]\s*[-–]\s*)?\d+(?:\.\d+)*\s+\S',current['text']):break
                            labels.insert(0,b);current=b
            if options and not labels:
                # A comment box can put its sole N/A choice at the bottom.
                # Associate only through one actual closed containing region,
                # never through an arbitrary distance-based preceding caption.
                from .pdf_source_relations import source_text_inside_cell
                containers=[c for c in regions if _kind(c)=='text_box' and c.get('structure',{}).get('closed_region_evidence') and
                    all(_box(c)[0]<=_box(m)[0] and _box(m)[2]<=_box(c)[2]+1 and _box(c)[1]<=_box(m)[1] and _box(m)[3]<=_box(c)[3]+1 for m in row)]
                if len(containers)==1:
                    container=containers[0];cb=_box(container)
                    local=[b for b in text if b['block_id'] not in choice_blocks|section_headers|option_headers and
                        source_text_inside_cell(b,container,label=True) and _box(b)[0]<cb[0]+30 and _box(b)[3]<cy-8]
                    if local and max(_box(b)[3] for b in local)<cb[1]+35:
                        labels=sorted(local,key=lambda b:(_box(b)[1],_box(b)[0]))
            from .scanned_question_rows import source_choice_partition
            partition=source_choice_partition([ref(b) for b in row],page_blocks,lookup)
            if partition:
                labels=[lookup[r['block_id']] for r in partition['label_refs']]
                inline_refs=partition['label_refs']
            if options and labels and len({o['text_original'].lower() for o in options})==len(options):
                field=create(labels,row,options,inline_refs)
                if partition:
                    field['source_question_partition']=deepcopy(partition)
                    field['physical_label_region_ref']=deepcopy(partition['label_cell_ref'])
                    field['physical_response_region_ref']=deepcopy(partition['response_cell_ref'])
        # A printed Yes/No list is a response location too, even without a
        # checkbox widget. Keep source spans; the printed words are options,
        # never an existing Yes/No answer. Do not borrow its adjacent prompt.
        for choice in text:
            if choice['block_id'] in option_headers or not PLAIN_OPTIONS.fullmatch(_text(choice)):continue
            cb=_box(choice);cy=_cy(choice)
            labels=[b for b in text if b['block_id'] not in section_headers|fixed|option_headers and not NUMBER.fullmatch(_text(b)) and not re.fullmatch(r'\(?[a-z]\)',_text(b)) and not PLAIN_OPTIONS.fullmatch(_text(b)) and _box(b)[2]<cb[0]-3 and abs(_cy(b)-cy)<6]
            if not labels:
                labels=[b for b in text if b['block_id'] not in section_headers|fixed|option_headers and not NUMBER.fullmatch(_text(b)) and not re.fullmatch(r'\(?[a-z]\)',_text(b)) and not PLAIN_OPTIONS.fullmatch(_text(b)) and 0<=cb[1]-_box(b)[3]<20 and _box(b)[0]<cb[0]+20]
                if labels:labels=[max(labels,key=lambda b:_box(b)[3])]
            if not labels:continue
            label=max(labels,key=lambda b:_box(b)[2]);labels=[label]
            prior=[b for b in text if b['block_id'] not in claimed|section_headers|fixed|option_headers and abs(_box(b)[0]-_box(label)[0])<15 and 0<=_box(label)[1]-_box(b)[3]<8 and not NUMBER.fullmatch(_text(b)) and not re.fullmatch(r'\(?[a-z]\)',_text(b)) and not PLAIN_OPTIONS.fullmatch(_text(b)) and not any(NUMBER.fullmatch(_text(n)) and _box(n)[0]<_box(label)[0] and _cy(b)<_cy(n)<=_cy(label)+2 for n in text)]
            labels=sorted(prior+labels,key=lambda b:_box(b)[1])
            # Yes/No may sit beside the FIRST printed line of a wrapped
            # question. An unfinished clause plus aligned, tightly adjacent
            # continuation is one label; stop at a new number, conditional
            # prompt or independently answerable line. Claim it before the
            # next option group, otherwise that next group steals the tail.
            last=labels[-1]
            for _ in range(6):
                if not re.search(r'(?:\b(?:for|with|and|or|of|to|like|including)|[,，/／])\s*$',_text(last),re.I):break
                following=sorted([b for b in text if b['block_id'] not in claimed|section_headers|fixed|option_headers and b not in labels and
                    abs(_box(b)[0]-_box(last)[0])<15 and -1<=_box(b)[1]-_box(last)[3]<=12 and
                    not NUMBER.fullmatch(_text(b)) and not PLAIN_OPTIONS.fullmatch(_text(b))],key=lambda b:_box(b)[1])
                if not following:break
                nxt=following[0]
                if re.match(r'\s*(?:if\b|when\b|please\b|does\b|do\b|is\b|are\b|can\b|是否|若|如果)',nxt['text'],re.I):break
                if any(NUMBER.fullmatch(_text(n)) and _box(n)[2]<_box(nxt)[0] and abs(_cy(n)-_cy(nxt))<7 for n in text):break
                if any(PLAIN_OPTIONS.fullmatch(_text(n)) and _box(n)[0]>_box(nxt)[2] and abs(_cy(n)-_cy(nxt))<7 for n in text):break
                labels.append(nxt);last=nxt
            field=next((f for f in additions if f['label_refs'][0]['block_id']==label['block_id']),None)
            if field is None:
                field=new_field(labels[0],ref(labels[0]));field.update(field_id='scan-field:'+labels[0]['block_id'],question_id='scan-field:'+labels[0]['block_id'],label_refs=[ref(b) for b in labels],question_refs=[ref(b) for b in labels],question_text_original='\n'.join(b['text'] for b in labels),boundary_evidence='scan_printed_options',boundary_version=VERSION);additions.append(field)
            options=[]
            for m in re.finditer(r'Yes|Ycs|No|N\s*/?\s*A',choice['text'],re.I):
                rr=ref(choice,m.start(),m.end(),choice['block_id']+f':printed-option:{m.start()}')
                choice.setdefault('structure',{}).setdefault('logical_slots',[]).append({'slot_id':rr['slot_id'],'kind':'printed_option','start':m.start(),'end':m.end(),'option_label':m.group()})
                options.append({'refs':[ref(choice,m.start(),m.end())],'text_original':m.group(),'selected':False,'selection_source':'printed_choice_list','answer_region_refs':[rr]})
            field.update(options=options,answer_region_refs=[r for o in options for r in o['answer_region_refs']],answer_status='blank',answer_text_original='',answer_value_refs=[],answer_refs=[])
            claimed.update(b['block_id'] for b in labels);option_headers.add(choice['block_id'])
        # A bounded text box / short writing line beside a label. The nearest
        # independent label to its left terminates the region; no text search
        # through a blank region is permitted.
        for region in regions:
            if region['block_id'] in assigned_regions or _kind(region) not in {'write_line','text_box','blank_lane'} or not _located_response(region):continue
            rb=_box(region);candidates=[]
            for label in text:
                lb=_box(label)
                if label['block_id'] in claimed or label['block_id'] in section_headers or OPTION.fullmatch(_text(label)) or NUMBER.fullmatch(_text(label)) or re.fullmatch(r'\(?[a-z]\)',_text(label)):continue
                if lb[2]<=rb[0]+2 and rb[1]-4<=_cy(label)<=rb[3]+4 and rb[0]-lb[2]<220:
                    candidates.append(label)
            if not candidates:continue
            label=max(candidates,key=lambda b:_box(b)[2])
            if _kind(region)=='blank_lane' and label['block_id'] not in unruled_labels:
                region['structure'].update(scan_association_valid=False,nonfillable_reason='Unruled trailing whitespace has no independent scalar-form evidence')
                continue
            if _kind(region)=='blank_lane' and (re.match(r'\s*(?:if\s|please\s*attach|pleaseattach)',label['text'],re.I) or (re.search('[A-Z]',label['text']) and label['text'].upper()==label['text'] and len(label['text'])>15)):
                continue
            if _kind(region)=='blank_lane':
                # An unruled gap after the prompt is not another response when
                # explicit answer rules immediately follow the same prompt.
                following=[r for r in lines if r['structure'].get('blank_state')=='blank' and 0<=_box(r)[1]-_box(label)[3]<(75 if re.search(r'stamp|signature',label['text'],re.I) else 35) and abs(_box(r)[0]-_box(label)[0])<40 and _box(r)[2]>=_box(label)[2]]
                if any(not any(_box(label)[3]<_cy(t)<_box(r)[1] and _box(t)[0]<_box(r)[2] and not NUMBER.fullmatch(_text(t)) and not _text(t).startswith('(') for t in text) for r in following):
                    continue
            # A PDF printed header followed by underline may be a section, but
            # a physically separate right-hand response lane is strong local
            # evidence. A border spanning over the label is not this pattern.
            label_ref=ref(label)
            prefix=re.match(r'^\s*(?:Yes|No)\s+',label['text'],re.I)
            if prefix and ':' in label['text'][prefix.end():] and any(_kind(r)=='checkbox' and abs(_cy(r)-_cy(label))<7 and _box(label)[0]<_box(r)[0]<_box(region)[0] for r in regions):
                label_ref=ref(label,prefix.end(),len(label['text']))
            field=create([label],[region],label_refs=[label_ref])
            units=[b for b in text if UNIT.fullmatch(_text(b)) and 0<=_box(b)[0]-rb[2]<10 and abs(_cy(b)-_cy(region))<6]
            if len(units)==1:field['unit_refs']=[ref(units[0])];field['unit_text_original']=units[0]['text']
        # An explicit signature caption below one line identifies the manual
        # signing lane, not a missing company answer or a date to invent.
        caption_groups=[]
        for region in lines:
            if region['block_id'] in assigned_regions or not _located_response(region):continue
            rb=_box(region)
            captions=sorted([b for b in text if re.fullmatch(r'\([^?]{2,60}\)',b['text'].strip()) and rb[0]-4<=_box(b)[0]<rb[2]-15 and -3<=_box(b)[1]-rb[3]<12],key=lambda b:_box(b)[0])
            if len(captions)<2:continue
            children_here=[]
            for index,caption in enumerate(captions):
                x0=max(rb[0],_box(caption)[0]-2);x1=_box(captions[index+1])[0]-4 if index+1<len(captions) else rb[2]
                location=deepcopy(region['location']);location.update(bbox=[x0,rb[1],x1,rb[3]],parent_region_id=region['block_id'])
                derived=make_block('ocr_text','',region['order'],location,structure={'scan_region_kind':'captioned_lane','scan_candidate':True,'scan_association_valid':True,'blank_state':'blank','content_state':'empty','source_method':'physical_line_with_printed_captions','parent_region_id':region['block_id']}).model_dump()
                blocks.append(derived);lookup[derived['block_id']]=derived
                children_here.append(create([caption],[derived]))
            assigned_regions.add(region['block_id']);caption_groups.extend(children_here)
        if caption_groups:
            first=min(_box(lookup[f['label_refs'][0]['block_id']])[1] for f in caption_groups)
            headings=[b for b in text if b['text'].endswith(':') and 0<first-_box(b)[3]<120]
            if headings:
                heading=max(headings,key=lambda b:_box(b)[3]);parent=next((f for f in additions if f['label_refs'][0]['block_id']==heading['block_id']),None)
                if parent is None:parent=new_field(heading,ref(heading));additions.append(parent)
                parent.update(field_id='scan-group:'+heading['block_id'],question_id='scan-group:'+heading['block_id'],children=caption_groups,answer_region_refs=[],answer_status='blank',field_type_hint='compound',boundary_evidence='captioned_signature_lines',boundary_version=VERSION)
                signing_refs=[rr for child in caption_groups for rr in child.get('label_refs',[]) if re.fullmatch(r'(?:signature|(?:company)?(?:stamp|seal))',re.sub(r'[\W_]+','',lookup[rr['block_id']]['text'].casefold()))]
                for child in caption_groups:
                    child['parent_question_id']=parent['field_id'];child['context_refs']=[ref(heading)]
                    if signing_refs:child['signatory_context_refs']=deepcopy(signing_refs)
                claimed.add(heading['block_id'])
        for label in text:
            if label['block_id'] in claimed:continue
            lb=_box(label)
            candidates=[r for r in lines if r['block_id'] not in assigned_regions and r['structure'].get('blank_state')=='blank' and abs(_box(r)[0]-lb[0])<5 and -2<=lb[1]-_box(r)[3]<10 and _box(r)[2]>lb[2]]
            if not re.search(r'\b(?:signature|stamp)\b|签字|盖章',_text(label),re.I):candidates=[r for r in candidates if r['structure'].get('line_style')=='dotted']
            if len(candidates)==1:create([label],candidates)

        # Long text responses are often full-width lines BELOW their prompt.
        # Use the nearest local prompt and stop at every intervening label;
        # never borrow the next nonempty text as an answer.
        for region in sorted(lines,key=lambda b:_box(b)[1]):
            if region['block_id'] in assigned_regions or not _located_response(region):continue
            rb=_box(region)
            if any(f.get('answer_region_refs') and lookup[f['label_refs'][0]['block_id']]['location']['page']==page and not f.get('options') and _box(lookup[f['answer_region_refs'][0]['block_id']])[2]<rb[0]-5 and abs(_cy(lookup[f['answer_region_refs'][0]['block_id']])-_cy(region))<5 for f in additions):continue
            candidates=[b for b in text if b['block_id'] not in section_headers|fixed|option_headers and 0<=rb[1]-_box(b)[3]<(75 if re.search(r'stamp|signature',b['text'],re.I) else 45) and _box(b)[0]>=rb[0]-15 and _box(b)[0]<rb[0]+40 and not NUMBER.fullmatch(_text(b)) and not re.fullmatch(r'\(?[a-z]\)',_text(b)) and not re.fullmatch(r'\(?[a-z]\)',_text(b)) and not OPTION.fullmatch(_text(b)) and not PLAIN_OPTIONS.fullmatch(_text(b))]
            candidates.sort(key=lambda b:_box(b)[3],reverse=True)
            if not candidates:continue
            notes=[]
            while candidates and candidates[0]['text'].lstrip().startswith('('):notes.append(candidates.pop(0))
            if not candidates:continue
            label=candidates[0]
            if label['block_id'] in claimed:continue
            if re.search('[A-Z]',label['text']) and label['text'].upper()==label['text']:continue
            previous=[b for b in text if b['block_id'] not in claimed|section_headers|fixed|option_headers and abs(_box(b)[0]-_box(label)[0])<15 and -4<=_box(label)[1]-_box(b)[3]<8 and not NUMBER.fullmatch(_text(b)) and not re.fullmatch(r'\(?[a-z]\)',_text(b)) and not _text(b).startswith('(') and not _text(b).endswith(':') and b is not label]
            labels=sorted(previous+[label],key=lambda b:_box(b)[1])
            if len(' '.join(b['text'] for b in labels))<12:continue
            f=create(labels,[region]);f['context_refs']=[ref(b) for b in notes]

        # Printed Yes/No rows and isolated question-number columns provide a
        # stronger boundary than the OCR paragraph sequence. Every number is
        # anchored to its actual source row; independent side-by-side fields
        # already resolved above are never merged into one numbered prompt.
        anchors=sorted([b for b in text if NUMBER.fullmatch(_text(b)) and _box(b)[0]<150],key=_cy)
        numbered=[]
        if len(anchors)>=3:
            for index,anchor in enumerate(anchors):
                top=_cy(anchor)-7;bottom=_cy(anchors[index+1])-7 if index+1<len(anchors) else max(_box(t)[3] for t in page_blocks)+1
                band=[b for b in text if b is not anchor and (_box(b)[0]>max(_box(anchor)[0]+10,_box(anchor)[2]-15) or re.match(r'^\s*'+re.escape(anchor['text'].rstrip('.)'))+r'\.\d+',_text(b))) and top<=_cy(b)<bottom and not NUMBER.fullmatch(_text(b)) and not re.fullmatch(r'\(?[a-z]\)',_text(b))]
                if not band:continue
                inline=[b for b in band if abs(_cy(b)-_cy(anchor))<7 and not PLAIN_OPTIONS.fullmatch(_text(b))]
                if not inline:continue
                start=min(inline,key=lambda b:_box(b)[0])
                if re.fullmatch(r'\d+(?:\.0)?',anchor['text'].rstrip('.')) and re.search('[A-Za-z]',start['text']) and len(start['text'])<90 and not PLAIN_OPTIONS.fullmatch(start['text']):
                    section_headers.add(start['block_id']);start.setdefault('structure',{})['resolved_layout_role']='section_header';fixed.add(anchor['block_id']);continue
                owned=[f for f in additions if any(r['block_id'] in {b['block_id'] for b in band} for r in f['label_refs'])]
                # A numbered question can have an independent inline field to
                # its right (for example an inspection date and its own rule).
                # The physical label/response relationship established above
                # takes precedence over the broader numbered vertical band.
                # This is not a keyword rule and does not affect responses
                # below the main prompt, or labels sharing one response lane.
                separate=[]
                for current in owned:
                    labels_here=[lookup[r['block_id']] for r in current['label_refs']]
                    targets=[lookup[r['block_id']] for r in current.get('answer_region_refs',[])]
                    if (labels_here and targets and not current.get('options')
                        and all(b['block_id']!=start['block_id'] and _box(b)[0]>=_box(start)[2]
                                and abs(_cy(b)-_cy(start))<7 for b in labels_here)
                        and all(_kind(r) in {'write_line','text_box'} and _located_response(r)
                                and r.get('structure',{}).get('scan_field_id')==current['field_id']
                                and _box(r)[0]>=max(_box(b)[2] for b in labels_here)-2
                                and all(_box(r)[1]-4<=_cy(b)<=_box(r)[3]+4 for b in labels_here)
                                for r in targets)):
                        separate.append(current)
                if separate:
                    separate_labels={r['block_id'] for f in separate for r in f['label_refs']}
                    band=[b for b in band if b['block_id'] not in separate_labels]
                    owned=[f for f in owned if f not in separate]
                if len(owned)>=2:
                    prefix=anchor['text'].rstrip('.)')
                    nested=[f for f in owned if re.match(r'^\s*'+re.escape(prefix)+r'\.\d+(?!\d)',f['question_text_original'])]
                    if len(nested)>=2:
                        first_y=min(_box(lookup[f['label_refs'][0]['block_id']])[1] for f in nested)
                        parent_labels=[b for b in band if _box(b)[1]<first_y and not PLAIN_OPTIONS.fullmatch(_text(b)) and b['block_id'] not in option_headers]
                        if parent_labels:
                            parent=new_field(parent_labels[0],ref(parent_labels[0]));fid='scan-group:'+parent_labels[0]['block_id']
                            parent.update(field_id=fid,question_id=fid,label_refs=[ref(b) for b in parent_labels],question_refs=[ref(b) for b in parent_labels],question_text_original='\n'.join(b['text'] for b in parent_labels),children=nested,field_type_hint='compound',answer_status='blank',original_number=prefix,number_refs=[ref(anchor)],boundary_evidence='numbered_child_group',boundary_version=VERSION)
                            for child in nested:child['parent_question_id']=fid
                            additions.append(parent);claimed.update(b['block_id'] for b in parent_labels);claimed.add(anchor['block_id'])
                    continue
                if owned and owned[0].get('options'):
                    current=owned[0];option_top=min(_box(lookup[r['block_id']])[1] for r in current['answer_region_refs'])
                    prefix_labels=[b for b in band if b['block_id'] not in option_headers and not PLAIN_OPTIONS.fullmatch(_text(b)) and (_box(b)[3]<option_top or b['block_id'] in {rr['block_id'] for rr in current['label_refs']})]
                    if prefix_labels:
                        current['label_refs']=[ref(b) for b in prefix_labels];current['question_refs']=deepcopy(current['label_refs']);current['question_text_original']='\n'.join(b['text'] for b in prefix_labels);claimed.update(b['block_id'] for b in prefix_labels)
                    current['original_number']=anchor['text'].rstrip('.)');current['number_refs']=[ref(anchor)];claimed.add(anchor['block_id']);numbered.append(current);continue
                choice_blocks=[b for b in band if PLAIN_OPTIONS.fullmatch(_text(b))]
                labels=[b for b in band if b not in choice_blocks and b['block_id'] not in option_headers and _box(b)[0]<_box(start)[0]+20]
                # A separate If/Please follow-up in this numbered group is
                # kept as a child, not folded into a previous sentence.
                follows=[b for b in labels[1:] if re.match(r'^\s*(?:if\s*(?:yes|no)|ifnot|please\s+(?:give|provide|specify|describe))\b',b['text'],re.I)]
                cutoff=_box(follows[0])[1] if follows else bottom
                main=[b for b in labels if _box(b)[1]<cutoff]
                if not main:continue
                main.sort(key=lambda b:(_box(b)[1],_box(b)[0]))
                if owned and {r['block_id'] for r in owned[0]['label_refs']}=={b['block_id'] for b in main}:
                    field=owned[0]
                else:
                    owned_regions={r['block_id'] for old in owned for r in old.get('answer_region_refs',[])}
                    regions_here=[r for r in regions if (r['block_id'] not in assigned_regions or r['block_id'] in owned_regions) and _kind(r)=='write_line' and r['structure'].get('blank_state')=='blank' and max(_box(b)[3] for b in main)<=_box(r)[3]<cutoff and _box(r)[0]>=_box(start)[0]-10]
                    field=create(main,regions_here) if regions_here else new_field(main[0],ref(main[0]))
                    if not regions_here:
                        field.update(field_id='scan-field:'+main[0]['block_id'],question_id='scan-field:'+main[0]['block_id'],label_refs=[ref(b) for b in main],question_refs=[ref(b) for b in main],question_text_original='\n'.join(b['text'] for b in main),boundary_evidence='scan_numbered_prompt',boundary_version=VERSION)
                        additions.append(field);claimed.update(b['block_id'] for b in main)
                    if owned:
                        additions[:]=[f for f in additions if f not in owned]
                        field['children'].extend(child for old in owned for child in old.get('children',[]))
                if not field.get('options'):
                    # Preserve every physical response rule inside this explicit
                    # numbered boundary, including a first line whose scan skew
                    # narrowly misses the loose paragraph alignment heuristic.
                    existing={r['block_id'] for r in field.get('answer_region_refs',[])}
                    for r in regions:
                        if r['block_id'] in assigned_regions or _kind(r)!='write_line' or r['structure'].get('blank_state')!='blank':continue
                        if max(_box(b)[3] for b in main)<=_box(r)[3]<cutoff and _box(r)[0]>=_box(start)[0]-10:
                            field['answer_region_refs'].append(ref(r));r['structure'].update(scan_association_valid=True,scan_field_id=field['field_id']);assigned_regions.add(r['block_id'])
                    if field['answer_region_refs']:field['answer_status']='blank'
                    field['answer_region_refs'].sort(key=lambda rr:_box(lookup[rr['block_id']])[1])
                number=anchor['text'].rstrip('.)');field['original_number']=number;field['number_refs']=[ref(anchor)];claimed.add(anchor['block_id']);numbered.append(field)
                matching=[b for b in choice_blocks if _box(b)[1]<cutoff]
                if len(matching)==1:
                    choice=matching[0];options=[]
                    for m in re.finditer(r'Yes|Ycs|No|N\s*/?\s*A',choice['text'],re.I):
                        rr=ref(choice,m.start(),m.end(),choice['block_id']+f':printed-option:{m.start()}')
                        choice.setdefault('structure',{}).setdefault('logical_slots',[]).append({'slot_id':rr['slot_id'],'kind':'printed_option','start':m.start(),'end':m.end(),'option_label':m.group()})
                        options.append({'refs':[ref(choice,m.start(),m.end())],'text_original':m.group(),'selected':False,'selection_source':'printed_choice_list','answer_region_refs':[rr]})
                    field['options']=options;field['answer_region_refs'].extend(r for o in options for r in o['answer_region_refs']);field['answer_status']='blank';option_headers.add(choice['block_id'])
                for follow in follows:
                    if follow['block_id'] in claimed:continue
                    next_top=min((_box(t)[1] for t in follows if _box(t)[1]>_box(follow)[1]),default=bottom)
                    children_regions=[r for r in regions if r['block_id'] not in assigned_regions and _kind(r)=='write_line' and r['structure'].get('blank_state')=='blank' and _box(follow)[3]<=_box(r)[3]<next_top and _box(r)[0]>=_box(follow)[0]-10]
                    if children_regions:
                        child=create([follow],children_regions);child['parent_question_id']=field['field_id'];child['context_label']=field['question_text_original'];child['explicit_condition']={'status':'unresolved','source_text':follow['text'],'parent_field_id':field['field_id']};field['children'].append(child)
            for f in numbered:
                parts=f['original_number'].split('.')
                if len(parts)<3:continue
                parent=next((p for p in numbered if p['original_number']=='.'.join(parts[:-1])),None)
                if parent:f['parent_question_id']=parent['field_id'];f['context_label']=parent['question_text_original'];parent['children'].append(f)
        # Continue an already established multiline response lane until the
        # next independent label intersects the same vertical band.
        for f in [x for x in additions if lookup[x['label_refs'][0]['block_id']]['location']['page']==page and not x['options']]:
            owned=[lookup[r['block_id']] for r in f['answer_region_refs']]
            if len(owned)!=1 or _kind(owned[0])!='write_line':continue
            last=owned[0];lb=_box(last)
            for r in sorted(regions,key=lambda b:_box(b)[1]):
                rb=_box(r)
                if r['block_id'] in assigned_regions or _kind(r)!='write_line' or r['structure'].get('blank_state')!='blank':continue
                if not (0<rb[1]-lb[3]<28 and (abs(rb[0]-lb[0])<5 or abs(rb[0]-_box(lookup[f['label_refs'][0]['block_id']])[0])<5) and abs(rb[2]-lb[2])<12):continue
                if any(lb[3]-2<_cy(t)<rb[3]+2 and _box(t)[0]<rb[2] for t in text):break
                f['answer_region_refs'].append(ref(r));r['structure'].update(scan_association_valid=True,scan_field_id=f['field_id']);assigned_regions.add(r['block_id']);lb=rb

        for f in [x for x in additions if lookup[x['label_refs'][0]['block_id']]['location']['page']==page]:
            label=lookup[f['label_refs'][0]['block_id']]
            prior=[t for t in text if t['block_id'] in section_headers and _box(t)[3]<_box(label)[1]]
            if prior:
                section=max(prior,key=lambda b:_box(b)[1]);f['section_refs']=[ref(section)];f['section_text_original']=section['text'];f['context_label']=section['text']

        # Repeated rows can have a second response column with one printed
        # column heading. Its source is shared, its physical slots/IDs are not.
        for region in regions:
            if region['block_id'] in assigned_regions or _kind(region)!='write_line' or region['structure'].get('blank_state')!='blank':continue
            rb=_box(region)
            row_fields=[f for f in additions if f.get('label_refs') and lookup[f['label_refs'][0]['block_id']]['location']['page']==page and not f.get('options') and len(f['answer_region_refs'])==1 and _box(lookup[f['answer_region_refs'][0]['block_id']])[2]<rb[0]-5 and abs(_cy(lookup[f['answer_region_refs'][0]['block_id']])-_cy(region))<5]
            if len(row_fields)!=1:continue
            headers=[b for b in text if b['block_id'] not in claimed and len(b['text'])<50 and rb[0]-8<=_box(b)[0]<rb[2] and 0<rb[1]-_box(b)[3]<110 and not NUMBER.fullmatch(_text(b)) and not re.fullmatch(r'\(?[a-z]\)',_text(b))]
            if not headers:continue
            header=max(headers,key=lambda b:_box(b)[3]);parent=row_fields[0]
            child=create([header],[region]);child['field_id']=child['question_id']='scan-field:'+header['block_id']+':'+region['block_id'];child['parent_question_id']=parent['field_id'];child['context_refs']=deepcopy(parent['label_refs']);child['context_label']=parent['question_text_original'];parent['children'].append(child)
            # The same physical column header applies to following rows too.
            claimed.discard(header['block_id'])

        # A raster grid of reserved record cells is a table, not alternating
        # question/answer strings. Empty rows retain their source slots and do
        # not assert that the supplier must provide that many records.
        empty_boxes=[b for b in regions if _kind(b)=='text_box' and b['structure'].get('blank_state')=='blank' and b['block_id'] not in assigned_regions]
        grid=[]
        for box in sorted(empty_boxes,key=lambda b:(_box(b)[1],_box(b)[0])):
            row=next((r for r in grid if abs(_box(r[0])[1]-_box(box)[1])<4),None)
            if row is None:grid.append([box])
            else:row.append(box)
        grid=[sorted(r,key=lambda b:_box(b)[0]) for r in grid if len(r)>=3]
        if len(grid)>=2 and len({len(r) for r in grid})==1:
            first=grid[0];headers=[]
            for cell in first:
                rb=_box(cell);matches=[b for b in text if b['block_id'] not in fixed|section_headers and rb[0]-3<=(_box(b)[0]+_box(b)[2])/2<=rb[2]+3 and -3<=rb[1]-_box(b)[3]<28 and len(b['text'])<60]
                if not matches:headers=[];break
                headers.append(max(matches,key=lambda b:_box(b)[3]))
            if headers and len({h['block_id'] for h in headers})==len(headers):
                parent=new_field(headers[0],ref(headers[0]));fid='scan-records:'+first[0]['block_id']
                parent.update(field_id=fid,question_id=fid,label_refs=[ref(h) for h in headers],question_refs=[ref(h) for h in headers],question_text_original=' | '.join(h['text'] for h in headers),field_type_hint='repeating_record',answer_status='blank',record_columns=[{'label':h['text'],'label_ref':ref(h),'grid_col_start':i,'grid_col_end':i} for i,h in enumerate(headers,1)],record_rows=[],record_context_values=[],required_status='unknown',boundary_evidence='scan_empty_record_grid',boundary_version=VERSION)
                for rownum,row in enumerate(grid,1):
                    rowids=[]
                    for col,(header,cell) in enumerate(zip(headers,row),1):
                        child=create([header],[cell]);childid=fid+':'+cell['block_id'];child.update(field_id=childid,question_id=childid,parent_question_id=fid,record_index=rownum,record_column_index=col,record_slot_status='reserved',record_context=f'记录行 {rownum}',required_status='unknown',context_label=f'记录行 {rownum}',field_type_hint='short_text');rowids.append(childid);parent['children'].append(child)
                    parent['record_rows'].append({'row':rownum,'field_ids':rowids,'reserved_blank_row':True})
                additions.append(parent);claimed.update(h['block_id'] for h in headers)
                # Whole-width row rules belong to this grid, not six answers
                # to its first column header. Keep every cell child exactly
                # once under the record table.
                header_ids={h['block_id'] for h in headers}
                additions[:]=[f for f in additions if f is parent or f.get('parent_question_id') or not f.get('label_refs') or not all(r['block_id'] in header_ids for r in f['label_refs'])]
                # The rule just above printed column headings is the grid's
                # top border, not a response to the preceding introduction.
                x0,x1=_box(first[0])[0],_box(first[-1])[2];header_top=min(_box(h)[1] for h in headers)
                borders=[r for r in lines if abs(_box(r)[0]-x0)<4 and abs(_box(r)[2]-x1)<4 and 0<=header_top-_box(r)[3]<8]
                border_ids={r['block_id'] for r in borders};removed=set()
                for r in borders:r['structure'].update(scan_association_valid=False,nonfillable_reason='Top border of a verified record-table grid')
                for old in additions:
                    if not any(rr['block_id'] in border_ids for rr in old.get('answer_region_refs',[])):continue
                    old['answer_region_refs']=[rr for rr in old['answer_region_refs'] if rr['block_id'] not in border_ids]
                    if not old['answer_region_refs'] and not old.get('children'):
                        parent.setdefault('context_refs',[]).extend(deepcopy(old.get('label_refs',[])));removed.add(old['field_id'])
                        for rr in old.get('label_refs',[]):lookup[rr['block_id']].setdefault('structure',{})['resolved_layout_role']='record_table_instruction'
                additions[:]=[f for f in additions if f['field_id'] not in removed]
                for f in additions:f['children']=[c for c in f.get('children',[]) if c['field_id'] not in removed]

        # Lettered subsection captions delimit local scope and repeated child
        # rows. A multi-column details block stays a section; a single lane of
        # short repeated fields becomes a compound group with no invented value.
        lettered=[]
        for letter in text:
            local_field=next((f for f in additions if any(r['block_id']==letter['block_id'] for r in f.get('label_refs',[])) and f.get('answer_region_refs')),None)
            if re.match(r'^\s*[A-Z]\s*[-–]\s*\d+\s*',_text(letter)) and '?' not in _text(letter) and (
                _text(letter).endswith(':') or (local_field is None and len(letter['text'])<140 and not re.match(r'^\s*[A-Z]\s*[-–]\s*\d+\s*(?:if|when)\b',_text(letter),re.I))):
                lettered.append((letter,letter));continue
            if not re.fullmatch(r'\(?[a-z]\)',_text(letter)) or _box(letter)[0]>100:continue
            labels=[b for b in text if _box(b)[0]>_box(letter)[2]+8 and abs(_cy(b)-_cy(letter))<5]
            if len(labels)==1 and len(labels[0]['text'])<100:lettered.append((letter,labels[0]))
        lettered.sort(key=lambda p:_cy(p[0]))
        for index,(letter,heading) in enumerate(lettered):
            top=_box(heading)[3];bottom=_box(lettered[index+1][0])[1] if index+1<len(lettered) else max(_box(b)[3] for b in text)
            next_question=min((_box(b)[1] for b in text if top<_box(b)[1]<bottom and ('?' in b['text'] or len(b['text'])>90)),default=bottom)
            scoped=[f for f in additions if not f.get('parent_question_id') and lookup[f['label_refs'][0]['block_id']]['location']['page']==page and top<_cy(lookup[f['label_refs'][0]['block_id']])<bottom]
            for f in scoped:
                f['section_refs']=[ref(heading)];f['section_text_original']=heading['text'];f['context_label']=heading['text']
            local=[f for f in scoped if _cy(lookup[f['label_refs'][0]['block_id']])<next_question]
            lanes={round(_box(lookup[f['label_refs'][0]['block_id']])[0]/25) for f in local}
            if len(local)>=2 and len(lanes)==1 and all(len(f['question_text_original'])<80 and not f.get('options') for f in local):
                existing=next((f for f in additions if f['label_refs'][0]['block_id']==heading['block_id']),None)
                parent=existing or new_field(heading,ref(heading));parent.update(field_id='scan-group:'+heading['block_id'],question_id='scan-group:'+heading['block_id'],children=local,answer_region_refs=[],answer_status='blank',field_type='compound',field_type_hint='compound',boundary_evidence='scan_lettered_repeated_rows',boundary_version=VERSION)
                for child in local:child['parent_question_id']=parent['field_id'];child['context_refs']=[ref(heading)]
                if not existing:additions.append(parent)
                claimed.add(heading['block_id']);claimed.add(letter['block_id'])
            elif len(scoped)>=2:
                section_headers.add(heading['block_id']);fixed.add(letter['block_id'])
                additions[:]=[f for f in additions if f['label_refs'][0]['block_id']!=heading['block_id'] or not f.get('answer_region_refs') or any(_kind(lookup[r['block_id']])!='blank_lane' for r in f['answer_region_refs'])]

    # A detector can split a long printed line horizontally as well as wrap
    # it vertically. Preserve the adjacent source fragments in the label;
    # never jump over an answer slot or another independently claimed field.
    for field in additions:
        if field.get('record_columns'):continue
        refs=deepcopy(field.get('label_refs',[]));expanded=list(refs)
        for rr in expanded:
            base=lookup[rr['block_id']];bb=_box(base);page=base['location']['page']
            limit=min((_box(lookup[r['block_id']])[0] for r in field.get('answer_region_refs',[]) if abs(_cy(lookup[r['block_id']])-_cy(base))<8 and _box(lookup[r['block_id']])[0]>bb[0]),default=10000)
            for other in pages[page]:
                ob=_box(other)
                if not _text(other) or other['block_id'] in claimed|fixed|section_headers|option_headers or other is base:continue
                if ob[2]>limit or len(other['text'])<8 or abs(_cy(other)-_cy(base))>4 or not -12<=ob[0]-bb[2]<=12:continue
                if OPTION.fullmatch(_text(other)) or PLAIN_OPTIONS.fullmatch(_text(other)):continue
                expanded.append(ref(other));claimed.add(other['block_id']);other.setdefault('structure',{})['protected_label_ranges']=[{'start':0,'end':len(other['text'])}]
        if len(expanded)>len(refs):
            expanded.sort(key=lambda r:(_box(lookup[r['block_id']])[1],_box(lookup[r['block_id']])[0]))
            field.update(label_refs=expanded,question_refs=deepcopy(expanded),question_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in expanded))

    # A supplemental lane to the right of an explicit choice row belongs to
    # that local row (expiry, description if No, etc.). Repeated "valid until"
    # captions retain independent identities and their parent certification.
    children={c['field_id'] for f in additions for c in f.get('children',[])}
    for f in additions:
        if f['options'] or f['field_id'] in children:continue
        label=lookup[f['label_refs'][0]['block_id']];lb=_box(label)
        parents=[]
        for parent in additions:
            if not parent['options']:continue
            regions=[lookup[r['block_id']] for r in parent['answer_region_refs']]
            target_left=min((_box(lookup[r['block_id']])[0] for r in f.get('answer_region_refs',[])),default=lb[0])
            shared_option_label=any(rr['block_id']==label['block_id'] for o in parent.get('options',[]) for rr in o.get('refs',[]))
            right_bound=target_left if shared_option_label else lb[0]+3
            target_right=max((_box(lookup[r['block_id']])[2] for r in f.get('answer_region_refs',[])),default=lb[2])
            same_line=[r for r in regions if abs(_cy(r)-_cy(label))<7]
            between=(any(_box(r)[2]<lb[0] for r in same_line) and any(_box(r)[0]>target_right for r in same_line)
                and 0<lb[1]-_box(lookup[parent['label_refs'][0]['block_id']])[1]<40)
            if regions[0]['location']['page']==label['location']['page'] and (max(_box(r)[2] for r in regions)<right_bound or between) and min(abs(_cy(r)-_cy(label)) for r in regions)<7:
                parents.append(parent)
        if len(parents)==1:
            parent=parents[0];f['parent_question_id']=parent['field_id'];f['context_label']=parent['question_text_original'];f['semantic_context']=parent['question_text_original'];f['context_refs']=deepcopy(parent['label_refs'])
            if re.search(r'if\s*(?:yes|no)\b',label['text'],re.I):
                f['explicit_condition']={'status':'unresolved','source_text':label['text'],'parent_field_id':parent['field_id']}
            parent['children'].append(f);children.add(f['field_id'])
        elif not parents and re.match(r'\s*if\s*(?:yes|no)\b',label['text'],re.I):
            previous=[p for p in additions if p.get('options') and lookup[p['label_refs'][0]['block_id']]['location']['page']==label['location']['page'] and 0<lb[1]-max(_box(lookup[r['block_id']])[3] for r in p['answer_region_refs'])<45]
            if previous:
                parent=max(previous,key=lambda p:max(_box(lookup[r['block_id']])[3] for r in p['answer_region_refs']))
                f['parent_question_id']=parent['field_id'];f['context_refs']=deepcopy(parent['label_refs']);f['context_label']=parent['question_text_original'];f['explicit_condition']={'status':'unresolved','source_text':label['text'],'parent_field_id':parent['field_id']};parent['children'].append(f);children.add(f['field_id'])

    # A wrapped parent can end far below the first-line Yes/No captions.
    # Resolve an immediately following explicit If Yes/No against the complete
    # label boundary, including follow-ups that have their own choice options.
    for f in additions:
        if f['field_id'] in children or f.get('parent_question_id') or not f.get('label_refs'):continue
        first=lookup[f['label_refs'][0]['block_id']];fb=_box(first)
        if not re.match(r'\s*if\s*(?:yes|no)\b',first['text'],re.I):continue
        candidates=[]
        for parent in additions:
            if parent is f or not parent.get('options') or not parent.get('label_refs'):continue
            labels=[lookup[rr['block_id']] for rr in parent['label_refs']]
            if any(b['location']['page']!=first['location']['page'] for b in labels):continue
            end=max(_box(b)[3] for b in labels);left=min(_box(b)[0] for b in labels)
            if not 0<=fb[1]-end<22 or abs(left-fb[0])>=20:continue
            intervening=any(other is not f and other is not parent and other.get('label_refs') and
                lookup[other['label_refs'][0]['block_id']]['location']['page']==first['location']['page'] and
                end<_box(lookup[other['label_refs'][0]['block_id']])[1]<fb[1]-1 for other in additions)
            if not intervening:candidates.append(parent)
        if len(candidates)==1:
            parent=candidates[0]
            f.update(parent_question_id=parent['field_id'],context_refs=deepcopy(parent['label_refs']),context_label=parent['question_text_original'],
                explicit_condition={'status':'unresolved','source_text':first['text'],'parent_field_id':parent['field_id']})
            parent['children'].append(f);children.add(f['field_id'])

    # OCR sometimes includes the printed question number in the first line,
    # leaving a hanging-indent tail beside the options. Restore that prefix
    # from its actual adjacent source, never from a text guess.
    for f in additions:
        if not f.get('label_refs'):continue
        first=lookup[f['label_refs'][0]['block_id']];fb=_box(first)
        prefixes=[b for b in pages[first['location']['page']] if b['block_id'] not in claimed|fixed|section_headers|option_headers and re.match(r'^\s*\d+(?:\.\d+)+\s*[A-Za-z\u4e00-\u9fff]',_text(b)) and '?' not in b['text'] and -1<=fb[1]-_box(b)[3]<14 and 0<=fb[0]-_box(b)[0]<45]
        if len(prefixes)==1:
            prefix=prefixes[0];f['label_refs'].insert(0,ref(prefix));f['question_refs']=deepcopy(f['label_refs']);f['question_text_original']='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in f['label_refs']);claimed.add(prefix['block_id'])

    # Keep document titles and bounded instruction paragraphs in the native
    # ledger, rather than accepting model remnants as unanswered questions.
    instructions=set();titles=set();context_headers=set()
    for page,page_blocks in pages.items():
        body=sorted([b for b in page_blocks if _text(b) and not _kind(b)],key=lambda b:(_box(b)[1],_box(b)[0]))
        for i,b in enumerate(body):
            value=re.sub(r'\s+','',b['text']).casefold();bb=_box(b)
            if bb[1]<100 and re.match(r'(?:self-assessment)?questionnaire',value) and '?' not in value:
                titles.add(b['block_id']);b.setdefault('structure',{})['resolved_layout_role']='title'
            compact=re.sub(r'[\s,:：]','',b['text']).casefold()
            explicit_instruction=re.match(r'\(?(?:kindly(?:send|return)|pleasealsomention|note|thisquestionnaireshould|fortheassessment|additionalinformationorcomments|if(?:yes|no)(?:please)?(?:complete(?:part|pages)|provide(?:the)?(?:questionnaire|declaration|copy)))',compact)
            if explicit_instruction and b['block_id'] not in claimed|fixed|section_headers|option_headers:
                instructions.add(b['block_id']);last=b
                if re.match(r'kindlyreturn',compact):
                    for adjacent in body:
                        ab=_box(adjacent)
                        if 0<=ab[0]-bb[2]<35 and abs(_cy(adjacent)-_cy(b))<5 and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',_text(adjacent)):
                            instructions.add(adjacent['block_id'])
                for follow in body[i+1:]:
                    fb=_box(follow)
                    if last['text'].rstrip().endswith(('.',')','。','）')) or fb[1]-_box(last)[3]>6:break
                    if abs(fb[0]-bb[0])>15:continue
                    if follow['block_id'] in claimed|fixed|section_headers|option_headers or fb[1]-_box(last)[3]<-3 or NUMBER.fullmatch(_text(follow)):break
                    if re.match(r'\s*(?:if\b|does\b|is\b|are\b|can\b)',follow['text'],re.I):break
                    instructions.add(follow['block_id']);last=follow
            if re.match(r'pleasecompletepages?\d',value):
                instructions.add(b['block_id'])
                for follow in body[i+1:]:
                    if not (0<=_box(follow)[1]-bb[3]<5 and abs(_box(follow)[0]-bb[0])<12):break
                    instructions.add(follow['block_id']);bb=_box(follow)
            if re.match(r'(?:please)?(?:provide|attach|enclose)(?:a|the)?copy',value) or (re.search(r'\((?:please)?encloseacopy\)',value) and '?' not in value):
                if not any(b['block_id']==rr['block_id'] and f.get('answer_region_refs') for f in additions for rr in f.get('label_refs',[])):
                    instructions.add(b['block_id']);b.setdefault('structure',{})['resolved_layout_role']='attachment_requirement'
            if re.match(r'\(?if(?:yes|no):?please(?:enclose|attach)',value) or re.match(r'pleaseattachtothecompletedquestionnaire',value):
                instructions.add(b['block_id'])
            if re.match(r'\(?if(?:yes|no):?please(?:enclose|attach)',value):
                last=b
                for follow in body[i+1:]:
                    fb=_box(follow)
                    if last['text'].rstrip().endswith(('.',')')) or fb[1]-_box(last)[3]>7 or abs(fb[0]-_box(b)[0])>15 or NUMBER.fullmatch(_text(follow)) or '?' in follow['text'] or re.match(r'if\s',follow['text'],re.I):break
                    instructions.add(follow['block_id']);last=follow
            if re.match(r'pleaseattachtothecompletedquestionnaire',value):
                for follow in body[i+1:]:
                    fb=_box(follow)
                    if fb[1]-bb[3]>30 or fb[1]-_box(b)[1]>110 or (follow['text'].rstrip().endswith(':') and fb[0]<_box(b)[0]+10):break
                    if _box(b)[0]<fb[0]<_box(b)[0]+45:instructions.add(follow['block_id']);bb=fb
            if b['text'].lstrip().startswith('(') and not b['text'].rstrip().endswith(')') and '?' not in b['text']:
                sequence=[b]
                for follow in body[i+1:]:
                    fb=_box(follow)
                    if fb[1]-bb[3]>15 or fb[1]-_box(b)[1]>85 or NUMBER.fullmatch(_text(follow)):break
                    sequence.append(follow);bb=fb
                    if follow['text'].rstrip().endswith(')'):
                        if not any('?' in item['text'] for item in sequence):instructions.update(item['block_id'] for item in sequence)
                        break
    # Numbered group headings and conditional introductions are context,
    # not writable whitespace. Connect their following real fields while
    # preserving each independent answer target.
    for page,page_blocks in pages.items():
        body=sorted([b for b in page_blocks if _text(b) and b['block_id'] not in fixed|option_headers|section_headers],key=lambda b:_box(b)[1])
        for heading in body:
            hb=_box(heading);value=_text(heading)
            if heading['block_id'] in claimed or not value.endswith(':') or '?' in value or NUMBER.fullmatch(value):continue
            same_number=[b for b in body if NUMBER.fullmatch(_text(b)) and _box(b)[2]<hb[0]+5 and abs(_cy(b)-_cy(heading))<6]
            next_numbers=[b for b in body if (NUMBER.fullmatch(_text(b)) or re.match(r'^\s*\d+(?:\.\d+)+\s*[A-Za-z\u4e00-\u9fff]',_text(b))) and _box(b)[0]<hb[0]+10 and _box(b)[1]>hb[3]]
            bottom=min((_box(b)[1]-5 for b in next_numbers),default=hb[3]+180)
            scoped=[f for f in additions if not f.get('parent_question_id') and f.get('label_refs') and lookup[f['label_refs'][0]['block_id']]['location']['page']==page and hb[3]<=_box(lookup[f['label_refs'][0]['block_id']])[1]<bottom and _box(lookup[f['label_refs'][0]['block_id']])[0]>=hb[0]-5]
            # Two or more lettered/bulleted local choice rows substantiate a
            # group. A lone heading may only supply context to the next field.
            if same_number and len(scoped)>=2 and all(f.get('options') for f in scoped):
                parent=new_field(heading,ref(heading));fid='scan-group:'+heading['block_id'];parent.update(field_id=fid,question_id=fid,children=scoped,answer_region_refs=[],answer_status='blank',field_type_hint='compound',original_number=_text(same_number[0]),number_refs=[ref(same_number[0])],boundary_evidence='numbered_choice_group',boundary_version=VERSION)
                for child in scoped:child['parent_question_id']=fid;child.setdefault('context_refs',[]).append(ref(heading));children.add(child['field_id'])
                additions.append(parent);claimed.add(heading['block_id']);claimed.add(same_number[0]['block_id']);continue
            following=[f for f in scoped if 0<=_box(lookup[f['label_refs'][0]['block_id']])[1]-hb[3]<45]
            if not following and page+1 in pages and heading is body[-1]:
                next_body=[b for b in pages[page+1] if _text(b) and b['block_id'] not in fixed|option_headers|section_headers and not NUMBER.fullmatch(_text(b))]
                if next_body:
                    top=min(_box(b)[1] for b in next_body)
                    following=[f for f in additions if f.get('label_refs') and lookup[f['label_refs'][0]['block_id']]['location']['page']==page+1 and abs(_box(lookup[f['label_refs'][0]['block_id']])[1]-top)<8]
            if following:
                first=min(following,key=lambda f:_box(lookup[f['label_refs'][0]['block_id']])[1]);first.setdefault('context_refs',[]).append(ref(heading));context_headers.add(heading['block_id']);heading.setdefault('structure',{})['resolved_layout_role']='conditional_context' if re.match(r'(?:incase|if|[A-Z]-?\d+if)',re.sub(r'\s+','',value),re.I) else 'group_context'
    additions[:]=[f for f in additions if not f.get('answer_region_refs') and not f.get('children') and not any(rr['block_id'] in instructions for rr in f.get('label_refs',[])) or f.get('answer_region_refs') or f.get('children')]
    for bid in instructions:lookup[bid].setdefault('structure',{}).setdefault('resolved_layout_role','instruction')
    # Replace whole model groups only when their actual label sources were
    # geometrically resolved. All other source content remains in the ledger.
    remaining=[]
    for f in questions:
        labels=f.get('label_refs') or f.get('question_refs') or []
        # A model can mistake a typed answer layer for another question.
        # Its physical overlay relation is established before this decision;
        # keep the original text/diagnostics but never duplicate it as a field.
        if labels and all(r.get('block_id') in verified_values or lookup.get(r.get('block_id'),{}).get('structure',{}).get('resolved_layout_role') in {'checkbox_symbol','invisible_native_mark'} for r in labels):continue
        if any(r.get('block_id') in claimed for r in labels):continue
        if labels and all(r.get('block_id') in instructions|titles|fixed|section_headers|option_headers|context_headers for r in labels):continue
        if labels and all(r.get('block_id') in fixed for r in labels):continue
        if labels and all(r.get('block_id') in option_headers|section_headers|fixed or not lookup.get(r.get('block_id'),{}).get('text','').strip() for r in labels):continue
        # Zero-length geometry cannot be a question text range.
        clean=[r for r in labels if r.get('start',0)<r.get('end',0) and r.get('block_id') not in fixed]
        if clean and all(r.get('block_id') in verified_values or lookup.get(r.get('block_id'),{}).get('structure',{}).get('resolved_layout_role') in {'checkbox_symbol','invisible_native_mark'} for r in clean):continue
        if clean and all(r.get('block_id') in fixed for r in clean):continue
        if clean!=labels:
            f['label_refs']=deepcopy(clean);f['question_refs']=deepcopy(clean)
            f['question_text_original']='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in clean)
        contaminated=any(r.get('block_id') in fixed for key in ('answer_region_refs','answer_value_refs','answer_refs') for r in f.get(key,[]))
        contaminated=contaminated or any(r.get('block_id') in fixed for option in f.get('options',[]) for key in ('refs','answer_region_refs') for r in option.get(key,[]))
        if contaminated:
            f.update(answer_region_refs=[r for r in f.get('answer_region_refs',[]) if r.get('block_id') not in fixed],
                options=[o for o in f.get('options',[]) if not any(r.get('block_id') in fixed for key in ('refs','answer_region_refs') for r in o.get(key,[]))],
                answer_refs=[],answer_value_refs=[],answer_text_original='',answer_status='unresolved',review_status='needs_review')
            f.setdefault('review_reasons',[]).append('原配对引用了固定页码文字或其字形轮廓；已保留问题并解除错误答复关联。')
        if clean and not f.get('answer_region_refs') and all(UNIT.fullmatch(_text(lookup[r['block_id']])) for r in clean) and any(rr.get('block_id') in {r['block_id'] for r in clean} for candidate in additions for rr in candidate.get('unit_refs',[])):
            continue
        if clean and not f.get('answer_region_refs') and any(NUMBER.fullmatch(_text(lookup[r['block_id']])) for r in clean) and all(NUMBER.fullmatch(_text(lookup[r['block_id']])) or OPTION.fullmatch(_text(lookup[r['block_id']])) or PLAIN_OPTIONS.fullmatch(_text(lookup[r['block_id']])) or re.fullmatch(r'(?:(?:yes|no|n/?a))+',re.sub(r'\s+','',_text(lookup[r['block_id']])),re.I) for r in clean):
            continue
        if clean and all(NUMBER.fullmatch(_text(lookup[r['block_id']])) for r in clean):
            number_block=lookup[clean[0]['block_id']]
            neighbors=[candidate for candidate in additions if candidate.get('label_refs') and lookup[candidate['label_refs'][0]['block_id']]['location']['page']==number_block['location']['page'] and abs(_cy(lookup[candidate['label_refs'][0]['block_id']])-_cy(number_block))<12 and _box(lookup[candidate['label_refs'][0]['block_id']])[0]>_box(number_block)[0]]
            if neighbors:
                number_block.setdefault('structure',{})['resolved_layout_role']='question_number'
                continue
        if clean and not f.get('answer_region_refs'):
            block=lookup[clean[0]['block_id']]
            if re.search(r'questionnaire\s*assessment\s*by',block['text'],re.I) and block['text'].rstrip().endswith(':'):
                below=[c for c in additions if c.get('answer_region_refs') and lookup[c['label_refs'][0]['block_id']]['location']['page']==block['location']['page'] and 0<_box(lookup[c['label_refs'][0]['block_id']])[1]-_box(block)[3]<35]
                if below:
                    block.setdefault('structure',{})['resolved_layout_role']='section_header'
                    section_headers.add(block['block_id']);continue
        # A parenthetical explanation with no physical response target is
        # retained beside its local field, never counted as a second question.
        original=f.get('question_text_original','').strip()
        complete_parenthesis=original.endswith(')')
        note_parent=None
        if clean and original.startswith('('):
            note=lookup[clean[0]['block_id']];nb=_box(note)
            adjacent=[p for p in additions if p.get('options') and lookup[p['label_refs'][0]['block_id']]['location']['page']==note['location']['page'] and abs(_box(lookup[p['label_refs'][0]['block_id']])[1]-nb[1])<12 and max(_box(lookup[r['block_id']])[2] for r in p['answer_region_refs'])<=nb[0]+3]
            if len(adjacent)==1:note_parent=adjacent[0]
        if clean and original.startswith('(') and not complete_parenthesis:
            start_block=lookup[clean[0]['block_id']];sb=_box(start_block)
            continuation=sorted([b for b in blocks if _text(b) and b['location'].get('page')==start_block['location'].get('page') and 0<_box(b)[1]-sb[1]<45 and abs(_box(b)[0]-sb[0])<15],key=lambda b:_box(b)[1])
            for follow in continuation:
                if _text(follow).startswith('('):break
                if _text(follow).endswith(')'):
                    complete_parenthesis=True;break
        borrowed_targets=bool(f.get('answer_region_refs')) and all(r['block_id'] in assigned_regions for r in f['answer_region_refs'])
        if borrowed_targets and clean and original.startswith('(') and complete_parenthesis and not note_parent:
            borrowed_ids={r['block_id'] for r in f['answer_region_refs']}
            owners=[p for p in additions if borrowed_ids.issubset({r['block_id'] for r in p.get('answer_region_refs',[])})]
            if len(owners)==1:note_parent=owners[0]
        if clean and original.startswith('(') and (complete_parenthesis or note_parent) and '?' not in original and (not f.get('answer_region_refs') or (note_parent and borrowed_targets)):
            block=lookup[clean[0]['block_id']];page=block['location']['page'];y=_box(block)[1]
            prior=[p for p in additions if lookup[p['label_refs'][0]['block_id']]['location']['page']==page and _box(lookup[p['label_refs'][0]['block_id']])[1]<=y and y-_box(lookup[p['label_refs'][0]['block_id']])[1]<30]
            if prior or note_parent:
                parent=note_parent or max(prior,key=lambda p:_box(lookup[p['label_refs'][0]['block_id']])[1]);parent.setdefault('instruction_refs',[]).extend(deepcopy(clean))
                for rr in clean:lookup[rr['block_id']].setdefault('structure',{})['resolved_layout_role']='instruction'
                continue
        from .pdf_source_relations import validate_ocr_response_values
        if any(lookup.get(r.get('block_id'),{}).get('location',{}).get('extraction')=='ocr' for r in f.get('answer_value_refs',[])) and not validate_ocr_response_values(f,lookup,questions+additions):
            f['observed_answer_text']=f.get('answer_text_original','');f['unvalidated_answer_refs']=deepcopy(f.get('answer_value_refs',[]))
            f.update(answer_text_original='',answer_value_refs=[],answer_refs=[],answer_status='unresolved',review_status='needs_review')
            f.setdefault('review_reasons',[]).append('扫描已有值尚未与独立答复区域验证，保留原始提议供核对，不作为已有答案或自动入库。')
        remaining.append(f)
    questions[:]=sorted(remaining+[f for f in additions if f['field_id'] not in children],key=lambda f:lookup[(f.get('label_refs') or f.get('question_refs'))[0]['block_id']]['order'])
    # Continuation lines at a page boundary are accepted only with a verified
    # repeated header, a final numbered unanswered prompt and the immediately
    # succeeding number below those lines on the next page. Arbitrary leading
    # whitespace is not a continuation response.
    flat=[]
    def flatten(items):
        for item in items:flat.append(item);flatten(item.get('children',[]))
    flatten(questions)
    # A printed reviewer verdict can follow an independent name response on
    # the same sentence. It is a choice position, never the vendor name value.
    for page,page_blocks in pages.items():
        for block in page_blocks:
            match=re.fullmatch(r'\s*(is)\s+(Satisfactory)\s*/\s*(Unsatisfactory)\s*[.]?\s*',block.get('text',''),re.I)
            if not match:continue
            bb=_box(block)
            parents=[f for f in flat if f.get('answer_region_refs') and not f.get('options') and any(
                lookup[rr['block_id']]['location'].get('page')==page and 0<=bb[0]-_box(lookup[rr['block_id']])[2]<30 and
                abs(_cy(lookup[rr['block_id']])-_cy(block))<9 for rr in f['answer_region_refs'])]
            if len(parents)!=1:continue
            parent=parents[0];lr=ref(block,match.start(1),match.end(1));child=new_field(block,lr)
            fid='scan-field:'+block['block_id'];options=[]
            for group in (2,3):
                rr=ref(block,match.start(group),match.end(group),block['block_id']+f':printed-option:{match.start(group)}')
                block.setdefault('structure',{}).setdefault('logical_slots',[]).append({'slot_id':rr['slot_id'],'kind':'printed_option','start':rr['start'],'end':rr['end'],'option_label':match.group(group)})
                options.append({'refs':[ref(block,rr['start'],rr['end'])],'text_original':match.group(group),'selected':False,'selection_source':'printed_choice_list','answer_region_refs':[rr]})
            child.update(field_id=fid,question_id=fid,parent_question_id=parent['field_id'],label_refs=[lr],question_refs=[lr],question_text_original=match.group(1),
                context_refs=deepcopy(parent['label_refs']),context_label=parent['question_text_original'],options=options,
                answer_region_refs=[rr for o in options for rr in o['answer_region_refs']],answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='blank',
                boundary_evidence='scan_inline_review_choices',boundary_version=VERSION)
            parent['children']=[c for c in parent.get('children',[]) if c['field_id']!=fid]+[child];flat.append(child)
            block.setdefault('structure',{})['protected_label_ranges']=[{'start':lr['start'],'end':lr['end']}]
    for block in blocks:
        value=re.sub(r'\s+','',block.get('text','')).casefold()
        if 'assessmentby' in value and '?' not in value and len(value)<120 and not any(rr['block_id']==block['block_id'] for f in flat for rr in f.get('label_refs',[])):
            section_headers.add(block['block_id']);block.setdefault('structure',{})['resolved_layout_role']='section_header'
    all_labels={r['block_id'] for f in flat for r in f.get('label_refs',[])}
    inline_note_owners={}
    for field in flat:
        if not field.get('options') or not field.get('label_refs') or not field.get('answer_region_refs'):continue
        label_blocks=[lookup[r['block_id']] for r in field['label_refs']]
        targets=[lookup[r['block_id']] for r in field['answer_region_refs']]
        page=label_blocks[0]['location'].get('page');right=max(_box(b)[2] for b in targets)
        option_left=min(_box(b)[0] for b in targets);label_right=max(_box(b)[2] for b in label_blocks)
        top=min(_box(b)[1] for b in label_blocks);bottom=max(_box(b)[3] for b in label_blocks)
        notes=[b for b in pages.get(page,[]) if b['block_id'] not in all_labels|fixed|section_headers|option_headers and
            (_box(b)[0]>=right-5 or (_box(b)[0]>=max(option_left-3,label_right+20) and _box(b)[2]>right+40)) and
            top-6<=_box(b)[1]<=bottom+3 and re.match(r'^\s*[（(\[]',b.get('text',''))]
        for note in notes:
            inline_note_owners.setdefault(note['block_id'],[]).append(field)
    for bid,owners in inline_note_owners.items():
        if len(owners)!=1:continue
        owner=owners[0]
        # Same-row label/choices/note geometry is stronger than a preceding
        # field's below-caption heuristic. Keep this local note with one field.
        for field in flat:
            field['instruction_refs']=[rr for rr in field.get('instruction_refs',[]) if rr['block_id']!=bid]
        owner.setdefault('instruction_refs',[]).append(ref(lookup[bid]))
        lookup[bid].setdefault('structure',{})['resolved_layout_role']='instruction'
    # A short parenthetical line immediately below a source caption constrains
    # that field (period, name/function, etc.); it is not an unrelated note.
    for block in blocks:
        if block['block_id'] in all_labels|fixed|section_headers|option_headers or not re.fullmatch(r'\s*[（(].{2,100}[)）]\s*',block.get('text','')):continue
        if block['block_id'] in inline_note_owners:continue
        bb=_box(block);owners=[]
        for field in flat:
            if not field.get('label_refs') or not field.get('answer_region_refs'):continue
            labels=[lookup[rr['block_id']] for rr in field['label_refs']]
            if any(b['location'].get('page')!=block['location'].get('page') for b in labels):continue
            bottom=max(_box(b)[3] for b in labels);left=min(_box(b)[0] for b in labels)
            beneath_label=-3<=bb[1]-bottom<10 and abs(bb[0]-left)<15
            beneath_region=any(-2<=bb[1]-_box(lookup[rr['block_id']])[3]<10 and
                _box(lookup[rr['block_id']])[0]-3<=bb[0] and bb[2]<=_box(lookup[rr['block_id']])[2]+3 for rr in field['answer_region_refs'])
            if beneath_label or beneath_region:owners.append(field)
        if len(owners)==1:
            owner=owners[0]
            if not any(rr['block_id']==block['block_id'] for rr in owner.get('instruction_refs',[])):owner.setdefault('instruction_refs',[]).append(ref(block))
            block.setdefault('structure',{})['resolved_layout_role']='instruction'
    for page in sorted(pages):
        if page-1 not in pages:continue
        page_blocks=pages[page];previous_blocks=pages[page-1]
        header=[b for b in page_blocks if b['block_id'] in fixed and _box(b)[1]<max(_box(t)[3] for t in page_blocks)*.3]
        if not header:continue
        body=[b for b in page_blocks if _text(b) and b['block_id'] not in fixed and b['block_id'] not in option_headers|section_headers]
        if not body:continue
        first_y=min(_box(b)[1] for b in body)
        first_numbers=[b for b in body if re.fullmatch(r'\d+(?:\.\d+)+',_text(b)) and abs(_box(b)[1]-first_y)<8]
        if len(first_numbers)!=1:continue
        next_parts=[int(x) for x in first_numbers[0]['text'].split('.')]
        previous=[f for f in flat if f.get('label_refs') and lookup[f['label_refs'][0]['block_id']]['location']['page']==page-1]
        if not previous:continue
        field=max(previous,key=lambda f:max(_box(lookup[r['block_id']])[3] for r in f['label_refs']))
        number=str(field.get('original_number',''))
        if field.get('answer_region_refs') or field.get('options') or not re.fullmatch(r'\d+(?:\.\d+)+',number):continue
        parts=[int(x) for x in number.split('.')]
        if parts[:-1]!=next_parts[:-1] or parts[-1]+1!=next_parts[-1]:continue
        previous_body=[b for b in previous_blocks if _text(b) and b['block_id'] not in fixed and b['block_id'] not in option_headers|section_headers]
        last_bottom=max(_box(lookup[r['block_id']])[3] for r in field['label_refs'])
        if any(_box(b)[1]>last_bottom+3 for b in previous_body):continue
        header_bottom=max(_box(b)[3] for b in header);left=_box(lookup[field['label_refs'][0]['block_id']])[0]
        continuation=[b for b in page_blocks if _kind(b)=='write_line' and b['block_id'] not in assigned_regions and b['structure'].get('blank_state')=='blank' and header_bottom+5<_box(b)[1] and _box(b)[3]<first_y and abs(_box(b)[0]-left)<20 and _box(b)[2]-_box(b)[0]>140]
        continuation.sort(key=lambda b:_box(b)[1])
        if len(continuation)<2 or any(_box(b)[1]-_box(a)[3]>40 for a,b in zip(continuation,continuation[1:])):continue
        field.update(answer_region_refs=[ref(b) for b in continuation],answer_status='blank',answer_value_refs=[],answer_refs=[],answer_text_original='',boundary_evidence='continuous_numbered_cross_page_response',boundary_version=VERSION)
        for b in continuation:b['structure'].update(scan_association_valid=True,scan_field_id=field['field_id']);assigned_regions.add(b['block_id'])
    def units(items):
        for field in items:
            if not field.get('unit_refs'):
                found=[]
                for rr in field.get('label_refs',[]):
                    block=lookup[rr['block_id']];start,end=rr.get('start',0),rr.get('end',len(block['text']));value=block['text'][start:end]
                    for match in re.finditer(r'(?<![A-Za-z])(?:°\s*[CF]|m[²³]|Sq\.?\s*Mt\.?)(?![A-Za-z])|(?<=[(（])(?:kg|mg|g|ppm|ppb|%|人)(?=[)）])',value,re.I):
                        found.append(ref(block,start+match.start(),start+match.end()))
                if len(found)==1:
                    field['unit_refs']=found;rr=found[0];field['unit_text_original']=lookup[rr['block_id']]['text'][rr['start']:rr['end']]
            units(field.get('children',[]))
    units(questions)
    headings=sorted([lookup[bid] for bid in section_headers if bid in lookup],key=lambda b:b['order'])
    def scope(items):
        for field in items:
            labels=field.get('label_refs',[])
            if labels:
                label=lookup[labels[0]['block_id']]
                prior=[b for b in headings if b['order']<label['order']]
                if prior:
                    heading=prior[-1];field['section_refs']=[ref(heading)];field['section_text_original']=heading['text'];label.setdefault('structure',{})['section_path_block_ids']=[[heading['block_id']]]
                    if 'assessmentby' in re.sub(r'\s+','',heading['text']).casefold():
                        outer=[b for b in prior[:-1] if 'assessmentby' in re.sub(r'\s+','',b['text']).casefold() and 0<=heading['location']['page']-b['location']['page']<=1]
                        if outer:
                            evidence=ref(outer[-1]);field.setdefault('context_refs',[])
                            if not any(rr['block_id']==evidence['block_id'] for rr in field['context_refs']):field['context_refs'].append(evidence)
            scope(field.get('children',[]))
    scope(questions)
    for field in flat:
        refs=field.get('label_refs',[])
        if len(refs)<2 or not all(lookup.get(r.get('block_id'),{}).get('location',{}).get('extraction')=='ocr' for r in refs):continue
        ordered=_order_label_refs(refs,lookup)
        if ordered!=refs:
            field.update(label_refs=deepcopy(ordered),question_refs=deepcopy(ordered),question_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in ordered),boundary_version=VERSION)
    separate_printed_choice_suffixes(flat,lookup)
    from .joined_option_peer_rows import bind_verified_condition
    for parent in additions:
        bind_verified_condition(parent,lookup)
    return questions
