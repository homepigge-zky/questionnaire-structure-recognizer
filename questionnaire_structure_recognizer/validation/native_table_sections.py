"""Source-proved chapter headers for fields created late in native PDF tables."""
from collections import defaultdict
from copy import deepcopy
import math
import re

from .pdf_furniture_ownership import _protected

VERSION='native-table-sections-v1-same-physical-table-spanning-heading'


def inherit_native_table_sections(fields,blocks,lookup):
    def native(b):
        loc=b.get('location',{});s=b.get('structure',{});box=loc.get('bbox') or []
        return (b.get('kind')=='table_cell' and loc.get('type')=='pdf' and loc.get('extraction')=='native'
            and s.get('layout_evidence')=='visible_vector_borders' and s.get('table_id')
            and not s.get('is_merge_continuation') and len(box)==4
            and all(type(x) in (int,float) and math.isfinite(x) for x in box)
            and box[0]<box[2] and box[1]<box[3])
    def table(b):return (b['location'].get('page'),b['structure']['table_id'])
    tables=defaultdict(list)
    for b in blocks:
        if native(b):tables[table(b)].append(b)
    headings={}
    for key,cells in tables.items():
        verified=[]
        for b in cells:
            s=b['structure'];bb=b['location']['bbox'];text=b.get('text','')
            if (s.get('row_start')!=1 or s.get('row_end')!=1 or s.get('grid_col_start')!=1
                    or type(s.get('table_column_count')) is not int or s['table_column_count']<2
                    or s.get('grid_col_end')!=s['table_column_count'] or b.get('controls')
                    or not re.match(r'\s*(?:SECTION\s+(?:[IVXLCDM]+|\d+)\s*[:：]|第[一二三四五六七八九十百\d]+(?:部分|章)\s*[:：])\s*\S',text,re.I)
                    or s.get('native_bold_fraction',0)<.5):continue
            spans=s.get('native_text_spans') or []
            if not spans or any(type(p.get('start')) is not int or type(p.get('end')) is not int or
                    not 0<=p['start']<p['end']<=len(text) for p in spans):continue
            # The next physical row must really divide the spanning heading.
            edge=bb[3];next_row=2
            while True:
                below=[c for c in cells if c is not b and c['structure'].get('row_start')==next_row
                       and abs(c['location']['bbox'][1]-edge)<1]
                # A native full-width ownership/instruction row may sit between
                # the chapter and divided fields. Follow its actual shared edge;
                # a missing row, second chapter or shifted cell stops the chain.
                if len(below)!=1:break
                middle=below[0];ms=middle['structure'];mb=middle['location']['bbox']
                if (ms.get('row_end')!=next_row or ms.get('grid_col_start')!=1
                        or ms.get('grid_col_end')!=s['table_column_count']
                        or abs(mb[0]-bb[0])>=1 or abs(mb[2]-bb[2])>=1
                        or re.match(r'\s*SECTION\s',middle.get('text',''),re.I)):break
                edge=mb[3];next_row+=1
            if len(below)<2:continue
            spans=sorted((c['structure'].get('grid_col_start'),c['structure'].get('grid_col_end')) for c in below)
            if any(type(n) is not int for span in spans for n in span):continue
            if spans[0][0]!=1 or spans[-1][1]!=s['table_column_count'] or any(a[1]+1!=z[0] for a,z in zip(spans,spans[1:])):continue
            if min(c['location']['bbox'][0] for c in below)<bb[0]-1 or max(c['location']['bbox'][2] for c in below)>bb[2]+1:continue
            verified.append(b)
        if len(verified)==1:headings[key]=verified[0]
    changed=[]
    def assign(items,protected=False):
        for f in items:
            blocked=protected or _protected(f)
            if not blocked and not (f.get('section_refs') or f.get('section_text_original')):
                labels=[lookup.get(r.get('block_id'),{}) for r in f.get('label_refs') or []]
                if labels and all(native(b) for b in labels) and len({table(b) for b in labels})==1:
                    h=headings.get(table(labels[0]))
                    if h and all(b['structure'].get('row_start',0)>h['structure']['row_end'] and
                                 b['location']['bbox'][1]>=h['location']['bbox'][3]-1 for b in labels):
                        refs=[{'block_id':h['block_id'],'start':0,'end':len(h['text'])}]
                        node={'section_id':'native-table-section:'+h['block_id'],'refs':deepcopy(refs),
                              'text_original':h['text'],'level':0}
                        f.update(section_refs=refs,section_text_original=h['text'],section_path=[node],
                                 section_scope_evidence={'version':VERSION,'kind':'native_spanning_section_in_same_table',
                                                        'table_id':h['structure']['table_id'],'heading_ref':deepcopy(refs[0])})
                        changed.append(f['field_id'])
            assign(f.get('children') or [],blocked)
    assign(fields)
    return {'version':VERSION,'updated_fields':changed}
