"""Keep separately ruled choice groups separate inside a shared form cell."""
from copy import deepcopy

from .field_boundaries import ref
from .selection_evidence import selected_scan_caption

VERSION='scan-ruled-choice-groups-v3-verified-response-cell-top-border'


def _box(block):
    return block.get('location',{}).get('bbox',[])


def _protected(field):
    return bool(field.get('manual_override') or field.get('answer_origin') in {'manual','knowledge','reviewed_knowledge'}
                or any(_protected(c) for c in field.get('children',[])))


def _divider_geometry(block):
    """A literal rule or the observed top edge of a closed answer-only cell."""
    structure=block.get('structure',{});bb=_box(block)
    if len(bb)!=4 or structure.get('source_method')!='raster_geometry':return None
    if structure.get('scan_region_kind')=='write_line':return bb
    if structure.get('scan_region_kind')=='text_box':
        from .scan_section_boundaries import _partition_cell
        if _partition_cell(block) and 8<=bb[3]-bb[1]<=40:
            return [bb[0],bb[1],bb[2],bb[1]]
    return None


def _set_options(field,options,lookup):
    field['options']=deepcopy(options)
    field['answer_region_refs']=[deepcopy(r) for o in options for r in o['answer_region_refs']]
    for option in field['options']:
        for rr in option['answer_region_refs']:
            lookup[rr['block_id']].setdefault('structure',{}).update(scan_field_id=field['field_id'],
                scan_association_valid=True,scan_option_caption_refs=deepcopy(option['refs']))
    selected=[r for o in field['options'] if o.get('selected') is True for r in o['refs']]
    known=all(isinstance(o.get('selected'),bool) for o in field['options'])
    verified=known and all(selected_scan_caption(r,field,lookup) for r in selected)
    field.update(answer_value_refs=deepcopy(selected) if verified else [],answer_refs=deepcopy(selected) if verified else [],
        answer_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in selected) if verified else '',
        answer_status=('filled' if selected else 'blank') if verified else 'unresolved',
        review_status='model_proposed' if verified else 'needs_review',
        review_reasons=[] if verified else ['原页分隔线已明确选项组边界，但部分选中状态尚未证实。'])
    field.pop('choice_groups',None)
    field.pop('form_definition',None)
    field.pop('source_observation',None)
    if verified and selected:field['answer_pairing_evidence']='physical_selected_box_and_source_caption'


def split_ruled_choice_groups(fields,blocks,lookup):
    lines=[b for b in blocks if _divider_geometry(b) is not None]
    for field in list(fields):
        if field.get('boundary_evidence')!='scan_choice_cell_grid' or _protected(field):continue
        options=field.get('options',[])
        if len(options)<4 or any(len(o.get('answer_region_refs',[]))!=1 for o in options):continue
        targets=[lookup.get(o['answer_region_refs'][0]['block_id'],{}) for o in options]
        pages={b.get('location',{}).get('page') for b in targets}
        if len(pages)!=1 or any(len(_box(b))!=4 for b in targets):continue
        page=next(iter(pages));labels=[lookup.get(r['block_id'],{}) for r in field.get('label_refs',[])]
        if not labels or any(len(_box(b))!=4 for b in labels):continue
        # Only a separate response-side divider can establish an independent
        # group. A writing line beginning after an option caption cannot.
        left=min(_box(b)[0] for b in targets);right=max(_box(b)[2] for b in targets)
        label_right=max(_box(b)[2] for b in labels)
        candidates=[]
        for line in lines:
            loc=line['location'];lb=_divider_geometry(line)
            if loc.get('page')!=page or len(lb)!=4:continue
            yy=lb[3]
            above=[i for i,b in enumerate(targets) if _box(b)[3]<lb[1]+2]
            below=[i for i,b in enumerate(targets) if _box(b)[1]>yy+1]
            if len(above)<2 or len(below)<2 or len(above)+len(below)!=len(options):continue
            if not label_right<lb[0]+3 or not left-12<=lb[0]<=left+3 or lb[2]<right+10:continue
            lower_bottom=max(_box(targets[i])[3] for i in below)
            if lower_bottom-yy>40:continue
            if line.get('structure',{}).get('scan_region_kind')=='text_box':
                from .pdf_source_relations import source_text_inside_cell
                if any(not source_text_inside_cell(targets[i],line) for i in below):continue
            # The lower group's actual captions must also fit under the rule.
            captions=[lookup.get(r['block_id'],{}) for i in below for r in options[i].get('refs',[])]
            if any(len(_box(b))!=4 or _box(b)[1]<yy-2 or _box(b)[2]>lb[2]+4 for b in captions):continue
            candidates.append((line,above,below))
        if len(candidates)!=1:continue
        line,above,below=candidates[0]
        selected_options=[options[i] for i in below]
        rr=[deepcopy(r) for o in selected_options for r in o['refs']]
        fid='scan-divided-choice:'+line['block_id']
        if any(c.get('field_id')==fid for c in field.get('children',[])):continue
        child=deepcopy(field)
        child.update(field_id=fid,question_id=fid,parent_question_id=field['field_id'],children=[],
            label_refs=deepcopy(field['label_refs']),question_refs=deepcopy(field['label_refs']),
            question_text_original=field['question_text_original'],
            option_group_caption_refs=deepcopy(rr),
            option_group_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in rr),
            context_refs=deepcopy(field['label_refs']),context_label=field['question_text_original'],
            boundary_evidence='closed_scan_divided_option_group',boundary_version=VERSION,
            divided_choice_evidence={'divider_ref':ref(line),'parent_label_refs':deepcopy(field['label_refs']),
                'option_caption_refs':deepcopy(rr),'option_region_refs':[deepcopy(r) for o in selected_options for r in o['answer_region_refs']]})
        _set_options(child,selected_options,lookup)
        _set_options(field,[options[i] for i in above],lookup)
        field.setdefault('children',[]).append(child)
        field.setdefault('boundary_history',[]).append({'action':'split_at_observed_response_divider',
            'version':VERSION,'divider_ref':ref(line),'child_field_id':fid})


