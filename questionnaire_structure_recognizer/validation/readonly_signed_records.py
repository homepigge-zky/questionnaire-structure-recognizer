"""Retain existing electronic signature records as source-only table cells.

The explicit signing title and physically proved populated record table must
both exist. This does not create a signing field, answer, or writable target.
"""
from copy import deepcopy
import re

from .field_boundaries import ref
from .scan_matrix_structure import box, contents, page

VERSION='readonly-signed-record-v1-physical-source-cells'


def _human_role_overlap(fields,owned):
    for field in fields:
        protected=(field.get('review_status') in {'confirmed','reviewed','approved','human_corrected'} or
                   field.get('human_corrected') is True or
                   (field.get('form_definition') or {}).get('mapping_review_status')=='human_corrected')
        if protected and any(r.get('block_id') in owned for key in
            ('label_refs','question_refs','answer_region_refs','answer_value_refs') for r in field.get(key,[])):
            return True
        if _human_role_overlap(field.get('children',[]),owned):return True
    return False


def retain_signed_source_records(fields,blocks,lookup,source_hash):
    if not re.fullmatch(r'[0-9a-f]{64}',str(source_hash)):return []
    from .scanned_cells import _owned_by_reviewed, _replace_owned
    texts=[b for b in blocks if b.get('text','').strip() and
           len(b.get('location',{}).get('bbox',[]))==4]
    seen=set();nonqa=[]
    for header in blocks:
        proof=header.get('structure',{}).get('scan_header_evidence') or {}
        if proof.get('type')!='occupied_record_header_with_two_aligned_rows':continue
        hrefs=proof.get('header_region_refs',[]);body=proof.get('body_region_refs',[])
        signature=tuple(r.get('block_id') for r in hrefs)
        if signature in seen:continue
        seen.add(signature)
        if len(hrefs)<3 or len(body)!=len(hrefs) or len({len(column) for column in body})!=1 or len(body[0])<2:continue
        ids={r.get('block_id') for r in hrefs}|{r.get('block_id') for column in body for r in column}
        if any(bid not in lookup for bid in ids):continue
        headers=[lookup[r['block_id']] for r in hrefs]
        if headers!=sorted(headers,key=lambda c:box(c)[0]):continue
        grid=[headers]+[[lookup[column[i]['block_id']] for column in body] for i in range(len(body[0]))]
        if len({page(c) for row in grid for c in row})!=1:continue
        top=min(box(c)[1] for c in headers);left=min(box(c)[0] for c in headers);right=max(box(c)[2] for c in headers)
        titles=[b for b in texts if page(b)==page(header) and
                re.fullmatch(r'(?:electronic|digital)\s+signatures?|电子签(?:名|署)(?:记录)?',b['text'].strip(),re.I) and
                0<=top-box(b)[3]<80 and left<=(box(b)[0]+box(b)[2])/2<=right]
        if len(titles)!=1:continue
        title=titles[0]
        if any(page(b)==page(header) and box(title)[3]<box(b)[1] and box(b)[3]<top-3 and
               left<=(box(b)[0]+box(b)[2])/2<=right for b in texts):continue
        cell_texts=[[contents(c,texts) for c in row] for row in grid]
        if any(not local for row in cell_texts for local in row):continue
        owned=[b for row in cell_texts for local in row for b in local]
        if len({b['block_id'] for b in owned})!=len(owned):continue
        owned_ids={b['block_id'] for b in owned}|ids|{title['block_id']}
        if _owned_by_reviewed(fields,owned_ids) or _human_role_overlap(fields,owned_ids):continue
        table_id='readonly-record:'+headers[0]['block_id']
        _replace_owned(fields,{b['block_id'] for b in owned}|{title['block_id']},lookup)
        title.setdefault('structure',{}).update(resolved_layout_role='signed_declaration_prose',
            readonly_record_caption_for=table_id,readonly_record_version=VERSION)
        nonqa.append({'role':'signed_declaration_prose','refs':[ref(title)],'text_original':title['text'],
                      'reason':'Existing electronic signing record; original source only'})
        for row_index,(cells,textcells) in enumerate(zip(grid,cell_texts)):
            for column_index,(cell,local) in enumerate(zip(cells,textcells)):
                record={'table_id':table_id,'row_index':row_index,'column_index':column_index,
                    'row_span':1,'column_span':1,'row_count':len(grid),'column_count':len(headers),
                    'cell_ref':ref(cell),'caption_refs':[ref(title)],'header_refs':deepcopy(hrefs),
                    'source_hash':source_hash,'version':VERSION,'read_only':True}
                cell.setdefault('structure',{}).update(readonly_record=deepcopy(record),
                    label_only_region=True,readonly_record_version=VERSION)
                for b in local:
                    b.setdefault('structure',{}).update(resolved_layout_role='readonly_record_cell',
                        readonly_record=deepcopy(record),readonly_record_version=VERSION)
                    nonqa.append({'role':'readonly_record_cell','refs':[ref(b)],'text_original':b['text'],
                        'reason':'Existing signed table cell retained as read-only source',
                        'readonly_record':deepcopy(record)})
    return nonqa
