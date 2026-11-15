"""Pair occupied raster cells only when their physical row boundary exists.

The answer is OCR source text and remains a review draft. Missing rectangles,
handwritten selection ink and record-column captions are not guessed here.
"""
from copy import deepcopy
import re

from .field_boundaries import ref
from .form_completion import new_field
from .pdf_source_relations import _inside, validate_ocr_response_values
from ..parser_core.models import make_block

VERSION = 'scan-cell-pairs-v13-preserve-complete-choice-ownership'


def _label_inside(value, region):
    if len(value)!=4 or len(region)!=4:return False
    area=max(.001,(value[2]-value[0])*(value[3]-value[1]))
    overlap=max(0,min(value[2],region[2])-max(value[0],region[0]))*max(0,min(value[3],region[3])-max(value[1],region[1]))
    return overlap/area>.9 and region[0]-3<=value[0] and value[2]<=region[2]+3


def _same_row(left,right):
    lh=left[3]-left[1];rh=right[3]-right[1]
    return (min(lh,rh)>0 and (max(lh,rh)/min(lh,rh)<1.25 or min(lh,rh)<=30 and abs(lh-rh)<8) and
            min(left[3],right[3])-max(left[1],right[1])>=.8*min(lh,rh) and
            abs((left[1]+left[3]-right[1]-right[3])/2)<8)


def _reviewed(field):
    return bool(field.get('manual_override') or field.get('answer_origin') in {'manual','knowledge','reviewed_knowledge'})


def _owned_by_reviewed(questions, owned):
    for field in questions:
        if _reviewed(field) and any(r.get('block_id') in owned for key in
                ('label_refs','question_refs','answer_region_refs','answer_value_refs') for r in field.get(key,[])):
            return True
        if _owned_by_reviewed(field.get('children',[]),owned):return True
    return False


def _replace_owned(questions,owned,lookup):
    """Remove only spans physically owned by a recovered row, never neighbors."""
    retained=[]
    for old in questions:
        if _reviewed(old):retained.append(old);continue
        refs=old.get('label_refs') or old.get('question_refs') or []
        if not any(r['block_id'] in owned for r in refs):retained.append(old);continue
        residual=[r for r in refs if r['block_id'] not in owned]
        if not residual:continue
        if all(re.fullmatch(r'\s*(?:yes|no|n/?a|\d+[./\d]*)\s*',lookup.get(r['block_id'],{}).get('text',''),re.I) for r in residual):continue
        remaining=deepcopy(old)
        remaining.update(label_refs=residual,question_refs=deepcopy(residual),
            question_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in residual),
            options=[],answer_region_refs=[],answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='unresolved',review_status='needs_review')
        remaining.setdefault('review_reasons',[]).append('原分组跨越实体行边界，已拆出有来源的字段；剩余原文待核对。')
        retained.append(remaining)
    questions[:]=retained


def _restore_closed_choice_rows(questions,blocks,lookup):
    """Two closed adjacent cells establish one row, including bilingual labels.

    Unlike text-baseline grouping this also pairs vertically stacked Yes/No.
    Each caption must match one physical box inside that exact response cell.
    """
    from .selection_evidence import selected_scan_caption
    from .pdf_source_relations import source_cells_share_row
    cells=[b for b in blocks if b.get('location',{}).get('extraction')=='ocr' and b.get('structure',{}).get('scan_region_kind')=='text_box']
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr']
    marks=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='checkbox' and b.get('structure',{}).get('resolved_layout_role')!='page_counter_glyph']
    page_bottom={p:max(b['location']['bbox'][3] for b in texts if b['location']['page']==p) for p in {b['location']['page'] for b in texts}}
    claimed=set()
    for left in cells:
        page=left['location']['page'];lb=left['location']['bbox']
        if page not in page_bottom:continue
        rights=[b for b in cells if b is not left and source_cells_share_row(left,b)]
        if len(rights)!=1:continue
        right=rights[0];rb=right['location']['bbox']
        region_text=[b for b in texts if b['location']['page']==page and _label_inside(b['location']['bbox'],rb)]
        local=[b for b in marks if b['location']['page']==page and _inside(b['location']['bbox'],rb)]
        def caption_match(b):
            text=b['text'];cb=b['location']['bbox']
            match=re.match(r'^[\s□☐☑☒口Z√✓✔]*(yes|no|not\s+applicable|n\s*/?\s*a|是|否|不适用)(?=$|[\s,:：])',text,re.I)
            if match:return match
            # OCR can join the visible box/mark to a caption as one character.
            # Its real rectangle must cover precisely that leading fragment;
            # this is not permission to strip arbitrary letters from answers.
            match=re.fullmatch(r'.(yes|no|not\s+applicable)',text,re.I)
            if not match:return None
            prefix_end=cb[0]+(cb[2]-cb[0])*match.start(1)/len(text)
            candidates=[m for m in local if cb[0]-2<=m['location']['bbox'][0]<=prefix_end and
                prefix_end-2<=m['location']['bbox'][2]<=prefix_end+3 and
                abs((m['location']['bbox'][1]+m['location']['bbox'][3])/2-(cb[1]+cb[3])/2)<7]
            return match if len(candidates)==1 else None
        caption_matches={b['block_id']:caption_match(b) for b in region_text}
        captions=[b for b in region_text if caption_matches[b['block_id']]]
        edge_row=rb[3]>=page_bottom[page]*.9 or rb[1]<page_bottom[page]*.13
        if not (2<=len(captions)<=3 or len(captions)==1 and edge_row):continue
        if len({caption_matches[b['block_id']].group(1).casefold() for b in captions})!=len(captions):continue
        labels=[b for b in texts if b['location']['page']==page and (_label_inside(b['location']['bbox'],lb) or
            re.match(r'^\s*\d+(?:\.\d+)+\s*[-—:：]?',b['text']) and lb[1]<=b['location']['bbox'][1] and b['location']['bbox'][3]<=lb[3] and
            lb[0]-35<=b['location']['bbox'][0] and b['location']['bbox'][2]<=lb[2]) and not re.fullmatch(r'\d+[.\d]*',b['text'].strip())]
        if not labels or any(b['block_id'] in claimed for b in labels):continue
        labels.sort(key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0]))
        options=[];used=set()
        horizontal=max((b['location']['bbox'][1]+b['location']['bbox'][3])/2 for b in captions)-min((b['location']['bbox'][1]+b['location']['bbox'][3])/2 for b in captions)<7
        for caption in sorted(captions,key=lambda b:b['location']['bbox'][0] if horizontal else b['location']['bbox'][1]):
            cb=caption['location']['bbox'];cy=(cb[1]+cb[3])/2
            candidates=[b for b in local if abs((b['location']['bbox'][1]+b['location']['bbox'][3])/2-cy)<7 and
                min(abs(b['location']['bbox'][0]-cb[2]),abs(b['location']['bbox'][2]-cb[0]))<20]
            if len(candidates)!=1:break
            mark=candidates[0]
            if mark['block_id'] in used:break
            used.add(mark['block_id']);control=(mark.get('controls') or [{}])[0]
            match=caption_matches[caption['block_id']];caption_ref=ref(caption,match.start(1),match.end(1))
            options.append({'refs':[caption_ref],'text_original':match.group(1),'selected':control.get('selected'),
                'selection_source':control.get('source'),'answer_region_refs':[ref(mark)]})
        if len(options)!=len(captions):continue
        ids={b['block_id'] for b in labels+captions}
        if _owned_by_reviewed(questions,ids|{r['block_id'] for o in options for r in o['answer_region_refs']}):continue
        fid='scan-closed-choice:'+left['block_id'];f=new_field(labels[0],ref(labels[0]))
        f.update(field_id=fid,question_id=fid,label_refs=[ref(b) for b in labels],question_refs=[ref(b) for b in labels],
            question_text_original='\n'.join(b['text'] for b in labels),options=options,
            answer_region_refs=[r for o in options for r in o['answer_region_refs']],answer_value_refs=[],answer_refs=[],answer_text_original='',
            answer_status='blank' if all(o['selected'] is False for o in options) else 'unresolved',
            physical_label_region_ref=ref(left),physical_response_region_ref=ref(right),boundary_evidence='closed_scan_choice_row',boundary_version=VERSION)
        for option in options:
            for rr in option['answer_region_refs']:
                lookup[rr['block_id']]['structure'].update(scan_association_valid=True,scan_field_id=fid,scan_option_caption_refs=deepcopy(option['refs']))
        selected=[r for o in options if o['selected'] is True for r in o['refs']]
        if len(selected)==1 and all(isinstance(o['selected'],bool) for o in options) and all(selected_scan_caption(r,f,lookup) for r in selected):
            f.update(answer_status='filled',answer_value_refs=deepcopy(selected),answer_refs=deepcopy(selected),
                answer_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in selected),
                answer_pairing_evidence='physical_selected_box_and_source_caption')
        if f['answer_status']=='unresolved':
            f.update(review_status='needs_review',review_reasons=['实体答复格已定位，但选择状态不完整或存在多选，需对照原页。'])
        _replace_owned(questions,ids,lookup)
        for b in labels:b.setdefault('structure',{})['protected_label_ranges']=[{'start':0,'end':len(b['text'])}]
        for b in captions:b.setdefault('structure',{})['resolved_layout_role']='option'
        questions.append(f);claimed.update(ids)


