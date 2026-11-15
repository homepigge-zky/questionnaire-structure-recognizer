"""Physical matrix columns delimit local groups before occupied-cell pairing.

Column captions classify a region only with observed lower row cells. This
never chooses a value, removes source blocks, or infers a missing rectangle.
"""
from copy import deepcopy
import re

from .field_boundaries import ref
from .pdf_source_relations import source_cells_share_row

VERSION = 'scan-matrix-structure-v3-partial-source-response-lanes'


def box(b): return b['location']['bbox']
def page(b): return b['location']['page']


def contents(cell, texts):
    from .scanned_cells import _label_inside
    return sorted([b for b in texts if page(b)==page(cell) and _label_inside(box(b),box(cell))],
                  key=lambda b:(box(b)[1],box(b)[0]))


def matrix_groups(cells, texts):
    captions=[]
    for c in cells:
        local=contents(c,texts)
        if len(local)==1 and re.fullmatch(r'yes|no|n\s*/?\s*a',local[0]['text'].strip(),re.I):
            captions.append((c,local[0]))
    groups=[]
    for first in captions:
        if first[1]['text'].strip().casefold()!='yes': continue
        chain=[first]
        for expected in ('no','na'):
            nexts=[p for p in captions if re.sub(r'\s|/','',p[1]['text']).casefold()==expected
                   and source_cells_share_row(chain[-1][0],p[0])]
            if len(nexts)!=1: break
            chain.append(nexts[0])
        if len(chain)>=2:groups.append(chain)
    return groups


def _aligned_rows(header,cells):
    hb=box(header);rows=[];end=hb[3]
    # Follow observed shared horizontal borders, not every similarly aligned
    # region farther down the page. A later header begins a different group.
    candidates=sorted([c for c in cells if c is not header and page(c)==page(header)
        and abs(box(c)[0]-hb[0])<4 and abs(box(c)[2]-hb[2])<4
        and box(c)[1]>=hb[3]-8],key=lambda c:box(c)[1])
    for c in candidates:
        if abs(box(c)[1]-end)>8:break
        if rows and box(c)[3]<=end+2:continue
        rows.append(c);end=box(c)[3]
    return rows


def _label_body_proves_rows(header,cells,texts,response_rows):
    rows=_aligned_rows(header,cells)
    if len(rows)>=2:return True
    if len(rows)!=1 or len(response_rows)<2:return False
    # A single observed label contour can span two independently bordered
    # response rows. Each original caption must still align to its own row;
    # this proves the heading role, not extra answer cells or selections.
    body=rows[0];bb=box(body);first,second=response_rows[:2]
    if abs(bb[1]-box(first)[1])>8 or abs(bb[3]-box(second)[3])>8:return False
    local=contents(body,texts)
    if len(local)<2:return False
    owners=[]
    for text in local:
        y=(box(text)[1]+box(text)[3])/2
        matches=[i for i,row in enumerate((first,second)) if box(row)[1]-1<=y<=box(row)[3]+1]
        if len(matches)!=1:return False
        owners.append(matches[0])
    return set(owners)=={0,1}


