"""Physical scan group boundaries and source comments (no model requests).

Only independently bounded cells establish ownership. Source data, superseded
model groups and human edits are retained; comments never become new answers.
"""
from copy import deepcopy
import re
from .field_boundaries import ref
from .form_completion import new_field
from .pdf_source_relations import scan_label_inside
from ..parser_core.models import make_block

VERSION='scan-context-groups-v2-closed-source-ownership'


def _flat(fields):
    for f in fields:
        yield f
        yield from _flat(f.get('children',[]))


def _box(b):return b.get('location',{}).get('bbox',[])
def _page(b):return b.get('location',{}).get('page')
def _manual(f):return f.get('manual_override') or f.get('review_status') in {'confirmed','human_corrected'}
def _inside(b,c):return _page(b)==_page(c) and scan_label_inside(_box(b),_box(c))
def _text(refs,lookup):return '\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in refs)


def _discard_refs(fields,ids,lookup):
    kept=[]
    for f in fields:
        if _manual(f):kept.append(f);continue
        refs=f.get('label_refs') or f.get('question_refs') or []
        remaining=[r for r in refs if r['block_id'] not in ids]
        if len(refs)!=len(remaining):
            if not remaining:continue
            f=deepcopy(f);f.update(label_refs=remaining,question_refs=deepcopy(remaining),question_text_original=_text(remaining,lookup),
                answer_text_original='',answer_refs=[],answer_value_refs=[],answer_region_refs=[],options=[],answer_status='unresolved',review_status='needs_review')
        _discard_refs(f.get('children',[]),ids,lookup);kept.append(f)
    fields[:]=kept