def _join_split_choice_rows(questions,blocks,lookup):
    """Join a physical Yes/No row split at the bottom/top of adjacent pages."""
    from .selection_evidence import selected_scan_caption
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr']
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box']
    marks=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='checkbox']
    page_bottom={p:max(b['location']['bbox'][3] for b in texts if b['location']['page']==p) for p in {b['location']['page'] for b in texts}}
    used=set()
    for field in list(questions):
        if _reviewed(field):continue
        options=field.get('options',[])
        if len(options)!=1 or options[0]['text_original'].strip().casefold() not in {'yes','no'}:continue
        targets=[lookup.get(r['block_id'],{}) for r in options[0].get('answer_region_refs',[])]
        if len(targets)!=1 or targets[0].get('structure',{}).get('scan_region_kind')!='checkbox':continue
        target=targets[0];page=target['location']['page'];box=target['location']['bbox']
        containing=[c for c in cells if c['location']['page']==page and _inside(box,c['location']['bbox']) and c['location']['bbox'][2]-c['location']['bbox'][0]<180]
        if not containing or max(c['location']['bbox'][3] for c in containing)<page_bottom[page]*.9 or page+1 not in page_bottom:continue
        opposite='no' if options[0]['text_original'].strip().casefold()=='yes' else 'yes'
        candidates=[]
        for caption in texts:
            cb=caption['location']['bbox']
            if caption['location']['page']!=page+1 or caption['text'].strip().casefold()!=opposite:continue
            local=[b for b in marks if b['location']['page']==page+1 and abs(b['location']['bbox'][0]-box[0])<25 and
                abs((b['location']['bbox'][1]+b['location']['bbox'][3]-cb[1]-cb[3])/2)<7 and
                min(abs(b['location']['bbox'][2]-cb[0]),abs(b['location']['bbox'][0]-cb[2]))<20]
            if len(local)!=1:continue
            right=[c for c in cells if c['location']['page']==page+1 and _inside(local[0]['location']['bbox'],c['location']['bbox']) and c['location']['bbox'][2]-c['location']['bbox'][0]<180]
            if len(right)!=1:continue
            # The continuation must be the first response cell in this column.
            rb=right[0]['location']['bbox']
            if rb[1]>page_bottom[page+1]*.13:continue
            if any(c['location']['page']==page+1 and abs(c['location']['bbox'][0]-rb[0])<5 and c['location']['bbox'][1]<rb[1]-4 for c in cells):continue
            candidates.append((caption,local[0],right[0]))
        if len(candidates)!=1:continue
        caption,mark,right=candidates[0]
        if mark['block_id'] in used:continue
        rb=right['location']['bbox']
        left=[c for c in cells if c['location']['page']==page+1 and abs(c['location']['bbox'][2]-rb[0])<4 and _same_row(c['location']['bbox'],rb)]
        continuation=[]
        if len(left)==1:
            continuation=[b for b in texts if b['location']['page']==page+1 and _label_inside(b['location']['bbox'],left[0]['location']['bbox']) and not re.fullmatch(r'\d+[.\d]*',b['text'].strip())]
        control=(mark.get('controls') or [{}])[0]
        owned={caption['block_id']}|{b['block_id'] for b in continuation}
        if _owned_by_reviewed(questions,owned|{mark['block_id']}):continue
        option={'refs':[ref(caption)],'text_original':caption['text'],'selected':control.get('selected'),
            'selection_source':control.get('source'),'answer_region_refs':[ref(mark)]}
        mark['structure'].update(scan_association_valid=True,scan_field_id=field['field_id'],scan_option_caption_refs=deepcopy(option['refs']))
        original_labels=deepcopy(field['label_refs'])
        # Do not remove this field itself when one former model group already
        # included its cross-page continuation.
        others=[f for f in questions if f is not field];_replace_owned(others,owned,lookup)
        questions[:]=others+[field]
        known={r['block_id'] for r in original_labels}
        field['label_refs']=original_labels+[ref(b) for b in continuation if b['block_id'] not in known]
        field['question_refs']=deepcopy(field['label_refs'])
        field['question_text_original']='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in field['label_refs'])
        field['options'].append(option);field['answer_region_refs'].extend(option['answer_region_refs'])
        field['cross_page_evidence']={'kind':'last_and_first_aligned_physical_choice_cells','pages':[page,page+1],
            'continuation_cell_ref':ref(right),'continuation_label_refs':[ref(b) for b in continuation]}
        field.update(answer_status='blank' if all(o['selected'] is False for o in field['options']) else 'unresolved',
                     answer_value_refs=[],answer_refs=[],answer_text_original='')
        selected=[r for o in field['options'] if o['selected'] is True for r in o['refs']]
        if len(selected)==1 and all(isinstance(o['selected'],bool) for o in field['options']) and all(selected_scan_caption(r,field,lookup) for r in selected):
            field.update(answer_status='filled',answer_value_refs=deepcopy(selected),answer_refs=deepcopy(selected),
                answer_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in selected),
                answer_pairing_evidence='physical_selected_box_and_source_caption')
        used.add(mark['block_id'])


