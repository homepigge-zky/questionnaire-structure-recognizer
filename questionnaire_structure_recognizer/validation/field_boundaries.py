"""Resolve physically evidenced form boundaries before reading answer values.

This layer never searches for the next nonempty text. Ambiguous model links
remain reviewable; empty physical cells and explicit slots remain addressable.
"""
from collections import defaultdict
from copy import deepcopy
import re

VERSION = 'physical-field-boundaries-v7-compound-label-source-identity'
NUMBER = re.compile(r'^\s*\d+(?:\.\d+)*[.)、]?\s*$')
UNITS = re.compile(r'^(?:%|m[²2]|㎡|人|名|个|kg|g|mg|ml|l|employees|shifts?|years?|months?|days?|hours?|ppm|ppb|年|月|天|小时)\s*$', re.I)
PLACEHOLDER = re.compile(r'^[\s_＿.·…\-–—]*[_＿]{2,}[\s_＿.·…\-–—]*$')


def ref(block, start=0, end=None, slot=''):
    return {'block_id': block['block_id'], 'start': start,
            'end': len(block.get('text', '')) if end is None else end, 'slot_id': slot}


def placeholder(block, text):
    value = text.strip()
    if (block.get('structure') or {}).get('is_placeholder') is True: return True
    if not value or PLACEHOLDER.fullmatch(value): return True
    lines=[line.strip() for line in value.splitlines() if line.strip()]
    if lines and all(PLACEHOLDER.fullmatch(line) or re.fullmatch(r'(?:\d+|[ivxlcdm]+)[.)、]\s*[_＿]{3,}\s*',line,re.I) for line in lines): return True
    controls = block.get('controls') or []
    if controls and all(c.get('placeholder', c.get('is_placeholder', False)) for c in controls): return True
    # A native checkbox is evidence of an option, not a filled text value.
    if controls and all(c.get('type', c.get('control_type')) in {'checkbox', 'legacy_checkbox'} and c.get('selected', c.get('checked')) is False for c in controls): return True
    if re.search(r'[☐□\uf031]', value) and not re.search(r'[☑☒✓✔]', value): return True
    return False


def _s(block): return block.get('structure') or {}
def _row(block): return _s(block).get('row_start', _s(block).get('row', 0))
def _col(block): return _s(block).get('grid_col_start', _s(block).get('column_start', 0))
def _end(block): return _s(block).get('grid_col_end', _s(block).get('column_end', _col(block)))


def _paragraph_labels(block):
    """Return source-backed label ranges, without interpreting adjacent text."""
    labels = _s(block).get('label_list') or []
    if labels:
        return labels
    labels = []
    for paragraph in _s(block).get('paragraph_spans', []):
        text = paragraph.get('text', '')
        if text.strip().endswith((':', '：')) and len(text) < 120:
            end = paragraph.get('end', 0) - (len(text) - len(text.rstrip())) - 1
            labels.append({**paragraph, 'label_start': paragraph.get('start', 0),
                           'label_end': end})
    return labels


