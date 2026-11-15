"""Verified links between native PDF values and raster response positions."""


def _inside(value, region):
    if len(value)!=4 or len(region)!=4:return False
    area=max(.001,(value[2]-value[0])*(value[3]-value[1]))
    overlap=max(0,min(value[2],region[2])-max(value[0],region[0]))*max(0,min(value[3],region[3])-max(value[1],region[1]))
    return overlap/area>.7 and region[0]-1<=value[0] and value[2]<=region[2]+1


def scan_label_inside(value, region):
    """Allow the OCR caption's glyph box to touch a raster border (3pt)."""
    if len(value)!=4 or len(region)!=4:return False
    area=max(.001,(value[2]-value[0])*(value[3]-value[1]))
    overlap=max(0,min(value[2],region[2])-max(value[0],region[0]))*max(0,min(value[3],region[3])-max(value[1],region[1]))
    return overlap/area>.9 and region[0]-3<=value[0] and value[2]<=region[2]+3


def source_text_inside_cell(value, cell, *, label=False):
    """A skewed cell's bounding-box corner is not part of its interior."""
    vl, cl = value.get('location', {}), cell.get('location', {})
    inside_box = scan_label_inside if label else _inside
    if vl.get('page') != cl.get('page') or not inside_box(vl.get('bbox', []), cl.get('bbox', [])):
        return False
    polygon = (cell.get('structure', {}).get('closed_region_evidence') or {}).get('polygon') or []
    if len(polygon) != 4:
        return True
    vb = vl['bbox']; x, y = (vb[0]+vb[2])/2, (vb[1]+vb[3])/2
    inside = False
    for (ax, ay), (bx, by) in zip(polygon, polygon[1:]+polygon[:1]):
        if (ay > y) != (by > y) and x < (bx-ax)*(y-ay)/(by-ay)+ax:
            inside = not inside
    return inside


def source_cells_share_row(left, right):
    """Compare a skewed row at its shared edge, not global box extremes."""
    ll,rl=left.get('location',{}),right.get('location',{});lb,rb=ll.get('bbox',[]),rl.get('bbox',[])
    if ll.get('page')!=rl.get('page') or len(lb)!=4 or len(rb)!=4 or abs(lb[2]-rb[0])>=4:return False
    lh,rh=lb[3]-lb[1],rb[3]-rb[1]
    if min(lh,rh)>0 and (max(lh,rh)/min(lh,rh)<1.25 or min(lh,rh)<=30 and abs(lh-rh)<8) and min(lb[3],rb[3])-max(lb[1],rb[1])>=.8*min(lh,rh) and abs((lb[1]+lb[3]-rb[1]-rb[3])/2)<8:return True
    edges=[]
    for b,side in [(left,'right'),(right,'left')]:
        evidence=b.get('structure',{}).get('closed_region_evidence') or {};poly=evidence.get('polygon') or [];coverage=evidence.get('edge_ink_coverage') or []
        if len(poly)!=4 or len(coverage)!=4 or min(coverage)<.8:return False
        x=(max if side=='right' else min)(p[0] for p in poly)
        ys=sorted(p[1] for p in poly if abs(p[0]-x)<3)
        if len(ys)!=2 or ys[1]-ys[0]<4:return False
        edges.append(ys)
    return all(abs(a-b)<4 for a,b in zip(edges[0],edges[1]))


