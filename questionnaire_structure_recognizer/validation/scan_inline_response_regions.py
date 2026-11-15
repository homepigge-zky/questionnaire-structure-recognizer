"""Source-located writing lines inside question cells, separate from choice columns."""
from copy import deepcopy
import re
from .field_boundaries import ref
from .pdf_furniture_ownership import _closed
from .pdf_source_relations import source_text_inside_cell, source_cells_share_row
from .scan_business_grids import verified_adjacent_cell
from .scan_closed_local_grids import box,page,contents,field,install,walk,row,shares_horizontal_border
from .scan_ruled_text_order import cell_reading_order
import hashlib
import json

VERSION='scan-inline-response-regions-v3-conditional-and-reviewed-subvalues'
UNITS=re.compile(r'^(?:%|％|years?|days?|months?|hours?|m[²2]|kg|g|人|年|天|日|月|小时)$',re.I)
BINARY=re.compile(r'^\s*(?:is|are|does|do|has|have|can|will)\b',re.I)


def _cy(b):return (box(b)[1]+box(b)[3])/2

def _number_labels(cell,texts):
    cb=box(cell)
    return [t for t in texts if page(t)==page(cell) and re.fullmatch(r'\d+(?:\.\d+)*[.)]?',t['text'].strip())
            and 0<cb[0]-box(t)[2]<55 and cb[1]+2<_cy(t)<cb[3]-2]

def tail_region_plans(blocks):
    """Candidate geometry only; actual ink must be measured before use."""
    real=[b for b in blocks if b.get('location',{}).get('type')=='pdf' and _closed(b)]
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('type')=='pdf']
    plans=[]
    for cell in real:
        cb=box(cell);labels=cell_reading_order(contents(cell,texts),cell)
        if cb[2]-cb[0]<180 or len(_number_labels(cell,texts))!=1 or len(labels)<2:continue
        tail=labels[-1];tb=box(tail)
        # A short label below an explicit question can identify the remaining
        # interior strip. A sentence ending in punctuation cannot do so.
        if not (2<len(tail['text'].strip())<48 and not re.search(r'[?？!。()（）]',tail['text']) and
                len(tail['text'].split())<=5 and tb[0]<cb[0]+18 and
                any(re.search(r'[?？]',t['text']) and box(t)[3]<tb[1]+2 for t in labels[:-1]) and
                0<=cb[3]-tb[3]<16 and cb[2]-tb[2]>80):continue
        bbox=[tb[2]+4,max(cb[1]+3,tb[1]),cb[2]-4,min(cb[3]-3,tb[3]+2)]
        if bbox[3]-bbox[1]<6:continue
        if any(b.get('structure',{}).get('scan_region_kind')=='write_line' and page(b)==page(cell) and
               source_text_inside_cell(b,cell) and abs(_cy(b)-_cy(tail))<8 and box(b)[0]>tb[2]-2 for b in blocks):continue
        plans.append((cell,tail,labels,bbox))
    return plans


def conditional_tail_plans(blocks):
    """Explicit wrapped conditional labels can leave a small same-cell tail.

    The slanted original lower edge, not its enclosing rectangle, bounds the
    writing strip. The observer must still prove the proposed strip is blank.
    """
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('type')=='pdf']
    plans=[]
    for cell in blocks:
        if cell.get('location',{}).get('type')!='pdf' or not _closed(cell):continue
        cb=box(cell);labels=cell_reading_order(contents(cell,texts),cell)
        if not (15<cb[3]-cb[1]<40 and len(labels)>=2 and
            re.match(r'^if\s+(?:yes|no)\s*[:,]?\s*(?:please\s+)?(?:give|provide|specify)\b',labels[0]['text'],re.I)
            and labels[-1]['text'].rstrip().endswith((':','：'))):continue
        label=labels[-1];lb=box(label);left=lb[2]+3;right=cb[2]-4
        if right-left<40:continue
        polygon=cell['structure']['closed_region_evidence']['polygon']
        lower=sorted(sorted(polygon,key=lambda p:p[1])[-2:])
        if lower[1][0]-lower[0][0]<cb[2]-cb[0]-8:continue
        def bottom(x):return lower[0][1]+(x-lower[0][0])*(lower[1][1]-lower[0][1])/(lower[1][0]-lower[0][0])
        bbox=[left,lb[1],right,min(bottom(x) for x in (left,right))-1.5]
        if bbox[3]-bbox[1]<6:continue
        proof={'kind':'polygon_bounded_conditional_colon_tail','source_polygon':deepcopy(polygon)}
        plans.append((cell,label,labels,bbox,proof))
    return plans

