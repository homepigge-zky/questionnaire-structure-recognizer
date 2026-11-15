"""Response subregions bounded by a source rectangle and its internal rule."""
from copy import deepcopy

from ..parser_core.models import make_block
from .field_boundaries import ref
from .form_completion import new_field
from .pdf_source_relations import scan_label_inside, source_text_inside_cell, validate_ocr_response_values

VERSION='scan-ruled-partitions-v1-observed-four-edges-and-divider'


def _box(b):return b.get('location',{}).get('bbox',[])


def _closed(b):
    s=b.get('structure',{});e=s.get('closed_region_evidence')or{}
    bb=_box(b);poly=e.get('polygon',[])
    if len(bb)!=4 or len(poly)!=4:return False
    corners=[[bb[0],bb[1]],[bb[2],bb[1]],[bb[2],bb[3]],[bb[0],bb[3]]]
    axis_aligned=all(any(abs(p[0]-c[0])<=3 and abs(p[1]-c[1])<=3 for p in poly)for c in corners)
    return axis_aligned and s.get('scan_region_kind')=='text_box'and s.get('source_method')=='raster_geometry'and e.get('method')=='observed_four_sided_polygon_v1'and len(e.get('edge_ink_coverage',[]))==4 and min(e['edge_ink_coverage'])>=.9


def _plan(outer,blocks):
    if not _closed(outer):return None
    bb=_box(outer);page=outer['location']['page']
    if not 30<bb[2]-bb[0]<220 or not 35<bb[3]-bb[1]<180:return None
    rules=[b for b in blocks if b.get('location',{}).get('page')==page and b.get('structure',{}).get('scan_region_kind')=='write_line'and b['structure'].get('source_method')=='raster_geometry'and b['structure'].get('line_style')=='solid'and _box(b)[0]<=bb[0]+1 and _box(b)[2]>=bb[2]-1 and bb[1]+12<_box(b)[3]<bb[3]-12]
    if len(rules)!=1:return None
    rule=rules[0];y=_box(rule)[3]
    # A neighboring independently closed label cell establishes that this
    # observed line is a row divider, not an answer underline inside the box.
    lefts=[b for b in blocks if b is not outer and _closed(b)and b['location']['page']==page and abs(_box(b)[2]-bb[0])<4 and abs(_box(b)[1]-bb[1])<4 and abs(_box(b)[3]-y)<4]
    if len(lefts)!=1:return None
    left=lefts[0];lb=_box(left)
    if _box(rule)[0]>lb[0]+3:return None
    texts=[b for b in blocks if b.get('location',{}).get('page')==page and b.get('location',{}).get('extraction')=='ocr'and b.get('text','').strip()]
    result=[]
    for top,bottom in [(bb[1],y),(y,bb[3])]:
        lane=[lb[0],top,bb[0],bottom];area=[bb[0],top,bb[2],bottom]
        labels=sorted([b for b in texts if scan_label_inside(_box(b),lane)],key=lambda b:(_box(b)[1],_box(b)[0]))
        values=sorted([b for b in texts if scan_label_inside(_box(b),area)and source_text_inside_cell(b,outer)],key=lambda b:(_box(b)[1],_box(b)[0]))
        if not labels:return None
        # No OCR run may straddle the divider; a broken answer remains one
        # unresolved source region instead of being split or duplicated.
        if any(_box(b)[0]>=bb[0]-3 and _box(b)[2]<=bb[2]+3 and _box(b)[1]<y-2 and _box(b)[3]>y+2 for b in texts):return None
        if any(b.get('controls')and b.get('location',{}).get('page')==page and scan_label_inside(_box(b),area)for b in blocks):return None
        result.append((labels,values,area))
    return left,rule,result


def restore_scanned_ruled_partitions(fields, blocks, lookup):
    from .scanned_cells import _replace_owned
    def walk(items,inherited=False):
        for f in items:
            protected=inherited or f.get('manual_override')or f.get('answer_origin')in{'manual','knowledge','reviewed_knowledge'}
            yield f,protected
            yield from walk(f.get('children',[]),protected)
    def retire(items,owned):
        for f in items:retire(f.get('children',[]),owned)
        _replace_owned(items,owned,lookup)
    for outer in list(blocks):
        plan=_plan(outer,blocks)
        if not plan:continue
        left,rule,rows=plan
        owned={b['block_id']for labels,values,_ in rows for b in labels+values}
        if any(protected and any(r['block_id']in owned|{outer['block_id']}for r in f.get('label_refs',[])+f.get('answer_region_refs',[]))for f,protected in walk(fields)):continue
        recovered=[]
        for index,(labels,values,area)in enumerate(rows):
            s={'scan_region_kind':'text_box','source_method':'raster_geometry','scan_candidate':True,
               'blank_state':'unresolved','content_state':'text'if values else 'unreadable',
               'write_requires_association':True,'scan_partition_evidence':{'version':VERSION,
                   'parent_ref':ref(outer),'divider_ref':ref(rule),'label_anchor_ref':ref(left),'row_index':index,
                   'boundary_method':'source_four_sided_parent_and_full_width_observed_rule'}}
            # Preserve measured source edges as references. No synthetic ink
            # confidence or pretend independently observed polygon is created.
            region=make_block('paragraph','',outer['order'],{**deepcopy(outer['location']),'bbox':area},structure=s).model_dump()
            if region['block_id']in lookup:region=lookup[region['block_id']]
            else:blocks.append(region);lookup[region['block_id']]=region
            f=new_field(labels[0],ref(labels[0]),[ref(region)]);fid='scan-ruled:'+region['block_id']
            f.update(field_id=fid,question_id=fid,label_refs=[ref(b)for b in labels],question_refs=[ref(b)for b in labels],
                question_text_original='\n'.join(b['text']for b in labels),answer_region_refs=[ref(region)],
                answer_refs=[],answer_value_refs=[],answer_text_original='',answer_status='unresolved',
                boundary_evidence='observed_ruled_response_partition',boundary_version=VERSION,
                review_status='needs_review',review_reasons=['填写位置已按原框与贯穿分隔线定位；扫描内容需对照原页核对。'])
            refs=[ref(b)for b in values]
            if refs:
                f.update(answer_value_refs=deepcopy(refs),answer_refs=deepcopy(refs),answer_text_original='\n'.join(b['text']for b in values),answer_status='filled')
                if not validate_ocr_response_values(f,lookup,[f]):
                    f.update(observed_answer_text=f['answer_text_original'],unvalidated_answer_refs=deepcopy(refs),answer_text_original='',answer_value_refs=[],answer_refs=[],answer_status='unresolved')
                else:
                    from .reviewed_transcription import resolve_transcription_reason
                    resolve_transcription_reason(f,lookup,{'填写位置已按原框与贯穿分隔线定位；扫描内容需对照原页核对。'})
            recovered.append(f)
        retire(fields,owned)
        for f in recovered:
            for r in f['label_refs']:lookup[r['block_id']].setdefault('structure',{})['protected_label_ranges']=[{'start':r['start'],'end':r['end']}]
            for r in f.get('answer_value_refs',[])+f.get('unvalidated_answer_refs',[]):
                lookup[r['block_id']].setdefault('structure',{}).pop('protected_label_ranges',None)
        fields.extend(recovered)
        outer.setdefault('structure',{})['observed_response_partition']={'version':VERSION,'divider_ref':ref(rule),'child_refs':[deepcopy(f['answer_region_refs'][0])for f in recovered]}
    return fields