def ocr_value_in_region(value_ref, region_refs, lookup):
    """Recheck a previously verified scan value; no text/ID fallback."""
    value=lookup.get(value_ref.get('block_id'),{});loc=value.get('location',{});text=value.get('text','')
    start,end=value_ref.get('start'),value_ref.get('end')
    if loc.get('extraction')!='ocr' or not isinstance(start,int) or not isinstance(end,int) or not 0<=start<end<=len(text):return False
    valid=[]
    for rr in region_refs:
        region=lookup.get(rr.get('block_id'),{});s=region.get('structure',{});rloc=region.get('location',{})
        if rr.get('slot_id') and rr.get('block_id')==value_ref.get('block_id') and rr.get('start')==start and rr.get('end')==end:
            from .scanned_subfields import verified_scan_value_slot
            slot=next((x for x in s.get('logical_slots',[]) if x.get('slot_id')==rr['slot_id']),{})
            if verified_scan_value_slot(region,slot,lookup):valid.append(rr['slot_id'])
        if (s.get('scan_association_valid') and s.get('scan_region_kind') in {'text_box','write_line'} and
            value_ref in s.get('ocr_value_refs',[]) and loc.get('page')==rloc.get('page') and
            source_text_inside_cell(value,region)):valid.append(region['block_id'])
    return len(set(valid))==1


def validate_ocr_response_values(field, lookup, all_fields):
    """Accept actual OCR values only inside an independently bounded response.

    This creates a review draft, not knowledge approval. A question block, a
    nearby free text line or a rectangle containing both label and value never
    qualifies. Every value and physical target must have one unambiguous link.
    """
    from copy import deepcopy
    from ..source_text_review import verified_reviewed_text
    values=field.get('answer_value_refs',[]);regions=field.get('answer_region_refs',[])
    labels=field.get('label_refs',[]) or field.get('question_refs',[])
    if not values or not regions or not labels:return False
    if field.get('manual_override') or field.get('answer_origin') in {'manual','knowledge','reviewed_knowledge'}:return False
    label_ids={r.get('block_id') for f in all_fields for r in (f.get('label_refs') or f.get('question_refs') or []) if r.get('start',0)<r.get('end',0)}
    assignments=[]
    for vr in values:
        value=lookup.get(vr.get('block_id'),{});vl=value.get('location',{});text=value.get('text','')
        start,end=vr.get('start'),vr.get('end')
        if (vl.get('type')!='pdf' or vl.get('extraction')!='ocr' or (value.get('style',{}).get('ocr_confidence',0)<.9 and not verified_reviewed_text(value)) or
            not isinstance(start,int) or not isinstance(end,int) or not 0<=start<end<=len(text) or not text[start:end].strip() or
            vr.get('block_id') in label_ids):return False
        matches=[]
        for rr in regions:
            region=lookup.get(rr.get('block_id'),{});s=region.get('structure',{});rl=region.get('location',{});rb=rl.get('bbox',[])
            if s.get('scan_region_kind') not in {'text_box','write_line'} or rl.get('page')!=vl.get('page') or not source_text_inside_cell(value,region):continue
            if any(lookup.get(bid,{}).get('location',{}).get('page')==vl.get('page') and _inside(lookup.get(bid,{}).get('location',{}).get('bbox',[]),rb) for bid in label_ids):continue
            nearby=False
            # A complete left-label cell and its adjacent response cell are
            # stronger than a distance from a short caption to the right edge.
            label_region=lookup.get(field.get('physical_label_region_ref',{}).get('block_id'),{})
            lrl=label_region.get('location',{});lrb=lrl.get('bbox',[])
            if (field.get('boundary_evidence')=='closed_scan_cell_pair' and
                label_region.get('structure',{}).get('scan_region_kind')=='text_box' and len(lrb)==4 and
                source_cells_share_row(label_region,region) and
                all(scan_label_inside(lookup.get(lr.get('block_id'),{}).get('location',{}).get('bbox',[]),lrb) for lr in labels)):
                nearby=True
            for lr in labels:
                ll=lookup.get(lr.get('block_id'),{}).get('location',{});lb=ll.get('bbox',[])
                if ll.get('page')!=vl.get('page') or len(lb)!=4:continue
                cy=(lb[1]+lb[3])/2
                left=0<=rb[0]-lb[2]<100 and rb[1]-3<=cy<=rb[3]+3
                above=0<=rb[1]-lb[3]<12 and max(rb[0],lb[0])<min(rb[2],lb[2])
                nearby=nearby or left or above
            if nearby:matches.append(region)
        if len(matches)!=1:return False
        assignments.append((vr,matches[0]))
    for vr,region in assignments:
        s=region.setdefault('structure',{});s.update(scan_association_valid=True,scan_field_id=field.get('field_id'),blank_state='filled',content_state='text')
        if vr not in s.setdefault('ocr_value_refs',[]):s['ocr_value_refs'].append(deepcopy(vr))
    field['answer_pairing_evidence']='ocr_text_inside_independent_physical_response'
    field['review_status']='needs_review'
    field['source_observation']={'origin':'source_document','status':'pending_review','answer_value_refs':deepcopy(values)}
    reason='已定位扫描答复区域及原文值；OCR 文字须在导入审核时核对，审核前不得参与知识库匹配。'
    field['source_review_reasons']=[reason]
    if reason not in field.setdefault('review_reasons',[]):field['review_reasons'].append(reason)
    from .reviewed_transcription import resolve_transcription_reason
    resolve_transcription_reason(field,lookup,{reason})
    return True