def _restore_edge_choice_fragments(questions,blocks,lookup):
    """A page may cut the label border while preserving its answer rectangle."""
    from .selection_evidence import selected_scan_caption
    text=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr']
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box']
    marks=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='checkbox']
    bottom={p:max(b['location']['bbox'][3] for b in text if b['location']['page']==p) for p in {b['location']['page'] for b in text}}
    for cell in cells:
        rb=cell['location']['bbox'];page=cell['location']['page']
        if page not in bottom or rb[3]<bottom[page]*.9 or not 60<rb[2]-rb[0]<180:continue
        local=[b for b in marks if b['location']['page']==page and _inside(b['location']['bbox'],rb)]
        if len(local)!=1:continue
        mark=local[0]
        if any(f.get('boundary_evidence')=='closed_scan_choice_row' and any(rr['block_id']==mark['block_id'] for rr in f['answer_region_refs']) for f in questions):continue
        captions=[b for b in text if b['location']['page']==page and _label_inside(b['location']['bbox'],rb) and re.fullmatch(r'[ZV]?(?:Yes|No)',b['text'].strip(),re.I)]
        if len(captions)!=1:continue
        labels=[b for b in text if b['location']['page']==page and b['location']['bbox'][2]<rb[0]-5 and
            rb[1]<=(b['location']['bbox'][1]+b['location']['bbox'][3])/2<=rb[3]+8 and
            re.search(r'[?？]',b['text']) and b.get('structure',{}).get('resolved_layout_role') not in {'section_header','repeated_header_footer'}]
        if len(labels)!=1:continue
        caption=captions[0];m=re.search(r'Yes|No',caption['text'],re.I);rr=ref(caption,m.start(),m.end());control=(mark.get('controls') or [{}])[0]
        if _owned_by_reviewed(questions,{labels[0]['block_id'],caption['block_id'],mark['block_id']}):continue
        label=labels[0];f=new_field(label,ref(label));fid='scan-edge-choice:'+label['block_id']
        option={'refs':[rr],'text_original':m.group(),'selected':control.get('selected'),'selection_source':control.get('source'),'answer_region_refs':[ref(mark)]}
        f.update(field_id=fid,question_id=fid,options=[option],answer_region_refs=[ref(mark)],physical_response_region_ref=ref(cell),
            boundary_evidence='page_edge_question_beside_closed_response',boundary_version=VERSION)
        mark['structure'].update(scan_association_valid=True,scan_field_id=fid,scan_option_caption_refs=[rr])
        if control.get('selected') is True and selected_scan_caption(rr,f,lookup):
            f.update(answer_status='filled',answer_value_refs=[rr],answer_refs=[rr],answer_text_original=m.group(),answer_pairing_evidence='physical_selected_box_and_source_caption')
        _replace_owned(questions,{label['block_id'],caption['block_id']},lookup)
        questions.append(f)


def _record_heading_pair(left,right,values,cells,blocks):
    """A centered column caption above repeated option records is not a value."""
    lb=left['location']['bbox'];rb=right['location']['bbox'];page=left['location']['page']
    # Three repeated physical columns beneath a short heading band are a
    # record grid, even when OCR wraps a caption onto two lines. This requires
    # actual stacked cells, not a vocabulary list or "first row is header".
    if max(lb[3]-lb[1],rb[3]-rb[1])<35 and max(lb[2]-lb[0],rb[2]-rb[0])<150:
        # Raster contours share the ink of one border. Slightly overlapping
        # axis boxes on a skewed page still describe adjacent physical rows.
        body=[c for c in cells if c['location']['page']==page and -3<=c['location']['bbox'][1]-min(lb[3],rb[3])<6 and
            c['location']['bbox'][3]-c['location']['bbox'][1]>max(lb[3]-lb[1],rb[3]-rb[1])*1.3]
        adjacent=sorted(body,key=lambda c:c['location']['bbox'][0]);chain=[]
        for c in adjacent:
            cb=c['location']['bbox']
            if chain and abs(cb[0]-chain[-1]['location']['bbox'][2])>4:chain=[]
            chain.append(c)
            if len(chain)<3:continue
            if not any(abs(c['location']['bbox'][0]-lb[0])<4 and abs(c['location']['bbox'][2]-lb[2])<4 for c in chain):continue
            if not any(abs(c['location']['bbox'][0]-rb[0])<4 and abs(c['location']['bbox'][2]-rb[2])<4 for c in chain):continue
            repeats=sum(any(other['location']['page']==page and abs(other['location']['bbox'][0]-c['location']['bbox'][0])<4 and
                abs(other['location']['bbox'][2]-c['location']['bbox'][2])<4 and -3<=other['location']['bbox'][1]-c['location']['bbox'][3]<6
                for other in cells if other is not c) for c in chain)
            if repeats>=2:return True
    if len(values)!=1:return False
    rb=right['location']['bbox'];vb=values[0]['location']['bbox'];page=left['location']['page'];lb=left['location']['bbox']
    if abs((vb[0]+vb[2]-rb[0]-rb[2])/2)>max(8,(rb[2]-rb[0])*.1):return False
    following=[b for b in cells if b['location']['page']==page and 0<=b['location']['bbox'][1]-rb[3]<65 and
        abs(b['location']['bbox'][0]-rb[0])<4 and abs(b['location']['bbox'][2]-rb[2])<4]
    if any(sum(b.get('structure',{}).get('scan_region_kind')=='checkbox' and b['location']['page']==page and
        _inside(b['location']['bbox'],row['location']['bbox']) for b in blocks)>=2 for row in following):return True
    # A numbering column beside a proved record header is part of that same
    # header band, never a question whose answer is the next column caption.
    number_labels=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('page')==page and _label_inside(b['location'].get('bbox',[]),lb)]
    if len(number_labels)==1 and re.fullmatch(r'no\.?|序号|编号',number_labels[0]['text'].strip(),re.I):
        return len(following)>=1 and any(b.get('structure',{}).get('resolved_layout_role')=='record_column_header' and
            b.get('location',{}).get('page')==page and abs((b['location']['bbox'][1]+b['location']['bbox'][3]-vb[1]-vb[3])/2)<8 for b in blocks)
    return False


