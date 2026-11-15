"""Bind independently located response boxes to their printed caption band.

Unknown contours inside printed prose remain unknown, retained source objects.
They may be excluded from this specific response group only when the separate
actual rectangles, literal captions and question establish the local boundary.
"""
from copy import deepcopy
import re

VERSION='separate-printed-response-v2-column-boundary'
TOKEN=re.compile(r'(?<![A-Za-z])(?:Yes|No|N\s*/\s*A|Planned|In preparation)(?![A-Za-z])',re.I)


def box(b):return b.get('location',{}).get('bbox',[])
def cy(b):return (box(b)[1]+box(b)[3])/2
def ref(b,a=0,z=None):return {'block_id':b['block_id'],'start':a,'end':len(b['text']) if z is None else z,'slot_id':''}
def inside(a,b):return b[0]-1<=a[0]<a[2]<=b[2]+1 and b[1]-1<=a[1]<a[3]<=b[3]+1
def unchanged(b):return {'block_id':b['block_id'],'text':b['text'],'location':deepcopy(b['location'])}


def scope_printed_response_groups(groups,decoded,text,inline):
    out=[]
    for row in groups:
        real=[b for b in row if len(b.get('controls',[]))==1 and
              b['controls'][0].get('selected') is not None and
              b.get('structure',{}).get('blank_state') in {'blank','filled'}]
        if len(real) not in (1,2,3,4) or len(real)==len(row):out.append(row);continue
        real.sort(key=lambda b:box(b)[0]);working=dict(decoded);y=sum(cy(b) for b in real)/len(real)
        # A whole caption run encloses exactly these separately traced boxes.
        # Character-width interpolation is not a source of physical positions.
        runs=[]
        for b in text:
            if abs(cy(b)-y)>7:continue
            matches=list(TOKEN.finditer(b['text']));residue=b['text']
            for m in reversed(matches):residue=residue[:m.start()]+residue[m.end():]
            if len(matches)!=len(real) or residue.strip(' \t\n\r:;□☐☑☒口O()/|'):continue
            if len({m[0].casefold() for m in matches})!=len(real):continue
            if not all(box(b)[0]-2<=box(r)[0]<box(r)[2]<=box(b)[2]+2 for r in real):continue
            runs.append((b,matches))
        if len(runs)==1:
            b,matches=runs[0]
            for r,m in zip(real,matches):working[r['block_id']]=(b,ref(b,m.start(),m.end()))
        if any(r['block_id'] not in working for r in real):out.append(row);continue
        captions=[working[r['block_id']] for r in real]
        words=[b['text'][rr['start']:rr['end']].strip().casefold() for b,rr in captions]
        if len(words)!=len(set(words)) or any(not TOKEN.fullmatch(w) for w in words):out.append(row);continue
        if len(real)>1 and not {'yes','no'}.issubset(words):out.append(row);continue
        page=real[0]['location'].get('page')
        if any(b['location'].get('page')!=page or abs(cy(b)-cy(r))>7 for r,(b,rr) in zip(real,captions)):out.append(row);continue
        candidates=[]
        for b in text:
            if b['location'].get('page')!=page or len(b['text'])<12:continue
            prior=[rr for source,rr in captions if source['block_id']==b['block_id']]
            end=min((rr['start'] for rr in prior),default=len(b['text']))
            value=b['text'][:end].rstrip()
            if not value.endswith(('?','？')):continue
            if abs(cy(b)-y)<=7 and (box(b)[0]<box(real[0])[0]-20):
                candidates.append((b,ref(b,0,len(value))))
        # A question above the response must physically share its column.
        # Being somewhere to its left is insufficient on a multi-column page.
        first_center=(box(real[0])[0]+box(real[0])[2])/2
        above=[b for b in text if b['location'].get('page')==page and
               b['text'].rstrip().endswith(('?','？')) and len(b['text'])>12 and
               7<y-cy(b)<30 and box(b)[0]-2<=first_center<=box(b)[2]+2]
        if candidates and any(not any(box(b)[0]<box(q)[2] and box(q)[0]<box(b)[2]
                                      for q,rr in candidates) for b in above):
            # Competing lateral and upper-column prompts need more evidence.
            # Do not grant a local proof based solely on baseline proximity.
            out.append(row);continue
        if not candidates and above:
            nearest=max(above,key=cy)
            blockers=[b for b in text if b['location'].get('page')==page and
                      box(b)[0]<box(nearest)[2] and box(nearest)[0]<box(b)[2] and
                      cy(nearest)+4<cy(b)<y-5 and
                      (b['text'].rstrip().endswith(('?','？')) or re.fullmatch(r'\d+(?:\.\d+)+',b['text'].strip()))]
            if not blockers:candidates=[(nearest,ref(nearest))]
        if len(candidates)!=1:out.append(row);continue
        question,qref=candidates[0]
        # A single visible Yes is only scoped if its literal suffix is attached
        # to this question. The existing wrapped-option stage must locate No.
        if len(real)==1 and (words!=['yes'] or captions[0][0]['block_id']!=question['block_id']):out.append(row);continue
        evidence=[question]+[b for b,rr in captions]
        evidence += [b for b in text if re.fullmatch(r'\d+(?:\.\d+)+',b['text'].strip()) and
                     abs(cy(b)-cy(question))<7 and box(b)[2]<box(question)[0]+1]
        evidence=list({b['block_id']:b for b in evidence}.values())
        h=min(box(r)[3]-box(r)[1] for r in real);w=min(box(r)[2]-box(r)[0] for r in real)
        extras=[b for b in row if b not in real];valid=True
        for b in extras:
            bb=box(b)
            if (len(b.get('controls',[]))!=1 or b['controls'][0].get('selected') is not None or
                b.get('structure',{}).get('native_selection_verified') or
                bb[2]-bb[0]>.85*w or bb[3]-bb[1]>.75*h or
                not any(inside(bb,box(source)) for source in evidence)):
                valid=False;break
            # An independent short-caption area is not sufficient to dismiss
            # an unknown mark: require the full joined band or question prose.
            containers=[s for s in evidence if inside(bb,box(s))]
            below_caption=False
            if box(question)[3]<=min(box(r)[1] for r in real)+1:
                for s in containers:
                    owners=[r for r,(caption,rr) in zip(real,captions) if caption['block_id']==s['block_id']]
                    if (TOKEN.fullmatch(s['text'].strip()) and len(owners)==1 and
                        bb[2]<=box(owners[0])[0]-4 and bb[2]-bb[0]<=.65*w and bb[3]-bb[1]<=.65*h):
                        below_caption=True
            if not any(s is question or len(list(TOKEN.finditer(s['text'])))>=2 or
                       len(s['text'])>12 or re.fullmatch(r'\d+(?:\.\d+)+',s['text'].strip()) for s in containers) and not below_caption:
                valid=False;break
        if not valid:out.append(row);continue
        proof={'version':VERSION,'question_ref':qref,'sources':[unchanged(b) for b in evidence],
               'region_ids':[b['block_id'] for b in real],
               'region_locations':[deepcopy(b['location']) for b in real],
               'caption_refs':[deepcopy(rr) for b,rr in captions],
               'unknown_contour_ids':[b['block_id'] for b in extras],
               'unknown_contours':[{**unchanged(b),'controls':deepcopy(b.get('controls',[]))} for b in extras]}
        for r in real:r.setdefault('structure',{})['separate_printed_response_group']=deepcopy(proof)
        for r in extras:r.setdefault('structure',{})['separate_response_exclusion']={
            **deepcopy(proof),'status':'undetermined_source_content','action':'excluded_from_this_response_group_only'}
        decoded.update({b['block_id']:working[b['block_id']] for b in real})
        # Keep the verified owner through later grouping. Re-selecting the
        # nearest text there would discard the column evidence above.
        inline[real[0]['block_id']]=(question,deepcopy(qref))
        out.append(real)
    return out


