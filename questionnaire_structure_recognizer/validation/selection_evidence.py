"""Shared source verification for selection captions outside response cells."""
from __future__ import annotations

import re


def verified_vector_region_ref(ref: dict, field: dict, lookup: dict) -> bool:
    """A native table label may share a PDF frame with separate vector boxes."""
    if field.get('physical_form_region', {}).get('source') == 'native_table_prefix_choices':
        from .native_table_positions import verified_native_table_choice
        return verified_native_table_choice(ref, field, lookup)
    from .table_choice_evidence import verified_table_vector_ref
    if field.get('physical_form_region', {}).get('source') == 'native_vector_table_row':
        return verified_table_vector_ref(ref, field, lookup)
    from ..parser_core.pdf_vector_forms import verified_vector_control
    block=lookup.get(ref.get('block_id'),{});region=field.get('physical_form_region',{})
    native_region={k:v for k,v in region.items() if k!='local_bbox'}
    if (ref not in field.get('answer_region_refs',[]) or not verified_vector_control(block)
            or region.get('source')!='native_vector_closed_edges' or
            native_region not in block.get('structure',{}).get('native_closed_form_regions',[])):return False
    labels=field.get('label_refs',[]);rb=region.get('bbox',[])
    if not labels or len(rb)!=4:return False
    local=region.get('local_bbox',rb);target_box=block.get('location',{}).get('bbox',[])
    if (len(local)!=4 or len(target_box)!=4 or not rb[0]<=local[0]<local[2]<=rb[2]
            or not rb[1]<=local[1]<local[3]<=rb[3]
            or not local[0]-1<=(target_box[0]+target_box[2])/2<=local[2]+1
            or not local[1]-1<=(target_box[1]+target_box[3])/2<=local[3]+1):return False
    for lr in labels:
        label=lookup.get(lr.get('block_id'),{});loc=label.get('location',{});bb=loc.get('bbox',[])
        a,z=lr.get('start'),lr.get('end')
        if (loc.get('type')!='pdf' or loc.get('page')!=region.get('page') or len(bb)!=4
                or not isinstance(a,int) or not isinstance(z,int) or not 0<=a<z<=len(label.get('text',''))
                or native_region not in label.get('structure',{}).get('native_closed_form_regions',[])
                or not rb[0]-1<=(bb[0]+bb[2])/2<=rb[2]+1
                or not rb[1]-1<=(bb[1]+bb[3])/2<=rb[3]+1):return False
    control=block['controls'][0]
    return any(ref in option.get('answer_region_refs',[]) and
        option.get('refs')==control.get('caption_refs') and option.get('selected') is control.get('selected')
        for option in field.get('options',[]))


