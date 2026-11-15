"""Auditable unknown states in real ruled Yes/No/NA matrix cells.

This module does not classify ink as a tick or as empty. It only retains a
review control after both the physical cell and its matrix association have
been measured. Ordinary text boxes never acquire a choice permission.
"""
from copy import deepcopy
import math
import re

VERSION='source-matrix-choice-review-v1-measured-unknown'


def _box(block):
    b=block.get('location',{}).get('bbox')
    return b if (isinstance(b,list) and len(b)==4 and
        all(type(x) in (int,float) and math.isfinite(x) for x in b) and b[0]<b[2] and b[1]<b[3]) else None


def _closed_review_cell(block,lookup=None):
    """Native contours or source-measured recovery, never model rectangles."""
    from .validation.pdf_furniture_ownership import _closed
    if _closed(block):return True
    s=block.get('structure') or {};p=s.get('closed_region_evidence') or {};loc=block.get('location') or {}
    refs=p.get('neighbor_refs') or [];header=p.get('header_ref') or {}
    if (s.get('source_method')!='post_model_matrix_boundary' or s.get('scan_region_kind')!='text_box'
        or s.get('scan_candidate') is not True or block.get('text')!=''
        or p.get('method')!='observed_matrix_local_four_edges'
        or not re.fullmatch('[0-9a-f]{64}',str(p.get('source_sha256','')))
        or not re.fullmatch('[0-9a-f]{64}',str(p.get('cell_patch_sha256','')))
        or len(refs)!=2 or any(not isinstance(r,dict) or not r.get('block_id') for r in refs)
        or not header.get('block_id')
        or loc.get('matrix_source_edge_refs')!=[r['block_id'] for r in refs]+[header['block_id']]
        or len(set(loc['matrix_source_edge_refs']))!=3):return False
    # Reuse the native convex polygon/finite edge/bbox checks only after the
    # exact pixel-recovery evidence has been established above.
    proxy=deepcopy(block);proxy['structure']['source_method']='raster_geometry'
    proxy['structure']['closed_region_evidence']['method']='observed_four_sided_polygon_v1'
    if not _closed(proxy):return False
    if lookup is None:return True
    anchors=[lookup.get(r['block_id'],{}) for r in refs]+[lookup.get(header['block_id'],{})]
    # Recovery must terminate at actual native cells, not a chain of invented
    # or cyclic recovered rectangles. Its exact row association is checked by
    # the shared-edge traversal below.
    if any(not _closed(b) or b.get('location',{}).get('page')!=loc.get('page') for b in anchors):return False
    bb=_box(block);hb=_box(anchors[-1])
    return hb[3]<=bb[1] and bb[0]-3<=(hb[0]+hb[2])/2<=bb[2]+3


def valid_matrix_review_region(block,lookup=None):
    from .validation.pdf_furniture_ownership import _closed
    s=block.get('structure') or {};p=s.get('matrix_choice_review_region') or {};bb=_box(block)
    if (s.get('scan_region_kind')!='text_box'
        or s.get('scan_candidate') is not True or block.get('text')!='' or not _closed_review_cell(block,lookup)
        or p.get('version')!=VERSION or p.get('region_id')!=block.get('block_id')
        or p.get('region_bbox')!=bb or p.get('page')!=block.get('location',{}).get('page')
        or p.get('polygon')!=s['closed_region_evidence'].get('polygon')
        or not re.fullmatch('[0-9a-f]{64}',str(p.get('source_hash','')))
        or not re.fullmatch('[0-9a-f]{64}',str(p.get('cell_patch_sha256','')))
        or len(p.get('measured_edge_coverage',[]))!=4
        or any(type(v) not in (int,float) or not math.isfinite(v) or not .9<=v<=1 for v in p['measured_edge_coverage'])):
        return False
    if s.get('source_method')=='post_model_matrix_boundary':
        from .source_text_review import _digest
        boundary=s['closed_region_evidence']
        if (boundary.get('source_sha256')!=p.get('source_hash')
            or p.get('header_ref')!=boundary.get('header_ref')
            or p.get('derived_boundary_sha256')!=_digest(boundary)):return False
    if lookup is None:return True
    from .validation.pdf_source_relations import source_cells_share_row,scan_label_inside
    header=lookup.get(p.get('header_ref',{}).get('block_id'),{})
    label=lookup.get(p.get('label_region_ref',{}).get('block_id'),{})
    hb,lb=_box(header),_box(label);page=p['page']
    if not hb or not lb or any(b.get('location',{}).get('page')!=page for b in (header,label)):
        return False
    if not _closed(header) or not _closed(label) or not hb[3]<=bb[1] or not bb[0]-3<=(hb[0]+hb[2])/2<=bb[2]+3:
        return False
    refs=p.get('caption_refs',[])
    if len(refs)!=1:return False
    rr=refs[0];caption=lookup.get(rr.get('block_id'),{});a,z=rr.get('start'),rr.get('end');text=caption.get('text','')
    if (type(a) is not int or type(z) is not int or not 0<=a<z<=len(text)
        or not re.fullmatch(r'yes|no|n\s*/?\s*a',text[a:z],re.I)
        or caption.get('location',{}).get('page')!=page or not scan_label_inside(_box(caption) or [],hb)):
        return False
    frontier=[label];seen={label.get('block_id')}
    for _ in range(3):
        following=[b for b in lookup.values() if b.get('block_id') not in seen and
            b.get('location',{}).get('page')==page and _box(b) and _closed_review_cell(b,lookup) and
            any(_box(b)[0]>=_box(old)[2]-3 and source_cells_share_row(old,b) for old in frontier)]
        if block.get('block_id') in {b['block_id'] for b in following}:return True
        seen.update(b['block_id'] for b in following);frontier=following
    return False


def retain_unknown_matrix_control(original,region,option,observation,pixels,source_hash,lookup):
    """Keep unknown as unknown; review, not an ink threshold, resolves it."""
    if original.get('controls') or pixels is None:return False
    s=region.get('structure') or {}
    candidate=deepcopy(original)
    candidate.setdefault('structure',{})['matrix_choice_review_region']={
        'version':VERSION,'source_hash':source_hash,'region_id':region['block_id'],
        'region_bbox':deepcopy(region['location']['bbox']),'page':region['location']['page'],
        'polygon':deepcopy(s.get('closed_region_evidence',{}).get('polygon')),
        'header_ref':deepcopy(s.get('scan_column_header_ref')),
        'label_region_ref':deepcopy(s.get('scan_matrix_label_region_ref')),
        'caption_refs':deepcopy(option['refs']),
        'cell_patch_sha256':observation.get('cell_patch_sha256'),
        'measured_edge_coverage':deepcopy(pixels['edge_coverage'])}
    if s.get('source_method')=='post_model_matrix_boundary':
        from .source_text_review import _digest
        candidate['structure']['matrix_choice_review_region']['derived_boundary_sha256']=_digest(s.get('closed_region_evidence'))
    if not valid_matrix_review_region(candidate,lookup):return False
    original['structure']['matrix_choice_review_region']=candidate['structure']['matrix_choice_review_region']
    original['controls']=[{'type':'checkbox','source':'bounded_matrix_choice_unknown','selected':None,
        'bbox':deepcopy(region['location']['bbox'])}]
    return True


def reviewed_matrix_state(region,lookup):
    from .source_choice_review import verified_reviewed_choice
    if not valid_matrix_review_region(region,lookup):return None
    for i,c in enumerate(region.get('controls') or []):
        if verified_reviewed_choice(region,i):return c['selected']
    return None
