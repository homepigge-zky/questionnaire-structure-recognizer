"""Corroborate multi-field cells with local rendered paragraph baselines.

The local rendering contains no injected marker text. Answers and final write
locations always come from native paragraph spans, never from PDF text or list
indices; rendered origins only corroborate the native relationships.
"""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re

VERSION = 'filled-paragraph-lanes-v7-unmodified-native-render'


def label_groups(block):
    groups=[]; current=[]
    for p in block.get('structure',{}).get('paragraph_spans',[]):
        if not p.get('text','').strip():
            if current:groups.append(current)
            current=[]
        else:current.append(p)
    if current:groups.append(current)
    result=[]
    for group in groups:
        if not group[-1]['text'].rstrip().endswith((':','：')):continue
        if len(group)>2:continue
        if len(group)==2:
            # Adjacent original-language/English expressions within one
            # physical paragraph group. Same-language labels stay separate.
            latin=[bool(re.search(r'[A-Za-z]{3}',p['text'])) for p in group]
            nonlatin=[bool(re.search(r'[\u0370-\u03ff\u3040-\u9fff]',p['text'])) for p in group]
            if not (latin.count(True)==1 and nonlatin.count(True)>=1):continue
        result.append({'start':group[0]['start'],'end':group[-1]['end'],
            'paragraph_id':group[0]['paragraph_id'], 'paragraph_index':group[0]['paragraph_index'],
            'paragraph_ids':[p['paragraph_id'] for p in group]})
    return result


def associate_lanes(labels, paragraphs, positions):
    """Find unique native response paragraphs inside nonoverlapping bands."""
    result=[]
    for index,label in enumerate(labels):
        anchor_ids=label.get('paragraph_ids') or [label['paragraph_id']]
        # Bilingual labels can straddle a page. Use the native label paragraph
        # on the response's page, never shift its text with diagnostic markers.
        available=[pid for pid in anchor_ids if pid in positions]
        anchors=[]
        for pid in available:
            if positions[pid][0] not in {positions[a][0] for a in anchors}:anchors.append(pid)
        anchor=next((pid for pid in anchors if any(p['paragraph_id'] in positions
            and positions[p['paragraph_id']][0]==positions[pid][0]
            and positions[p['paragraph_id']][1]>positions[pid][1]
            and abs(positions[p['paragraph_id']][2]-positions[pid][2])<=16
            for p in paragraphs if p.get('text','').strip())),label['paragraph_id'])
        loc=positions.get(anchor)
        if not loc:continue
        page,x,y=loc
        next_loc=positions.get(labels[index+1]['paragraph_id']) if index+1<len(labels) else None
        if next_loc and next_loc[0]<page:continue
        bottom=next_loc[2]-3 if next_loc and next_loc[0]==page else float('inf')
        candidates=[p for p in paragraphs if p['paragraph_id'] in positions
            and positions[p['paragraph_id']][0]==page
            and positions[p['paragraph_id']][1]>x
            and y-3<=positions[p['paragraph_id']][2]<bottom]
        populated=[p for p in candidates if p.get('text','').strip()]
        if populated:
            # A lower lone paragraph without same-line evidence could belong
            # to another unrecognized caption. Do not guess that association.
            if abs(positions[populated[0]['paragraph_id']][2]-y)>16:continue
            chosen=populated
        else:
            chosen=[p for p in candidates if abs(positions[p['paragraph_id']][2]-y)<3]
            if len(chosen)!=1:continue
        result.append({'label_start':label['start'],'label_end':label['end'],
            'label_paragraph':anchor,
            'answer_paragraphs':[p['paragraph_id'] for p in chosen],
            'answer_origins':{p['paragraph_id']:list(positions[p['paragraph_id']]) for p in chosen},
            'page':page+1,'baseline':y,'evidence':'local_rendered_native_paragraph_bands',
            'layout_measurement_version':VERSION})
    return result


def align_filled_lanes(path, native, cache_dir):
    from .router import _libreoffice_executable
    from docx import Document
    rows=defaultdict(list)
    for b in native['source_blocks']:
        s=b.get('structure',{})
        if b['kind']=='table_cell' and b['location'].get('part')=='word/document.xml' and not s.get('is_merge_continuation'):
            rows[(s.get('table_id'),s.get('row_start'))].append(b)
    candidates=[]
    for cells in rows.values():
        cells=sorted(cells,key=lambda b:b['structure']['grid_col_start'])
        if len(cells)!=2:continue
        left,right=cells; groups=label_groups(left)
        if len(groups)<2 or right.get('controls') or not right['text'].strip():continue
        if left['structure']['grid_col_end']+1!=right['structure']['grid_col_start']:continue
        if right['structure'].get('label_list'):continue
        candidates.append((left,right,groups))
    if not candidates:return {'status':'not_needed','resolved':0,'version':VERSION}
    exe=_libreoffice_executable()
    if not exe:return {'status':'unavailable','resolved':0,'version':VERSION}
    cache=Path(cache_dir);cache.mkdir(parents=True,exist_ok=True)
    template_hash=hashlib.sha256(Path(path).read_bytes()).hexdigest()
    key=hashlib.sha256((native['source_hash']+template_hash+native['parser_version']+VERSION).encode()).hexdigest()
    target=cache/(key+'.json')
    if target.exists():mappings=json.loads(target.read_text(encoding='utf8'));hit=True
    else:
        from .native_layout import document_paragraph_positions
        selected={b['block_id']:b for left,right,_ in candidates for b in (left,right)}
        wanted={p['paragraph_id'] for b in selected.values() for p in b['structure']['paragraph_spans'] if p['text'].strip()}
        positions=document_paragraph_positions(Document(path),selected,wanted)
        mappings=[]
        for left,right,groups in candidates:
            for mapping in associate_lanes(groups,right['structure']['paragraph_spans'],positions):
                mapping.update(label_block=left['block_id'],answer_block=right['block_id'])
                mappings.append(mapping)
        target.write_text(json.dumps(mappings,ensure_ascii=False,indent=2),encoding='utf8')
        (cache/(key+'-positions.json')).write_text(json.dumps(positions),encoding='utf8')
        hit=False
    lookup={b['block_id']:b for b in native['source_blocks']}
    for mapping in mappings:
        lookup[mapping['answer_block']]['structure'].setdefault('filled_lane_alignment',[]).append(mapping)
    return {'status':'complete','resolved':len(mappings),'cache_hit':hit,'version':VERSION}