def verified_raster_tick_control(control: dict) -> bool:
    """Accept only the bounded two-stroke measurement emitted by native CV.

    This operates on original source-block controls, never model JSON. The
    image digest is retained for independent source audit; a selected boolean
    without its physical measurement is not evidence of a marked option.
    """
    evidence=control.get('mark_evidence') or {}
    shape=evidence.get('crop_shape',[])
    external=False
    if evidence.get('shape_variant')=='observed_near_border_junction':
        box=evidence.get('observed_box_shape',[]);joint=evidence.get('external_junction',[])
        external=bool(len(box)==len(joint)==len(shape)==2 and all(isinstance(v,int) and v>=10 for v in box)
            and box[1]==shape[1] and 0<shape[0]-box[0]<=max(2,round(box[0]*.22))
            and box[1]*.2<=joint[0]<box[1]*.5 and box[0]-1<=joint[1]<shape[0]
            and 1<=evidence.get('short_arm_slope',0)<12 and -3<=evidence.get('long_arm_slope',0)<-.5
            and .9<=evidence.get('ink_fit_fraction',0)<=1)
    internal=False
    if evidence.get('shape_variant')=='observed_internal_joint_fit':
        joint=evidence.get('observed_joint',[]);axes=evidence.get('observed_arm_intersection',[])
        internal=bool(len(shape)==len(joint)==len(axes)==2 and all(isinstance(v,int) and v>=12 for v in shape)
            and shape[1]*.2<=joint[0]<shape[1]*.6 and shape[0]*.6<=joint[1]<shape[0]-2
            and shape[1]*.15<axes[0]<shape[1]*.65 and shape[0]*.55<axes[1]<shape[0]-2
            and .75<=evidence.get('short_arm_slope',0)<12 and -4<=evidence.get('long_arm_slope',0)<-.5
            and .92<=evidence.get('ink_fit_fraction',0)<=1)
    right_exit=False
    if evidence.get('shape_variant')=='observed_right_exit_joint':
        box=evidence.get('observed_box_shape',[]);joint=evidence.get('observed_joint',[]);axes=evidence.get('observed_arm_intersection',[])
        right_exit=bool(len(box)==len(shape)==len(joint)==len(axes)==2
            and all(isinstance(v,int) and v>=12 for v in box)
            and shape[0]==box[0]+round(box[0]*.23) and shape[1]==box[1]+round(box[1]*.50)
            and box[1]*.2<=joint[0]<box[1]*.75 and box[0]*.5<=joint[1]<shape[0]-1
            and box[1]*.15<axes[0]<box[1]*.8 and box[0]*.45<axes[1]<shape[0]+1
            and abs(axes[0]-joint[0])<=3 and abs(axes[1]-joint[1])<=3
            and evidence.get('right_exit_pixels',0)>=max(5,box[0]*.25)
            and .75<=evidence.get('short_arm_slope',0)<12 and -4<=evidence.get('long_arm_slope',0)<-.5
            and .94<=evidence.get('ink_fit_fraction',0)<=1)
    return bool(control.get('source')=='raster_two_stroke_tick' and control.get('selected') is True
        and evidence.get('method')=='bounded_two_stroke_tick_v1'
        and evidence.get('two_stroke_shape_verified') is True
        and re.fullmatch(r'[0-9a-f]{64}',str(evidence.get('patch_sha256','')))
        and len(shape)==2 and all(isinstance(v,int) and v>=10 for v in shape)
        and (external or internal or right_exit or evidence.get('shape_variant')!='observed_right_exit_joint' and isinstance(evidence.get('short_arm_slope'),(float,int)) and .4<evidence['short_arm_slope']<3
        and isinstance(evidence.get('long_arm_slope'),(float,int)) and -2.5<evidence['long_arm_slope']<-.4)
        and isinstance(evidence.get('ink_fit_fraction'),(float,int)) and .86<=evidence['ink_fit_fraction']<=1)


def checked_matrix_region(block: dict) -> bool:
    """Only call for a verified physical choice-matrix response cell."""
    return (any(c.get("selected") is True for c in block.get("controls") or [])
            or bool(re.fullmatch(r"\s*[☑☒✓✔√]\s*", block.get("text", ""))))


def selected_column_caption(ref: dict, field: dict, lookup: dict) -> bool:
    """Verify header meaning AND its separate, checked physical response cell.

    This exception is intentionally not a generic permission to use any table
    header, neighboring Yes/No text, or model-selected option as an answer.
    """
    from .native_vector_forms import selected_vector_caption
    if selected_vector_caption(ref,field,lookup) or selected_scan_caption(ref,field,lookup):return True
    header = lookup.get(ref.get("block_id"), {})
    hs = header.get("structure") or {}
    start, end = ref.get("start"), ref.get("end")
    if not (hs.get("local_layout_type") == "choice_matrix_header"
            and isinstance(start, int) and isinstance(end, int)
            and 0 <= start < end <= len(header.get("text", ""))):
        return False
    labels = [lookup.get(r.get("block_id"), {}).get("structure", {})
              for r in field.get("label_refs") or field.get("question_refs") or []]
    for option in field.get("options") or []:
        if option.get("selected") is not True or ref not in option.get("refs", []):
            continue
        for target in option.get("answer_region_refs") or []:
            if target not in field.get("answer_region_refs", []):
                continue
            block = lookup.get(target.get("block_id"), {})
            bs = block.get("structure") or {}
            if not (bs.get("local_layout_type") == "choice_matrix"
                    and bs.get("table_id") == hs.get("table_id")
                    and bs.get("grid_col_start") == hs.get("grid_col_start")
                    and bs.get("choice_header_row") == hs.get("row_start")
                    and any(ls.get("table_id") == bs.get("table_id")
                            and ls.get("row_start") == bs.get("row_start") for ls in labels)):
                continue
            if checked_matrix_region(block):
                return True
    return False


