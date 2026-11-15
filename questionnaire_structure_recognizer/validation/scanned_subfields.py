"""Exact source subfields inside an already bounded scan response cell.

No OCR correction and no answer guessing: delimiters and the physical enclosing
cell establish scope; source character ranges retain every observed value.
"""
from copy import deepcopy
import math
import re

from .field_boundaries import ref
from .form_completion import new_field
from .pdf_source_relations import scan_label_inside

VERSION = 'scan-subfields-v4-independent-pairing-and-transcription-review'
SOURCE_REVIEW_REASON = '已保留实体答复格中的原文标签和值；扫描文字需对照原页审核。'


def _verified_inline_rule(value, labels, cell, rule, lookup):
    from .pdf_source_relations import source_text_inside_cell
    vb=value.get('location',{}).get('bbox',[]); rb=rule.get('location',{}).get('bbox',[])
    if (len(labels)!=1 or len(vb)!=4 or len(rb)!=4 or
        rule.get('structure',{}).get('scan_region_kind')!='write_line' or
        rule.get('structure',{}).get('source_method')!='raster_geometry' or
        rule.get('location',{}).get('page')!=value.get('location',{}).get('page') or
        not source_text_inside_cell(value,cell)):return False
    label=lookup.get(labels[0].get('block_id'),{}); lb=label.get('location',{}).get('bbox',[])
    text=label.get('text',''); start,end=labels[0].get('start'),labels[0].get('end')
    if (len(lb)!=4 or not isinstance(start,int) or not isinstance(end,int) or not 0<=start<end<=len(text) or
        not re.search(r'[:：?？]\s*$',text[start:end]) or not source_text_inside_cell(label,cell)):return False
    return bool(0<=rb[0]-lb[2]<12 and rb[0]-2<=vb[0]<vb[2]<=rb[2]+2 and
        abs((lb[1]+lb[3]-vb[1]-vb[3])/2)<10 and rb[1]-8<=(vb[1]+vb[3])/2<=rb[3]+2 and
        cell['location']['bbox'][0]-2<=rb[0] and rb[2]<=cell['location']['bbox'][2]+2)


def verified_scan_value_slot(block, slot, lookup):
    text=block.get('text','');start,end=slot.get('start'),slot.get('end')
    loc=block.get('location',{})
    if (slot.get('kind')!='scan_bounded_source_value' or loc.get('extraction')!='ocr' or
        slot.get('expected_source_text')!=text or not isinstance(start,int) or not isinstance(end,int) or
        not 0<=start<end<=len(text) or not text[start:end].strip()):return False
    cell=lookup.get(slot.get('container_ref',{}).get('block_id'),{})
    cb=cell.get('location',{}).get('bbox',[])
    if (cell.get('structure',{}).get('scan_region_kind')!='text_box' or
        cell.get('structure',{}).get('source_method')!='raster_geometry' or
        cell.get('location',{}).get('page')!=loc.get('page') or
        not scan_label_inside(loc.get('bbox',[]),cb)):return False
    labels=slot.get('label_refs') or []
    if not labels:return False
    for lr in labels:
        b=lookup.get(lr.get('block_id'),{});s,e=lr.get('start'),lr.get('end')
        if (not isinstance(s,int) or not isinstance(e,int) or not 0<=s<e<=len(b.get('text','')) or
            b.get('location',{}).get('page')!=loc.get('page') or
            not scan_label_inside(b.get('location',{}).get('bbox',[]),cb) or
            b.get('block_id')==block.get('block_id') and max(start,s)<min(end,e)):return False
    if slot.get('inline_rule_ref') and not _verified_inline_rule(block,labels,cell,
            lookup.get(slot['inline_rule_ref'].get('block_id'),{}),lookup):return False
    return slot.get('evidence')=='explicit_prompt_and_value_in_closed_response'