def verified_tail_slot(cell,slot,lookup=None,source_hash=None):
    proof=slot.get('blank_observation',{})
    if proof.get('method')!='source_pixel_blank_tail_v1' or slot.get('kind')!='scan_observed_blank_tail':return False
    payload={k:v for k,v in proof.items() if k!='proof_digest'}
    digest=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    valid=(proof.get('proof_digest')==digest and proof.get('region_id')==cell['block_id'] and
            proof.get('region_bbox')==box(cell) and proof.get('slot_bbox')==slot.get('bbox') and
            proof.get('region_text')==cell.get('text','') and proof.get('blank') is True and
            bool(re.fullmatch('[0-9a-f]{64}',proof.get('source_sha256',''))) and
            slot.get('source_sha256')==proof.get('source_sha256') and slot.get('blank') is True)
    if not valid or source_hash is not None and source_hash!=proof['source_sha256']:return False
    if lookup is not None:
        rr=proof.get('tail_label_ref') or {};label=lookup.get(rr.get('block_id'),{})
        if (label.get('text')!=proof.get('label_text') or label.get('location',{}).get('bbox')!=proof.get('label_bbox')
            or label.get('location',{}).get('page')!=proof.get('page') or rr.get('start')!=0
            or rr.get('end')!=len(label.get('text','')) or slot.get('tail_label_ref')!=rr):return False
        snapshots=proof.get('source_label_snapshots',[])
        if not snapshots:return False
        for snapshot in snapshots:
            r=snapshot['ref'];b=lookup.get(r['block_id'],{})
            if (r.get('start')!=0 or r.get('end')!=len(b.get('text','')) or b.get('text')!=snapshot['text']
                or b.get('location',{}).get('bbox')!=snapshot['bbox'] or b.get('location',{}).get('page')!=proof['page']):return False
        local=proof.get('inner_rule_evidence',{})
        if local.get('kind')=='bounded_optional_free_text':
            lower=lookup.get(local.get('following_ref',{}).get('block_id'),{})
            lane=lookup.get(local.get('lane_ref',{}).get('block_id'),{})
            if (lower.get('text')!=local.get('following_text') or lower.get('location',{}).get('bbox')!=local.get('following_bbox')
                or lane.get('location',{}).get('bbox')!=local.get('lane_bbox') or
                lower.get('location',{}).get('page')!=proof['page'] or lane.get('location',{}).get('page')!=proof['page']):return False
            if not proof['label_bbox'][3]<slot['bbox'][1]<slot['bbox'][3]<local['following_bbox'][1]:return False
        elif local.get('kind')=='column_caption_continuation':
            header=lookup.get(local.get('header_ref',{}).get('block_id'),{})
            if header.get('location',{}).get('bbox')!=local.get('header_bbox') or header.get('location',{}).get('page')!=proof['page']:return False
        elif local.get('kind')=='polygon_bounded_conditional_colon_tail':
            if not _closed(cell) or cell['structure']['closed_region_evidence']['polygon']!=local.get('source_polygon'):return False
            plans=[p for p in conditional_tail_plans(list(lookup.values())) if p[0]['block_id']==cell['block_id']]
            if len(plans)!=1 or plans[0][3]!=slot['bbox']:return False
    return True


def _question_refs(labels):
    """A line wrap does not move the beginning of a binary question."""
    out=[]
    for label in labels:
        match=re.search(r'[?？]',label['text'])
        out.append(ref(label,0,match.end() if match else len(label['text'])))
        if match:break
    return out