def verified_self_caption_value(value,field,lookup):
    """The field is named by its own mutually separate option captions.

    This permits reading a selected option, not writing over any caption.
    The separate physical checkbox and original divider remain mandatory.
    """
    if field.get('boundary_evidence')!='closed_scan_divided_option_group':return False
    proof=field.get('divided_choice_evidence',{})
    if field.get('label_refs')!=proof.get('option_caption_refs') or field.get('answer_region_refs')!=proof.get('option_region_refs'):return False
    if value not in field.get('label_refs',[]) or not selected_scan_caption(value,field,lookup):return False
    divider=lookup.get(proof.get('divider_ref',{}).get('block_id'),{});db=_divider_geometry(divider)
    if db is None:return False
    if field.get('context_refs')!=proof.get('parent_label_refs'):return False
    for target in field.get('answer_region_refs',[]):
        b=lookup.get(target['block_id'],{});bb=_box(b)
        if len(bb)!=4 or b.get('location',{}).get('page')!=divider.get('location',{}).get('page'):return False
        if not db[0]-3<=bb[0]<bb[2]<=db[2] or not db[3]+1<bb[1]<bb[3]<=db[3]+40:return False
    return True


def verified_divided_choice_group(field,lookup):
    """Verify a subgroup caption without promoting its options to a question.

    The source's shared left label names both groups. The lower option captions
    identify the subgroup only when the original divider and all separate
    physical boxes remain the same. Used for exact source-position comparison.
    """
    if (field.get('boundary_evidence')!='closed_scan_divided_option_group' or
        field.get('boundary_version') not in {VERSION,'scan-ruled-choice-groups-v2-parent-label-distinct-option-captions'}
        or not field.get('parent_question_id')):return False
    proof=field.get('divided_choice_evidence') or {}
    captions=[r for o in field.get('options',[]) for r in o.get('refs',[])]
    targets=[r for o in field.get('options',[]) for r in o.get('answer_region_refs',[])]
    if (field.get('label_refs')!=proof.get('parent_label_refs') or
        field.get('context_refs')!=proof.get('parent_label_refs') or
        field.get('option_group_caption_refs')!=captions or captions!=proof.get('option_caption_refs') or
        field.get('answer_region_refs')!=targets or targets!=proof.get('option_region_refs') or len(targets)<2):return False
    divider=lookup.get(proof.get('divider_ref',{}).get('block_id'),{});db=_divider_geometry(divider)
    if db is None:return False
    page=divider.get('location',{}).get('page')
    for rr in field['label_refs']+captions:
        b=lookup.get(rr.get('block_id'),{});a,z=rr.get('start'),rr.get('end');bb=_box(b)
        if (type(a) is not int or type(z) is not int or not 0<=a<z<=len(b.get('text','')) or
            len(bb)!=4 or b.get('location',{}).get('page')!=page):return False
        if rr in field['label_refs'] and bb[2]>db[0]+3:return False
        if rr in captions and (bb[1]<db[3]-2 or bb[2]>db[2]+4):return False
    if {r['block_id'] for r in field['label_refs']} & {r['block_id'] for r in captions}:return False
    for rr in targets:
        b=lookup.get(rr.get('block_id'),{});bb=_box(b)
        if (len(bb)!=4 or b.get('location',{}).get('page')!=page or
            b.get('structure',{}).get('scan_region_kind')!='checkbox' or
            not db[0]-3<=bb[0]<bb[2]<=db[2] or not db[3]+1<bb[1]<bb[3]<=db[3]+40):return False
        if divider.get('structure',{}).get('scan_region_kind')=='text_box':
            from .pdf_source_relations import source_text_inside_cell
            if not source_text_inside_cell(b,divider):return False
    return len({r['block_id'] for r in targets})==len(targets)


def verified_shared_parent_label(parent,child,lookup):
    """A shared merged label names two physically independent option groups."""
    if (not verified_divided_choice_group(child,lookup)
        or child.get('parent_question_id')!=parent.get('field_id')
        or parent.get('boundary_evidence')!='scan_choice_cell_grid'
        or parent.get('label_refs')!=child.get('label_refs')
        or sum(c.get('field_id')==child.get('field_id') for c in parent.get('children',[]))!=1):return False
    divider=lookup.get(child['divided_choice_evidence']['divider_ref']['block_id'],{})
    db=_divider_geometry(divider)
    regions=[r for o in parent.get('options',[]) for r in o.get('answer_region_refs',[])]
    child_ids={r['block_id'] for r in child['answer_region_refs']}
    if (db is None or not regions or regions!=parent.get('answer_region_refs')
        or len({r['block_id'] for r in regions})!=len(regions)
        or child_ids.intersection(r['block_id'] for r in regions)):return False
    for rr in regions:
        b=lookup.get(rr.get('block_id'),{});bb=_box(b)
        if (len(bb)!=4 or b.get('location',{}).get('page')!=divider.get('location',{}).get('page')
            or b.get('structure',{}).get('scan_region_kind')!='checkbox'
            or not db[0]-3<=bb[0]<bb[2]<=db[2] or bb[3]>=db[1]+2):return False
    return True