def verified_separate_response(field,lookup):
    options=field.get('options',[])
    if len(options) not in (2,3,4):return False
    proofs=[];seen=[]
    for option in options:
        refs=option.get('refs',[]);targets=option.get('answer_region_refs',[])
        if len(refs)!=1 or len(targets)!=1 or targets[0] not in field.get('answer_region_refs',[]):return False
        target=lookup.get(targets[0].get('block_id'),{});proof=target.get('structure',{}).get('separate_printed_response_group')
        if not proof or proof.get('version')!=VERSION:return False
        if proof.get('question_ref') not in field.get('label_refs',[]):return False
        for saved in proof['sources']:
            b=lookup.get(saved['block_id'],{})
            if b.get('text')!=saved['text'] or b.get('location')!=saved['location']:return False
        for saved in proof.get('unknown_contours',[]):
            b=lookup.get(saved['block_id'],{})
            if (b.get('text')!=saved['text'] or b.get('location')!=saved['location'] or
                    b.get('controls')!=saved['controls']):return False
        if target.get('block_id') not in proof['region_ids']:return False
        i=proof['region_ids'].index(target['block_id'])
        if target.get('location')!=proof['region_locations'][i] or refs[0]!=proof['caption_refs'][i]:return False
        source=lookup.get(refs[0]['block_id'],{});a,z=refs[0].get('start'),refs[0].get('end')
        if not isinstance(a,int) or not isinstance(z,int) or not 0<=a<z<=len(source.get('text','')):return False
        if source['text'][a:z]!=option.get('text_original') or refs[0] not in target.get('structure',{}).get('scan_option_caption_refs',[]):return False
        controls=target.get('controls',[])
        if len(controls)!=1 or controls[0].get('selected') is not option.get('selected'):return False
        if option.get('selected') is True:
            from .selection_evidence import selected_scan_caption
            if not selected_scan_caption(refs[0],field,lookup):return False
        seen.append(target['block_id']);proofs.append(proof)
    return len(set(seen))==len(options) and set(seen)=={r['block_id'] for r in field.get('answer_region_refs',[])}