def _complete_grid_companions(blocks,lookup):
    """Close a broken occupied cell using recorded physical grid evidence.

    Two full row borders, an existing left cell and a same-column right cell
    elsewhere in the contiguous grid are required. This is not an arbitrary
    whitespace rectangle. The supporting source IDs remain inspectable.
    """
    cells=[b for b in blocks if b.get('location',{}).get('extraction')=='ocr' and b.get('structure',{}).get('scan_region_kind')=='text_box']
    lines=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='write_line']
    created=[]
    for left in cells:
        loc=left['location'];lb=loc['bbox'];page=loc['page']
        if lb[3]-lb[1]<35:continue
        peers=[b for b in cells if b is not left and b['location']['page']==page and abs(b['location']['bbox'][0]-lb[2])<4 and b['location']['bbox'][2]-b['location']['bbox'][0]>60]
        if not peers:continue
        if any(abs(b['location']['bbox'][1]-lb[1])<3 and abs(b['location']['bbox'][3]-lb[3])<3 for b in peers):continue
        peer=min(peers,key=lambda b:abs(b['location']['bbox'][1]-lb[1]));pb=peer['location']['bbox']
        if abs(pb[1]-lb[1])>350:continue
        top=[b for b in lines if b['location']['page']==page and abs(b['location']['bbox'][3]-lb[1])<3 and b['location']['bbox'][0]<=lb[0]+3 and b['location']['bbox'][2]>lb[2]+60 and abs(b['location']['bbox'][2]-pb[2])<5]
        bottom=[b for b in lines if b['location']['page']==page and abs(b['location']['bbox'][3]-lb[3])<3 and b['location']['bbox'][0]<=lb[0]+3 and b['location']['bbox'][2]>lb[2]+60 and abs(b['location']['bbox'][2]-pb[2])<5]
        if not top or not bottom:continue
        top=max(top,key=lambda b:b['location']['bbox'][2]);bottom=max(bottom,key=lambda b:b['location']['bbox'][2])
        x1=min(top['location']['bbox'][2],bottom['location']['bbox'][2])
        box=[lb[2],lb[1],x1,lb[3]]
        derived=make_block('ocr_text','',left['order'],{**loc,'bbox':box,'grid_parent_cell':left['block_id']},
            structure={'scan_region_kind':'text_box','scan_candidate':True,'blank_state':'unresolved','content_state':'unreadable',
            'source_method':'evidenced_raster_grid_closure','grid_evidence_refs':[ref(left),ref(top),ref(bottom),ref(peer)],
            'write_requires_association':True,'text_overlap':True}).model_dump()
        if derived['block_id'] not in lookup:created.append(derived);lookup[derived['block_id']]=derived
    blocks.extend(created)


def _complete_left_label_bands(blocks,lookup):
    """Locate a broken label cell using its real right cell and ruled neighbors.

    This is a label band only, never a newly invented writable answer region.
    Two observed row-border fragments and a complete neighboring row are kept
    as evidence; absence of either leaves the field unresolved.
    """
    cells=[b for b in blocks if b.get('location',{}).get('extraction')=='ocr' and b.get('structure',{}).get('scan_region_kind')=='text_box']
    lines=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='write_line']
    texts=[b for b in blocks if b.get('text','').strip()]
    created=[]
    for right in cells:
        loc=right['location'];rb=loc['bbox'];page=loc['page']
        if not 60<rb[2]-rb[0]<180:continue
        if not any(b['location']['page']==page and _label_inside(b['location']['bbox'],rb) and re.fullmatch(r'[ZV]?(?:Yes|No)',b['text'].strip(),re.I) for b in texts):continue
        if any(c['location']['page']==page and abs(c['location']['bbox'][2]-rb[0])<4 and _same_row(c['location']['bbox'],rb) for c in cells):continue
        peers=[]
        for left_peer in cells:
            pb=left_peer['location']['bbox']
            if left_peer['location']['page']!=page or not 180<pb[2]-pb[0]<470 or abs(pb[2]-rb[0])>12 or abs(pb[1]-rb[1])>180:continue
            right_peers=[c for c in cells if c['location']['page']==page and abs(c['location']['bbox'][0]-pb[2])<4 and _same_row(c['location']['bbox'],pb)]
            if len(right_peers)==1:peers.append((left_peer,right_peers[0]))
        if not peers:continue
        peer,peer_right=min(peers,key=lambda pair:abs(pair[0]['location']['bbox'][1]-rb[1]))
        pb=peer['location']['bbox'];pr=peer_right['location']['bbox'];shift=rb[0]-pr[0]
        band=[pb[0]+shift,rb[1]+pb[1]-pr[1],rb[0],rb[3]+pb[3]-pr[3]]
        boundaries=[]
        for edge in (band[1],band[3]):
            found=[b for b in lines if b['location']['page']==page and abs(b['location']['bbox'][3]-edge)<7 and
                max(0,min(band[2],b['location']['bbox'][2])-max(band[0],b['location']['bbox'][0]))>(band[2]-band[0])*.3]
            if not found:break
            boundaries.append(min(found,key=lambda b:abs(b['location']['bbox'][3]-edge)))
        if len(boundaries)!=2:continue
        derived=make_block('ocr_text','',right['order'],{**loc,'bbox':band,'row_band_anchor':right['block_id']},
            structure={'scan_region_kind':'text_box','scan_candidate':True,'blank_state':'unresolved','content_state':'text',
                'label_only_region':True,'source_method':'evidenced_neighbor_label_band',
                'grid_evidence_refs':[ref(right),ref(peer),ref(peer_right)]+[ref(b) for b in boundaries]}).model_dump()
        if derived['block_id'] not in lookup:created.append(derived);lookup[derived['block_id']]=derived
    blocks.extend(created)