def verified_scan_subfield_pairing(field, lookup):
    """Recheck exact physical slots; this never approves their OCR text.

    A source flag or a rectangle alone is insufficient. The current field must
    still use every original label and value range of one closed source cell.
    Model metadata cannot turn a different question's slot into its answer.
    """
    from .pdf_source_relations import source_text_inside_cell
    if (field.get('boundary_evidence') != 'closed_scan_source_subfield' or
            field.get('answer_pairing_evidence') != 'ocr_text_inside_verified_source_subfield' or
            field.get('answer_status') != 'filled'):
        return False
    labels = field.get('label_refs') or []
    values = field.get('answer_value_refs') or []
    regions = field.get('answer_region_refs') or []
    observation = field.get('source_observation') or {}
    key = lambda r: (r.get('block_id'), r.get('start'), r.get('end'))
    if (not labels or not values or len(regions) != len(values) or
            observation.get('origin') != 'source_document' or observation.get('status') != 'pending_review' or
            list(map(key, observation.get('answer_value_refs') or [])) != list(map(key, values))):
        return False
    label_keys = list(map(key, labels))
    if len(set(label_keys)) != len(label_keys):
        return False
    for lr in labels:
        text = lookup.get(lr.get('block_id'), {}).get('text', '')
        start, end = lr.get('start'), lr.get('end')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            return False
    if field.get('question_text_original') != '\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in labels):
        return False
    consumed, containers, fragments = set(), set(), []
    for vr in values:
        block = lookup.get(vr.get('block_id'), {})
        start, end = vr.get('start'), vr.get('end')
        text = block.get('text', '')
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text) or
                block.get('location', {}).get('type') != 'pdf'):
            return False
        matching = [r for r in regions if key(r) == key(vr) and r.get('slot_id')]
        if len(matching) != 1 or key(vr) in consumed:
            return False
        region = matching[0]
        slots = [s for s in block.get('structure', {}).get('logical_slots', [])
                 if s.get('slot_id') == region['slot_id']]
        if (len(slots) != 1 or slots[0].get('start') != start or slots[0].get('end') != end or
                list(map(key, slots[0].get('label_refs') or [])) != label_keys or
                not verified_scan_value_slot(block, slots[0], lookup)):
            return False
        cell = lookup.get(slots[0]['container_ref'].get('block_id'), {})
        if (not _verified_closed_cell(cell) or not source_text_inside_cell(block, cell) or
                any(not source_text_inside_cell(lookup[r['block_id']], cell, label=True) for r in labels)):
            return False
        # Preserve strict ownership checks even for two ranges in one OCR line.
        for protected in block.get('structure', {}).get('protected_label_ranges', []):
            if max(start, protected.get('start', 0)) < min(end, protected.get('end', 0)):
                return False
        for prior_block, prior_start, prior_end in consumed:
            if prior_block == vr['block_id'] and max(start, prior_start) < min(end, prior_end):
                return False
        consumed.add(key(vr))
        containers.add(cell.get('block_id'))
        fragments.append(text[start:end])
    return len(containers) == 1 and field.get('answer_text_original') == '\n'.join(fragments)


def _verified_closed_cell(cell):
    """A loose box is not the measured four-sided source response boundary."""
    evidence = cell.get('structure', {}).get('closed_region_evidence') or {}
    polygon = evidence.get('polygon') or []
    coverage = evidence.get('edge_ink_coverage') or []
    box = cell.get('location', {}).get('bbox') or []
    number = lambda x: type(x) in (int, float) and math.isfinite(x)
    if (evidence.get('method') != 'observed_four_sided_polygon_v1' or len(polygon) != 4 or
            any(not isinstance(p, (list, tuple)) or len(p) != 2 or not all(map(number, p)) for p in polygon) or
            len(coverage) != 4 or any(not number(c) or not .8 <= c <= 1 for c in coverage) or
            len(box) != 4 or not all(map(number, box))):
        return False
    measured = [min(p[0] for p in polygon), min(p[1] for p in polygon),
                max(p[0] for p in polygon), max(p[1] for p in polygon)]
    if any(abs(a-b) > 3 for a, b in zip(box, measured)):
        return False
    turns = []
    for i in range(4):
        a, b, c = polygon[i], polygon[(i+1) % 4], polygon[(i+2) % 4]
        turns.append((b[0]-a[0])*(c[1]-b[1]) - (b[1]-a[1])*(c[0]-b[0]))
    return all(t > 0 for t in turns) or all(t < 0 for t in turns)