def reconcile_closed_field_ownership(fields,blocks,lookup):
    """One proved physical row owns its bilingual labels and answer controls.

    A model fragment may survive inside a parent even after top-level recovery.
    It must not duplicate a native question or borrow another row's check mark.
    Only verified source-cell geometry establishes ownership; equal text,
    model confidence and the acceptance gold are never used.
    """
    all_fields=list(_flat(fields));proofs=[]
    def overlaps(a,b,region=False):
        if a.get('block_id')!=b.get('block_id'):return False
        if region:return True
        return max(a.get('start',0),b.get('start',0))<min(a.get('end',0),b.get('end',0))
    for f in all_fields:
        if f.get('boundary_evidence') not in {'closed_scan_choice_row','closed_scan_cell_pair'}:continue
        left=lookup.get(f.get('physical_label_region_ref',{}).get('block_id'),{})
        right=lookup.get(f.get('physical_response_region_ref',{}).get('block_id'),{})
        if not right and len(f.get('answer_region_refs',[]))==1:right=lookup.get(f['answer_region_refs'][0]['block_id'],{})
        labels=f.get('label_refs',[]);targets=f.get('answer_region_refs',[])
        if (not labels or not targets or left.get('structure',{}).get('scan_region_kind')!='text_box' or
            right.get('structure',{}).get('scan_region_kind')!='text_box' or _page(left)!=_page(right) or
            not all(_inside(lookup.get(r['block_id'],{}),left) for r in labels) or
            not all(_inside(lookup.get(r['block_id'],{}),right) for r in targets)):continue
        proofs.append(f)
    manual=[f for f in all_fields if _manual(f)]
    retired={id(f) for f in proofs if not _manual(f) and any(
        any(overlaps(a,b) for a in f['label_refs'] for b in human.get('label_refs',[])) for human in manual)}
    proofs=[f for f in proofs if id(f) not in retired]
    proof_ids={id(f) for f in proofs};removed=[];conflicts=[]
    def is_row_number(refs,owner):
        from .scanned_cells import _same_row
        if len(refs)!=1:return False
        b=lookup.get(refs[0]['block_id'],{});text=b.get('text','')[refs[0].get('start',0):refs[0].get('end',0)]
        if not re.fullmatch(r'\d+(?:\.\d+)*[.)]?',text.strip()):return False
        left=lookup[owner['physical_label_region_ref']['block_id']];lb=_box(left)
        number_cells=[c for c in blocks if c.get('structure',{}).get('scan_region_kind')=='text_box' and _inside(b,c) and
            _page(c)==_page(left) and len(_box(c))==4 and abs(_box(c)[2]-lb[0])<4 and
            _box(c)[2]-_box(c)[0]<min(60,(lb[2]-lb[0])*.3) and _same_row(_box(c),lb)]
        return len(number_cells)==1
    def visit(items):
        retained=[]
        for f in items:
            if _manual(f):retained.append(f);continue
            children=f.get('children',[]);visit(children)
            if id(f) in retired:
                retained.extend(children);removed.append(f.get('field_id'));continue
            if id(f) in proof_ids:retained.append(f);continue
            refs=f.get('label_refs') or f.get('question_refs') or []
            owners=[p for p in proofs if any(overlaps(a,b) for a in refs for b in p['label_refs']) or
                    any(overlaps(a,b,True) for a in f.get('answer_region_refs',[]) for b in p.get('answer_region_refs',[]))]
            if not owners:retained.append(f);continue
            number_owner=[p for p in owners if is_row_number(refs,p)]
            if len(number_owner)==1:
                owner=number_owner[0];owner['number_refs']=deepcopy(refs)
                owner['question_number_original']=_text(refs,lookup)
                for r in refs:lookup[r['block_id']].setdefault('structure',{}).update(resolved_layout_role='question_number',scan_number_owner=owner['field_id'])
                retained.extend(children);removed.append(f.get('field_id'));continue
            covered=[r for r in refs if any(overlaps(r,lr) for p in owners for lr in p['label_refs'])]
            remaining=[r for r in refs if r not in covered]
            if covered and not remaining:
                # Children have their own source identities; do not discard a
                # real conditional write-in when removing a duplicate parent.
                retained.extend(children);removed.append(f.get('field_id'));continue
            owned_targets={r['block_id'] for p in owners for r in p.get('answer_region_refs',[])}
            target_collision=any(r['block_id'] in owned_targets for r in f.get('answer_region_refs',[]))
            if covered or target_collision:
                if covered:
                    f['label_refs']=remaining;f['question_refs']=deepcopy(remaining)
                    f['question_text_original']=_text(remaining,lookup)
                f.update(answer_region_refs=[],answer_value_refs=[],answer_refs=[],options=[],
                         answer_text_original='',answer_status='unresolved',review_status='needs_review')
                reason='原模型引用跨越另一条已验证实体字段；保留未归属原文，不能沿用该字段的选项或答案。'
                if reason not in f.setdefault('review_reasons',[]):f['review_reasons'].append(reason)
                f['source_boundary_conflict']={'version':VERSION,'owner_field_ids':[p['field_id'] for p in owners]}
                conflicts.append(f.get('field_id'))
            retained.append(f)
        items[:]=retained
    visit(fields)
    return {'version':VERSION,'retired_derived_fields':removed,'unresolved_cross_boundary_fields':conflicts}


