"""Keep proved physical option glyphs from reappearing as answerable fields.

No word/character blacklist: removal needs a resolved source role, actual
checkbox geometry, and one valid peer owning every claimed response target.
Source blocks and human corrections are preserved.
"""
import math
from copy import deepcopy

VERSION='checkbox-symbol-ownership-v1-source-control-and-target-proof'
HUMAN={'confirmed','approved','reviewed','human_corrected','corrected'}

def _walk(fields):
    for f in fields:
        yield f
        yield from _walk(f.get('children') or [])

def _protected(f):
    return bool(f.get('manual_override') or f.get('human_mapping') or
        f.get('review_status') in HUMAN or f.get('mapping_review_status') in HUMAN or
        f.get('answer_origin') in {'manual','knowledge','reviewed_knowledge'})

def _key(r):return (r.get('block_id'),r.get('start'),r.get('end'),r.get('slot_id') or '')

def _bbox(b):
    v=b.get('location',{}).get('bbox')
    if not isinstance(v,(list,tuple)) or len(v)!=4:return None
    if not all(isinstance(x,(int,float)) and math.isfinite(x) for x in v):return None
    return v if v[0]<v[2] and v[1]<v[3] else None

def _over_control(glyph,control):
    a,b=_bbox(glyph),_bbox(control)
    if not a or not b or glyph.get('location',{}).get('page')!=control.get('location',{}).get('page'):return False
    overlap=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    return overlap>=.7*min((a[2]-a[0])*(a[3]-a[1]),(b[2]-b[0])*(b[3]-b[1]))

def _glyphs(field,lookup):
    refs=field.get('label_refs') or field.get('question_refs') or []
    found=[]
    for r in refs:
        b=lookup.get(r.get('block_id'),{});a,z=r.get('start'),r.get('end')
        if not (b.get('location',{}).get('type')=='pdf' and
                b.get('structure',{}).get('resolved_layout_role') in {'checkbox_symbol','invisible_native_mark'} and
                isinstance(a,int) and isinstance(z,int) and 0<=a<z<=len(b.get('text',''))):return []
        found.append(b)
    return found

def _owner_proof(field,owners,lookup):
    if field.get('children') or field.get('answer_status') not in {'blank','unresolved'} or any(
            field.get(k) for k in ('answer_text_original','original_answer','answer_value_refs','answer_refs','observed_answer_text')):return None
    glyphs=_glyphs(field,lookup)
    targets=field.get('answer_region_refs') or []
    if not glyphs or not targets or any(r.get('block_id') not in lookup for r in targets):return None
    target_keys=set(map(_key,targets));matches=[]
    for owner in owners:
        if owner is field or _glyphs(owner,lookup) or not owner.get('label_refs'):continue
        if owner.get('pairing_validation')!='valid' or owner.get('validation_status')!='valid':continue
        regions=owner.get('answer_region_refs') or [];keys=set(map(_key,regions))
        if not target_keys<=keys or len(keys)!=len(regions):continue
        options=owner.get('options') or []
        option_regions=[r for o in options for r in o.get('answer_region_refs',[])]
        if len(options)<2 or len(option_regions)!=len(regions) or set(map(_key,option_regions))!=keys:continue
        controls=[lookup.get(r.get('block_id'),{}) for r in regions]
        if any(c.get('location',{}).get('type')!='pdf' or not any(
                v.get('type')=='checkbox' for v in c.get('controls',[])) for c in controls):continue
        associations=[]
        for glyph in glyphs:
            containing=[c for c in controls if _over_control(glyph,c)]
            if len(containing)!=1:break
            associations.append({'glyph_block_id':glyph['block_id'],'control_block_id':containing[0]['block_id']})
        else:matches.append({'owner_field_id':owner['field_id'],'control_associations':associations,'shared_answer_region_refs':deepcopy(targets)})
    return matches[0] if len(matches)==1 else None

def reconcile_checkbox_symbol_ownership(fields,lookup):
    owners=[f for f in _walk(fields) if f.get('options')]
    report={'version':VERSION,'removed_fields':[]}
    def visit(items,inherited_protection=False):
        kept=[]
        for f in items:
            protected=inherited_protection or _protected(f)
            proof=None if protected else _owner_proof(f,owners,lookup)
            if proof:
                record={'field_id':f['field_id'],'label_refs':deepcopy(f.get('label_refs') or f.get('question_refs') or []),
                    'reason':'A proved option glyph cannot own a second field over the identical response controls of a validated field.',**proof}
                report['removed_fields'].append(record)
                for r in record['label_refs']:
                    diagnostics=lookup[r['block_id']].setdefault('structure',{}).setdefault('suppressed_derived_fields',[])
                    if not any(x.get('field_id')==f['field_id'] for x in diagnostics):diagnostics.append(deepcopy(record))
                continue
            if f.get('children') and not protected:visit(f['children'])
            kept.append(f)
        items[:]=kept
    visit(fields)
    return report
