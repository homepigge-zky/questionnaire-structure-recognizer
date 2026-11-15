"""Resolve unequal compound paragraph grids using local render coordinates.

Only an isolated temporary probe is instrumented. Stable OOXML paragraph IDs
remain the write targets; PDF coordinates are corroborating layout evidence.
If markers do not align uniquely, the native mapping remains unresolved.
"""
import hashlib,json,subprocess,tempfile
from pathlib import Path
from collections import defaultdict

VERSION='word-layout-alignment-v1'


def align_compound_paragraphs(path, native, cache_dir):
    from .router import _libreoffice_executable
    from docx import Document
    from docx.shared import Pt
    import fitz
    exe=_libreoffice_executable()
    if not exe:return {'status':'unavailable','resolved':0}
    rows=defaultdict(list)
    for b in native['source_blocks']:
        s=b.get('structure',{})
        if b['kind']=='table_cell' and b.get('location',{}).get('part')=='word/document.xml' and not s.get('is_merge_continuation'):
            rows[(b['location'].get('table'),s.get('row_start'))].append(b)
    candidates=[]
    for (table,row),cells in rows.items():
        cells.sort(key=lambda b:b['structure'].get('grid_col_start',0))
        if len(cells)!=2:continue
        left,right=cells; ls,rs=left['structure'],right['structure']
        labels=ls.get('label_list',[]); lp=ls.get('paragraph_spans',[]);rp=rs.get('paragraph_spans',[])
        if len(labels)<2 or len(lp)==len(rp) or right.get('controls'):continue
        if any(p['text'].strip() for p in rp[1:]):continue
        if any(p.get('style',{}).get('numbering') for p in lp[1:]):continue
        candidates.append((left,right,labels))
    if not candidates:return {'status':'not_needed','resolved':0}
    cache=Path(cache_dir);cache.mkdir(parents=True,exist_ok=True)
    key=hashlib.sha256((native['source_hash']+native['parser_version']+VERSION).encode()).hexdigest()
    target=cache/(key+'.json')
    if target.exists(): mappings=json.loads(target.read_text(encoding='utf8'));hit=True
    else:
        doc=Document(path);registry={};count=0
        for left,right,labels in candidates:
            for block,entries,kind in [(left,labels,'label'),(right,[p for p in right['structure']['paragraph_spans'] if p['blank']],'blank')]:
                loc=block['location'];s=block['structure']
                cell=doc.tables[loc['table']-1].rows[s['row_start']-1].cells[s['grid_col_start']-1]
                for entry in entries:
                    count+=1;token=f'QZ{count:05d}'
                    p=cell.paragraphs[entry['paragraph_index']-1]
                    run=p.add_run(token)
                    if kind=='label':
                        run.font.size=Pt(1);p._p.insert(1 if p._p.pPr is not None else 0,run._r)
                    registry[token]={'block_id':block['block_id'],'paragraph_id':entry['paragraph_id'],'kind':kind}
        with tempfile.TemporaryDirectory(prefix='qsr-layout-') as tmp:
            tmp=Path(tmp);probe=tmp/'probe.docx';doc.save(probe)
            profile=tmp/'profile';profile.mkdir()
            (profile/'user').mkdir()
            (profile/'user/registrymodifications.xcu').write_text('''<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item><item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item></oor:items>''',encoding='utf8')
            proc=subprocess.run([exe,f'-env:UserInstallation={profile.as_uri()}','--headless','--nologo','--norestore','--convert-to','pdf','--outdir',str(tmp),str(probe)],capture_output=True,timeout=60)
            if proc.returncode or not (tmp/'probe.pdf').exists():return {'status':'render_failed','resolved':0}
            positions=defaultdict(list)
            with fitz.open(tmp/'probe.pdf') as pdf:
                for pn,page in enumerate(pdf):
                    for b in page.get_text('dict')['blocks']:
                        for line in b.get('lines',[]):
                            for span in line.get('spans',[]):
                                for token in registry:
                                    if token in span['text']:positions[token].append((pn,span['origin'][0],span['origin'][1]))
            mappings=[]
            for left,right,labels in candidates:
                for token,item in registry.items():
                    if item['kind']!='label' or item['block_id']!=left['block_id'] or len(positions[token])!=1:continue
                    page,x,y=positions[token][0]
                    matches=[(other,val) for other,val in registry.items() if val['block_id']==right['block_id'] and val['kind']=='blank' and len(positions[other])==1 and positions[other][0][0]==page and positions[other][0][1]>x and abs(positions[other][0][2]-y)<3]
                    if len(matches)==1:
                        mappings.append({'label_block':left['block_id'],'label_paragraph':item['paragraph_id'],'answer_block':right['block_id'],'answer_paragraph':matches[0][1]['paragraph_id'],'page':page+1,'baseline':y})
            target.write_text(json.dumps(mappings,ensure_ascii=False,indent=2),encoding='utf8')
        hit=False
    lookup={b['block_id']:b for b in native['source_blocks']}
    for m in mappings:
        lookup[m['answer_block']]['structure'].setdefault('rendered_paragraph_alignment',{})[m['label_paragraph']]=m
    return {'status':'complete','resolved':len(mappings),'cache_hit':hit,'version':VERSION}