def verified_scan_signatory_refs(field, lookup):
    """Source signature in the same physical cell protects its name/title."""
    cell=lookup.get(field.get('scan_signatory_container_ref',{}).get('block_id'),{})
    cb=cell.get('location',{}).get('bbox',[]);page=cell.get('location',{}).get('page')
    if cell.get('structure',{}).get('scan_region_kind')!='text_box' or cell.get('structure',{}).get('source_method')!='raster_geometry':return []
    labels=field.get('label_refs') or []
    candidates=field.get('signatory_context_refs') or []
    if not labels or not candidates:return []
    for rr in labels+candidates:
        b=lookup.get(rr.get('block_id'),{});s,e=rr.get('start'),rr.get('end')
        if (not isinstance(s,int) or not isinstance(e,int) or not 0<=s<e<=len(b.get('text','')) or
            b.get('location',{}).get('page')!=page or not scan_label_inside(b.get('location',{}).get('bbox',[]),cb)):return []
    return deepcopy([r for r in candidates if re.match(r'\s*(?:signature|sign\s*/\s*date|签字|簽名)',lookup[r['block_id']]['text'][r['start']:r['end']],re.I)])


def _emit(fields, blocks, lookup, cell, labels, values, parent=None, rule=None):
    from .scanned_cells import _replace_owned
    if not labels or not values:return
    slots=[]
    pending_slots=[]
    for vr in values:
        b=lookup[vr['block_id']];sid=f"scan-subvalue:{b['block_id']}:{vr['start']}:{vr['end']}"
        slot={'slot_id':sid,'kind':'scan_bounded_source_value','start':vr['start'],'end':vr['end'],
              'expected_source_text':b['text'],'label_refs':deepcopy(labels),'container_ref':ref(cell),
              'evidence':'explicit_prompt_and_value_in_closed_response','bbox':deepcopy(b['location']['bbox']),
              'bbox_precision':'whole_ocr_line_with_exact_character_range'}
        if rule:slot['inline_rule_ref']=ref(rule)
        if not verified_scan_value_slot(b,slot,lookup):return
        existing = b.get('structure',{}).get('logical_slots',[])
        same = [s for s in existing if s.get('slot_id') == sid]
        if same and (len(same) != 1 or same[0] != slot):return
        if not same:pending_slots.append((b,slot))
        slots.append({**deepcopy(vr),'slot_id':sid})
    for b,slot in pending_slots:
        b.setdefault('structure',{}).setdefault('logical_slots',[]).append(slot)
    owned={r['block_id'] for r in labels+values}
    # A parent can share the Yes caption block with its subordinate "Date:";
    # do not remove that parent while replacing the model's false value labels.
    saved=[f for f in fields if parent and f.get('field_id')==parent.get('field_id')]
    _replace_owned(fields,owned,lookup)
    if saved and not any(f.get('field_id')==parent['field_id'] for f in fields):fields.extend(saved)
    f=new_field(lookup[labels[0]['block_id']],labels[0],slots)
    fid='scan-subfield:'+labels[0]['block_id']+':'+str(labels[0]['start'])
    f.update(field_id=fid,question_id=fid,label_refs=deepcopy(labels),question_refs=deepcopy(labels),
        question_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in labels),
        answer_region_refs=slots,answer_value_refs=deepcopy(values),answer_refs=deepcopy(values),
        answer_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in values),
        answer_status='filled',review_status='needs_review',boundary_evidence='closed_scan_source_subfield',boundary_version=VERSION,
        review_reasons=[SOURCE_REVIEW_REASON],source_review_reasons=[SOURCE_REVIEW_REASON],
        answer_pairing_evidence='ocr_text_inside_verified_source_subfield',
        source_observation={'origin':'source_document','status':'pending_review','answer_value_refs':deepcopy(values)})
    if parent:f.update(parent_question_id=parent['question_id'],context_refs=deepcopy(parent.get('label_refs',[])))
    for lr in labels:lookup[lr['block_id']].setdefault('structure',{})['protected_label_ranges']=[{'start':lr['start'],'end':lr['end']}]
    for vr in values:
        b=lookup[vr['block_id']];s=b.setdefault('structure',{})
        # Retain a same-block label's exact range; retire only disjoint stale
        # model protections which previously called this value a question.
        s['protected_label_ranges']=[x for x in s.get('protected_label_ranges',[]) if not max(x['start'],vr['start'])<min(x['end'],vr['end'])]
        s['resolved_layout_role']='existing_answer'
    if verified_scan_subfield_pairing(f,lookup):
        from .reviewed_transcription import resolve_transcription_reason
        resolve_transcription_reason(f,lookup,{SOURCE_REVIEW_REASON})
    fields.append(f)
    return f