def selected_scan_caption(ref: dict, field: dict, lookup: dict) -> bool:
    """Verify a local scan option caption against its physically marked box."""
    from .pdf_source_relations import native_overlay_value_in_region
    caption=lookup.get(ref.get('block_id'),{});loc=caption.get('location',{});text=caption.get('text','')
    start,end=ref.get('start'),ref.get('end');cb=loc.get('bbox',[])
    if loc.get('type')!='pdf' or not isinstance(start,int) or not isinstance(end,int) or not 0<=start<end<=len(text) or len(cb)!=4:return False
    for option in field.get('options',[]):
        if option.get('selected') is not True or ref not in option.get('refs',[]):continue
        for target in option.get('answer_region_refs',[]):
            region=lookup.get(target.get('block_id'),{});s=region.get('structure',{});rl=region.get('location',{});rb=rl.get('bbox',[])
            if s.get('scan_region_kind')=='text_box':
                from ..parser_core.pdf_matrix_marks import verified_matrix_cell_mark
                from ..source_matrix_choices import reviewed_matrix_state
                reviewed=s.get('matrix_choice_review_region') or {}
                if (target in field.get('answer_region_refs',[]) and reviewed_matrix_state(region,lookup) is True
                    and ref in reviewed.get('caption_refs',[]) and field.get('physical_label_region_ref')==reviewed.get('label_region_ref')):return True
                proof=s.get('matrix_mark_observation') or {}
                if (target in field.get('answer_region_refs',[]) and verified_matrix_cell_mark(region,lookup) and
                    ref in proof.get('caption_refs',[]) and field.get('physical_label_region_ref')==proof.get('label_region_ref')):return True
            if (target not in field.get('answer_region_refs',[]) or not s.get('scan_association_valid') or
                s.get('scan_region_kind')!='checkbox' or ref not in s.get('scan_option_caption_refs',[]) or
                rl.get('page')!=loc.get('page') or len(rb)!=4):continue
            # A header is aligned with the physical option column; a local
            # caption shares the option's baseline. Merely nearby text fails.
            a=cb[0]+(cb[2]-cb[0])*start/max(1,len(text));b=cb[0]+(cb[2]-cb[0])*end/max(1,len(text))
            column=rb[0]-3<=(a+b)/2<=rb[2]+3 and cb[3]<=rb[3]+3
            inline=abs((cb[1]+cb[3])/2-(rb[1]+rb[3])/2)<7 and min(abs(a-rb[2]),abs(rb[0]-b))<20
            band=s.get('scan_option_caption_band') or {};container=lookup.get(band.get('container_ref',{}).get('block_id'),{});container_box=container.get('location',{}).get('bbox',[])
            stacked=(band.get('method')=='closed_cell_stacked_checkbox_caption_band' and ref in band.get('caption_refs',[]) and
                container.get('structure',{}).get('scan_region_kind')=='text_box' and container.get('location',{}).get('page')==rl.get('page') and
                len(container_box)==4 and container_box[0]-2<=rb[0]<=container_box[0]+25 and
                container_box[0]-3<=cb[0] and cb[2]<=container_box[2]+3 and
                band.get('start_y',-999)<=((cb[1]+cb[3])/2)<band.get('end_y',-999) and
                abs(band.get('start_y',-999)-((rb[1]+rb[3])/2-8))<.01)
            caption_cell=lookup.get(s.get('scan_option_caption_cell_ref',{}).get('block_id'),{})
            cl=caption_cell.get('location',{});ccb=cl.get('bbox',[])
            from .pdf_source_relations import scan_label_inside
            closed_caption=(caption_cell.get('structure',{}).get('scan_region_kind')=='text_box' and
                cl.get('page')==rl.get('page') and len(ccb)==4 and -2<=ccb[0]-rb[2]<16 and
                ccb[1]-2<=(rb[1]+rb[3])/2<=ccb[3]+2 and scan_label_inside(cb,ccb) and
                all(scan_label_inside(lookup.get(r['block_id'],{}).get('location',{}).get('bbox',[]),ccb)
                    for r in option.get('refs',[])))
            from ..parser_core.pdf_caption_fragments import verified_caption_crop
            crop_caption=verified_caption_crop(region,ref,lookup)
            if not (column or inline or stacked or closed_caption or crop_caption):continue
            for control_index,c in enumerate(region.get('controls',[])):
                if c.get('selected') is not True:continue
                if c.get('source')=='reviewed_source_choice':
                    from ..source_choice_review import verified_reviewed_choice
                    if verified_reviewed_choice(region,control_index):return True
                if c.get('source')=='native_mark_in_scan_rectangle':
                    bid=c.get('value_block_id');value=lookup.get(bid,{})
                    if native_overlay_value_in_region({'block_id':bid,'start':0,'end':len(value.get('text',''))},[target],lookup):return True
                elif c.get('source')=='ocr_mark_in_scan_rectangle':
                    evidence=c.get('mark_evidence') or {}
                    if evidence.get('text','').strip() in {'√','✓','✔','☑','☒','X','x'} and evidence.get('confidence',0)>=.9:return True
                elif s.get('source_method')=='raster_geometry' and verified_raster_tick_control(c):return True
    return False