def classify_scan_headers(fields,blocks,lookup):
    from .scanned_cells import _owned_by_reviewed,_replace_owned,_same_row
    from .scanned_cells import _leaf_closed_cells
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box'
           and len(b.get('location',{}).get('bbox',[]))==4]
    cells=_leaf_closed_cells(cells)
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr'
           and len(b['location'].get('bbox',[]))==4]
    proofs=[];classified=set()
    def classify(candidates,evidence):
        ids={b['block_id'] for c in candidates for b in contents(c,texts)}
        if not ids or _owned_by_reviewed(fields,ids|{c['block_id'] for c in candidates}):return
        _replace_owned(fields,ids,lookup)
        for c in candidates:
            c.setdefault('structure',{})['scan_header_evidence']=deepcopy(evidence)
            c['structure']['label_only_region']=True
            for b in contents(c,texts):
                role='matrix_column_header' if evidence['type'].startswith(('matrix_','numbered_')) else 'record_column_header'
                b.setdefault('structure',{}).update(resolved_layout_role=role,scan_header_evidence=deepcopy(evidence))
        classified.update(ids);proofs.append(evidence)
    for group in matrix_groups(cells,texts):
        first=group[0][0];header_bottom=max(box(c)[3] for c,t in group)
        rowsets=[_aligned_rows(c,cells) for c,t in group]
        if min(map(len,rowsets),default=0)<1 or max(map(len,rowsets),default=0)<2:continue
        # A missing response contour does not turn its printed column header
        # into an answer. Prove the header from actual adjacent lower lanes;
        # do not create the missing response or declare its selection.
        reference_rows=max(rowsets,key=len)
        # The adjacent label column can span the observation-group heading
        # and Yes/No captions; its lower border must meet the first body row.
        labels=[c for c in cells if page(c)==page(first) and abs(box(c)[2]-box(first)[0])<4
                and abs(box(c)[3]-header_bottom)<8 and 8<box(c)[3]-box(c)[1]<60
                and contents(c,texts) and _label_body_proves_rows(c,cells,texts,reference_rows)]
        # Two raster contours can surround the same printed heading. They
        # are equivalent evidence only when their original text IDs agree.
        if len(labels)>1 and len({tuple(b['block_id'] for b in contents(c,texts)) for c in labels})==1:
            labels=[min(labels,key=lambda c:(box(c)[2]-box(c)[0])*(box(c)[3]-box(c)[1]))]
        if len(labels)!=1:continue
        header_cells=[labels[0],*[c for c,t in group]]
        # Numbering column is accepted by its actual numeric body rows, not
        # the spelling of S.No., No., etc.
        number_headers=[c for c in cells if page(c)==page(first) and source_cells_share_row(c,labels[0])
                        and contents(c,texts)]
        for c in number_headers:
            body=_aligned_rows(c,cells)
            if len(body)>=2 and all(len(v:=contents(r,texts))==1 and re.fullmatch(r'\d+(?:\.\d+)*[.)]?',v[0]['text'].strip()) for r in body[:2]):
                header_cells.append(c)
        classify(header_cells,{'version':VERSION,'type':'matrix_header_above_repeated_physical_rows',
            'header_region_refs':[ref(c) for c in header_cells],
            'body_region_refs':[[ref(c) for c in rows[:2]] for rows in rowsets]})
    # Matrix captions may themselves be unreadable (e.g. only the last glyph
    # of Yes survived). Two actual numbered rows beneath the narrow ordinal
    # column still prove the header band, without inventing that option text.
    for left in cells:
        lb=box(left)
        labels=contents(left,texts)
        if lb[2]-lb[0]>65 or not labels or all(re.fullmatch(r'\d+(?:\.\d+)*[.)]?',t['text'].strip()) for t in labels):continue
        right=[c for c in cells if source_cells_share_row(left,c) and box(c)[2]-box(c)[0]>150]
        if len(right)!=1 or not contents(right[0],texts):continue
        near=sorted([c for c in cells if c is not left and page(c)==page(left)
            and abs(box(c)[0]-lb[0])<4 and abs(box(c)[2]-lb[2])<4
            and -4<=box(c)[1]-lb[3]<160],key=lambda c:box(c)[1])
        body=[]
        for c in near:
            ts=contents(c,texts)
            if len(ts)!=1 or not re.fullmatch(r'\d+(?:\.\d+)*[.)]?',ts[0]['text'].strip()):continue
            peers=[p for p in cells if source_cells_share_row(c,p)
                   and abs(box(p)[0]-box(right[0])[0])<4 and abs(box(p)[2]-box(right[0])[2])<4
                   and contents(p,texts)]
            if len(peers)==1:body.append((c,peers[0]))
            if len(body)==2:break
        if len(body)!=2 or box(body[0][0])[1]-lb[3]>120:continue
        # The first numbered row may be separated by one full-width section
        # band; any gap must be explained by an observed enclosing rectangle.
        if box(body[0][0])[1]-lb[3]>8 and not any(page(c)==page(left)
            and box(c)[0]<=lb[0]+4 and box(c)[2]>=box(right[0])[2]-4
            and abs(box(c)[1]-lb[3])<12 and abs(box(c)[3]-box(body[0][0])[1])<12 for c in cells) and not all(p in _aligned_rows(right[0],cells) for c,p in body):continue
        classify([left,right[0]],{'version':VERSION,'type':'numbered_matrix_header_with_source_rows',
            'header_region_refs':[ref(left),ref(right[0])],
            'body_region_refs':[[ref(c),ref(p)] for c,p in body]})
    # A complete three-or-more-column record table: centered short captions,
    # two taller occupied rows and congruent physical columns. Do not pair
    # adjacent headings, irrespective of caption wording or column widths.
    for first in cells:
        if first.get('structure',{}).get('scan_header_evidence'):continue
        chain=[first]
        for _ in range(12):
            nxt=[c for c in cells if c not in chain and source_cells_share_row(chain[-1],c)]
            if len(nxt)!=1:break
            chain.append(nxt[0])
        if len(chain)<3 or any(box(c)[3]-box(c)[1]>40 for c in chain):continue
        locals_=[contents(c,texts) for c in chain]
        if any(not v or len(' '.join(t['text'] for t in v))>80 for v in locals_):continue
        if any(abs((min(box(t)[0] for t in v)+max(box(t)[2] for t in v)-box(c)[0]-box(c)[2])/2)>max(8,(box(c)[2]-box(c)[0])*.12) for c,v in zip(chain,locals_)):continue
        rows=[_aligned_rows(c,cells) for c in chain]
        if any(len(v)<2 for v in rows):continue
        if any(box(r)[3]-box(r)[1] < (box(c)[3]-box(c)[1])*1.3 or not contents(r,texts)
               for c,rs in zip(chain,rows) for r in rs[:2]):continue
        if any(not all(_same_row(box(rs[i]),box(rows[0][i])) for rs in rows) for i in range(2)):continue
        classify(chain,{'version':VERSION,'type':'occupied_record_header_with_two_aligned_rows',
            'header_region_refs':[ref(c) for c in chain],
            'body_region_refs':[[ref(c) for c in rows_] for rows_ in rows]})
    return proofs


def is_physical_checkbox_text(block,marks):
    """An OCR glyph over a real control is an observation, not a field label."""
    from .scanned_cells import _label_inside
    if not re.fullmatch(r'[\s□☐☑☒口O0]+',block.get('text','')):return False
    return any(page(m)==page(block) and _label_inside(box(m),box(block)) for m in marks)