def restore_scanned_subfields(fields, blocks, lookup):
    text=[b for b in blocks if b.get('location',{}).get('extraction')=='ocr' and b.get('text','').strip()]
    for parent in list(fields):
        cell=lookup.get(parent.get('physical_response_region_ref',{}).get('block_id'),{})
        if not cell or not parent.get('options'):continue
        cb=cell['location']['bbox'];page=cell['location']['page']
        local=sorted([b for b in text if b['location']['page']==page and scan_label_inside(b['location']['bbox'],cb)],key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0]))
        for i,b in enumerate(local):
            raw=b['text'];prefix=re.match(r'^[\s□☐☑☒口Z√✓✔]*(?:yes|no)\s*[,，]\s*',raw,re.I)
            start=prefix.end() if prefix else 0
            match=re.match(r'([^:：?？]{2,45}[:：?？])\s*(.*)$',raw[start:])
            if not match:continue
            label=ref(b,start,start+len(match.group(1)))
            values=[];value=match.group(2).strip()
            if value:
                at=start+match.start(2)+len(match.group(2))-len(match.group(2).lstrip())
                values=[ref(b,at,at+len(value))]
            else:
                for nxt in local[i+1:]:
                    if re.match(r'^[\s□☐☑☒口Z√✓✔]*(?:yes|no)(?=$|[,，:： ])',nxt['text'],re.I) or re.search(r'[:：?？]\s*$',nxt['text']):break
                    if nxt['location']['bbox'][1]-b['location']['bbox'][3]>45:break
                    values.append(ref(nxt))
            if values:_emit(fields,blocks,lookup,cell,[label],values,parent)
    # Explicit "Answer/Ans/答" markers within one actual closed cell also
    # delimit long descriptions. The nearest preceding instruction is kept.
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box']
    used=set()
    for cell in sorted(cells,key=lambda b:(b['location']['bbox'][2]-b['location']['bbox'][0])*(b['location']['bbox'][3]-b['location']['bbox'][1])):
        cb=cell['location']['bbox'];page=cell['location']['page']
        local=sorted([b for b in text if b['location']['page']==page and scan_label_inside(b['location']['bbox'],cb)],key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0]))
        for i,b in enumerate(local):
            if b['block_id'] in used:continue
            m=re.match(r'\s*(?:答\s*)?(?:Ans(?:wer)?\s*[:：])\s*(\S.+)',b['text'],re.I)
            if not m or i==0:continue
            labels=[ref(local[i-1])]
            if not re.search(r'please|provide|describe|如果|请|請',local[i-1]['text'],re.I):continue
            values=[ref(b,m.start(1),m.end(1))]
            for nxt in local[i+1:]:
                if nxt['location']['bbox'][1]-b['location']['bbox'][3]>25 or re.search(r'[:：?？]',nxt['text']):break
                values.append(ref(nxt))
            _emit(fields,blocks,lookup,cell,labels,values);used.update(r['block_id'] for r in labels+values)
        if len(local)==2 and re.search(r'(?:supplemental\s*comments?|additional\s*comments?|補充意见|补充意见)',local[0]['text'],re.I):
            _emit(fields,blocks,lookup,cell,[ref(local[0])],[ref(local[1])])
        signature=[b for b in local if re.match(r'\s*(?:signature|sign\s*/\s*date|签字|簽名)',b['text'],re.I)]
        if len(signature)==1 and len(local)<=5:
            sign=signature[0];end=sign['text'].find(':')+1
            signref=ref(sign,0,end if end else len(sign['text']))
            for b in local:
                if b['block_id'] in used:continue
                m=re.match(r'([^:：]{2,80}[:：])\s*(\S.+)',b['text'])
                if not m:continue
                f=_emit(fields,blocks,lookup,cell,[ref(b,0,len(m.group(1)))],[ref(b,m.start(2),m.end(2))])
                if f:
                    f.update(scan_signatory_container_ref=ref(cell),signatory_context_refs=[signref])
                    used.add(b['block_id'])
    _restore_inline_rule_values(fields,blocks,lookup)
    _restore_record_text_columns(fields,blocks,lookup)