def _caption_subset_of_closed_choice(labels,marks,questions,lookup):
    """A sub-grid cannot reuse one option as its question and the rest as answers.

    This uses exact source ranges and a proven two-cell parent row, not a list
    of words such as Yes. Independent response regions and mixed source labels
    are deliberately not consumed by this guard.
    """
    target_ids={b['block_id'] for b in marks}
    if not labels or not target_ids:return False
    for owner in questions:
        if owner.get('boundary_evidence')!='closed_scan_choice_row':continue
        left=lookup.get(owner.get('physical_label_region_ref',{}).get('block_id'),{})
        right=lookup.get(owner.get('physical_response_region_ref',{}).get('block_id'),{})
        lb,rb=left.get('location',{}).get('bbox',[]),right.get('location',{}).get('bbox',[])
        page=left.get('location',{}).get('page')
        if (len(lb)!=4 or len(rb)!=4 or page!=right.get('location',{}).get('page') or
            left.get('structure',{}).get('scan_region_kind')!='text_box' or
            right.get('structure',{}).get('scan_region_kind')!='text_box'):continue
        owned={r['block_id'] for r in owner.get('answer_region_refs',[])}
        options=owner.get('options',[])
        if len(options)<2 or not target_ids<owned:continue
        if owned!={r['block_id'] for o in options for r in o.get('answer_region_refs',[])}:continue
        captions=[r for o in options for r in o.get('refs',[])]
        if not captions or not owner.get('label_refs'):continue
        def within(r,box):
            b=lookup.get(r.get('block_id'),{});a,z=r.get('start'),r.get('end')
            return (b.get('location',{}).get('page')==page and type(a) is int and type(z) is int and
                    0<=a<z<=len(b.get('text','')) and _label_inside(b['location'].get('bbox',[]),box))
        if not all(within(r,lb) for r in owner['label_refs']) or not all(within(r,rb) for r in captions):continue
        if not all(lookup.get(bid,{}).get('location',{}).get('page')==page and
                   _label_inside(lookup[bid]['location'].get('bbox',[]),rb) for bid in owned):continue
        if all(any(r['block_id']==b['block_id'] and r['start']==0 and r['end']==len(b['text']) for r in captions) for b in labels):
            return True
    return False