def _attach_parent(child,parent,labels):
    child['parent_question_id']=parent['field_id']
    child['context_refs']=deepcopy(parent['label_refs'])
    child['context_label']=parent['question_text_original']
    conditions=[]
    for label in labels:
        m=re.search(r'\bif\s+(yes|no)\b',label['text'],re.I)
        if m:conditions.append((label,m))
    if len(conditions)==1:
        label,m=conditions[0];condition_ref=ref(label,m.start(),len(label['text']))
        child['explicit_condition']={'status':'unresolved','source_text':label['text'][m.start():],
            'source_refs':[condition_ref],'parent_field_id':parent['field_id'],
            'equals_option':m.group(1).capitalize(),'source_relationship':'same_cell_preceding_question',
            'reason':'原文同一单元格明确给出条件，父题未回答时保留未确定。'}
    parent['children'].append(child)


def _continued_local_condition(child,cell,cells,texts,fields):
    """An explicit conditional text row follows one uninterrupted choice grid."""
    current=cell;chain=[]
    for _ in range(20):
        prior=[c for c in cells if _closed(c) and page(c)==page(cell) and abs(box(c)[0]-box(cell)[0])<5
               and abs(box(c)[2]-box(cell)[2])<5 and box(c)[1]<box(current)[1]
               and shares_horizontal_border(c,current,max(box(c)[0],box(current)[0]),min(box(c)[2],box(current)[2]))]
        if len(prior)!=1:return
        current=prior[0];chain.append(ref(current));labels=contents(current,texts)
        if not labels:return
        ids={b['block_id'] for b in labels}
        owners=[f for f in walk(fields) if any(r['block_id'] in ids for r in f.get('label_refs',[])) and f.get('options')]
        if len(owners)!=1:return
        parent=owners[0]
        if BINARY.match(parent.get('question_text_original','')):
            child.update(parent_question_id=parent['field_id'],context_refs=deepcopy(parent['label_refs']),
                context_label=parent['question_text_original'],explicit_condition={'status':'unresolved',
                'source_text':child['question_text_original'],'source_refs':deepcopy(child['label_refs']),
                'parent_field_id':parent['field_id'],'equals_option':'Yes',
                'source_relationship':'continuous_closed_choice_rows','intervening_cell_refs':chain,
                'reason':'条件原文与父题处于连续且同列的实体表格，父题未回答时保留未确定。'})
            return


def _same_row_condition(child,cell,cells,fields,lookup):
    """A right-hand conditional tail belongs to the physically adjoining choice.

    Require both the choice cell and its label cell to share the actual row
    edges. A nearby question or a merely similar caption is not a parent.
    """
    condition=re.match(r'^if\s+(yes|no)\b',child['question_text_original'],re.I)
    if not condition:return False
    middle=[c for c in cells if _closed(c) and source_cells_share_row(c,cell)]
    if len(middle)!=1:return False
    middle=middle[0]
    left=[c for c in cells if _closed(c) and source_cells_share_row(c,middle)]
    if len(left)!=1:return False
    candidates=[]
    for parent in walk(fields):
        options=parent.get('options') or []
        labels=parent.get('label_refs') or []
        option_refs=[r for o in options for r in o.get('refs',[])]
        if (not labels or not option_refs or not BINARY.match(parent.get('question_text_original',''))
            or not {'yes','no'} <= {o.get('text_original','').strip().casefold() for o in options}):continue
        if (all(source_text_inside_cell(lookup.get(r['block_id'],{}),left[0],label=True) for r in labels)
            and all(source_text_inside_cell(lookup.get(r['block_id'],{}),middle,label=True) for r in option_refs)):
            candidates.append(parent)
    if len(candidates)!=1:return False
    parent=candidates[0]
    child.update(parent_question_id=parent['field_id'],context_refs=deepcopy(parent['label_refs']),
        context_label=parent['question_text_original'],explicit_condition={'status':'unresolved',
        'source_text':child['question_text_original'],'source_refs':deepcopy(child['label_refs']),
        'parent_field_id':parent['field_id'],'equals_option':condition.group(1).capitalize(),
        'source_relationship':'same_closed_row_choice_parent','row_cell_refs':[ref(c) for c in [left[0],middle,cell]],
        'reason':'条件文字与父题的标签格、选择格具有连续的原生行边界；未选择时不决定适用性。'})
    return True