def _restore_inline_rule_values(fields,blocks,lookup):
    """A source underline bounds a same-line value inside a larger form cell.

    The label can be a conditional subquestion. Neither a neighbor without an
    actual rule nor text from the next question qualifies as its existing value.
    """
    from .pdf_source_relations import source_text_inside_cell
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box']
    rules=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='write_line']
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr']
    used=set()
    for label in texts:
        if not re.search(r'[:：?？]\s*$',label['text']) or label['block_id'] in used:continue
        page=label['location']['page'];lb=label['location']['bbox'];cy=(lb[1]+lb[3])/2
        local_rules=[r for r in rules if r['location']['page']==page and
                     0<=r['location']['bbox'][0]-lb[2]<12 and r['location']['bbox'][1]-12<=cy<=r['location']['bbox'][3]+12]
        if not local_rules:continue
        containers=[c for c in cells if source_text_inside_cell(label,c)]
        if not containers:continue
        cell=min(containers,key=lambda b:(b['location']['bbox'][2]-b['location']['bbox'][0])*(b['location']['bbox'][3]-b['location']['bbox'][1]))
        labels=[ref(label)]; candidates=[]
        local_texts=[b for b in texts if b is not label and b['location']['page']==page and
                     b['location']['bbox'][0]>=lb[2]-2 and abs((b['location']['bbox'][1]+b['location']['bbox'][3])/2-cy)<10]
        for rule in local_rules:
            matches=[b for b in local_texts if _verified_inline_rule(b,labels,cell,rule,lookup)]
            if len(matches)==1:candidates.append((rule,matches[0]))
        if len(candidates)!=1:continue
        rule,value=candidates[0];ids={label['block_id'],value['block_id']}
        if any((f.get('manual_override') or f.get('answer_origin') in {'manual','knowledge','reviewed_knowledge'}) and
               any(r.get('block_id') in ids for r in f.get('label_refs',[])+f.get('answer_value_refs',[])) for f in fields):continue
        if any(f.get('boundary_evidence')=='closed_scan_source_subfield' and
               any(r.get('block_id') in ids for r in f.get('label_refs',[])) for f in fields):continue
        f=_emit(fields,blocks,lookup,cell,labels,[ref(value)],rule=rule)
        if not f:continue
        f['inline_rule_evidence_ref']=ref(rule)
        from ..source_text_review import verified_reviewed_text
        if value.get('style',{}).get('ocr_confidence',0)<.9 and not verified_reviewed_text(value):
            f.update(observed_answer_text=f['answer_text_original'],unvalidated_answer_refs=deepcopy(f['answer_value_refs']),
                     answer_status='unresolved',answer_text_original='',answer_value_refs=[],answer_refs=[])
            f['review_reasons']=['已定位同格填写线及原文值；文字转录未通过校验，保留原页内容待核对。']
        used.update(ids)


