"""Numbered section bars and PART captions around an actual choice matrix.

Require full-width physical rules, numbered child rows and shared source
column headers. Words such as General Information alone carry no authority.
"""
from copy import deepcopy
import re

from .field_boundaries import ref

VERSION = 'scan-matrix-sections-v1-ruled-numbered-source'


def _box(block): return block.get('location', {}).get('bbox', [])
def _page(block): return block.get('location', {}).get('page')
def _flat(items):
    for field in items:
        yield field
        yield from _flat(field.get('children', []))


def _protected(field):
    return (field.get('manual_override') or field.get('review_status') in {'human_corrected','confirmed','approved'} or
            field.get('answer_origin') in {'manual','knowledge','reviewed_knowledge'})


def resolve_matrix_section_bands(fields, blocks, lookup):
    text = [b for b in blocks if b.get('location', {}).get('type') == 'pdf' and b.get('text','').strip() and len(_box(b)) == 4]
    rules = [b for b in blocks if b.get('structure',{}).get('scan_region_kind') == 'write_line' and len(_box(b)) == 4]
    all_fields = list(_flat(fields)); headings = []
    protected_refs=set()
    def collect_protected(items, guarded=False):
        for field in items:
            protected=guarded or _protected(field)
            if protected:
                protected_refs.update(r['block_id'] for key in ('label_refs','answer_region_refs','answer_value_refs') for r in field.get(key,[]))
            collect_protected(field.get('children',[]),protected)
    collect_protected(fields)

    def eligible(heading, inline=False):
        bid=heading['block_id']
        if bid in protected_refs:return False
        owners=[f for f in all_fields if any(r['block_id']==bid for r in f.get('label_refs',[]))]
        for f in owners:
            if _protected(f) or f.get('children') or any(r['block_id']!=bid for r in f.get('label_refs',[])):
                return False
            answer_refs=f.get('answer_region_refs',[])+f.get('answer_value_refs',[])
            if answer_refs and not (inline and f.get('boundary_evidence')=='ocr_inline_delimiter_with_row_borders' and
                                    all(r['block_id']==bid for r in answer_refs)):
                return False
        return True

    for heading in text:
        hb=_box(heading); match=re.fullmatch(r'\s*(\d+)[.)]\s+([^?？:：\n]{2,90})\s*',heading['text'])
        if not match or not eligible(heading):continue
        local=[b for b in text if _page(b)==_page(heading)]
        numbered=[b for b in local if re.fullmatch(re.escape(match[1])+r'\.\d+[.)]?',b['text'].strip()) and
                  0<_box(b)[1]-hb[3]<115 and _box(b)[0]<hb[0]+35]
        if len({b['text'] for b in numbered})<2:continue
        nearby=[f for f in all_fields if f.get('boundary_evidence')=='closed_scan_column_choice_row' and f.get('label_refs') and
                (lb:=lookup.get(f['label_refs'][0]['block_id'])) and _page(lb)==_page(heading) and 0<_box(lb)[1]-hb[3]<115]
        if len(nearby)<2:continue
        shared=None
        for f in nearby:
            refs={(r['block_id'],r['start'],r['end']) for o in f.get('options',[]) for r in o.get('refs',[])}
            shared=refs if shared is None else shared&refs
        if not shared or len(shared)<2:continue
        headers=[lookup[k[0]] for k in shared]
        if any(_page(b)!=_page(heading) or not 0<hb[1]-_box(b)[3]<35 for b in headers):continue
        xright=max(_box(lookup[r['block_id']])[2] for f in nearby for r in f.get('answer_region_refs',[]))
        spanning=[b for b in rules if _page(b)==_page(heading) and _box(b)[0]<=hb[0]+3 and _box(b)[2]>=xright-4]
        tops=[b for b in spanning if -2<=hb[1]-_box(b)[3]<8]
        bottoms=[b for b in spanning if -2<=_box(b)[3]-hb[3]<8]
        if not tops or not bottoms:continue
        top=min(tops,key=lambda b:abs(hb[1]-_box(b)[3]));bottom=min(bottoms,key=lambda b:abs(_box(b)[3]-hb[3]))
        if top is bottom or not 7<_box(bottom)[3]-_box(top)[3]<30:continue
        # A genuine answer contour alongside the caption contradicts a
        # merged section band, even when its wording resembles a heading.
        side=[b for b in blocks if _page(b)==_page(heading) and b.get('structure',{}).get('scan_region_kind')=='text_box' and
              len(_box(b))==4 and _box(b)[0]>hb[2] and abs((_box(b)[1]+_box(b)[3]-hb[1]-hb[3])/2)<5 and
              _box(b)[1]>=_box(top)[3]-2 and _box(b)[3]<=_box(bottom)[3]+2]
        if side:continue
        proof={'version':VERSION,'section_ref':ref(heading),'row_rule_refs':[ref(top),ref(bottom)],
               'child_number_refs':[ref(b) for b in numbered],'matrix_header_refs':[{'block_id':b,'start':s,'end':e} for b,s,e in sorted(shared)]}
        headings.append((heading,proof))

    parts=[]
    for heading,proof in headings:
        hb=_box(heading); headers=[lookup[r['block_id']] for r in proof['matrix_header_refs']]
        header_top=min(_box(b)[1] for b in headers)
        candidates=[b for b in text if _page(b)==_page(heading) and
                    (m:=re.fullmatch(r'\s*PART\s*[-–]?\s*[A-Z0-9]+\s*[:：]\s*(\S.{2,90})',b['text'],re.I)) and
                    m[1].isupper() and '?' not in m[1] and '？' not in m[1] and
                    0<header_top-_box(b)[3]<40 and _box(b)[0]<hb[0]+30 and eligible(b,inline=True)]
        if len(candidates)==1:
            part=candidates[0]
            parts.append((part,{'version':VERSION,'section_ref':ref(part),'child_section_ref':ref(heading),
                                'matrix_header_refs':deepcopy(proof['matrix_header_refs'])}))

    proven={b['block_id']:(b,p) for b,p in parts+headings}
    if not proven:return []
    removed=[]
    def visit(items, guarded=False):
        kept=[]
        for f in items:
            protected=guarded or _protected(f)
            refs=f.get('label_refs',[])
            if not protected and refs and all(r['block_id'] in proven for r in refs) and not f.get('children'):
                removed.append(f['field_id']);continue
            if not protected:visit(f.get('children',[]))
            kept.append(f)
        items[:]=kept
    visit(fields)
    for block,proof in proven.values():
        block.setdefault('structure',{}).update(resolved_layout_role='section_header',matrix_section_evidence=deepcopy(proof))
        proof['removed_field_ids']=[f['field_id'] for f in all_fields if f['field_id'] in removed and
                                    any(r['block_id']==block['block_id'] for r in f.get('label_refs',[]))]
    for field in _flat(fields):
        if _protected(field) or not field.get('label_refs'):continue
        label=lookup.get(field['label_refs'][0]['block_id']);lb=_box(label) if label else []
        if not lb:continue
        prior=[b for b,p in headings if _page(b)==_page(label) and _box(b)[3]<_box(label)[1]]
        if not prior:continue
        section=max(prior,key=lambda b:_box(b)[3]);parts_here=[b for b,p in parts if p['child_section_ref']['block_id']==section['block_id']]
        path=[[b['block_id']] for b in parts_here]+[[section['block_id']]]
        for rr in field['label_refs']:lookup[rr['block_id']].setdefault('structure',{})['section_path_block_ids']=deepcopy(path)
    return [p for b,p in proven.values()]
