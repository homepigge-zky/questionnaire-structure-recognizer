"""Audited human readings of individual, physically located scan options.

These records authorize a source transcription only. They neither approve the
field association nor grant knowledge reuse. Original extraction is immutable.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math

from .source_text_review import _digest, _sha, _lookup, _validate_evidence


VERSION = 'source-choice-review-v1-physical-control'


def _geometry(block):
    result={key: deepcopy(block.get(key)) for key in ('block_id','kind','text','location')} | {
        'physical_kind': block.get('structure',{}).get('scan_region_kind'),
        'physical_method': block.get('structure',{}).get('source_method')}
    if result['physical_kind']=='text_box':
        result['matrix_choice_review_region']=deepcopy(block.get('structure',{}).get('matrix_choice_review_region'))
    return result


def _control(block,index):
    loc=block.get('location') or {}; s=block.get('structure') or {}; bbox=loc.get('bbox')
    from .source_matrix_choices import valid_matrix_review_region
    physical_option=(s.get('scan_region_kind')=='checkbox' and s.get('source_method')=='raster_geometry') or valid_matrix_review_region(block)
    if (loc.get('type')!='pdf' or type(loc.get('page')) is not int or loc['page']<1
            or block.get('text')!=''
            or not physical_option or s.get('scan_candidate') is not True
            or not isinstance(bbox,list) or len(bbox)!=4
            or any(type(v) not in (int,float) or not math.isfinite(v) for v in bbox)
            or bbox[0]>=bbox[2] or bbox[1]>=bbox[3]):
        raise ValueError('A source review requires a real, empty-text raster checkbox region')
    controls=block.get('controls') or []
    if type(index) is not int or not 0<=index<len(controls):raise ValueError('Unknown source control')
    control=controls[index]
    if (control.get('type')!='checkbox' or control.get('bbox')!=bbox
            or control.get('selected') is not None and type(control.get('selected')) is not bool):
        raise ValueError('Control must retain its physical rectangle and three-state reading')
    return control


def create_source_choice_review(source_hash,source_blocks,edits,*,reviewer,evidence):
    _sha(source_hash,'Original source')
    if not isinstance(reviewer,str) or not reviewer.strip() or len(reviewer)>200:raise ValueError('Review actor required')
    pages=_validate_evidence(evidence); lookup=_lookup(source_blocks)
    if not isinstance(edits,list) or not 1<=len(edits)<=1000:raise ValueError('Review edits required')
    saved=[]; seen=set()
    for edit in edits:
        if not isinstance(edit,dict) or set(edit)!={'block_id','control_index','before','after'}:raise ValueError('Invalid control review')
        bid,index=edit['block_id'],edit['control_index']
        if not isinstance(bid,str) or bid not in lookup or type(index) is not int or (bid,index) in seen:raise ValueError('Unknown or repeated source control')
        seen.add((bid,index)); block=lookup[bid]; control=_control(block,index)
        if block.get('structure',{}).get('scan_region_kind')=='text_box':
            from .source_matrix_choices import valid_matrix_review_region
            proof=block['structure'].get('matrix_choice_review_region') or {}
            if proof.get('source_hash')!=source_hash or not valid_matrix_review_region(block,lookup):
                raise ValueError('Matrix choice review needs unchanged source, closed cell, question row and option column')
        if block['location']['page'] not in pages:raise ValueError('Control page has not been visually reviewed')
        if edit['before'] is not control.get('selected'):raise ValueError('The source selection state has changed')
        if type(edit['after']) is not bool or edit['after'] is edit['before']:raise ValueError('Review must explicitly resolve a changed boolean selection')
        saved.append(deepcopy(edit)|{'location':deepcopy(block['location']),
            'geometry_sha256':_digest(_geometry(block)), 'before_control_sha256':_digest(control)})
    record={'schema_version':VERSION,'source_hash':source_hash,'reviewer':reviewer,
            'created_at':datetime.now(timezone.utc).isoformat(),'evidence':deepcopy(evidence),'edits':saved,
            'scope':'reviewed_source_selection_only','knowledge_approval':False,'requires_structure_reanalysis':True}
    record['review_digest']=_digest(record)
    return record


def apply_source_choice_reviews(source_hash,source_blocks,reviews):
    _sha(source_hash,'Original source')
    if not isinstance(reviews,list):raise ValueError('Saved reviews must be a list')
    output=deepcopy(source_blocks); lookup=_lookup(output); digests=[]; edited=set()
    keys={'schema_version','source_hash','reviewer','created_at','evidence','edits','scope','knowledge_approval','requires_structure_reanalysis','review_digest'}
    edit_keys={'block_id','control_index','before','after','location','geometry_sha256','before_control_sha256'}
    for review in reviews:
        if (not isinstance(review,dict) or set(review)!=keys or review.get('schema_version')!=VERSION
                or review.get('source_hash')!=source_hash or review.get('scope')!='reviewed_source_selection_only'
                or review.get('knowledge_approval') is not False or review.get('requires_structure_reanalysis') is not True):
            raise ValueError('Unsupported review or different document')
        digest=_sha(review.get('review_digest'),'Review')
        if digest in digests or digest!=_digest({k:v for k,v in review.items() if k!='review_digest'}):raise ValueError('Changed or duplicate review')
        if not isinstance(review.get('edits'),list) or any(not isinstance(e,dict) or set(e)!=edit_keys for e in review['edits']):raise ValueError('Invalid saved edits')
        create_source_choice_review(source_hash,output,[{k:e[k] for k in ('block_id','control_index','before','after')} for e in review['edits']],reviewer=review['reviewer'],evidence=review['evidence'])
        for edit in review['edits']:
            block=lookup[edit['block_id']]; control=_control(block,edit['control_index'])
            if (edit['location']!=block['location'] or edit['geometry_sha256']!=_digest(_geometry(block))
                    or edit['before_control_sha256']!=_digest(control)):raise ValueError('Reviewed geometry or source control changed')
            original=deepcopy(control)
            control['selected']=edit['after']; control['source']='reviewed_source_choice'
            proof={'version':VERSION,'source_hash':source_hash,'block_id':block['block_id'],
                'control_index':edit['control_index'],'geometry_sha256':edit['geometry_sha256'],
                'before_control':original,'selected':edit['after'],'review_digest':digest,'reviewer':review['reviewer'],
                'source_image_sha256':next(p['image_sha256'] for p in review['evidence']['pages'] if p['page']==block['location']['page']),
                'knowledge_approval':False}
            proof['proof_digest']=_digest(proof); control['source_choice_review']=proof
            block['structure']['blank_state']='has_content' if edit['after'] else 'blank'
            block['structure']['content_state']='readable'; edited.add(block['block_id'])
        digests.append(digest)
    audit={'version':VERSION,'source_hash':source_hash,'review_digests':digests,
           'review_digest':_digest({'version':VERSION,'source_hash':source_hash,'reviews':digests}),
           'reviewed_block_ids':sorted(edited),'reviewed_blocks':len(edited),
           'original_extraction_unchanged':True,'requires_structure_reanalysis':bool(reviews),'knowledge_approval':False}
    return output,audit


def verified_reviewed_choice(block,index):
    """Validate a proof created by the authenticated source-review application.

    The server must not import source blocks/review records from model output.
    Geometry validation remains mandatory in the caller's field association.
    """
    try:
        control=_control(block,index); proof=control.get('source_choice_review') or {}
        if (control.get('source')!='reviewed_source_choice' or proof.get('version')!=VERSION
                or proof.get('block_id')!=block['block_id'] or proof.get('control_index')!=index
                or proof.get('geometry_sha256')!=_digest(_geometry(block))
                or proof.get('selected') is not control.get('selected') or type(proof.get('selected')) is not bool
                or proof.get('knowledge_approval') is not False or not proof.get('reviewer')):return False
        for key in ('source_hash','review_digest','source_image_sha256'):_sha(proof.get(key),key)
        if proof.get('proof_digest')!=_digest({k:v for k,v in proof.items() if k!='proof_digest'}):return False
        before=proof.get('before_control') or {}
        return before.get('type')=='checkbox' and before.get('bbox')==control.get('bbox')
    except (ValueError,KeyError,TypeError):return False
