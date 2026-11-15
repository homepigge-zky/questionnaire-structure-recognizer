"""Source-bounded completion metadata and current-submission attachment facts."""
from copy import deepcopy
import re
from .field_boundaries import ref
from .scanned_local_tables import _contents,_physical_contains,_manual

VERSION='pdf-submission-groups-v4-closed-choice-matrix-instructions'
META=re.compile(r'(?:printed)?name|designation|jobtitle|title|position|signature(?:date)?|date|姓名|职务|签名|签字|日期')
def compact(s):return re.sub(r'[^\w\u4e00-\u9fff]','',s.casefold())
def box(b):return b.get('location',{}).get('bbox',[])
def page(b):return b.get('location',{}).get('page')

def _completion_caption(text):
    # This only restricts automatic reuse in an explicitly signed completion
    # row. An OCR error in another short colon caption must not turn Name into
    # reusable historical contact data. Never rewrite the observed caption.
    text=text.strip()
    return bool(META.fullmatch(compact(text)) or (text.endswith((':','：'))
        and len(text)<=40 and not any(c in text for c in '?？\n')))

def _header_caption(cell,texts):
    # OCR glyph bounds may extend a little beyond an observed thin rule. The
    # caption's centre must still be inside this unique physical header cell.
    cb=box(cell)
    return [t for t in texts if compact(t['text']) in {'yes','no','na'} and
        cb[0]<sum(box(t)[::2])/2<cb[2] and cb[1]<sum(box(t)[1::2])/2<cb[3]]

def completion_group(field,lookup):
    labels=field.get('label_refs') or field.get('question_refs') or []
    if len(labels)!=1:return None
    anchor=lookup.get(labels[0]['block_id'],{});ab=box(anchor)
    if anchor.get('location',{}).get('type')!='pdf' or len(ab)!=4:return None
    if not _completion_caption(anchor.get('text','')):return None
    headings=[b for b in lookup.values() if page(b)==page(anchor) and len(box(b))==4
        and re.fullmatch(r'(?:(?:the|this)?questionnaire(?:hasbeen|was)?)?(?:completed|filled(?:in|out)?)by|填表人信息|填写人信息',compact(b.get('text','')))
        and 0<=ab[1]-box(b)[3]<=60 and box(b)[0]<=ab[0]+4]
    if len(headings)!=1:return None
    h=headings[0];cy=(ab[1]+ab[3])/2
    row=[b for b in lookup.values() if page(b)==page(anchor) and len(box(b))==4
        and b.get('text','').strip() and abs(sum(box(b)[1::2])/2-cy)<10 and box(b)[0]>=box(h)[0]-5]
    if not row or any(not _completion_caption(b['text']) for b in row):return None
    if not any(compact(b['text']) in {'signature','signaturedate','签字','签名'} for b in row):return None
    return h,sorted(row,key=lambda b:box(b)[0])

def current_completer_refs(field,lookup):
    group=completion_group(field,lookup)
    return [ref(group[0])] if group else []