def _reviewed_inline_subvalues(fields,blocks,lookup,non_qa):
    """Split only source-reviewed values in a verified conditional question cell.

    Stage every slot before changing the parent so an invalid or moved value
    cannot partly rewrite a question. This recovers original answers, not
    knowledge authorization, and never edits manually corrected structures.
    """
    from .scanned_subfields import _emit, verified_scan_subfield_pairing
    from .pdf_furniture_ownership import _protected
    from ..source_text_review import verified_reviewed_text
    report=[]
    if non_qa is None:return report
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('type')=='pdf']
    for parent in list(walk(fields)):
        if _protected(parent) or not parent.get('options') or parent.get('children'):continue
        cell=lookup.get(parent.get('physical_label_region_ref',{}).get('block_id'),{})
        if not cell or not _closed(cell):continue
        local=contents(cell,texts)
        if not local or not BINARY.match(local[0]['text']):continue
        aligned=[b for b in local if abs(box(b)[0]-box(local[0])[0])<22]
        qrefs=_question_refs(aligned)
        if not qrefs or not re.search(r'[?？]',lookup[qrefs[-1]['block_id']]['text'][qrefs[-1]['start']:qrefs[-1]['end']]):continue
        last=lookup[qrefs[-1]['block_id']];tail=last['text'][qrefs[-1]['end']:]
        cm=re.search(r'\bif\s+(yes|no)\b',tail,re.I)
        if not cm:continue
        pairs=[]
        for b in aligned:
            if box(b)[1]<=box(last)[1]+4:continue
            match=re.fullmatch(r'([^:：?？]{2,60}[:：])\s*(\S.{0,60})',b['text'])
            if match:pairs.append((b,ref(b,0,match.end(1)),ref(b,match.start(2),match.end(2))))
        if len(pairs)<2 or any(not verified_reviewed_text(b) for b,_,_ in pairs):continue
        used={r['block_id'] for r in qrefs}|{b['block_id'] for b,_,_ in pairs}
        notes=[b for b in local if b['block_id'] not in used]
        if any(not verified_reviewed_text(b) or box(b)[0]<=max(box(v)[2] for v,_,_ in pairs)+15 for b in notes):continue
        # Another independent field owning these spans makes the relationship
        # ambiguous. Do not replace its source value or edit by list index.
        owned={b['block_id'] for b,_,_ in pairs}
        if any(f is not parent and any(r['block_id'] in owned for key in
            ('label_refs','answer_value_refs','answer_region_refs') for r in f.get(key,[])) for f in walk(fields)):continue
        staged=deepcopy(lookup);generated=[]
        for b,lr,vr in pairs:
            child=_emit(generated,list(staged.values()),staged,staged[cell['block_id']],[lr],[vr])
            if not child or not verified_scan_subfield_pairing(child,staged):break
        if len(generated)!=len(pairs):continue
        condition_ref=ref(last,qrefs[-1]['end']+cm.start(),len(last['text']))
        parent.setdefault('boundary_history',[]).append({'action':'source_reviewed_conditional_subvalues',
            'version':VERSION,'prior_label_refs':deepcopy(parent.get('label_refs',[])),
            'prior_question_text_original':parent.get('question_text_original','')})
        for b,_,_ in pairs:lookup[b['block_id']]['structure']=deepcopy(staged[b['block_id']]['structure'])
        parent.update(label_refs=deepcopy(qrefs),question_refs=deepcopy(qrefs),
            question_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in qrefs),
            instruction_refs=[condition_ref]+[ref(b) for b in notes])
        if notes:
            for b in notes:b.setdefault('structure',{})['resolved_layout_role']='instruction'
            parent['source_note_groups']=[{'kind':'source_reviewed_same_cell_note',
                'container_ref':ref(cell),'refs':[ref(b) for b in notes]}]
            non_qa.append({'role':'note','refs':[ref(b) for b in notes],'text_original':'\n'.join(b['text'] for b in notes),
                'reason':'同一实体区域旁侧的已校对原文说明，保留来源但不混入题干或答案。','field_id':parent['field_id']})
        for child in generated:
            child.update(parent_question_id=parent['field_id'],context_refs=deepcopy(qrefs),
                context_label=parent['question_text_original'],section_refs=deepcopy(parent.get('section_refs',[])),
                section_path=deepcopy(parent.get('section_path',[])),section_text_original=parent.get('section_text_original',''),
                explicit_condition={'status':'unresolved','source_text':last['text'][condition_ref['start']:],
                    'source_refs':[condition_ref],'parent_field_id':parent['field_id'],'equals_option':cm.group(1).capitalize(),
                    'source_relationship':'same_closed_cell_explicit_reviewed_values'})
        parent['children']=generated
        parent.pop('form_definition',None)
        report.append({'parent':parent['field_id'],'children':[f['field_id'] for f in generated],'note_refs':[ref(b) for b in notes]})
    return report