def _restore_choice_cells(questions,blocks,lookup):
    from .scanned_forms import _order_label_refs
    from ..parser_core.pdf_caption_fragments import verified_caption_crop
    from .selection_evidence import selected_scan_caption
    cells=[b for b in blocks if b.get('location',{}).get('extraction')=='ocr' and b.get('structure',{}).get('scan_region_kind')=='text_box' and b['location']['bbox'][3]-b['location']['bbox'][1]>35]
    lines=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='write_line']
    marks=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='checkbox']
    text=[b for b in blocks if b.get('location',{}).get('extraction')=='ocr' and b.get('text','').strip()]
    completed=set()
    for anchor in cells:
        al=anchor['location'];ab=al['bbox'];page=al['page']
        samecol=[b for b in cells if b is not anchor and b['location']['page']==page and abs(b['location']['bbox'][0]-ab[0])<4 and abs(b['location']['bbox'][2]-ab[2])<4]
        if len(samecol)<2:continue
        borders=sorted([b for b in lines if b['location']['page']==page and abs(b['location']['bbox'][0]-ab[0])<5 and b['location']['bbox'][2]>ab[2]+70],key=lambda b:b['location']['bbox'][3])
        row_bounds=[(top,bottom,top['location']['bbox'][3],bottom['location']['bbox'][3]) for top,bottom in zip(borders,borders[1:])]
        # A genuine closed panel may retain the entire choice group while a
        # damaged divider prevents separate left/right contours. Its actual
        # perimeter is stronger than inventing missing horizontal lines.
        row_bounds.extend((panel,panel,panel['location']['bbox'][1],panel['location']['bbox'][3]) for panel in cells
            if panel['location']['page']==page and abs(panel['location']['bbox'][0]-ab[0])<5 and panel['location']['bbox'][2]>ab[2]+70)
        for top,bottom,y0,y1 in row_bounds:
            key=(page,round(ab[2]/5),round(y0/5),round(y1/5))
            if key in completed or not 20<y1-y0<140:continue
            rowmarks=[b for b in marks if b['location']['page']==page and b['location']['bbox'][0]>=ab[2]-3 and y0-2<=b['location']['bbox'][1] and b['location']['bbox'][3]<=y1+2]
            if len(rowmarks)<2:continue
            labels=[b for b in text if b['location']['page']==page and ab[0]-3<=b['location']['bbox'][0] and b['location']['bbox'][2]<=ab[2]+3 and y0-2<=b['location']['bbox'][1] and b['location']['bbox'][3]<=y1+2 and not re.fullmatch(r'\d+(?:\.\d+)*[.)]?',b['text'].strip())]
            if not labels:continue
            labels=[lookup[rr['block_id']] for rr in _order_label_refs([ref(b) for b in labels],lookup)];labelids={b['block_id'] for b in labels}
            if _owned_by_reviewed(questions,labelids|{b['block_id'] for b in rowmarks}):continue
            if _caption_subset_of_closed_choice(labels,rowmarks,questions,lookup):continue
            if sum(bool(re.match(r'\s*(?:is\b|are\b|does\b|do\b|has\b|have\b|whether\b)',b['text'],re.I)) for b in labels)>1:continue
            if sum(b['text'].count('?')+b['text'].count('？') for b in labels)>1:continue
            # A model-proposed partial option list must not block the complete
            # physically bounded group. Manual fields were protected above;
            # replacement below happens only after every source box has a
            # unique caption and actual region evidence.
            options=[];captionids=set();caption_ranges=set()
            for mark in sorted(rowmarks,key=lambda b:(round(b['location']['bbox'][1]/8),b['location']['bbox'][0])):
                mb=mark['location']['bbox'];cy=(mb[1]+mb[3])/2
                crops=(mark.get('structure',{}).get('scan_option_caption_crop') or {}).get('caption_refs',[])
                if len(crops)==1 and verified_caption_crop(mark,crops[0],lookup):
                    caption_ref=deepcopy(crops[0]);caption=lookup[caption_ref['block_id']]
                else:
                    candidates=[b for b in text if b['location']['page']==page and -4<=b['location']['bbox'][0]-mb[0]<18 and abs((b['location']['bbox'][1]+b['location']['bbox'][3])/2-cy)<6 and len(b['text'])<40]
                    if len(candidates)!=1:continue
                    caption=candidates[0];cb=caption['location']['bbox']
                    # A merged line covering several controls is not one
                    # option. Require local crop/source-span proof instead.
                    if sum(cb[0]-3<=b['location']['bbox'][0]<=cb[2] and abs((b['location']['bbox'][1]+b['location']['bbox'][3])/2-cy)<6 for b in rowmarks)>1:continue
                    caption_ref=ref(caption)
                key_ref=(caption_ref['block_id'],caption_ref['start'],caption_ref['end'])
                if key_ref in caption_ranges:continue
                selected=(mark.get('controls') or [{}])[0].get('selected')
                options.append({'refs':[caption_ref],'text_original':caption['text'][caption_ref['start']:caption_ref['end']],'selected':selected,'selection_source':(mark.get('controls') or [{}])[0].get('source','scan_rectangle'),'answer_region_refs':[ref(mark)]})
                captionids.add(caption['block_id']);caption_ranges.add(key_ref)
            if len(options)<2 or len(options)!=len(rowmarks):continue
            # A complete native cell pair already proves this source row.
            # A weaker line-grid reconstruction must not discard its physical
            # ownership, needed to keep a group heading out of child answers.
            region_ids={b['block_id'] for b in rowmarks}
            if any(old.get('boundary_evidence')=='closed_scan_choice_row' and
                region_ids=={rr['block_id'] for rr in old.get('answer_region_refs',[])} and
                labelids=={rr['block_id'] for rr in old.get('label_refs',[])} for old in questions):continue
            if len({re.sub(r'\s+','',o['text_original'].lower()) for o in options})!=len(options):continue
            choice_groups=[]
            for index,option in enumerate(options):
                target=lookup[option['answer_region_refs'][0]['block_id']];box=target['location']['bbox'];cy=(box[1]+box[3])/2
                group=next((g for g in choice_groups if abs(g['source_row_center']-cy)<6),None)
                if group is None:
                    group={'group_id':'scan-option-row:'+target['block_id'],'source_row_center':cy,'option_indices':[],
                           'answer_region_refs':[],'selection_limit':'unknown'};choice_groups.append(group)
                group['option_indices'].append(index);group['answer_region_refs'].extend(deepcopy(option['answer_region_refs']))
                option['group_id']=group['group_id']
            f=new_field(labels[0],ref(labels[0]));fid='scan-choice-cell:'+labels[0]['block_id']
            f.update(field_id=fid,question_id=fid,label_refs=[ref(b) for b in labels],question_refs=[ref(b) for b in labels],question_text_original='\n'.join(b['text'] for b in labels),
                options=options,choice_groups=choice_groups,answer_region_refs=[rr for o in options for rr in o['answer_region_refs']],answer_value_refs=[],answer_refs=[],answer_text_original='',
                answer_status='unresolved',
                review_status='needs_review',review_reasons=['依据原页表格边界恢复选择区域；未知勾选保留待核对，不用墨迹猜测答案。'],
                boundary_evidence='scan_choice_cell_grid',boundary_version=VERSION,grid_evidence_refs=[ref(anchor),ref(top),ref(bottom)])
            for o in options:
                for rr in o['answer_region_refs']:
                    lookup[rr['block_id']]['structure'].update(scan_association_valid=True,scan_field_id=fid,scan_option_caption_refs=deepcopy(o['refs']))
                for rr in o['refs']:lookup[rr['block_id']].setdefault('structure',{})['resolved_layout_role']='option'
            actual=[rr for o in options if o['selected'] is True for rr in o['refs']]
            if all(isinstance(o['selected'],bool) for o in options) and all(selected_scan_caption(rr,f,lookup) for rr in actual):
                f.update(answer_status='filled' if actual else 'blank',answer_value_refs=deepcopy(actual),answer_refs=deepcopy(actual),
                    answer_text_original='\n'.join(lookup[rr['block_id']]['text'][rr['start']:rr['end']] for rr in actual),
                    review_status='model_proposed',review_reasons=[])
            for b in labels:b.setdefault('structure',{})['protected_label_ranges']=[{'start':0,'end':len(b['text'])}]
            # Only remove model fields entirely made from this verified label
            # or option group. Mixed-source groups remain visible for review.
            owned=labelids|captionids
            questions[:]=[old for old in questions if not (old.get('label_refs') and all(rr['block_id'] in owned for rr in old['label_refs']))]
            questions.append(f);completed.add(key)


def _complete_closed_blank_labels(questions,blocks,lookup):
    """Complete a prompt inside its existing closed cell, not nearest text.

    No new answer region is guessed: an existing uniquely assigned blank
    right cell and its physical left companion must already be established.
    """
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box' and b.get('location',{}).get('extraction')=='ocr']
    for f in questions:
        if _reviewed(f) or f.get('options') or f.get('answer_status')!='blank' or f.get('boundary_evidence')!='scan_physical_form':continue
        regions=f.get('answer_region_refs',[])
        if len(regions)!=1:continue
        right=lookup.get(regions[0]['block_id'],{});rs=right.get('structure',{});rl=right.get('location',{});rb=rl.get('bbox',[])
        if rs.get('scan_region_kind')!='text_box' or rs.get('blank_state')!='blank' or len(rb)!=4:continue
        lefts=[b for b in cells if b['location']['page']==rl['page'] and abs(b['location']['bbox'][2]-rb[0])<4 and _same_row(b['location']['bbox'],rb)]
        if len(lefts)!=1:continue
        left=lefts[0];lb=left['location']['bbox']
        labels=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('page')==rl['page'] and _label_inside(b['location'].get('bbox',[]),lb)]
        ids={b['block_id'] for b in labels};oldrefs=f.get('label_refs',[])
        if len(labels)<2 or not oldrefs or not all(rr['block_id'] in ids for rr in oldrefs):continue
        if any(other is not f and any(rr['block_id'] in ids for rr in other.get('label_refs',[])) for other in questions):continue
        labels.sort(key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0]))
        if any(b.get('structure',{}).get('resolved_layout_role') in {'option','existing_answer','repeated_header_footer'} for b in labels):continue
        if sum(b['text'].count('?')+b['text'].count('？') for b in labels)>1:continue
        refs=[ref(b) for b in labels]
        f.update(label_refs=refs,question_refs=deepcopy(refs),question_text_original='\n'.join(b['text'] for b in labels),
            physical_label_region_ref=ref(left),label_completion_evidence={
                'version':VERSION,'label_region_ref':ref(left),'label_refs':deepcopy(refs)})
        for b in labels:b.setdefault('structure',{})['protected_label_ranges']=[{'start':0,'end':len(b['text'])}]