def attached_matrix_refs(field,lookup):
    options=field.get('options') or []
    if len(options)!=3 or {compact(o.get('text_original','')) for o in options}!={'yes','no','na'}:return []
    targets=[lookup.get(r['block_id'],{}) for o in options for r in o.get('answer_region_refs',[])]
    if len(targets)!=3 or any(len(box(b))!=4 for b in targets) or len({page(b) for b in targets})!=1:return []
    p=page(targets[0]);left=min(box(b)[0] for b in targets);right=max(box(b)[2] for b in targets);top=min(box(b)[1] for b in targets)
    cells=[b for b in lookup.values() if page(b)==p and b.get('structure',{}).get('scan_region_kind')=='text_box'
        and len(b.get('structure',{}).get('closed_region_evidence',{}).get('polygon',[]))==4]
    texts=[b for b in lookup.values() if page(b)==p and b.get('text','').strip() and len(box(b))==4]
    # Find the nearest closed spanning header over this exact three-column lane.
    # A prior Observation table cannot authorize a later Attached table or vice versa.
    candidates=[]
    for c in cells:
        cb=box(c)
        if not(cb[0]<=left and right<=cb[2] and cb[3]<top and cb[3]-cb[1]<45 and cb[2]-cb[0]<170):continue
        captions=_contents(c,texts)
        if len(captions)!=1:continue
        headers=[s for s in cells if -4<=box(s)[1]-cb[3]<8 and cb[0]-3<=box(s)[0] and box(s)[2]<=cb[2]+3
            and len(_header_caption(s,texts))==1]
        if len(headers)!=3 or {compact(_header_caption(s,texts)[0]['text']) for s in headers}!={'yes','no','na'}:continue
        if any(not any(box(s)[0]-2<=box(t)[0]<=box(t)[2]<=box(s)[2]+2 for s in headers) for t in targets):continue
        candidates.append((cb[3],captions[0]))
    if not candidates:return []
    _,caption=max(candidates,key=lambda x:x[0])
    return [ref(caption)] if compact(caption['text']) in {'attached','enclosed','附件已附','已附'} else []

def reconcile_completion_positions(fields,blocks,lookup,non_qa=None):
    changes=[]
    def walk(fs):
        for f in fs:
            yield f
            yield from walk(f.get('children',[]))
    for f in walk(fields):
        group=completion_group(f,lookup)
        if not group or _manual(f) or f.get('answer_text_original'):continue
        h,row=group;label=lookup[f['label_refs'][0]['block_id']];lb=box(label);cy=sum(lb[1::2])/2
        position=next(i for i,b in enumerate(row) if b['block_id']==label['block_id'])
        right=box(row[position+1])[0] if position+1<len(row) else lb[2]+180
        lines=[b for b in blocks if page(b)==page(label) and b.get('structure',{}).get('scan_region_kind')=='write_line'
            and b.get('structure',{}).get('source_method')=='raster_geometry' and b.get('structure',{}).get('blank_state')=='blank'
            and len(box(b))==4 and lb[2]-4<=box(b)[0]<box(b)[2]<=right+2 and abs(sum(box(b)[1::2])/2-cy)<13]
        if len(lines)!=1:continue
        new=[ref(lines[0])]
        if f.get('answer_region_refs')==new:continue
        changes.append({'field_id':f['field_id'],'before':deepcopy(f.get('answer_region_refs',[])),'after':new})
        f.setdefault('boundary_history',[]).append({'action':'source_bound_completion_line','version':VERSION,'prior_answer_regions':deepcopy(f.get('answer_region_refs',[]))})
        f.update(answer_region_refs=new,answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='blank')
        f.pop('form_definition',None)
        lines[0]['structure'].update(scan_association_valid=True,scan_field_id=f['field_id'])
    from .pdf_customer_panels import reconcile_customer_panel_positions
    customer=reconcile_customer_panel_positions(fields,blocks,lookup)
    changes.extend({'customer_panel':entry,'version':customer['version']} for entry in customer['restored'])
    changes.extend(_attachment_table_captions(fields,lookup))
    changes.extend(_parallel_matrix_instructions(fields,blocks,lookup,non_qa))
    return changes