def reconcile_inline_response_regions(fields,blocks,lookup,non_qa=None):
    real=[b for b in blocks if b.get('location',{}).get('type')=='pdf' and _closed(b)]
    cells=[b for b in blocks if b.get('location',{}).get('type')=='pdf' and verified_adjacent_cell(b,real)]
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('type')=='pdf']
    lines=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='write_line' and
           b['structure'].get('blank_state')=='blank' and b['structure'].get('text_overlap') is False]
    report={'version':VERSION,'restored':[],'protected':[]}
    report['reviewed_subvalues']=_reviewed_inline_subvalues(fields,blocks,lookup,non_qa)
    # An option's explicit "(specify)" tail is a separate conditional text
    # slot. Its printed dots and blank pixels were measured from this source.
    for cell in cells:
        for slot in cell.get('structure',{}).get('logical_slots',[]):
            if (not verified_tail_slot(cell,slot,lookup) or
                slot['blank_observation'].get('inner_rule_evidence',{}).get('kind')!='observed_dotted_option_tail'):continue
            lr=slot['tail_label_ref'];owners=[]
            for parent in walk(fields):
                for option in parent.get('options',[]):
                    if any(r['block_id']==lr['block_id'] for r in option.get('refs',[])):
                        owners.append((parent,option))
            if len(owners)!=1:continue
            parent,option=owners[0]
            from .pdf_furniture_ownership import _protected
            if _protected(parent):continue
            anchor=lookup[lr['block_id']];rr={**ref(cell),'slot_id':slot['slot_id']}
            fid='closed-grid:'+cell['block_id']+':option-tail:'+slot['slot_id']
            if any(f['field_id']==fid for f in walk(fields)):continue
            child=field(cell,[anchor],[],suffix=':option-tail:'+slot['slot_id'],context=[lookup[r['block_id']] for r in parent['label_refs']])
            child.update(answer_region_refs=[rr],parent_question_id=parent['field_id'],options=[],field_type_hint='long_text',
                answer_status='blank',review_status='model_proposed',review_reasons=[],section_refs=deepcopy(parent.get('section_refs',[])),
                section_path=deepcopy(parent.get('section_path',[])),section_text_original=parent.get('section_text_original',''),
                explicit_condition={'status':'unresolved','source_text':anchor['text'],'source_refs':[lr],
                    'parent_field_id':parent['field_id'],'equals_option':option['text_original'],
                    'source_relationship':'printed_option_specify_with_measured_dots'},
                inline_response_evidence={'kind':'source_pixel_verified_option_tail','region_ref':rr,'tail_label_ref':lr})
            cell['structure'].update(scan_association_valid=True,scan_field_id=child['field_id'])
            parent.setdefault('children',[]).append(child)
            report['restored'].append({'kind':'source_pixel_verified_option_tail','field_id':child['field_id'],'region_ref':rr})
    for cell in cells:
        cb=box(cell)
        # A wrapped conditional instruction can occupy a complete short cell
        # without providing an actual writing span. Preserve both source lines
        # as one request, and explicitly keep its missing target unresolved.
        short_labels=cell_reading_order(contents(cell,texts),cell)
        short_tails=[s for s in cell.get('structure',{}).get('logical_slots',[])
            if s.get('blank_observation',{}).get('inner_rule_evidence',{}).get('kind')=='polygon_bounded_conditional_colon_tail'
            and verified_tail_slot(cell,s,lookup)]
        if len(short_tails)==1:
            slot=short_tails[0];rr={**ref(cell),'slot_id':slot['slot_id']}
            f=field(cell,short_labels,[],suffix=':conditional-tail:'+slot['slot_id'])
            f.update(answer_region_refs=[rr],options=[],answer_status='blank',review_reasons=[],
                review_status='model_proposed',field_type_hint='long_text',
                explicit_condition={'status':'unresolved','source_text':f['question_text_original'],
                    'source_refs':deepcopy(f['label_refs']),'parent_field_id':'',
                    'equals_option':re.match(r'^if\s+(yes|no)\b',f['question_text_original'],re.I).group(1).capitalize(),
                    'source_relationship':'unresolved_original_conditional_label',
                    'reason':'原文明确为条件填写，父题关系未经实际行边界证明时不自动填写。'},
                inline_response_evidence={'version':VERSION,'kind':'source_pixel_verified_conditional_tail',
                    'tail_label_ref':deepcopy(slot['tail_label_ref']),'region_ref':rr})
            linked=_same_row_condition(f,cell,cells,fields,lookup)
            installed=install(fields,[f],[cell],[cell],texts,lookup,
                {'kind':'polygon_bounded_conditional_colon_tail','version':VERSION},report)
            if installed and linked:
                parent=next((p for p in walk(fields) if p['field_id']==f['parent_question_id']),None)
                from .pdf_furniture_ownership import _protected
                if parent is not None and not _protected(parent) and f in fields:
                    # Store the relationship in the document tree as well as
                    # the conditional reference, so persistence and export do
                    # not depend on the scan's skewed text ordering.
                    fields.remove(f);parent.setdefault('children',[]).append(f)
            continue
        if (cb[3]-cb[1]<40 and len(short_labels)>=2 and
            re.match(r'^if\s+yes\s*[:,]?\s*(?:please\s+)?(?:give|provide|specify)\b',short_labels[0]['text'],re.I)
            and max(box(t)[3] for t in short_labels)>cb[3]-4
            and not any(source_text_inside_cell(line,cell) for line in lines)):
            f=field(cell,short_labels,[],suffix=':unlocated-instruction')
            f.update(answer_status='not_located',review_status='needs_review',
                review_reasons=['原件该格仅有换行的填写要求，未找到独立答复区域；不能借用相邻字段或选择格。'],
                source_template_limitation={'kind':'instruction_fills_source_cell','cell_ref':ref(cell),
                    'label_refs':[ref(t) for t in short_labels]},field_type_hint='long_text')
            install(fields,[f],[cell],[],texts,lookup,{'kind':'wrapped_instruction_without_response_area','version':VERSION},report)
        if cb[2]-cb[0]<180:continue
        labels=cell_reading_order(contents(cell,texts),cell)
        if not labels:continue
        # A conditional prompt before a later independent question belongs to
        # the preceding row. The dedicated mixed-question parser owns it.
        if re.match(r'^\s*if\b',labels[0]['text'],re.I) and any(BINARY.match(t['text']) for t in labels[1:]):continue
        numbers=_number_labels(cell,texts)
        conditional_prompt=bool(re.match(r'^if\s+yes\s*[:,]?\s*(?:please\s+)?(?:specify|provide|name|list)\b',labels[0]['text'],re.I))
        if len(numbers)!=1 and not conditional_prompt:continue
        local=[]
        for line in lines:
            lb=box(line)
            if not (page(line)==page(cell) and cb[0]+2<lb[0]<lb[2]<cb[2]-2 and lb[2]-lb[0]>25
                    and source_text_inside_cell(line,cell) and lb[1]>cb[1]+4):continue
            # Letter strokes and table rules are not writing lines. A target
            # may touch a label edge, but cannot pass through printed text.
            if any(min(lb[2],box(t)[2])-max(lb[0],box(t)[0])>4 and
                   min(lb[3],box(t)[3])-max(lb[1],box(t)[1])>2 for t in labels):continue
            local.append(line)
        local.sort(key=lambda b:(_cy(b),box(b)[0]))
        groups=[]
        for line in local:
            lb=box(line)
            anchors=[t for t in labels if not UNITS.fullmatch(t['text'].strip()) and
                     -2<=lb[0]-box(t)[2]<35 and abs(_cy(t)-_cy(line))<9]
            if anchors:
                anchor=max(anchors,key=lambda t:box(t)[2]);groups.append({'label':anchor,'lines':[line]})
            elif conditional_prompt and not groups:
                above=[t for t in labels if 0<lb[1]-box(t)[3]<20 and abs(lb[0]-box(t)[0])<15]
                if len(above)==1:groups.append({'label':above[0],'lines':[line]})
            elif groups and 0<_cy(line)-_cy(groups[-1]['lines'][-1])<22:
                previous=groups[-1]['lines'][-1]
                intervening=[t for t in labels if _cy(previous)+3<_cy(t)<_cy(line)-3]
                if not intervening:groups[-1]['lines'].append(line)
        tails=[s for s in cell.get('structure',{}).get('logical_slots',[]) if verified_tail_slot(cell,s)]
        if not groups and not tails:continue
        question_refs=_question_refs(labels)
        question_text='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in question_refs)
        first_question=labels[0]
        binary=bool(BINARY.match(question_text) and not re.search(r'\bor\b',question_text,re.I))
        source_ids={t['block_id'] for t in labels}
        new=[]
        if binary:
            prior=[f for f in walk(fields) if any(rr['block_id']==first_question['block_id'] for rr in f.get('label_refs',[])) and f.get('options')]
            if len(prior)!=1:continue
            parent=deepcopy(prior[0]);parent.update(label_refs=deepcopy(question_refs),question_refs=deepcopy(question_refs),
                question_text_original=question_text,children=[],instruction_refs=[ref(t) for t in labels if t is not first_question])
            new=[parent]
        for gi,group in enumerate(groups):
            anchor=group['label'];targets=group['lines'];lb=box(targets[0])
            units=[t for t in labels if UNITS.fullmatch(t['text'].strip()) and -3<=box(t)[0]-box(targets[-1])[2]<12 and
                   abs(_cy(t)-_cy(targets[-1]))<10]
            prompt=[anchor] if binary else [t for t in labels if t not in units]
            f=field(cell,prompt,targets,suffix=':inner-line:'+targets[0]['block_id'])
            f.update(options=[],unit_refs=[ref(t) for t in units],unit_text_original='\n'.join(t['text'] for t in units),
                     field_type_hint='decimal' if units and units[0]['text'].strip() in {'%','％'} else 'long_text',
                     original_number=numbers[0]['text'].rstrip('.)') if numbers else '',
                     number_refs=[ref(n) for n in numbers],
                     inline_response_evidence={'version':VERSION,'kind':'observed_blank_lines_inside_question_cell',
                        'label_cell_ref':ref(cell),'anchor_label_ref':ref(anchor),'line_refs':[ref(t) for t in targets]},
                     answer_status='blank',review_reasons=[],review_status='model_proposed')
            if binary:
                _attach_parent(f,parent,labels)
            else:
                if conditional_prompt:_continued_local_condition(f,cell,cells,texts,fields)
                new.append(f)
        for slot in tails:
            anchor=lookup[slot['tail_label_ref']['block_id']]
            prompt=[anchor] if binary else labels
            f=field(cell,prompt,[],suffix=':tail:'+slot['slot_id'])
            rr={**ref(cell),'slot_id':slot['slot_id']}
            f.update(answer_region_refs=[rr],options=[],answer_status='blank',review_reasons=[],
                     original_number=numbers[0]['text'].rstrip('.)') if numbers else '',number_refs=[ref(n) for n in numbers],
                     field_type_hint='long_text',inline_response_evidence={'version':VERSION,
                         'kind':'source_pixel_verified_blank_tail','tail_label_ref':ref(anchor),'region_ref':rr})
            slot['protected_label_refs']=deepcopy(f['label_refs'])
            if binary:
                _attach_parent(f,parent,labels)
            else:new.append(f)
        evidence={'kind':'inner_writing_lines_separate_from_option_columns','version':VERSION,'label_cell':ref(cell),
                  'response_lines':[ref(x) for g in groups for x in g['lines']], 'kept_parent_choice':binary}
        install(fields,new,[cell], [x for g in groups for x in g['lines']]+([cell] if tails else []), texts,lookup,evidence,report)
    from .scan_continuation_conditions import reconcile_continued_conditions
    report['continued_conditions']=reconcile_continued_conditions(fields,blocks,lookup)
    return report