def _restore_record_text_columns(fields,blocks,lookup):
    from .scanned_cells import _replace_owned
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box']
    headers=[b for b in blocks if b.get('structure',{}).get('resolved_layout_role')=='record_column_header' and not re.fullmatch(r'no\.?|序号|编号',b.get('text','').strip(),re.I)]
    handled=set()
    for header in headers:
        hb=header['location']['bbox'];page=header['location']['page']
        containers=[b for b in cells if b['location']['page']==page and scan_label_inside(hb,b['location']['bbox'])]
        if not containers:continue
        hc=min(containers,key=lambda b:(b['location']['bbox'][2]-b['location']['bbox'][0])*(b['location']['bbox'][3]-b['location']['bbox'][1]));cb=hc['location']['bbox']
        if hc['block_id'] in handled:continue
        handled.add(hc['block_id'])
        captions=sorted([b for b in headers if b['location']['page']==page and scan_label_inside(b['location']['bbox'],cb)],
                        key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0]))
        labelrefs=[ref(b) for b in captions]
        rows=sorted([b for b in cells if b['location']['page']==page and b['location']['bbox'][1]>=cb[3]-8 and
            abs(b['location']['bbox'][0]-cb[0])<4 and abs(b['location']['bbox'][2]-cb[2])<4],key=lambda b:b['location']['bbox'][1])
        chain=[];bottom=cb[3]
        for cell in rows:
            if abs(cell['location']['bbox'][1]-bottom)>8:break
            chain.append(cell);bottom=cell['location']['bbox'][3]
        if len(chain)<2:continue
        for index,cell in enumerate(chain):
            if any(f.get('manual_override') and any(r.get('block_id')==cell['block_id'] for r in f.get('answer_region_refs',[])) for f in fields):continue
            rb=cell['location']['bbox']
            values=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('page')==page and scan_label_inside(b['location'].get('bbox',[]),rb)]
            # This path handles a scalar record column only. Option columns
            # need individual native mark evidence and remain separate.
            if not values or any(re.search(r'[:：?？]',b['text']) for b in values):continue
            if any(b.get('controls') and b['location'].get('page')==page and scan_label_inside(b['location'].get('bbox',[]),rb) and
                not any(scan_label_inside(b['location']['bbox'],v['location']['bbox']) for v in values) for b in blocks):continue
            values.sort(key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0]));refs=[ref(b) for b in values]
            _replace_owned(fields,{b['block_id'] for b in values},lookup)
            for b in values:b.setdefault('structure',{}).pop('protected_label_ranges',None)
            f=new_field(header,ref(header),[ref(cell)]);fid='scan-record:'+cell['block_id']
            f.update(field_id=fid,question_id=fid,question_text_original='\n'.join(b['text'] for b in captions),label_refs=deepcopy(labelrefs),question_refs=deepcopy(labelrefs),
                answer_region_refs=[ref(cell)],answer_value_refs=deepcopy(refs),answer_refs=deepcopy(refs),answer_text_original='\n'.join(b['text'] for b in values),
                answer_status='filled',review_status='needs_review',boundary_evidence='closed_record_column',boundary_version=VERSION,
                record_group_id=hc['block_id'],record_instance_id=cell['block_id'],record_row_index=index,
                record_identity_status='ambiguous',
                record_identity_reason='原文已定位独立记录行；仅有列名和行号不能确定跨问卷业务对象，禁止跨记录自动复用。',
                review_reasons=['已按表头和连续实体单元格恢复记录列；记录对象及扫描文字需审核，不能跨记录自动复用。'],
                source_observation={'origin':'source_document','status':'pending_review','answer_value_refs':deepcopy(refs)})
            cell.setdefault('structure',{}).update(scan_association_valid=True,scan_field_id=fid,ocr_value_refs=deepcopy(refs),
                record_header_ref=ref(header),record_column_region_refs=[ref(x) for x in chain],blank_state='filled',content_state='text')
            from .scanned_record_pairing import verified_scan_record_pairing,RECORD_REVIEW_REASON
            if verified_scan_record_pairing(f,lookup):
                f['source_review_reasons']=[RECORD_REVIEW_REASON]
                from .reviewed_transcription import resolve_transcription_reason
                resolve_transcription_reason(f,lookup,{RECORD_REVIEW_REASON})
            fields[:]=[old for old in fields if old.get('field_id')!=fid]
            fields.append(f)