def native_overlay_value_in_region(value_ref, region_refs, lookup):
    value = lookup.get(value_ref.get('block_id'), {})
    text = value.get('text', '')
    start, end = value_ref.get('start'), value_ref.get('end')
    location = value.get('location', {})
    if (location.get('type') != 'pdf' or location.get('extraction') != 'native' or
        not isinstance(start,int) or not isinstance(end,int) or not 0 <= start < end <= len(text)):
        return False
    box = location.get('bbox', [])
    if len(box) != 4 or box[2] <= box[0] or box[3] <= box[1]: return False
    matches=[]
    for rr in region_refs:
        region = lookup.get(rr.get('block_id'), {})
        structure = region.get('structure', {}); rloc = region.get('location', {})
        if (value['block_id'] not in structure.get('overlay_value_block_ids', []) or
            rloc.get('type') != 'pdf' or rloc.get('page') != location.get('page') or
            not structure.get('scan_association_valid')): continue
        rb = rloc.get('bbox', [])
        if len(rb) != 4: continue
        if structure.get('scan_region_kind') == 'checkbox':
            if not structure.get('native_selection_verified') or text[start:end].strip() not in {'√','✓','✔','☑','☒'}: continue
            overlap = max(0,min(box[2],rb[2])-max(box[0],rb[0])) * max(0,min(box[3],rb[3])-max(box[1],rb[1]))
            valid = overlap / ((box[2]-box[0])*(box[3]-box[1])) > .45
        elif structure.get('scan_region_kind') in {'write_line','text_box'}:
            overflow=value.get('structure',{}).get('native_overlay_overflow_evidence',{})
            overflow_valid = (overflow.get('value_bbox')==box and overflow.get('region_bbox')==rb and
                overflow.get('background_overlap_checked') is True and overflow.get('adjacent_response_checked') is True and
                not any(other.get('location',{}).get('page')==location.get('page') and
                    other.get('location',{}).get('extraction')=='ocr_region' and other.get('text','').strip() and
                    len(ob:=other.get('location',{}).get('bbox',[]))==4 and
                    max(0,min(box[2],ob[2])-max(box[0],ob[0]))*max(0,min(box[3],ob[3])-max(box[1],ob[1]))/
                        ((box[2]-box[0])*(box[3]-box[1]))>.12 for other in lookup.values()))
            valid = (value.get('structure',{}).get('native_overlay_answer_region') == region.get('block_id') and
                rb[0]-2 <= box[0] < rb[2] and rb[1]-5 <= (box[1]+box[3])/2 <= rb[3]+2 and
                (box[2] <= rb[2]+2 or overflow_valid))
            if not valid and text[start:end] in {'.','。'}:
                from .native_overlay_value_typography import wraps_final_stop
                valid = wraps_final_stop(value,region,lookup)
        else: valid=False
        if valid: matches.append(region['block_id'])
    return len(set(matches)) == 1