def _leaf_closed_cells(boxes):
    """Keep source contours, but pair against complete observed inner rows.

    A raster contour can enclose several genuinely ruled cells. It is not a
    physical merged answer cell when an independently observed chain of closed
    child cells covers the same four outer sides and shares every inner edge.
    Text count, wording and answer presence never establish this partition.
    """
    def edges(block):
        evidence=block.get('structure',{}).get('closed_region_evidence') or {}
        poly=evidence.get('polygon',[]);coverage=evidence.get('edge_ink_coverage',[])
        if len(poly)!=4 or len(coverage)!=4 or min(coverage)<.8:return None
        left=sorted(sorted(poly,key=lambda p:p[0])[:2],key=lambda p:p[1])
        right=sorted(sorted(poly,key=lambda p:p[0])[2:],key=lambda p:p[1])
        if min(left[1][1]-left[0][1],right[1][1]-right[0][1])<8:return None
        return [left[0],right[0]],[left[1],right[1]]
    def shared(a,b):return all(abs(x-y)<4 for p,q in zip(a,b) for x,y in zip(p,q))
    proofs={b['block_id']:edges(b) for b in boxes};superseded=set()
    for outer in boxes:
        proof=proofs[outer['block_id']]
        if not proof:continue
        ob=outer['location']['bbox'];top,bottom=proof
        candidates=[b for b in boxes if b is not outer and b['location']['page']==outer['location']['page']
            and proofs[b['block_id']] and abs(b['location']['bbox'][0]-ob[0])<4 and abs(b['location']['bbox'][2]-ob[2])<4
            and b['location']['bbox'][3]-b['location']['bbox'][1]<(ob[3]-ob[1])*.85
            and b['location']['bbox'][1]>=ob[1]-4 and b['location']['bbox'][3]<=ob[3]+4]
        chain=[];cursor=top
        for _ in range(len(candidates)):
            next_cells=[b for b in candidates if b not in chain and shared(cursor,proofs[b['block_id']][0])]
            if len(next_cells)!=1:break
            child=next_cells[0];chain.append(child);cursor=proofs[child['block_id']][1]
            if shared(cursor,bottom):break
        if len(chain)>1 and shared(cursor,bottom):
            superseded.add(outer['block_id'])
            outer.setdefault('structure',{})['observed_inner_row_partition']={
                'version':VERSION,'child_region_refs':[ref(b) for b in chain],
                'evidence':'complete_four_sided_child_cells_shared_inner_edges',
                'original_region_retained':True}
    return [b for b in boxes if b['block_id'] not in superseded]


