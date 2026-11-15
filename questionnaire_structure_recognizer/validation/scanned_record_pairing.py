"""Verify physical record-column association without guessing record identity."""
from .pdf_source_relations import source_text_inside_cell

RECORD_REVIEW_REASON='已按表头和连续实体单元格恢复记录列；记录对象及扫描文字需审核，不能跨记录自动复用。'


def _closed(block):
    s=block.get('structure',{});proof=s.get('closed_region_evidence') or {};bb=block.get('location',{}).get('bbox',[]);poly=proof.get('polygon',[]);edges=proof.get('edge_ink_coverage',[])
    if (s.get('source_method')!='raster_geometry' or s.get('scan_region_kind')!='text_box'
            or proof.get('method')!='observed_four_sided_polygon_v1' or len(poly)!=4 or len(edges)!=4
            or min(edges)<.85 or len(bb)!=4 or bb[0]>=bb[2] or bb[1]>=bb[3]):return False
    corners=[[bb[0],bb[1]],[bb[2],bb[1]],[bb[2],bb[3]],[bb[0],bb[3]]]
    return all(any(abs(p[0]-c[0])<4 and abs(p[1]-c[1])<4 for p in poly)for c in corners)


def verified_scan_record_pairing(field,lookup):
    if field.get('boundary_evidence')!='closed_record_column':return False
    labels=field.get('label_refs') or [];values=field.get('answer_value_refs') or [];regions=field.get('answer_region_refs') or []
    if not labels or not values or len(regions)!=1:return False
    cell=lookup.get(regions[0].get('block_id'),{});head=lookup.get(field.get('record_group_id'),{})
    if not _closed(cell) or not _closed(head) or field.get('record_instance_id')!=cell.get('block_id'):return False
    page=head['location'].get('page');hb=head['location']['bbox'];s=cell.get('structure',{})
    chain=s.get('record_column_region_refs') or [];index=field.get('record_row_index')
    if (type(index) is not int or not 0<=index<len(chain) or len(chain)<2
            or chain[index]!=regions[0] or len({r.get('block_id') for r in chain})!=len(chain)):return False
    bottom=hb[3]
    for rr in chain:
        row=lookup.get(rr.get('block_id'),{});bb=row.get('location',{}).get('bbox',[])
        if (not _closed(row) or row['location'].get('page')!=page or abs(bb[0]-hb[0])>=4
                or abs(bb[2]-hb[2])>=4 or abs(bb[1]-bottom)>8):return False
        bottom=bb[3]
    if s.get('record_header_ref') not in labels:return False
    for rr in labels:
        block=lookup.get(rr.get('block_id'),{});a,z=rr.get('start'),rr.get('end')
        if (type(a) is not int or type(z) is not int or not 0<=a<z<=len(block.get('text',''))
                or not source_text_inside_cell(block,head,label=True)):return False
    texts=[]
    for rr in values:
        block=lookup.get(rr.get('block_id'),{});a,z=rr.get('start'),rr.get('end')
        if (type(a) is not int or type(z) is not int or not 0<=a<z<=len(block.get('text',''))
                or not source_text_inside_cell(block,cell) or rr not in s.get('ocr_value_refs',[])
                or any(source_text_inside_cell(block,lookup[r['block_id']]) for r in chain if r!=regions[0])):return False
        texts.append(block['text'][a:z])
    return field.get('answer_text_original')=='\n'.join(texts)