def resolve_scanned_context_groups(fields,blocks,lookup):
    reconcile_closed_field_ownership(fields,blocks,lookup)
    notes=[];texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr']
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box' and b.get('structure',{}).get('source_method')=='raster_geometry']
    protected={r['block_id'] for f in _flat(fields) if _manual(f) for key in ('label_refs','answer_value_refs') for r in f.get(key,[])}
    option_targets={r['block_id'] for f in _flat(fields) for o in f.get('options',[]) for r in o.get('answer_region_refs',[])}
    def associated_control(b):
        return b.get('controls') and (b['block_id'] in option_targets or b.get('structure',{}).get('scan_association_valid'))
    def retain(b,role,reason):
        b.setdefault('structure',{}).update(resolved_layout_role=role,scan_context_evidence={'version':VERSION,'reason':reason})
        notes.append({'role':role,'refs':[ref(b)],'text_original':b['text'],'reason':reason})
    # Page counters require the printed current page / total, a bottom margin,
    # and a central page position; ordinary numeric answer fragments survive.
    page_count=max((_page(b) or 0 for b in blocks),default=0)
    for b in texts:
        m=re.fullmatch(r'\s*(\d+)\s*/\s*(\d+)\s*',b['text'])
        if not m or b['block_id'] in protected or int(m[1])!=_page(b) or int(m[2])!=page_count:continue
        peers=[t for t in texts if _page(t)==_page(b)];width=max(_box(t)[2] for t in peers);bottom=max(_box(t)[3] for t in peers);bb=_box(b)
        if bb[1]<bottom*.94 or not width*.35<(bb[0]+bb[2])/2<width*.65:continue
        retain(b,'footer','Printed current-page/total in the actual bottom page margin.');_discard_refs(fields,{b['block_id']},lookup)
    # A closed, full-width numbered section bar has no separate input area.
    for cell in cells:
        cb=_box(cell)
        if cb[2]-cb[0]<400 or not 8<cb[3]-cb[1]<36:continue
        content=[b for b in texts if _inside(b,cell)]
        if len(content)!=1:continue
        b=content[0];text=b['text']
        if b['block_id'] in protected or not re.match(r'^\s*\d+[.、]\s*\S',text) or re.search(r'[?？:：]',text):continue
        matches=[f for f in _flat(fields) if any(r['block_id']==b['block_id'] for r in f.get('label_refs',[]))]
        if any(_manual(f) or f.get('children') or f.get('boundary_evidence')=='closed_scan_cell_pair' for f in matches):continue
        if any(associated_control(c) and _inside(c,cell) for c in blocks):continue
        retain(b,'section_header','Numbered source section occupies a closed full-width bar without an input control.')
        b['structure']['superseded_scan_assignments']=deepcopy(matches)
        _discard_refs(fields,{b['block_id']},lookup)
    # A skewed full-width header can lack its own rectangle even though its
    # upper and lower grid rows and its actual spanning bottom rule survive.
    # Reconstruct that one enclosed grid band from all four neighboring cells,
    # never from a text gap or a guessed nearby answer location.
    for f in list(fields):
        refs=f.get('label_refs') or [];labels=[lookup.get(r['block_id'],{}) for r in refs]
        if _manual(f) or not labels or f.get('physical_response_region_ref') or any(len(_box(b))!=4 for b in labels):continue
        page=_page(labels[0]);y0=min(_box(b)[1] for b in labels);y1=max(_box(b)[3] for b in labels)
        if not all(_page(b)==page for b in labels) or not re.search(r'following|下列|以下',f.get('question_text_original',''),re.I):continue
        if any(all(_inside(b,c) for b in labels) and _box(c)[2]-_box(c)[0]>400 for c in cells):continue
        pairs=[]
        for child in fields:
            l=lookup.get(child.get('physical_label_region_ref',{}).get('block_id'),{});r=lookup.get(child.get('physical_response_region_ref',{}).get('block_id'),{})
            if len(_box(l))==len(_box(r))==4 and _page(l)==page:pairs.append((l,r))
        below=[(l,r) for l,r in pairs if -4<=min(_box(l)[1],_box(r)[1])-y1<12]
        above=[(l,r) for l,r in pairs if -4<=y0-max(_box(l)[3],_box(r)[3])<12]
        if len(below)!=1 or len(above)!=1:continue
        left,right=below[0];pl,pr=above[0];lb,rb,pb,qb=map(_box,(left,right,pl,pr))
        if abs(lb[0]-pb[0])>6 or abs(rb[2]-qb[2])>6 or abs(lb[2]-pb[2])>6:continue
        rules=[b for b in blocks if _page(b)==page and b.get('structure',{}).get('scan_region_kind')=='write_line' and
            _box(b)[0]<=lb[0]+6 and _box(b)[2]>=rb[2]-3 and min(lb[1],rb[1])-12<=_box(b)[3]<=max(lb[1],rb[1])+10]
        if not rules:continue
        bounds=[min(lb[0],pb[0]),min(pb[3],qb[3]),max(rb[2],qb[2]),max(lb[1],rb[1])]
        if not 10<bounds[3]-bounds[1]<60 or not all(scan_label_inside(_box(b),bounds) for b in labels):continue
        derived=make_block('ocr_text','',min(b['order'] for b in labels),{'type':'pdf','page':page,'extraction':'ocr','bbox':bounds,'closed_grid_band':left['block_id']},
            structure={'scan_region_kind':'text_box','source_method':'raster_grid_boundaries','group_only_region':True,
                'grid_evidence_refs':[ref(b) for b in (pl,pr,left,right)]+[ref(rules[0])]}).model_dump()
        if derived['block_id'] not in lookup:blocks.append(derived);lookup[derived['block_id']]=derived;cells.append(derived)
    # Complete spanning header cells: associate only adjacent verified child
    # rows. A new independent number cell ends the group; it is not an index
    # into a text list. Wide rectangles containing controls are not headers.
    for cell in sorted(cells,key=lambda b:(_page(b),_box(b)[1])):
        cb=_box(cell);page=_page(cell)
        if cb[2]-cb[0]<400 or not 12<cb[3]-cb[1]<75:continue
        content=[b for b in texts if _inside(b,cell) and b.get('structure',{}).get('resolved_layout_role') not in {'footer','section_header'}]
        if not content or any(b['block_id'] in protected for b in content) or any(associated_control(b) and _inside(b,cell) for b in blocks):continue
        content.sort(key=lambda b:(_box(b)[1],_box(b)[0]));prompt='\n'.join(b['text'] for b in content)
        if not re.search(r'[?？:：]|\b(?:following|provided\s+for|these\s+tests)\b|下列|以下',prompt,re.I):continue
        ids={b['block_id'] for b in content}
        old=[f for f in fields if any(r['block_id'] in ids for r in f.get('label_refs',[]))]
        if not old or any(_manual(f) or f.get('children') for f in old):continue
        candidates=[]
        for f in fields:
            left=lookup.get(f.get('physical_label_region_ref',{}).get('block_id'),{});right=lookup.get(f.get('physical_response_region_ref',{}).get('block_id'),{})
            lb,rb=_box(left),_box(right)
            if (f in old or _manual(f) or not f.get('options') or len(lb)!=4 or len(rb)!=4 or _page(left)!=page or
                abs(lb[0]-cb[0])>6 or abs(rb[2]-cb[2])>6 or lb[1]<cb[3]-10):continue
            candidates.append((lb[1],f,left,right))
        candidates.sort(key=lambda x:x[0]);children=[];bottom=cb[3]
        for y,f,left,right in candidates:
            if y-bottom>12:break
            lb,rb=_box(left),_box(right)
            numbered=[c for c in cells if _page(c)==page and _box(c)[2]<=lb[0]+3 and -3<=lb[0]-_box(c)[2]<6 and
                abs(_box(c)[1]-lb[1])<8 and abs(_box(c)[3]-lb[3])<8 and
                any(re.fullmatch(r'\d+\.\d+',t['text'].strip()) and _inside(t,c) for t in texts)]
            if numbered:break
            children.append(f);bottom=max(lb[3],rb[3])
        if len(children)<2:continue
        # A previously verified last/first-page split keeps its parent context
        # even where the left cell itself is open at the page cut. The closed
        # response cell and source-label lane, not a text similarity, prove it.
        for child in fields:
            cp=child.get('cross_page_evidence') or {}
            if child in children or _manual(child) or cp.get('pages',[None])[0]!=page:continue
            labels=[lookup[r['block_id']] for r in child.get('label_refs',[]) if _page(lookup[r['block_id']])==page]
            marks=[lookup[r['block_id']] for r in child.get('answer_region_refs',[]) if _page(lookup[r['block_id']])==page]
            boxes=[c for c in cells if _page(c)==page and _box(c)[2]-_box(c)[0]<200 and all(_inside(m,c) for m in marks)] if marks else []
            if len(boxes)!=1 or not labels:continue
            rb=_box(boxes[0]);band=[cb[0],rb[1],rb[0],rb[3]]
            if -8<=rb[1]-bottom<12 and abs(rb[2]-cb[2])<6 and all(scan_label_inside(_box(b),band) for b in labels):
                children.append(child);bottom=rb[3]
        # Continue an established group over a page boundary only until the
        # first independent numbered source row. Its physically aligned
        # continuation has no new spanning heading of its own.
        page_bottom=max((_box(b)[3] for b in texts if _page(b)==page),default=0)
        if bottom>=page_bottom*.90:
            next_rows=[]
            for child in fields:
                l=lookup.get(child.get('physical_label_region_ref',{}).get('block_id'),{});r=lookup.get(child.get('physical_response_region_ref',{}).get('block_id'),{})
                if child in children or _manual(child) or len(_box(l))!=4 or len(_box(r))!=4 or _page(l)!=page+1:continue
                lb,rb=_box(l),_box(r)
                if abs(lb[0]-cb[0])<12 and abs(rb[2]-cb[2])<12:next_rows.append((lb[1],child,l,r))
            next_rows.sort(key=lambda x:x[0]);prior=None
            for y,child,l,r in next_rows:
                lb,rb=_box(l),_box(r)
                if prior is None and y>100 or prior is not None and y-prior>12:break
                if any(_page(c)==page+1 and -3<=lb[0]-_box(c)[2]<6 and abs(_box(c)[1]-lb[1])<8 and abs(_box(c)[3]-lb[3])<8 and
                    any(re.fullmatch(r'\d+\.\d+',t['text'].strip()) and _inside(t,c) for t in texts) for c in cells):break
                if any(_page(c)==page+1 and _box(c)[2]-_box(c)[0]>400 and 0<_box(c)[3]<y and
                    any(_inside(t,c) and re.search(r'following|下列|以下|^\d+[.、]',t['text'],re.I) for t in texts) for c in cells):break
                children.append(child);prior=max(lb[3],rb[3])
        # Header/source ownership plus child-row evidence must agree: an
        # independently located parent answer must never be replaced.
        child_targets={r['block_id'] for f in children for r in f.get('answer_region_refs',[])}
        if any(any(r['block_id'] not in child_targets and lookup.get(r['block_id'],{}).get('text','').strip() and r['block_id'] not in ids for r in f.get('answer_region_refs',[])) for f in old):continue
        parent=new_field(content[0],ref(content[0]));fid='scan-group:'+cell['block_id']
        parent.update(field_id=fid,question_id=fid,label_refs=[ref(b) for b in content],question_refs=[ref(b) for b in content],question_text_original=prompt,
            children=children,answer_status='blank',review_status='model_proposed',boundary_evidence='closed_scan_question_group',boundary_version=VERSION,
            group_region_ref=ref(cell),superseded_scan_assignments=deepcopy(old),group_evidence={'header_region_ref':ref(cell),'child_field_ids':[f['field_id'] for f in children]})
        for child in children:
            child['parent_question_id']=fid
            child['context_refs']=list(child.get('context_refs',[]))+[ref(b) for b in content]
        consumed={f['field_id'] for f in old+children};fields[:]=[f for f in fields if f['field_id'] not in consumed];fields.append(parent)
        for b in content:b.setdefault('structure',{}).update(resolved_layout_role='group_context',scan_group_field_id=fid)
    # Actual written notes inside verified answer cells stay tied to that
    # answer, including its already-proved continuation cell on the next page.
    for parent in list(_flat(fields)):
        rr=parent.get('physical_response_region_ref')
        if not rr or not parent.get('options') or _manual(parent):continue
        regions=[lookup.get(rr['block_id'],{})]
        continuation=(parent.get('cross_page_evidence') or {}).get('continuation_cell_ref')
        if continuation:regions.append(lookup.get(continuation['block_id'],{}))
        excluded={r['block_id'] for f in _flat(fields) if f.get('boundary_evidence')=='closed_scan_source_subfield' or _manual(f) for key in ('label_refs','answer_value_refs') for r in f.get(key,[])}
        excluded.update(r['block_id'] for o in parent.get('options',[]) for r in o.get('refs',[]))
        candidates=[b for b in texts if b['block_id'] not in excluded|protected and any(_inside(b,c) for c in regions) and
            b.get('structure',{}).get('resolved_layout_role') not in {'footer','section_header'}]
        # A new prompt within an answer cell has its own field and is not a
        # comment merely because the model has not paired it yet.
        candidates=[b for b in candidates if not re.search(r'[?？:]|^\s*(?:if\b|若|如果|答\s*Ans)',b['text'],re.I)]
        if not candidates:continue
        candidates.sort(key=lambda b:(_page(b),_box(b)[1],_box(b)[0]));owned={b['block_id'] for b in candidates}
        parent['source_comment_refs']=[ref(b) for b in candidates]
        parent['source_comment_text_original']='\n'.join(b['text'] for b in candidates)
        parent['instruction_refs']=list(parent.get('instruction_refs',[]))+[ref(b) for b in candidates]
        _discard_refs(fields,owned,lookup)
        for b in candidates:retain(b,'instruction','Existing comment inside this field\'s verified physical response cell; retained verbatim, not a separate question.')
    # Conditional write-ins and footnotes inside a single closed option-list
    # cell must remain attached to that list, not become unrelated questions.
    for parent in list(_flat(fields)):
        container=lookup.get((parent.get('selection_mode_evidence') or {}).get('container_ref',{}).get('block_id'),{})
        if not container or _manual(parent):continue
        local=sorted([b for b in texts if _inside(b,container)],key=lambda b:(_box(b)[1],_box(b)[0]))
        for b in local:
            if b['block_id'] in protected:continue
            if re.match(r'^\s*\(?\*\s*:',b['text']):
                _discard_refs(fields,{b['block_id']},lookup);parent.setdefault('instruction_refs',[]).append(ref(b));retain(b,'instruction','Explicit footnote within the option group, retained verbatim.');continue
            if not re.search(r'\bif\s+not\b|(?<![A-Za-z])explain\s*[:：]',b['text'],re.I):continue
            bb=_box(b);below=[x for x in local if 0<_box(x)[1]-bb[3]<20 and re.match(r'^\s*(?:答\s*)?Ans\s*[:：]',x['text'],re.I)]
            labels=[b]+below[:1];anchor=labels[-1];ab=_box(anchor)
            lines=[x for x in blocks if x.get('structure',{}).get('scan_region_kind')=='write_line' and _inside(x,container) and
                abs((_box(x)[1]+_box(x)[3]-ab[1]-ab[3])/2)<10 and -5<=_box(x)[0]-ab[2]<20]
            if len(lines)!=1:continue
            line=lines[0]
            # Existing native OCR values on the line are not overwritten or
            # declared blank by this conservative blank-slot recovery.
            if any(x not in labels and _inside(x,line) for x in local):continue
            child=new_field(labels[0],ref(labels[0]),[ref(line)]);fid='scan-conditional:'+line['block_id']
            child.update(field_id=fid,question_id=fid,parent_question_id=parent['field_id'],label_refs=[ref(x) for x in labels],question_refs=[ref(x) for x in labels],
                question_text_original='\n'.join(x['text'] for x in labels),context_refs=deepcopy(parent.get('label_refs',[])),
                explicit_condition={'status':'unresolved','parent_field_id':parent['field_id'],'source_text':b['text']},
                boundary_evidence='conditional_line_inside_closed_option_cell',boundary_version=VERSION)
            line.setdefault('structure',{}).update(scan_association_valid=True,scan_field_id=fid,blank_state='blank')
            _discard_refs(fields,{x['block_id'] for x in labels},lookup);parent.setdefault('children',[]).append(child)
    # A proved physical section replaces stale model-derived "sections" such
    # as an option caption. Each real label inherits the preceding source bar.
    section_bars=[b for b in texts if b.get('structure',{}).get('resolved_layout_role')=='section_header' and
        b.get('structure',{}).get('scan_context_evidence',{}).get('version')==VERSION]
    section_bars.sort(key=lambda b:(_page(b),_box(b)[1]))
    for field in _flat(fields):
        if _manual(field):continue
        labels=field.get('label_refs') or []
        if not labels:continue
        label=lookup.get(labels[0]['block_id'],{});lb=_box(label)
        if len(lb)!=4:continue
        previous=[b for b in section_bars if (_page(b),_box(b)[1])<(_page(label),lb[1])]
        if not previous:continue
        section=previous[-1]
        field['section_refs']=[ref(section)];field['section_text_original']=section['text']
        label.setdefault('structure',{})['section_path_block_ids']=[[section['block_id']]]
    reconcile_closed_field_ownership(fields,blocks,lookup)
    fields.sort(key=lambda f:lookup.get((f.get('label_refs') or [{}])[0].get('block_id'),{}).get('order',0))
    return notes