def restore_scanned_cell_pairs(questions, blocks, lookup):
    from .pdf_source_relations import source_cells_share_row, source_text_inside_cell
    _complete_grid_companions(blocks,lookup)
    _complete_left_label_bands(blocks,lookup)
    _complete_closed_blank_labels(questions,blocks,lookup)
    boxes = [b for b in blocks if b.get('location',{}).get('extraction') == 'ocr'
             and b.get('structure',{}).get('scan_region_kind') == 'text_box']
    boxes = _leaf_closed_cells(boxes)
    from .scan_matrix_structure import classify_scan_headers, is_physical_checkbox_text
    classify_scan_headers(questions,blocks,lookup)
    marks=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='checkbox']
    text = [b for b in blocks if b.get('location',{}).get('extraction') == 'ocr' and b.get('text','').strip()]
    additions=[];claimed=set()
    for left in boxes:
        loc=left['location'];lb=loc['bbox'];page=loc['page']
        if lb[3]-lb[1]<16 or left.get('structure',{}).get('scan_header_evidence'):continue
        right=[b for b in boxes if b is not left and source_cells_share_row(left,b)]
        if len(right)!=1:continue
        right=right[0];rb=right['location']['bbox']
        if right.get('structure',{}).get('label_only_region'):continue
        labels=[b for b in text if b['location']['page']==page and _label_inside(b['location']['bbox'],lb) and source_text_inside_cell(b,left,label=True)]
        values=[b for b in text if source_text_inside_cell(b,right)]
        if not labels or not values or any(b['block_id'] in claimed for b in labels+values):continue
        if all(is_physical_checkbox_text(b,marks) for b in labels):continue
        if _owned_by_reviewed(questions,{b['block_id'] for b in labels+values}|{left['block_id'],right['block_id']}):continue
        labels.sort(key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0]))
        values.sort(key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0]))
        question='\n'.join(b['text'] for b in labels)
        if all(re.fullmatch(r'\s*\d+(?:[./]\d+)*[.)]?\s*',b['text']) for b in labels):continue
        if _record_heading_pair(left,right,values,boxes,blocks):
            for b in labels+values:b.setdefault('structure',{})['resolved_layout_role']='record_column_header'
            _replace_owned(questions,{b['block_id'] for b in labels+values},lookup)
            continue
        # This bounded recovery is for explicit prompts, not arbitrary first
        # columns. A row of record headings is never paired as question/value.
        prompt=bool(re.search(r'[?？]|^(?:how\s|please\s|describe\s|list\s|provide\s|请|是否)',question,re.I))
        aligned=[b for b in boxes if b is not left and b['location']['page']==page and abs(b['location']['bbox'][0]-lb[0])<4 and abs(b['location']['bbox'][2]-lb[2])<4 and abs(b['location']['bbox'][1]-lb[1])<350]
        scalar=bool(len(aligned)>=2 and len(question)<90 and not question.isupper() and not re.search(r'^(?:note|instructions|form|page)\b',question,re.I))
        # An isolated scalar row can already have a source-backed field label
        # without two other similar rows. Its existing label classification is
        # useful only with two actual closed cells and their shared edge; it
        # never authorizes taking the next nonempty text as a value.
        label_ids={b['block_id'] for b in labels}
        declared_scalar=bool(left.get('structure',{}).get('closed_region_evidence') and
            right.get('structure',{}).get('closed_region_evidence') and
            any(old.get('label_refs') and {r['block_id'] for r in old['label_refs']}==label_ids and
                not old.get('options') and all(r.get('start')==0 and r.get('end')==len(lookup[r['block_id']]['text'])
                for r in old['label_refs']) for old in questions))
        if not prompt and not scalar and not declared_scalar:continue
        owned_ids=label_ids|{b['block_id'] for b in values}
        if any((old.get('manual_override') or old.get('answer_origin') in {'manual','knowledge','reviewed_knowledge'}) and
               any(r.get('block_id') in owned_ids for r in old.get('label_refs',[])+old.get('answer_value_refs',[])) for old in questions):continue
        if any(b.get('controls') and b['location'].get('page')==page and _inside(b['location'].get('bbox',[]),rb) for b in blocks):continue
        f=new_field(labels[0],ref(labels[0]));fid='scan-cell-field:'+left['block_id']
        f.update(field_id=fid,question_id=fid,label_refs=[ref(b) for b in labels],question_refs=[ref(b) for b in labels],
            question_text_original=question,answer_region_refs=[ref(right)],answer_value_refs=[ref(b) for b in values],
            answer_refs=[ref(b) for b in values],answer_text_original='\n'.join(b['text'] for b in values),answer_status='filled',
            physical_label_region_ref=ref(left),boundary_evidence='closed_scan_cell_pair',boundary_version=VERSION)
        ids={b['block_id'] for b in labels+values}
        retained=[];replaced=[]
        for old in questions:
            refs=old.get('label_refs') or old.get('question_refs') or []
            if not any(rr['block_id'] in ids for rr in refs):retained.append(old);continue
            replaced.append(deepcopy(old))
            residual=[rr for rr in refs if rr['block_id'] not in ids]
            if residual:
                remaining=deepcopy(old)
                remaining.update(label_refs=residual,question_refs=deepcopy(residual),
                    question_text_original='\n'.join(lookup[rr['block_id']]['text'][rr['start']:rr['end']] for rr in residual),
                    answer_region_refs=[],answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='unresolved',review_status='needs_review')
                remaining.setdefault('review_reasons',[]).append('部分原模型分组已由物理单元格重新定位；剩余内容保留待核对，未自动配对。')
                retained.append(remaining)
        printed_choices=(2<=len(values)<=3 and all(re.fullmatch(r'yes|no|n\s*/?\s*a|是|否|不适用',b['text'].strip(),re.I) for b in values)
                         and len({re.sub(r'\s+','',b['text']).casefold() for b in values})==len(values))
        if printed_choices:
            options=[]
            for value in values:
                original=value['text'];caption=original.strip();start=original.index(caption);end=start+len(caption)
                slot_id=value['block_id']+':printed-option:'+str(start)
                value.setdefault('structure',{}).setdefault('logical_slots',[]).append({'slot_id':slot_id,'kind':'printed_option','start':start,'end':end,'option_label':caption})
                value['structure']['resolved_layout_role']='option'
                options.append({'text_original':caption,'refs':[ref(value,start,end)],'selected':None,
                    'selection_source':'printed_options_without_verified_marks','answer_region_refs':[ref(value,start,end,slot_id)]})
            f.update(options=options,answer_region_refs=[rr for o in options for rr in o['answer_region_refs']],
                answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='unresolved',review_status='needs_review')
            f.setdefault('review_reasons',[]).append('已定位同一答复格中的印刷选项；尚无可靠勾选状态，选项文字不能当成已有答案。')
        valid=not printed_choices and validate_ocr_response_values(f,lookup,retained+[f])
        if valid:
            for b in values:
                # The previous scan-only baseline may have called this actual
                # value a field. Its source survives; the superseded protection
                # must not invalidate the newly proved independent row pairing.
                prior=b.setdefault('structure',{}).pop('protected_label_ranges',None)
                if prior:b['structure']['superseded_label_ranges']=prior
        if not valid and not printed_choices:
            # Geometry establishes the field boundary even when handwriting
            # quality cannot establish a reliable value. Retain actual OCR as
            # an observation, never promote it to a reusable original answer.
            f.update(observed_answer_text=f['answer_text_original'],unvalidated_answer_refs=deepcopy(f['answer_value_refs']),
                answer_text_original='',answer_value_refs=[],answer_refs=[],answer_status='unresolved',review_status='needs_review')
            f.setdefault('review_reasons',[]).append('已按实体表格找到答复区；其中手写或扫描文字未通过原值校验，请对照原页。')
            right['structure'].update(scan_association_valid=False,scan_field_id=fid)
        for b in values:b.setdefault('structure',{}).update(resolved_layout_role='option' if printed_choices else 'existing_answer',ocr_answer_region=right['block_id'])
        for b in labels:b.setdefault('structure',{})['protected_label_ranges']=[{'start':0,'end':len(b['text'])}]
        right.setdefault('structure',{})['replaced_model_groups']=replaced
        questions[:]=retained;additions.append(f);claimed.update(ids)
    questions.extend(additions)
    _restore_closed_choice_rows(questions,blocks,lookup)
    _restore_choice_cells(questions,blocks,lookup)
    _restore_edge_choice_fragments(questions,blocks,lookup)
    _join_split_choice_rows(questions,blocks,lookup)
    from .scanned_option_lists import restore_scanned_option_lists
    restore_scanned_option_lists(questions,blocks,lookup)
    from .scanned_subfields import restore_scanned_subfields
    restore_scanned_subfields(questions,blocks,lookup)
    from .scanned_record_choices import restore_scanned_record_choices
    restore_scanned_record_choices(questions,blocks,lookup)
    from .scan_ruled_choice_groups import split_ruled_choice_groups
    split_ruled_choice_groups(questions,blocks,lookup)
    questions.sort(key=lambda f:lookup.get((f.get('label_refs') or f.get('question_refs') or [{}])[0].get('block_id'),{}).get('order',0))