def restore_physical_fields(questions, blocks, lookup):
    tables = defaultdict(list)
    for block in blocks:
        s = _s(block)
        if block.get('kind') == 'table_cell' and s.get('table_id') and not s.get('is_merge_continuation') and s.get('blank_state') not in {'structural_spacer','merge_continuation'}:
            tables[s['table_id']].append(block)
    existing = defaultdict(list)
    for field in questions:
        for r in field.get('label_refs') or field.get('question_refs') or []:
            existing[r['block_id']].append(field)
    consumed, replacement = set(), []

    def field_for(label, regions, label_ref=None, parent=None):
        label_ref = label_ref or ref(label)
        prior = next((f for f in existing.get(label['block_id'], []) if len(f.get('label_refs') or []) == 1 and (f.get('label_refs') or [None])[0] == label_ref), None)
        s = _s(label)
        fid = (prior or {}).get('field_id') or f"physical:{label['block_id']}:{label_ref['start']}"
        if fid == parent:
            fid += f":child:{label_ref['end']}"
        section_refs = deepcopy((prior or {}).get('section_refs') or [])
        if not section_refs:
            ids = (s.get('section_path_block_ids') or [[]])[-1] or s.get('preceding_block_ids', [])
            section_refs = [ref(lookup[b]) for b in ids if b in lookup and lookup[b].get('text')]
        section = '\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in section_refs)
        f = {'field_id':fid, 'question_id':fid, 'parent_question_id':parent,
             'label_refs':[label_ref], 'question_refs':[label_ref],
             'question_text_original':label['text'][label_ref['start']:label_ref['end']],
             'section_refs':section_refs, 'section_text_original':section, 'context_label':section,
             'normalized_label':'', 'answer_region_refs':[], 'answer_value_refs':[], 'answer_refs':[],
             'answer_text_original':'', 'answer_status':'not_located', 'unit_refs':[], 'unit_text_original':'',
             'children':[], 'options':[], 'instruction_refs':[], 'attachment_refs':[], 'attachment_text_original':'',
             'review_status':'model_proposed', 'review_reasons':[], 'model_hints':{},
             'boundary_version':VERSION, 'boundary_evidence':'physical_local_form'}
        f['review_reasons']=[x for x in (prior or {}).get('review_reasons',[]) if '已有答案引用不是有效的非空原文范围' in x]
        value_refs, unit_refs, region_refs = [], [], []
        for region in regions:
            rs, text = _s(region), region.get('text','')
            loc=region.get('location',{}); nested_prefix=f"{loc.get('table')}.{loc.get('row')}.{loc.get('cell')}."
            nested=[b for b in blocks if b.get('kind')=='table_cell' and b.get('location',{}).get('part')==loc.get('part') and str(b.get('location',{}).get('table','')).startswith(nested_prefix)]
            if nested and re.match(r'^\s*(?:note\s*[:;]|说明[:：]|备注[:：])',text,re.I):
                # A warehouse list instruction above an embedded record grid
                # is fixed text, never a populated address. The actual child
                # record cells are restored separately with their own IDs.
                f['instruction_refs'].append(ref(region))
                f['context_refs']=deepcopy(f.get('context_refs',[]))+[ref(region)]
                continue
            if region.get('kind') in {'image','unsupported'} or rs.get('blank_state') == 'unreadable':
                f['review_reasons'].append('答复区域含未读取内容'); continue
            leading = next((slot for slot in rs.get('logical_slots',[]) if slot.get('kind')=='leading_blank_candidate'),None)
            if UNITS.fullmatch(text.strip()):
                unit_refs.append(ref(region))
                if leading:
                    region_refs.append(ref(region,leading['offset'],leading['offset'],leading['slot_id']))
                else: f['review_reasons'].append('发现单位，但数值填写片段尚未定位')
                continue
            if not rs.get('logical_slots') and len(text)<80 and text.strip().endswith((':','：')):
                # A printed prompt with no value after its delimiter is not
                # a completed answer. Keep it visible and require location
                # evidence before allowing a write into that source area.
                f.setdefault('unresolved_response_refs',[]).append(ref(region))
                f['review_reasons'].append('答复格只有填写提示，尚无实际值；具体填写片段需核对')
                continue
            region_refs.append(ref(region))
            if re.search(r'[☐□☑☒✓✔\uf031]',text):
                f['options'].append({'refs':[ref(region)],'text_original':text,'selected':bool(re.search(r'[☑☒✓✔]',text)),'selection_source':'native_symbol'})
            if not placeholder(region,text): value_refs.append(ref(region))
        f['answer_region_refs']=region_refs; f['answer_value_refs']=value_refs; f['answer_refs']=deepcopy(value_refs)
        f['answer_text_original']='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in value_refs).strip()
        f['unit_refs']=unit_refs; f['unit_text_original']='\n'.join(lookup[r['block_id']]['text'] for r in unit_refs)
        f['answer_status']='filled' if value_refs else ('blank' if region_refs else 'not_located')
        if f['review_reasons'] or not region_refs:
            f['review_status']='needs_review'
        f['source_locations']={'question':[deepcopy(label.get('location',{}))], 'answer_regions':[deepcopy(r.get('location',{})) for r in regions], 'answer':[deepcopy(lookup[r['block_id']].get('location',{})) for r in value_refs]}
        # Both final fields and the source ledger protect labels even if a
        # later model grouping fails to materialise the neighbouring field.
        s.setdefault('protected_label_ranges',[])
        span={'start':label_ref['start'],'end':label_ref['end']}
        if span not in s['protected_label_ranges']: s['protected_label_ranges'].append(span)
        s['semantic_role_hint']='field_label'
        return f

    for table_id,cells in tables.items():
        rows=defaultdict(list)
        for cell in cells: rows[_row(cell)].append(cell)
        for row in rows: rows[row].sort(key=_col)
        all_rows=list(rows.values())
        # Explicit repeated column headings and score rubrics are fixed
        # structure. Keep every source block, but do not reinterpret a rubric
        # such as "Satisfactory | Above 90 %" as a business answer.
        first=all_rows[0]
        # Record role columns describe the contact row; they are context, not
        # an answer asserting that the role name is a person's name/address.
        role_headers=[b for b in first if re.fullmatch(r'(?:responsibilit(?:y|ies)|role|职责|责任)',b.get('text','').strip(),re.I)]
        for heading in role_headers:
            for r in all_rows[1:]:
                for b in r:
                    if _col(b)==_col(heading) and b.get('text','').strip():_s(b)['fixed_record_context']=True
        for row in all_rows:
            for b in row:
                if re.fullmatch(r'\s*(?:<\s*[^<>]{1,80}\s*>|(?:vendor|supplier|company)\s+name(?:\s*(?:&|and|/)\s*address)?)\s*',b.get('text',''),re.I) and _s(b).get('local_layout_type')=='record_table':
                    _s(b)['is_placeholder']=True
        owner_columns=[b for b in first[1:] if b.get('text','').strip()]
        ownership_matrix=(not first[0].get('text','').strip() and len(owner_columns)>=2
            and any(re.fullmatch(r'(?:vendor|supplier|manufacturer|供方|供应商)',b['text'].strip(),re.I) for b in owner_columns)
            and len(all_rows)>=3)
        if ownership_matrix:
            consumed.update(b['block_id'] for b in first)
            for b in first:_s(b)['semantic_role_hint']='table_header'
            for row in all_rows[1:]:
                if not row[0].get('text','').strip():continue
                pairs=[]
                for heading in owner_columns:
                    hx=_s(heading).get('grid_x_twips')
                    values=[]
                    for b in row[1:]:
                        bx=_s(b).get('grid_x_twips')
                        if hx and bx:
                            intersection=max(0,min(hx[1],bx[1])-max(hx[0],bx[0]))
                            # Word creates sliver grid columns when adjacent
                            # rows have slightly different borders. Both
                            # intervals must mostly overlap, with a unique
                            # one-to-one match; index containment is unsafe.
                            aligned=intersection/min(hx[1]-hx[0],bx[1]-bx[0])>=.95 and intersection/max(hx[1]-hx[0],bx[1]-bx[0])>=.90
                        else:aligned=(_col(b),_end(b))==(_col(heading),_end(heading))
                        if aligned:values.append(b)
                    if len(values)==1:pairs.append((heading,values[0]))
                if len(pairs)!=len(owner_columns) or len({b['block_id'] for _,b in pairs})!=len(pairs):
                    consumed.update(b['block_id'] for b in row)
                    for b in row:_s(b)['semantic_role_hint']='section'
                    continue
                parent=field_for(row[0],[]);parent.update(answer_status='blank',boundary_evidence='physical_responsibility_matrix')
                for heading,value in pairs:
                    role='our_side' if re.fullmatch(r'(?:vendor|supplier|manufacturer|供方|供应商)',heading['text'].strip(),re.I) else 'customer'
                    _s(value).update(column_owner_role=role,column_owner_ref=heading['block_id'],column_owner_evidence={'kind':'native_grid_interval_alignment','header_block_id':heading['block_id'],'header_x_twips':_s(heading).get('grid_x_twips'),'region_x_twips':_s(value).get('grid_x_twips'),'unique_region':True})
                    child=field_for(heading,[value],parent=parent['field_id'])
                    child['field_id']=child['question_id']=f"{parent['field_id']}:owner:{_col(heading)}"
                    child['context_refs']=[ref(row[0])];child['boundary_evidence']='physical_responsibility_matrix'
                    parent['children'].append(child)
                replacement.append(parent);consumed.update(b['block_id'] for b in row)
            continue
        score_reference=(len(first)==2 and re.fullmatch(r'(?:score|rating|得分|评分)',first[1].get('text','').strip(),re.I)
            and len(all_rows)>=3 and all(len(r)==2 and '%' in r[1].get('text','') and not any(_s(b).get('logical_slots') and any(s.get('kind')=='inline_placeholder' for s in _s(b)['logical_slots']) for b in r) for r in all_rows[1:]))
        if score_reference:
            consumed.update(b['block_id'] for b in cells)
            for b in cells:_s(b)['semantic_role_hint']='reference_data'
            continue
        # Two side-by-side label cells above independently controlled option
        # cells are two vertical fields, not the left heading's answer.
        vertical_choice_pairs={}
        for rn,row in rows.items():
            below=rows.get(rn+1,[])
            if len(row)>=2 and len(below)==len(row) and all(b.get('text','').strip() for b in row) and all((_col(a),_end(a))==(_col(b),_end(b)) for a,b in zip(row,below)) and all(b.get('controls') and any(c.get('type')=='checkbox' for c in b['controls']) for b in below):
                vertical_choice_pairs[rn]=list(zip(row,below))
        # Mirrored signing columns with a blank gutter are parallel column
        # forms. Their repeated organization/role captions cannot be answers.
        parallel_signature_rows=set()
        for rn,row in rows.items():
            nonempty=[b for b in row if b.get('text','').strip()]
            below=rows.get(rn+1,[])
            if len(nonempty)<2 or len(row)!=len(below): continue
            below_blank=not any(b.get('text','').strip() for b in below)
            below_parallel_signing=all(any((_col(x),_end(x))==(_col(h),_end(h)) and re.search(r'\b(?:signee\s+(?:name|title)|sign(?:ature)?\s*[/&]|signature)\s*:',x.get('text',''),re.I) for x in below) for h in nonempty)
            if not (below_blank or below_parallel_signing):continue
            later=[r for no,r in rows.items() if rn<no<=rn+6]
            if any(sum(bool(re.search(r'\b(?:signee|sign(?:ature)?\s*[/&]|signature)',b.get('text',''),re.I)) for b in r)>=2 for r in later):
                parallel_signature_rows.add(rn)
        fixed_header_rows=set()
        for rn,row in rows.items():
            text=[b.get('text','').strip() for b in row if b.get('text','').strip()]
            if len(text)>=2 and any(re.fullmatch(r'(?:description|question|问题|描述)',t,re.I) for t in text) and any(re.fullmatch(r'(?:observations?|remarks?|comments?|answer|观察|备注|答案)',t,re.I) for t in text):fixed_header_rows.add(rn)
            if len(text)>=2 and not row[0].get('text','').strip() and all(re.fullmatch(r'(?:name|signature|date|姓名|签名|日期)',t,re.I) for t in text):fixed_header_rows.add(rn)
            if len(text)==1 and re.match(r'^\s*(?:[IVX]+\.|第[一二三四五六七八九十]+[章节])\s*\S',text[0]) and not re.search(r'[?？]',text[0]) and all(not b.get('text','').strip() for b in row[1:]):fixed_header_rows.add(rn)
        # Repeated final column labels beside a merged section heading and
        # above a wider grid are column headers (e.g. comment columns). Their
        # physical repetition/merge boundaries distinguish them from values.
        header_groups=defaultdict(list)
        for rn,row in rows.items():
            below=rows.get(rn+1,[])
            if len(row)==2 and _end(row[0])>_col(row[0]) and len(below)>2 and row[1].get('text','').strip() and len(row[1]['text'])<40 and _col(below[-1])==_col(row[1]) and not re.search(r'[?？]',row[0].get('text','')):
                header_groups[(_col(row[1]),_end(row[1]),row[1]['text'].strip())].append(rn)
        section_header_rows={rn for group in header_groups.values() if len(group)>=2 for rn in group}
        # Preserve locally delimited section/object scopes (A/B/C groups,
        # Part headings and full-width role headings) independently of names.
        local_context=[]; root_context=[]
        for rn, row in sorted(rows.items()):
            for b in row: _s(b)['local_context_block_ids']=list(local_context)
            first=row[0]; text=first.get('text','').strip()
            if re.match(r'^Part\s+[IVX\d]+\s*:',text,re.I):
                root_context=[first['block_id']]; local_context=list(root_context)
            elif rn in section_header_rows or re.match(r'^[A-Z]\.\s+\S',text) or (len(row)==1 and len(text)<150 and text.endswith((':','：'))):
                local_context=[*root_context,first['block_id']]
        # Repeated selectable cells beneath an unselected two-column header
        # are a record grid. The empty product rows are reserved records;
        # neither header is an answer to the other header.
        header_no=min(rows)
        header=rows[header_no]
        following=[r for rn,r in sorted(rows.items()) if rn>header_no]
        is_choice_record=(len(header)==2 and all(b.get('text','').strip() for b in header)
            and not any(re.search(r'[☐□☑☒]',b.get('text','')) for b in header)
            and len(following)>=2 and all(len(r)==2 and (_col(r[0]),_col(r[1]))==(_col(header[0]),_col(header[1])) and re.search(r'[☐□☑☒]',r[1].get('text','')) for r in following))
        if is_choice_record:
            for b in header: _s(b).update(local_layout_type='record_table_header',record_header_row=header_no)
            for r in following:
                for b in r: _s(b).update(local_layout_type='record_table',record_header_row=header_no)
            consumed.update(b['block_id'] for b in cells)
            continue
        # Repeated two-column physical form, with at least one actual blank
        # response, confirms orientation without requiring a populated header.
        pairs=[row for row in all_rows if len(row)==2 and row[0].get('text','').strip() and not NUMBER.fullmatch(row[0]['text'])]
        pair_groups=defaultdict(list)
        for p in pairs: pair_groups[(_col(p[0]),_end(p[0]),_col(p[1]),_end(p[1]))].append(p)
        form_signatures={key for key,group in pair_groups.items() if len(group)>=2 and any(not p[1].get('text','').strip() for p in group)}
        # Consecutively numbered labels across a row followed by aligned
        # value cells are a header-above-value form. Require repetition of
        # this pattern to distinguish it from a record table header.
        stacked={}
        for rn,row in rows.items():
            numbers=[re.match(r'^\s*(\d+)\.\s+\S',b.get('text','')) for b in row]
            below=rows.get(rn+1,[])
            if len(row)>=2 and all(numbers) and len(below)==len(row) and all((_col(a),_end(a))==(_col(b),_end(b)) for a,b in zip(row,below)) and any(not b.get('text','').strip() for b in below):
                ns=[int(n.group(1)) for n in numbers]
                if ns==list(range(ns[0],ns[0]+len(ns))): stacked[rn]=[(a,[b]) for a,b in zip(row,below)]
        if len(stacked)<2: stacked={}
        stacked_values={rn+1 for rn in stacked}
        explicit_header=None
        for row in all_rows:
            q=[b for b in row if re.fullmatch(r'(question(?:s)?|item|问题|题目)',b.get('text','').strip(),re.I)]
            a=[b for b in row if re.fullmatch(r'(answer(?:s)?|response(?:s)?|回答|答复)',b.get('text','').strip(),re.I)]
            if q and a: explicit_header=(_row(q[0]),_col(q[0]),_col(a[0])); break
        for row_no,row in rows.items():
            if row_no in fixed_header_rows|parallel_signature_rows:
                consumed.update(b['block_id'] for b in row)
                for b in row:_s(b)['semantic_role_hint']='table_header'
                continue
            if row_no in vertical_choice_pairs:
                for label,value in vertical_choice_pairs[row_no]:
                    replacement.append(field_for(label,[value]));consumed.update([label['block_id'],value['block_id']])
                continue
            if row_no in section_header_rows:
                consumed.update(b['block_id'] for b in row)
                for b in row: _s(b)['semantic_role_hint']='column_header'
                continue
            if row_no in stacked_values: continue
            if all(b['block_id'] in consumed for b in row): continue
            layout=_s(row[0]).get('local_layout_type','')
            if layout=='question_answer_header':
                consumed.update(b['block_id'] for b in row); continue
            if layout in {'record_table','record_table_header','choice_matrix_header'}: continue
            if layout=='choice_matrix' and not row[0].get('controls'): continue
            descriptors=[]
            # Explicit completion-owner headings delimit responsibility. The
            # instruction in the adjacent cell is not a prefilled response.
            if len(row)==2 and re.match(r'^\s*(?:on behalf of|由.+填写|填写方)',row[0].get('text',''),re.I) and re.search(r'(?:questionnaire|form).{0,30}(?:completed|evaluated|reviewed)|填写|审核',row[1].get('text',''),re.I):
                consumed.update(b['block_id'] for b in row)
                for b in row: _s(b)['semantic_role_hint']='completion_instruction'
                continue
            # Two explicit inline slots on the same row are independent
            # labels with in-cell targets, never a left/right answer pair.
            own_slots=[next((p for p in _s(b).get('logical_slots',[]) if (p.get('kind')=='inline_prompt_candidate' and p.get('start')==p.get('end')==len(b.get('text','')))
                            or p.get('kind')=='inline_value_candidate'),None) for b in row]
            if len(row)>=2 and all(own_slots) and all((slot['label_end']-slot['label_start'])<100 for slot in own_slots):
                consumed.update(b['block_id'] for b in row)
                for b,slot in zip(row,own_slots):
                    f=field_for(b,[],ref(b,slot['label_start'],slot['label_end']))
                    f['answer_region_refs']=[ref(b,slot['start'],slot['end'],slot['slot_id'])]
                    f['answer_status']='blank'; f['review_status']='model_proposed'
                    if slot.get('kind')=='inline_value_candidate':
                        value_ref=ref(b,slot['start'],slot['end'],slot['slot_id'])
                        f.update(answer_value_refs=[value_ref],answer_refs=[deepcopy(value_ref)],
                                 answer_text_original=b['text'][slot['start']:slot['end']],answer_status='filled')
                    f['source_locations']={'question':[deepcopy(b.get('location',{}))],
                        'answer_regions':[deepcopy(b.get('location',{}))],
                        'answer':[deepcopy(b.get('location',{}))] if f['answer_status']=='filled' else []}
                    replacement.append(f)
                continue
            signature=(_col(row[0]),_end(row[0]),_col(row[1]),_end(row[1])) if len(row)==2 else None
            pair_form=signature in form_signatures
            if explicit_header and row_no==explicit_header[0]:
                consumed.update(b['block_id'] for b in row); continue
            if row_no in stacked:
                descriptors=stacked[row_no]
            elif len(row)==1 and _s(row[0]).get('stacked_pair_candidate_block_id') in lookup:
                value=lookup[_s(row[0])['stacked_pair_candidate_block_id']]
                if _s(value).get('table_id')==table_id and _row(value)==_s(row[0]).get('row_end',row_no)+1 and (_col(value),_end(value))==(_col(row[0]),_end(row[0])):
                    descriptors=[(row[0],[value])]
            elif layout in {'numbered_question_answer','question_answer'} and _s(row[0]).get('label_column') is not None:
                label=next((b for b in row if _col(b)==_s(row[0])['label_column']),None)
                if label:
                    descriptors=[(label,[b for b in row if _col(b) in _s(row[0]).get('answer_columns',[])])]
            elif len(row)>=2 and NUMBER.fullmatch(row[0].get('text','')) and row[1].get('text','').strip():
                descriptors=[(row[1],row[2:])]
            elif explicit_header and row_no>explicit_header[0]:
                label=next((b for b in row if _col(b)==explicit_header[1]),None)
                region=[b for b in row if _col(b)==explicit_header[2]]
                if label and label.get('text','').strip(): descriptors=[(label,region)]
            elif _s(row[0]).get('row_layout_hint')=='horizontal_key_value_candidate' and len(row)%2==0:
                descriptors=[(row[i],[row[i+1]]) for i in range(0,len(row),2)]
            elif len(row)==2 and len(_paragraph_labels(row[0]))>=2 and _s(row[1]).get('paragraph_spans') and any(s.get('kind')=='blank_paragraph_candidate' for s in _s(row[1]).get('logical_slots',[])):
                # A title in the response cell does not erase the physical
                # empty paragraphs below it. Alignment is checked per child.
                descriptors=[(row[0],[row[1]])]
            elif row[0].get('controls') and all(c.get('type') in {'checkbox','legacy_checkbox'} for c in row[0]['controls']):
                # A selection cell and its explanatory policy are a matrix,
                # not an option label followed by a filled text answer.
                descriptors=[(row[0],[row[0]])]
            elif len(row)>=2 and row[0].get('text','').strip() and (layout=='label_response_form' or _s(row[0]).get('table_layout_hint')=='label_response_form_candidate' or pair_form or (_s(row[0]).get('row_span',1)>1 and all(any(s.get('kind')=='inline_prompt_candidate' for s in _s(b).get('logical_slots',[])) for b in row[1:]))):
                label=row[0]
                # A merged parent owns all response rows in its physical span.
                regions=[b for b in cells if _row(label)<=_row(b)<=_s(label).get('row_end',row_no) and _col(b)>_end(label)]
                descriptors=[(label,regions)]
            elif len(row)==2 and row[0]['block_id'] in existing and not row[1].get('text','').strip():
                descriptors=[(row[0],[row[1]])]
            elif len(row)>=2 and row[0].get('text','').strip() and (re.search(r'[☐□\uf031]',row[1].get('text','')) or (row[1].get('controls') and all(c.get('type')=='checkbox' for c in row[1]['controls']))):
                # Option cell plus a conditional instruction to its right.
                # The instruction remains context, not a text value.
                descriptors=[(row[0],[row[1]])]
            if not descriptors: continue
            consumed.update(b['block_id'] for b in row)
            for label,regions in descriptors:
                if not label.get('text','').strip(): continue
                f=field_for(label,regions)
                local_ids=_s(label).get('local_context_block_ids',[])
                if local_ids:
                    f['section_refs']=[ref(lookup[bid]) for bid in local_ids]
                    f['section_text_original']='\n'.join(lookup[bid]['text'] for bid in local_ids)
                    f['context_label']=f['section_text_original']
                if row_no in stacked or _s(label).get('stacked_pair_candidate_block_id'): f['boundary_evidence']='physical_stacked_form'
                if _s(label).get('stacked_pair_candidate_block_id') and not _s(label).get('stacked_pair_native_verified'):
                    f['review_reasons'].append('局部模型提出上下格配对，但尚无独立业务边界证据；须人工核对，禁止自动采用')
                    f['review_status']='needs_review'; f['answer_status']='unresolved'
                    f['observed_answer_text']=f['answer_text_original']
                consumed.update(b['block_id'] for b in regions)
                # Several labelled paragraphs opposite a stack of blank
                # paragraphs are a composite form, not one long question.
                # Only equal paragraph grids establish individual targets;
                # mismatched grids retain child labels for review.
                labels=_paragraph_labels(label)
                lp=_s(label).get('paragraph_spans',[])
                if len(labels)>=2 and len(regions)==1:
                    region=regions[0]; rp=_s(region).get('paragraph_spans',[])
                    if rp:
                        compound=[]; compound_labels=set()
                        for entry in labels:
                            lref=ref(label,entry.get('label_start',entry.get('start',0)),entry.get('label_end',entry.get('end',0)))
                            if not label['text'][lref['start']:lref['end']].strip(): continue
                            # The parser can expose value/prompt hypotheses for
                            # the same physical label. Keep both native slot
                            # observations, but emit their shared child once.
                            # Deduplicate inside this branch so its unresolved
                            # parent never becomes a whole-cell write target.
                            identity=(label['block_id'],lref['start'],lref['end'],
                                      entry.get('paragraph_id'),entry.get('paragraph_index'))
                            if identity in compound_labels: continue
                            compound_labels.add(identity)
                            child=field_for(label,[],lref,f['field_id'])
                            pi=entry.get('paragraph_index')
                            target=next((s for s in _s(region).get('logical_slots',[]) if s.get('kind')=='blank_paragraph_candidate' and s.get('paragraph_index')==pi),None)
                            alignment=_s(region).get('rendered_paragraph_alignment',{}).get(entry.get('paragraph_id'))
                            if alignment:
                                target=next((s for s in _s(region).get('logical_slots',[]) if s.get('kind')=='blank_paragraph_candidate' and s.get('paragraph_id')==alignment['answer_paragraph']),None)
                            if (len(lp)==len(rp) or alignment) and target:
                                child['answer_region_refs']=[ref(region,target['start'],target['end'],target['slot_id'])]
                                child['answer_status']='blank'; child['review_status']='model_proposed'
                                child['source_locations']['answer_regions']=[deepcopy(region.get('location',{}))]
                                child['boundary_evidence']='aligned_native_paragraph_slots'
                            else:
                                child['review_reasons']=['左右段落数量或位置不一致，需核对该子字段的填写段落']
                            compound.append(child)
                        if compound:
                            f['children']=compound; f['answer_region_refs']=[]; f['answer_value_refs']=[]; f['answer_refs']=[]
                            f['answer_text_original']=''; f['answer_status']='blank' if all(c['answer_status']=='blank' for c in compound) else 'unresolved'
                            # A heading opposite the opening number is context;
                            # other nonempty response paragraphs remain visible
                            # for review and are never copied into an answer.
                            header_paragraphs=[p for p in rp if p.get('text','').strip() and any(l.get('paragraph_index')==p.get('paragraph_index') and NUMBER.fullmatch(l.get('text','')) for l in lp)]
                            if header_paragraphs:
                                f['context_refs']=[ref(region,p['start'],p['end']) for p in header_paragraphs]
                                for child in compound:child['context_refs']=deepcopy(f['context_refs'])
                            other_text=[p for p in rp if p.get('text','').strip() and p not in header_paragraphs]
                            if other_text:
                                f['unresolved_response_refs']=[ref(region)]
                                f['review_reasons'].append('复合答复格含文字，需要逐子字段核对，不能整格作为原有答案')
                            if any(c['answer_status']=='not_located' for c in compound):
                                f['review_reasons'].append('子字段已保留，左右段落未可靠对齐，尚未确定各自填写位置')
                # Explicit prompt slots inside a response are child labels,
                # never already filled text. Keep each insertion position.
                children=[]; prompt_blocks=set()
                for region in regions:
                    for slot in _s(region).get('logical_slots',[]):
                        if slot.get('kind') not in {'inline_prompt_candidate','inline_placeholder'} or slot.get('label_start') is None: continue
                        if slot.get('kind')=='inline_prompt_candidate' and any(
                            other.get('kind')=='inline_placeholder' and
                            (other.get('label_start'),other.get('label_end'))==(slot.get('label_start'),slot.get('label_end')) and
                            isinstance(other.get('start'),int) and isinstance(other.get('end'),int) and
                            slot.get('end',0)<=other['start']<other['end']<=len(region['text']) and
                            not region['text'][slot.get('end',0):other['start']].strip() and
                            region['text'][other['start']:other['end']]==other.get('placeholder_text')
                            for other in _s(region).get('logical_slots',[])):
                            # A colon plus a line break before a physical
                            # underline is one response, not another insertion
                            # field with the same source identity.
                            continue
                        lref=ref(region,slot['label_start'],slot['label_end'])
                        bilingual_tail=region['text'][lref['end']:].strip().strip(';；:：').strip()
                        bilingual_label=bool(bilingual_tail and re.fullmatch(r'[\u3000-\u9fff\uf900-\ufaff\s]+',bilingual_tail))
                        if any(c['label_refs'][0]['block_id']==region['block_id'] and c['label_refs'][0]['end']==len(region['text']) for c in children): continue
                        if slot.get('kind')=='inline_prompt_candidate' and region['text'][lref['end']:].strip() in {';',':','：','；'}:
                            lref['end']=len(region['text'].rstrip())
                        if bilingual_label: lref['end']=len(region['text'])
                        if not region['text'][lref['start']:lref['end']].strip(): continue
                        child=field_for(region,[],lref,f['field_id'])
                        start=slot.get('start',slot.get('offset')); end=slot.get('end',start)
                        if bilingual_label:
                            trailing=next((s for s in _s(region).get('logical_slots',[]) if s['kind']=='trailing_fill_candidate'),None)
                            if trailing: start=end=trailing['offset']; slot=trailing
                        if start is None: continue
                        child['answer_region_refs']=[ref(region,start,end,slot['slot_id'])]
                        child['answer_status']='blank'; child['review_status']='model_proposed'
                        prior_child=next((c for c in children if c.get('label_refs')==child['label_refs']),None)
                        if prior_child:
                            for rr in child['answer_region_refs']:
                                if rr not in prior_child['answer_region_refs']:prior_child['answer_region_refs'].append(rr)
                        else:children.append(child)
                        prompt_blocks.add(region['block_id'])
                if children:
                    f['children']=children
                    f['answer_region_refs']=[r for r in f['answer_region_refs'] if r['block_id'] not in prompt_blocks]
                    f['answer_value_refs']=[r for r in f['answer_value_refs'] if r['block_id'] not in prompt_blocks]
                    f['answer_refs']=deepcopy(f['answer_value_refs'])
                    f['answer_text_original']='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in f['answer_value_refs']).strip()
                    f['answer_status']='filled' if f['answer_value_refs'] else 'blank'
                replacement.append(f)
    # Source identity, never text, determines replacement. Different sections
    # can retain identical labels and independently blank values.
    section_ids={bid for b in blocks for path in _s(b).get('section_path_block_ids',[]) for bid in path}
    def is_heading(bid):
        b=lookup[bid]
        return bid in section_ids and b.get('kind') in {'paragraph','header','footer'} and not b.get('controls') and not any(s.get('kind') in {'inline_placeholder','inline_prompt_candidate'} for s in _s(b).get('logical_slots',[]))
    questions[:]=[f for f in questions if f.get('question_text_original','').strip() and not any(r['block_id'] in consumed or is_heading(r['block_id']) for r in f.get('label_refs',[]))]+replacement
    questions.sort(key=lambda f:(lookup[(f.get('label_refs') or f['question_refs'])[0]['block_id']]['order'], (f.get('label_refs') or f['question_refs'])[0]['start']))