def _parallel_matrix_instructions(fields,blocks,lookup,non_qa):
    from .pdf_furniture_ownership import _closed,_protected
    from .scan_closed_local_grids import contents,shares_horizontal_border
    from .pdf_source_relations import source_cells_share_row
    groups={};changes=[]
    texts=[b for b in blocks if b.get('text','').strip() and len(box(b))==4]
    cells=[b for b in blocks if b.get('location',{}).get('type')=='pdf' and _closed(b)]
    for f in fields:
        e=f.get('local_grid_evidence',{})
        if e.get('kind')!='parallel_yes_no_matrix':continue
        header=lookup.get(e.get('label_header',{}).get('block_id'),{})
        hs=[lookup.get(rr['block_id'],{}) for rr in e.get('option_headers',[])]
        if not _closed(header) or len(hs)!=2 or not all(_closed(h) for h in hs):continue
        if not all(source_cells_share_row(a,b) for a,b in zip([header]+hs,hs)):continue
        labels=[contents(h,texts) for h in hs]
        if any(len(x)!=1 for x in labels) or [compact(x[0]['text']) for x in labels]!=['yes','no']:continue
        opts=f.get('options',[])
        if [compact(o.get('text_original','')) for o in opts]!=['yes','no']:continue
        targets=[lookup.get(o.get('answer_region_refs',[{}])[0].get('block_id'),{}) for o in opts]
        if not all(_closed(t) and page(t)==page(header) and abs(box(t)[0]-box(h)[0])<5 and abs(box(t)[2]-box(h)[2])<5
                   for t,h in zip(targets,hs)):continue
        group=groups.setdefault(header['block_id'],{'header':header,'options':hs,'fields':[]})
        group['fields'].append(f)
    for band in cells:
        bb=box(band)
        if bb[2]-bb[0]<300 or bb[3]-bb[1]>35:continue
        panels=[g for g in groups.values() if page(g['header'])==page(band)
                and bb[0]-4<=box(g['header'])[0] and box(g['options'][-1])[2]<=bb[2]+4
                and shares_horizontal_border(band,g['header'],box(g['header'])[0],box(g['header'])[2])]
        if len(panels)<2:continue
        captions=sorted(contents(band,texts),key=lambda b:box(b)[0])
        if not captions:continue
        original=' '.join(b['text'] for b in captions);normalized=compact(original)
        if not ('mark' in normalized and 'yes' in normalized and 'no' in normalized):continue
        bids={b['block_id'] for b in captions}
        owned=[f for f in fields if any(rr.get('block_id') in bids for rr in f.get('label_refs',[]))]
        if any(_protected(f) or f.get('answer_text_original') or f.get('answer_value_refs') or f.get('children')
               or f.get('answer_region_refs') or not {rr['block_id'] for rr in f.get('label_refs',[])}<=bids for f in owned):continue
        refs=[ref(b) for b in captions]
        proof={'version':VERSION,'kind':'closed_caption_over_parallel_choice_panels','band_ref':ref(band),
               'panel_headers':[ref(g['header']) for g in panels],
               'field_ids':[f['field_id'] for g in panels for f in g['fields']],
               'retired_fields':deepcopy(owned)}
        for g in panels:
            for f in g['fields']:
                existing=f.setdefault('instruction_refs',[])
                existing.extend(deepcopy(rr) for rr in refs if rr not in existing)
                f['matrix_instruction_evidence']=deepcopy(proof)
        for b in captions:b.setdefault('structure',{}).update(resolved_layout_role='instruction',matrix_instruction_evidence=deepcopy(proof))
        fields[:]=[f for f in fields if f not in owned]
        item={'role':'instruction','refs':refs,'text_original':original,
              'reason':'同一闭合说明带与并排选择矩阵共边；保留为原文填写说明，不生成额外空题。','diagnostic':proof}
        if non_qa is not None and not any(i.get('refs')==refs and i.get('role')=='instruction' for i in non_qa):non_qa.append(item)
        changes.append({'instruction_refs':refs,'evidence':proof})
    return changes

def _attachment_table_captions(fields,lookup):
    """A source caption over actual Attached matrices is fixed table print.

    A colon parser may have split its title into a made-up label and value.
    Preserve that derivation in diagnostics; never discard source characters.
    Optional Others answer fields without this physical table remain intact.
    """
    changes=[]
    for f in list(fields):
        if _manual(f) or f.get('children') or f.get('options'):continue
        refs=f.get('label_refs',[])+f.get('answer_value_refs',[])
        bids={r.get('block_id') for r in refs}
        if len(bids)!=1 or f.get('boundary_evidence')!='ocr_inline_delimiter_with_row_borders':continue
        b=lookup.get(next(iter(bids)),{});bb=box(b);text=b.get('text','')
        if len(bb)!=4 or not re.fullmatch(r'(?:others)?documents?to(?:be)?attachedalongwiththequestionnaire',compact(text)):continue
        if any(type(r.get('start')) is not int or type(r.get('end')) is not int or not 0<=r['start']<r['end']<=len(text) for r in refs):continue
        lanes={}
        for candidate in fields:
            for rr in attached_matrix_refs(candidate,lookup):
                header=lookup[rr['block_id']];hb=box(header)
                if page(header)==page(b) and 0<=hb[1]-bb[3]<50:
                    lanes[header['block_id']]=rr
        if len(lanes)<2:continue
        proof={'version':VERSION,'kind':'source_attachment_caption_above_parallel_attached_matrices',
               'header_refs':list(lanes.values()),'prior_derived_field':deepcopy(f)}
        b.setdefault('structure',{}).update(resolved_layout_role='section_header',source_section_evidence=proof)
        fields.remove(f);changes.append({'retired_field_id':f['field_id'],'caption_ref':ref(b),'evidence':proof})
    return changes

def reconcile_inline_source_options(fields,blocks,lookup):
    """An observed option strip after a colon is not a literal filled value."""
    from ..parser_core.pdf_caption_fragments import verified_caption_crop
    from .scanned_choice_boundaries import _set_field
    groups={};changes=[]
    for b in blocks:
        proof=b.get('structure',{}).get('scan_option_caption_crop',{})
        refs=proof.get('caption_refs',[])
        if len(refs)==1 and verified_caption_crop(b,refs[0],lookup):
            groups.setdefault((proof['container_ref']['block_id'],refs[0]['block_id']),[]).append((b,refs[0]))
    for (container_id,label_id),items in groups.items():
        if not 2<=len(items)<=8:continue
        source=lookup[label_id];text=source['text'];items.sort(key=lambda x:x[1]['start'])
        start=items[0][1]['start'];prefix=text[:start].rstrip()
        if not prefix.endswith((':','：')) or not prefix[:-1].strip():continue
        covered={i for _,r in items for i in range(r['start'],r['end'])}
        if any(c.isalnum() and i not in covered for i,c in enumerate(text) if i>=start):continue
        if any(a[1]['end']>b[1]['start'] for a,b in zip(items,items[1:])):continue
        label=ref(source,0,len(prefix));owned=[f for f in fields if any(r['block_id']==label_id for r in f.get('label_refs',[]))]
        if len(owned)!=1 or _manual(owned[0]):continue
        field=owned[0]
        if field.get('options'):continue
        controls=[(b.get('controls') or [{}])[0] for b,_ in items]
        if any(c.get('type')!='checkbox' for c in controls):continue
        options=[{'refs':[deepcopy(r)],'text_original':text[r['start']:r['end']],
                  'answer_region_refs':[ref(b)]} for b,r in items]
        old=deepcopy(field)
        _set_field(field,[label],options,lookup[container_id],lookup[container_id],'source_inline_options',lookup)
        field['boundary_evidence']='closed_scan_inline_caption_options'
        field['source_option_strip']={'version':VERSION,'container_ref':ref(lookup[container_id]),
            'caption_refs':[deepcopy(r) for _,r in items],'original_field':old}
        field.pop('form_definition',None)
        # The old colon parser's text slot described the printed options, not
        # an answer-writing range. Keep that history in the source audit only.
        structure=source.setdefault('structure',{})
        slots=structure.get('logical_slots',[])
        retired=[s for s in slots if s.get('kind')=='ocr_inline_existing_value' and s.get('start',-1)>=len(prefix)]
        if retired:
            structure.setdefault('source_option_superseded_slots',[]).extend(deepcopy(retired))
            structure['logical_slots']=[s for s in slots if s not in retired]
        changes.append({'field_id':field['field_id'],'old_field_id':old['field_id'],'old_value':old.get('answer_text_original'),
                        'physical_options':[b['block_id'] for b,_ in items]})
    return changes
