"""Restore numbered native PDF form fields from physical closed regions."""
from collections import defaultdict
from copy import deepcopy
import re

from .form_completion import new_field
from .field_boundaries import ref
from ..parser_core.pdf_vector_forms import verified_vector_control

VERSION='native-vector-numbered-fields-v5-terminal-matrix-writeins'
NUMBER=re.compile(r'^\d+(?:\.\d+)+(?:[.)])?$')
PREFIX_NUMBER=re.compile(r'^(?P<number>\d+(?:\.\d+)+[.)]?)\s+')


def _number_marker(block):
    text=block.get('text','').strip()
    if NUMBER.fullmatch(text) or re.fullmatch(r'[A-Z][.)]',text):return block
    match=PREFIX_NUMBER.match(block.get('text',''))
    if not match:return None
    # A native painted number run at the left, followed by separate prompt
    # runs, proves a source range even when the extractor grouped one line.
    run=next((r for r in block.get('structure',{}).get('native_pdf_font_runs',[])
        if r['start']==0 and r['end']==match.end('number')),None)
    if not run:return None
    number=deepcopy(block);number['text']=match['number'];number['_source_number_end']=match.end('number')
    number['location']['bbox']=deepcopy(run['bbox'])
    number['_source_block']=block
    return number


def _label_ref(block,number):
    start=number.get('_source_number_end',0)if block['block_id']==number['block_id']else 0
    while start<len(block['text'])and block['text'][start].isspace():start+=1
    return ref(block,start)


def _inside(block,box):
    b=block.get('location',{}).get('bbox',[])
    return len(b)==4 and len(box)==4 and box[0]-1<=(b[0]+b[2])/2<=box[2]+1 and box[1]-1<=(b[1]+b[3])/2<=box[3]+1


def _overlap(a,b):
    return a.get('block_id')==b.get('block_id') and max(a.get('start',0),b.get('start',0))<min(a.get('end',0),b.get('end',0))


def _protected(field):
    return bool(field.get('manual_override') or field.get('answer_origin')in{'manual','knowledge'}
        or field.get('origin')in{'manual','reviewed_knowledge'} or any(_protected(f)for f in field.get('children',[])))


def _source_text(refs,lookup):
    return '\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']]for r in refs)


def _italic(block):
    runs=block.get('structure',{}).get('native_pdf_font_runs',[])
    relevant=[r for r in runs if re.search(r'[A-Za-z]',block['text'][r['start']:r['end']])]
    return bool(relevant) and all(r['flags']&2 for r in relevant)


def _split_text_response(body):
    """A completed italic prompt plus a distinct regular response paragraph.

    Font is not used alone: require a unique numbered closed form, a complete
    request/question, an actual style transition, and a separate source lane.
    """
    italic=[i for i,b in enumerate(body)if _italic(b)]
    if not italic:return None
    first=italic[0];last=first
    while last+1<len(body) and _italic(body[last+1]):last+=1
    label=body[:last+1];tail=body[last+1:]
    text='\n'.join(b['text']for b in label)
    if not re.search(r'[?？]|\b(?:please\s+(?:provide|describe|list|define|state|submit|specify)|describe|indicate|specify|provide)\b',text,re.I):return None
    if not body[last]['text'].rstrip().endswith(('.',':','：','?','？',')')):return None
    if not tail:return None
    end=body[last]['location']['bbox'];first_value=tail[0]['location']['bbox']
    if first_value[1]<end[1]+5 or not min(b['location']['bbox'][0] for b in label)-3<=first_value[0]<=max(b['location']['bbox'][0] for b in label)+12:return None
    for block in tail:
        if _italic(block) or NUMBER.fullmatch(block['text'].strip()) or re.search(r'[:：]\s*$',block['text']):return None
        if re.match(r'\s*(?:If\b|When\b|Please\b|Note\b|Example\b|For example\b|Instructions?\b|若|如果|说明|例如)',block['text'],re.I):return None
    return [ref(b)for b in label],[ref(b)for b in tail]


def _number_ref(number):
    return ref(number,0,number.get('_source_number_end',len(number['text'])))


def _strip_number(refs,number):
    return [_label_ref(number_source,number) if r['block_id']==number['block_id'] and r['start']==0 and
            (number_source:=number.get('_source_block')) else r for r in refs]


def _split_underlined_lane(body):
    """A printed fill line around an actual value in a separate right lane.

    Keep exact value spans, never infer numbers. A second prompt, example or
    overlapping text prevents this mapping. The caller proves the numbered
    closed form and the verifier repeats these exact checks.
    """
    anchors=[b for b in body if re.search(r'_{2,}',b['text'])]
    if not anchors:return None
    x=min(b['location']['bbox'][0] for b in anchors)
    labels=[b for b in body if b['location']['bbox'][2]<x-12]
    values=[b for b in body if b not in labels]
    if not labels or not values or any(b['location']['bbox'][0]<x-12 for b in values):return None
    prompt='\n'.join(b['text'] for b in labels)
    if not re.search(r'[?？]|\b(?:please|specify|state|how long|how many|define)\b',prompt,re.I):return None
    if any(re.search(r'[?？:：]|\b(?:please|example|note|instructions?)\b',b['text'],re.I) for b in values):return None
    first=min(b['location']['bbox'][1] for b in values);last=max(b['location']['bbox'][3] for b in labels)
    if first>last+12 or first<min(b['location']['bbox'][1] for b in labels)-2:return None
    vrefs=[]
    for b in sorted(values,key=lambda b:b['location']['bbox'][1]):
        m=re.search(r'[^_\s](?:.*[^_\s])?',b['text'])
        if m:vrefs.append(ref(b,m.start(),m.end()))
    return ([ref(b) for b in labels],vrefs) if vrefs else None


def _split_numbered_table(body,number):
    label=number.get('_source_block');s=(label or {}).get('structure',{})
    if not label or label.get('kind')!='table_cell' or not number.get('_source_number_end'):return None
    cells=[b for b in body if b.get('kind')=='table_cell' and b.get('structure',{}).get('table_id')==s.get('table_id')
           and b['structure'].get('row_start')==s.get('row_start') and b['structure'].get('row_end')==s.get('row_end')]
    if len(cells)!=2 or label not in cells:return None
    other=next(b for b in cells if b is not label);lb=label['location']['bbox'];rb=other['location']['bbox']
    if abs(lb[2]-rb[0])>1 or abs(lb[1]-rb[1])>1 or abs(lb[3]-rb[3])>1:return None
    if not re.search(r'[?？]|\b(?:please|report|describe|state|specify)\b',label['text'],re.I):return None
    if PREFIX_NUMBER.match(other['text']) or other.get('controls'):return None
    return [_label_ref(label,number)],[ref(other)] if other['text'].strip() else [],[ref(other)]


def _split_followup_response(body):
    """One explicit follow-up question and its continuous source paragraph."""
    prompts=[(i,b) for i,b in enumerate(body) if re.match(r'^(?:How|What|When|Which|Where|Who)\b',b['text']) and b['text'].rstrip().endswith('?')]
    if len(prompts)!=1:return None
    index,prompt=prompts[0];labels=body[:index+1];values=body[index+1:]
    if not values or any(re.search(r'[A-Za-z]',b['text']) for b in body[:index]):return None
    # A bilingual first-language prompt must itself terminate as a question;
    # this cannot simply consume a preceding free-form business statement.
    if body[:index] and not body[index-1]['text'].rstrip().endswith(('?','？',';',';')):return None
    cursor=prompt['location']['bbox'][3];x=prompt['location']['bbox'][0]
    for b in values:
        box=b['location']['bbox'];text=b['text'].strip()
        if (box[1]-cursor>10 or abs(box[0]-x)>3 or _italic(b) or re.search(r'[?？]|[:：]\s*$',text) or
                re.match(r'^(?:[-•]|If\b|When\b|Please\b|Note\b|Example\b|Instructions?\b)',text,re.I)):return None
        cursor=box[3]
    return [ref(b) for b in labels],[ref(b) for b in values]


def _split_inline_response(body):
    """An explicit italic request and regular native value can share one line.

    The surrounding numbered closed form is required by the caller. Preserve
    exact source spans; an example/instruction and a subsequent prompt are not
    answers. Font is evidence only together with the explicit request boundary.
    """
    for index,anchor in enumerate(body):
        text=anchor.get('text','');runs=anchor.get('structure',{}).get('native_pdf_font_runs',[])
        match=re.search(r'\b(?:describe|specify|provide|state|explain)\b[^:\n]{0,100}:',text,re.I)
        if not match or re.search(r'\b(?:example|note|instruction)\b',text[:match.end()],re.I):continue
        split=match.end()
        if not text[split:].strip():continue
        label_runs=[r for r in runs if r['start']<split and r['end']>match.start() and
            re.search(r'[A-Za-z]',text[max(match.start(),r['start']):min(split,r['end'])])]
        value_runs=[r for r in runs if r['end']>split and text[max(split,r['start']):r['end']].strip()]
        if not label_runs or not all(r['flags']&2 for r in label_runs) or not value_runs or any(r['flags']&2 for r in value_runs):continue
        labels=[ref(b) for b in body[:index]]+[ref(anchor,0,split)]
        values=[ref(anchor,split)];last=anchor
        for b in body[index+1:]:
            bb=b['location']['bbox'];prev=last['location']['bbox'];line=b['text'].strip()
            if (_italic(b) or line.endswith((':','：')) or re.search(r'[?？]',line) or
                re.match(r'^(?:If|When|Please|Note|Example|Instructions?)\b',line,re.I) or
                bb[1]-prev[3]>10 or abs(bb[0]-anchor['location']['bbox'][0])>12):break
            values.append(ref(b));last=b
        return labels,values
    return None


def _text_slot(block,value_ref,label_refs,region,number_ref):
    sid=f"native-boxed-value:{block['block_id']}:{value_ref['start']}:{value_ref['end']}"
    slot={'slot_id':sid,'kind':'native_pdf_boxed_source_value','start':value_ref['start'],'end':value_ref['end'],
        'bbox':deepcopy(block['location']['bbox']),'label_refs':deepcopy(label_refs),'number_ref':deepcopy(number_ref),
        'closed_region':deepcopy(region),'evidence':'numbered_closed_form_prompt_then_native_font_response','version':VERSION}
    slots=block.setdefault('structure',{}).setdefault('logical_slots',[])
    if not any(s.get('slot_id')==sid for s in slots):slots.append(slot)
    return {**value_ref,'slot_id':sid}


def _choice_options(controls,lookup):
    options=[];regions=[];values=[]
    for control in sorted(controls,key=lambda b:b['controls'][0]['mark_evidence']['outline_paint_seqno']):
        native=control['controls'][0]
        # A native checkbox has a control identity, not a character-range slot.
        # Inventing a slot_id makes the later complete form validator search
        # for a nonexistent logical slot and reject an otherwise proven box.
        rr={'block_id':control['block_id'],'start':0,'end':0,'slot_id':'','control_id':native['control_id']}
        cr=deepcopy(control['structure']['native_vector_caption_refs'])
        selected=native.get('selected')if verified_vector_control(control)else None
        options.append({'option_id':native['control_id'],'refs':cr,'text_original':_source_text(cr,lookup),'selected':selected,
            'selection_source':'native_pdf_vector','answer_region_refs':[rr]})
        regions.append(rr)
        if selected:values.extend(cr)
    return options,regions,values


def _choice_set(controls,lookup):
    captions=[_source_text(b['structure']['native_vector_caption_refs'],lookup).strip().lower().replace('/','')for b in controls]
    return set(captions)in[{'yes','no'},{'yes','no','na'},{'是','否'},{'是','否','不适用'}]and len(set(captions))==len(captions)


def _write_choice(field,controls,lookup):
    options,regions,values=_choice_options(controls,lookup)
    unknown=any(o['selected']is None for o in options)or len([o for o in options if o['selected']])>1
    field.update(options=options,answer_region_refs=regions,answer_value_refs=values,answer_refs=deepcopy(values),
        answer_text_original=_source_text(values,lookup),answer_status='unresolved'if unknown else'filled'if values else'blank')
    if unknown:field.update(review_status='needs_review',review_reasons=['原生选择区域存在未确认笔画或互斥选项同时选中，保留原文待核对。'])


def _bullet_matrix(number,body,controls,region,lookup):
    bullets=[b for b in body if re.match(r'^\s*[-•]\s*\S',b['text'])]
    if len(bullets)<2:return None
    stem=[b for b in body if b['location']['bbox'][1]<bullets[0]['location']['bbox'][1]-2]
    if not stem or not stem[-1]['text'].rstrip().endswith((':','：')) or len(stem)+len(bullets)!=len(body):return None
    rows=[]
    for bullet in bullets:
        # Caption baselines, not box baselines: some printed N/A boxes wrap.
        center=sum(bullet['location']['bbox'][1::2])/2
        local=[c for c in controls if abs(sum(c['controls'][0]['mark_evidence']['caption_bbox'][1::2])/2-center)<3]
        if local and not _choice_set(local,lookup):return None
        rows.append(local)
    if len({c['block_id']for row in rows for c in row})!=len(controls):return None
    labels=[_label_ref(b,number)for b in stem];parent=new_field(stem[0],labels[0],[])
    fid=f"pdf-numbered:{number['block_id']}"
    parent.update(field_id=fid,question_id=fid,label_refs=labels,question_refs=deepcopy(labels),question_text_original=_source_text(labels,lookup),
        question_number=number['text'].rstrip('.'),question_number_refs=[ref(number)],boundary_evidence='native_vector_numbered_form',
        boundary_version=VERSION,physical_form_region=deepcopy(region),children=[],field_type_hint='compound',answer_status='not_located')
    from .native_matrix_writeins import terminal_matrix_writein
    writein=terminal_matrix_writein(number,stem,bullets,rows,region,lookup)
    for bullet,row in zip(bullets,rows):
        lr=ref(bullet);child=new_field(bullet,lr,[],parent=fid)
        child.update(field_id=fid+':'+bullet['block_id'],question_id=fid+':'+bullet['block_id'],
            context_refs=deepcopy(labels),boundary_evidence='native_vector_numbered_form',boundary_version=VERSION,
            physical_form_region=deepcopy(region),question_number_refs=[ref(number)])
        if row:_write_choice(child,row,lookup)
        elif writein and bullet is bullets[-1]:
            child.update(answer_region_refs=[writein],answer_status='blank',
                         boundary_evidence='native_matrix_terminal_writein')
        else:child.update(answer_status='not_located',review_status='needs_review',review_reasons=['原文子项保留，但该行未找到可验证的独立答复区域。'])
        parent['children'].append(child)
    return parent


def _multi_question_choice(number,body,controls,region,lookup):
    """Multiple explicit English questions with equal ordered option rows.

    The bilingual text is preserved as context. Do not infer the language
    correspondence or map a matrix with a different number of questions.
    """
    prompts=[b for b in body if _italic(b) and re.match(r'^(?:Do|Does|Is|Are|Have|Has|Can|If\b)[A-Za-z\s,]',b['text']) and b['text'].rstrip().endswith('?')]
    rows=[]
    for c in sorted(controls,key=lambda b:b['controls'][0]['mark_evidence']['caption_bbox'][1]):
        cy=sum(c['controls'][0]['mark_evidence']['caption_bbox'][1::2])/2
        row=next((r for y,r in rows if abs(y-cy)<3),None)
        if row is None:rows.append((cy,[c]))
        else:row.append(c)
    if len(prompts)<2 or len(prompts)!=len(rows) or not all(_choice_set(row,lookup) for _,row in rows):return None
    if any(re.search(r'[A-Za-z]',b['text']) for b in body if b not in prompts):return None
    labels=[_label_ref(b,number) for b in body];fid=f"pdf-numbered:{number['block_id']}"
    parent=new_field(body[0],labels[0],[])
    parent.update(field_id=fid,question_id=fid,label_refs=labels,question_refs=deepcopy(labels),question_text_original=_source_text(labels,lookup),
        question_number=number['text'].rstrip('.'),question_number_refs=[_number_ref(number)],boundary_evidence='native_vector_compound_group',
        boundary_version=VERSION,physical_form_region=deepcopy(region),children=[],field_type_hint='compound',answer_status='not_located')
    for prompt,(_,row) in zip(prompts,rows):
        lr=ref(prompt);child=new_field(prompt,lr,[],parent=fid)
        child.update(field_id=fid+':'+prompt['block_id'],question_id=fid+':'+prompt['block_id'],context_refs=labels,
                     boundary_evidence='native_vector_numbered_form',boundary_version=VERSION,physical_form_region=deepcopy(region),question_number_refs=[_number_ref(number)])
        _write_choice(child,row,lookup);parent['children'].append(child)
    return parent


def _open_numbered_response(number,local,recovered,lookup):
    """A borderless paragraph repeating two proved numbered form layouts.

    This is explicitly a paragraph source lane, never a claimed closed box.
    Only a complete native italic request followed by its regular paragraph
    is recovered. The same-line label/answer/number sources are revalidated.
    """
    if not number.get('_source_block'):return None
    page=number['location']['page'];prefix=number['text'].split('.')[0]
    peers=[f for f in recovered if f.get('boundary_evidence')=='native_vector_numbered_form' and
           f.get('question_number','').split('.')[0]==prefix and f.get('physical_form_region',{}).get('page')==page
           and f.get('answer_value_refs') and not f.get('options')]
    if len(peers)<2:return None
    source=number['_source_block'];top=source['location']['bbox'][1];x=source['location']['bbox'][0]
    candidates=sorted([b for b in local if b.get('location',{}).get('extraction')=='native' and b.get('kind')=='paragraph' and
                       b['location']['bbox'][1]>=top-1 and b['text'].strip()],key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0]))
    body=[];bottom=top
    for b in candidates:
        bb=b['location']['bbox']
        if b['block_id']!=source['block_id'] and (_number_marker(b) or b.get('structure',{}).get('native_closed_form_regions')):break
        if bb[1]-bottom>18 or bb[0]<x-2:break
        body.append(b);bottom=max(bottom,bb[3])
    split=_split_text_response(body)
    if not split:return None
    labels,values=split;labels=_strip_number(labels,number)
    if any(_protected(f) and any(_overlap(r,c) for r in f.get('label_refs',[]) for c in labels+values) for f in recovered):return None
    targets=[];peer_refs=[deepcopy(f['question_number_refs'][0]) for f in peers]
    for vr in values:
        sid=f"native-numbered-paragraph:{vr['block_id']}:{vr['start']}:{vr['end']}"
        slot={'slot_id':sid,'kind':'native_pdf_numbered_paragraph_value','start':vr['start'],'end':vr['end'],
              'bbox':deepcopy(lookup[vr['block_id']]['location']['bbox']),'version':VERSION,'number_ref':_number_ref(number),
              'label_refs':deepcopy(labels),'value_refs':deepcopy(values),'source_refs':[ref(b) for b in body],
              'peer_number_refs':peer_refs,'evidence':'same_page_repeated_numbered_native_font_response'}
        slots=lookup[vr['block_id']].setdefault('structure',{}).setdefault('logical_slots',[])
        if not any(s.get('slot_id')==sid for s in slots):slots.append(slot)
        targets.append({**vr,'slot_id':sid})
    f=new_field(lookup[labels[0]['block_id']],labels[0],targets);fid='pdf-open-numbered:'+number['block_id']
    f.update(field_id=fid,question_id=fid,label_refs=labels,question_refs=deepcopy(labels),question_text_original=_source_text(labels,lookup),
             answer_value_refs=values,answer_refs=deepcopy(values),answer_text_original=_source_text(values,lookup),answer_status='filled',
             question_number=number['text'].rstrip('.'),question_number_refs=[_number_ref(number)],
             boundary_evidence='native_pdf_numbered_paragraph_form',boundary_version=VERSION)
    return f


def _verified_paragraph_slot(block,slot,lookup):
    if slot.get('version')!=VERSION or slot.get('evidence')!='same_page_repeated_numbered_native_font_response':return False
    refs=slot.get('source_refs',[]);nr=slot.get('number_ref',{});peer_refs=slot.get('peer_number_refs',[])
    if not refs or nr.get('block_id')not in lookup:return False
    page=block.get('location',{}).get('page')
    if any(r.get('block_id')not in lookup or lookup[r['block_id']].get('location',{}).get('extraction')!='native' or lookup[r['block_id']]['location'].get('page')!=page for r in refs+peer_refs):return False
    number=_number_marker(lookup[nr['block_id']])
    if not number:return False
    valid_peers=[r for r in peer_refs if lookup[r['block_id']].get('structure',{}).get('native_closed_form_regions') and
                 (n:=_number_marker(lookup[r['block_id']])) and n['text'].split('.')[0]==number['text'].split('.')[0]]
    if len({r['block_id'] for r in valid_peers})<2:return False
    body=[lookup[r['block_id']] for r in refs]
    split=_split_text_response(body)
    key=lambda r:(r['block_id'],r['start'],r['end'])
    return bool(split and [key(r) for r in _strip_number(split[0],number)]==[key(r) for r in slot.get('label_refs',[])] and
                [key(r) for r in split[1]]==[key(r) for r in slot.get('value_refs',[])] and
                (block['block_id'],slot.get('start'),slot.get('end')) in [key(r) for r in split[1]])


def restore_native_vector_fields(fields,blocks,lookup):
    by_page=defaultdict(list)
    for block in blocks:
        location=block.get('location',{})
        page=location.get('page')
        if (location.get('type')=='pdf' and type(page) is int and page>0
                and len(location.get('bbox') or [])==4):
            by_page[page].append(block)
    recovered=[];consumed=[]
    for page,local in by_page.items():
        regions={r['region_id']:r for b in local for r in b.get('structure',{}).get('native_closed_form_regions',
            [b['structure']['native_closed_form_region']] if b.get('structure',{}).get('native_closed_form_region') else [])}
        numbers=[n for b in local if b.get('location',{}).get('extraction')=='native' for n in [_number_marker(b)]if n]
        for number in numbers:
            candidates=[r for r in regions.values()if _inside(number,r['bbox']) and number['location']['bbox'][0]<r['bbox'][0]+75]
            if not candidates:
                paragraph=_open_numbered_response(number,local,recovered,lookup)
                if paragraph:
                    consumed.extend(paragraph['label_refs']+paragraph['answer_value_refs']+[_number_ref(number)]);recovered.append(paragraph)
                continue
            region=min(candidates,key=lambda r:(r['bbox'][2]-r['bbox'][0])*(r['bbox'][3]-r['bbox'][1]))
            next_number=min((b['location']['bbox'][1] for b in numbers if b is not number and _inside(b,region['bbox'])
                and b['location']['bbox'][1]>number['location']['bbox'][1]+5),default=region['bbox'][3])
            band=[region['bbox'][0],max(region['bbox'][1],number['location']['bbox'][1]-2),region['bbox'][2],next_number-1]
            contents=[b for b in local if b is not number and _inside(b,band)]
            controls=[b for b in contents if b.get('structure',{}).get('physical_region_kind')=='native_vector_checkbox']
            caption_refs=[r for b in controls for r in b.get('structure',{}).get('native_vector_caption_refs',[])]
            body=[b for b in contents if b.get('text','').strip() and b.get('location',{}).get('extraction')=='native'
                  and not NUMBER.fullmatch(b['text'].strip()) and not any(r['block_id']==b['block_id']for r in caption_refs)]
            body.sort(key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0]))
            if not body:continue
            area_refs=[_label_ref(b,number)for b in body]+caption_refs
            if any(_protected(old) and any(_overlap(r,c)for r in old.get('label_refs',[])for c in area_refs)for old in fields):continue
            label_refs=[];value_refs=[];answer_regions=[];options=[];inline_split=False;lane_split=False
            children=[]
            if len(controls)>3:
                matrix=_bullet_matrix(number,body,controls,region,lookup) or _multi_question_choice(number,body,controls,region,lookup)
                if matrix:
                    recovered.append(matrix);consumed.extend(area_refs+[ref(number)])
                else:
                    # A genuine closed compound area with source options may
                    # have ambiguous bilingual rows. Keep it as one unresolved
                    # source group rather than accept arbitrary model fragments
                    # borrowing one another's labels or selected values.
                    labels=[_label_ref(b,number) for b in body];f=new_field(body[0],labels[0],[]);fid=f"pdf-numbered:{number['block_id']}"
                    options,option_regions,values=_choice_options(controls,lookup)
                    f.update(field_id=fid,question_id=fid,label_refs=labels,question_refs=deepcopy(labels),question_text_original=_source_text(labels,lookup),
                        question_number=number['text'].rstrip('.'),question_number_refs=[_number_ref(number)],options=options,
                        boundary_evidence='native_vector_compound_ambiguous',boundary_version=VERSION,physical_form_region=deepcopy(region),
                        answer_status='unresolved',answer_region_refs=option_regions,answer_value_refs=[],answer_refs=[],answer_text_original='',
                        source_observation={'origin':'source_document','status':'pending_review','answer_value_refs':values},
                        review_status='needs_review',review_reasons=['同一编号区域内多组选择框与双语子项位置不能唯一对应；保留原始选中证据，禁止按列表顺序猜配。'])
                    recovered.append(f);consumed.extend(area_refs+[_number_ref(number)])
                continue
            if len(controls)in{2,3}:
                if not _choice_set(controls,lookup):continue
                english=[b for b in body if _italic(b) and '?'in b['text']]
                terminal=max((b['location']['bbox'][3]for b in english),default=None)
                prompt=[b for b in body if terminal is None or b['location']['bbox'][1]<=terminal]
                # Keep English continuation lines of the actual question.
                if english:
                    last=max(body.index(b)for b in english)
                    while last+1<len(body) and _italic(body[last+1]) and body[last+1]['location']['bbox'][1]-body[last]['location']['bbox'][3]<4:
                        last+=1
                    prompt=body[:last+1]
                label_refs=[_label_ref(b,number)for b in prompt]
                options,answer_regions,value_refs=_choice_options(controls,lookup)
                if not label_refs:continue
                tail=[b for b in body if b not in prompt]
                followup=_split_followup_response(tail)
                if followup:
                    lr,vr=followup;rr=[_text_slot(lookup[r['block_id']],r,lr,region,_number_ref(number)) for r in vr]
                    for target in rr:
                        slot=next(s for s in lookup[target['block_id']]['structure']['logical_slots'] if s['slot_id']==target['slot_id'])
                        slot.update(evidence='explicit_followup_in_numbered_form',inline_value_refs=deepcopy(vr),split_source_refs=[ref(b) for b in tail])
                    child=new_field(lookup[lr[0]['block_id']],lr[0],rr,parent=f"pdf-numbered:{number['block_id']}")
                    child.update(field_id=f"pdf-followup:{lr[0]['block_id']}",question_id=f"pdf-followup:{lr[0]['block_id']}",label_refs=lr,question_refs=deepcopy(lr),
                                 question_text_original=_source_text(lr,lookup),answer_value_refs=vr,answer_refs=deepcopy(vr),answer_text_original=_source_text(vr,lookup),answer_status='filled',
                                 boundary_evidence='native_vector_numbered_form',boundary_version=VERSION,physical_form_region=deepcopy(region),question_number_refs=[_number_ref(number)],context_refs=deepcopy(label_refs))
                    children.append(child);consumed.extend(lr+vr)
            elif controls:
                continue
            else:
                table_split=_split_numbered_table(body,number)
                split=_split_underlined_lane(body) if table_split is None else None
                lane_split=split is not None
                if split is None and table_split is None:split=_split_text_response(body)
                if split is None:
                    split=_split_inline_response(body);inline_split=split is not None
                if table_split:
                    label_refs,value_refs,answer_regions=table_split
                elif split:
                    label_refs,value_refs=split;label_refs=_strip_number(label_refs,number)
                    answer_regions=[_text_slot(lookup[r['block_id']],r,label_refs,region,_number_ref(number))for r in value_refs]
                else:continue
                if (inline_split or lane_split) and not table_split:
                    for rr in answer_regions:
                        slot=next(s for s in lookup[rr['block_id']]['structure']['logical_slots'] if s['slot_id']==rr['slot_id'])
                        slot.update(evidence='numbered_closed_form_underlined_lane' if lane_split else 'numbered_closed_form_inline_font_response',inline_value_refs=deepcopy(value_refs))
                        if lane_split:slot['split_source_refs']=[ref(b) for b in body]
            f=new_field(lookup[label_refs[0]['block_id']],label_refs[0],answer_regions)
            fid=f"pdf-numbered:{number['block_id']}"
            unknown=any(o['selected']is None for o in options) or len([o for o in options if o['selected']])>1
            f.update(field_id=fid,question_id=fid,label_refs=label_refs,question_refs=deepcopy(label_refs),
                question_text_original=_source_text(label_refs,lookup),question_number=number['text'].rstrip('.'),question_number_refs=[_number_ref(number)],
                answer_value_refs=value_refs,answer_refs=deepcopy(value_refs),answer_text_original=_source_text(value_refs,lookup),
                options=options,answer_status='unresolved'if unknown else'filled'if value_refs else'blank',
                boundary_evidence='native_vector_numbered_form',boundary_version=VERSION,
                physical_form_region={**deepcopy(region),'local_bbox':band},children=children)
            if unknown:f.update(review_status='needs_review',review_reasons=['原生选择区域存在未确认笔画或互斥选项同时选中，保留原文待核对。'])
            recovered.append(f);consumed.extend(label_refs+value_refs+caption_refs+[_number_ref(number)])
            for r in label_refs:
                lookup[r['block_id']].setdefault('structure',{}).setdefault('protected_label_ranges',[]).append({'start':r['start'],'end':r['end']})
    # Repeated record cells can themselves hold a question and Yes/No boxes.
    # They retain physical row identity; never merge the same question wording
    # across different authorities/products in the neighbouring record cells.
    for block in blocks:
        if block.get('kind')!='table_cell' or block.get('location',{}).get('extraction')!='native':continue
        controls=[c for c in blocks if c.get('structure',{}).get('physical_region_kind')=='native_vector_checkbox'
            and c.get('location',{}).get('page')==block['location'].get('page')and _inside(c,block['location']['bbox'])
            and all(r['block_id']==block['block_id']for r in c['structure'].get('native_vector_caption_refs',[]))]
        if not _choice_set(controls,lookup):continue
        caption_refs=[r for c in controls for r in c['structure']['native_vector_caption_refs']]
        end=min(r['start']for r in caption_refs)
        text=block['text'][:end].rstrip()
        if not text or not re.search(r'[?？]|\b(?:do you|have you|are you|please)\b',text,re.I):continue
        lr=ref(block,0,len(text))
        if any(_overlap(lr,c)for c in consumed)or any(_protected(old)and any(_overlap(r,lr)for r in old.get('label_refs',[]))for old in fields):continue
        physical=block.get('structure',{}).get('native_closed_form_region')
        if not physical or not all(_inside(c,physical.get('bbox',[]))for c in controls):continue
        field=new_field(block,lr,[]);field.update(boundary_evidence='native_vector_numbered_form',boundary_version=VERSION,
            physical_form_region=deepcopy(physical))
        s=block.get('structure',{});row=s.get('row_start',s.get('row'))
        field['context_refs']=[ref(b)for b in blocks if b.get('kind')=='table_cell'and b['block_id']!=block['block_id']
            and b.get('structure',{}).get('table_id')==s.get('table_id')and b.get('structure',{}).get('row_start',b.get('structure',{}).get('row'))==row and b.get('text','').strip()]
        _write_choice(field,controls,lookup);recovered.append(field);consumed.extend([lr]+caption_refs)
    if not recovered:return fields
    kept=[]
    for old in fields:
        refs=old.get('label_refs')or old.get('question_refs')or[]
        if _protected(old) or not any(_overlap(r,c)for r in refs+old.get('answer_value_refs',[])for c in consumed):kept.append(old);continue
        remaining=[]
        for r in refs:
            pieces=[(r['start'],r['end'])]
            for c in consumed:
                if c['block_id']!=r['block_id']:continue
                pieces=[part for a,z in pieces for part in[(a,min(z,c['start'])),(max(a,c['end']),z)]if part[0]<part[1]]
            remaining.extend({**r,'start':a,'end':z}for a,z in pieces if lookup[r['block_id']]['text'][a:z].strip())
        if refs:lookup[refs[0]['block_id']].setdefault('structure',{}).setdefault('vector_replaced_groups',[]).append(deepcopy(old))
        if remaining:
            residual=deepcopy(old);residual.update(label_refs=remaining,question_refs=deepcopy(remaining),question_text_original=_source_text(remaining,lookup),
                answer_region_refs=[],answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='unresolved',options=[],
                review_status='needs_review',review_reasons=['已按原生题号与填写框恢复可确定字段；剩余原文关系保留待核对。'])
            kept.append(residual)
    fields[:]=sorted(kept+recovered,key=lambda f:(lookup.get((f.get('label_refs')or[{}])[0].get('block_id'),{}).get('order',0),(f.get('label_refs')or[{}])[0].get('start',0)))
    return fields


def selected_vector_caption(value_ref,field,lookup):
    for option in field.get('options',[]):
        if option.get('selected')is not True or value_ref not in option.get('refs',[]):continue
        for rr in option.get('answer_region_refs',[]):
            block=lookup.get(rr.get('block_id'),{})
            if rr not in field.get('answer_region_refs',[])or not verified_vector_control(block):continue
            if value_ref in block['structure'].get('native_vector_caption_refs',[])and block['controls'][0]['selected']is True:return True
    return False


def verified_boxed_value_slot(block,slot,lookup):
    if slot.get('kind')=='native_pdf_numbered_paragraph_value':return _verified_paragraph_slot(block,slot,lookup)
    a,z=slot.get('start'),slot.get('end');region=slot.get('closed_region',{})
    valid=bool(slot.get('kind')=='native_pdf_boxed_source_value' and slot.get('version')==VERSION
        and block.get('location',{}).get('extraction')=='native' and isinstance(a,int)and isinstance(z,int)and 0<=a<z<=len(block.get('text',''))
        and region.get('source')=='native_vector_closed_edges' and region.get('page')==block['location'].get('page')
        and _inside(block,region.get('bbox',[])) and slot.get('number_ref',{}).get('block_id')in lookup
        and NUMBER.fullmatch(lookup[slot['number_ref']['block_id']]['text'][slot['number_ref']['start']:slot['number_ref']['end']].strip())and slot.get('label_refs')
        and all(r.get('block_id')in lookup and isinstance(r.get('start'),int) and isinstance(r.get('end'),int)
            and 0<=r['start']<r['end']<=len(lookup[r['block_id']]['text'])
            and lookup[r['block_id']].get('location',{}).get('page')==region['page']
            and _inside(lookup[r['block_id']],region['bbox']) for r in slot['label_refs']))
    if not valid:return False
    if slot.get('evidence') in {'numbered_closed_form_inline_font_response','numbered_closed_form_underlined_lane','explicit_followup_in_numbered_form'}:
        refs=slot.get('split_source_refs') or slot.get('label_refs',[])+slot.get('inline_value_refs',[]);ids=list(dict.fromkeys(r['block_id'] for r in refs))
        if any(bid not in lookup for bid in ids):return False
        splitter={'numbered_closed_form_underlined_lane':_split_underlined_lane,'explicit_followup_in_numbered_form':_split_followup_response}.get(slot['evidence'],_split_inline_response)
        split=splitter([lookup[bid] for bid in ids])
        key=lambda r:(r['block_id'],r['start'],r['end'])
        number=_number_marker(lookup[slot['number_ref']['block_id']])
        if split and number:split=(_strip_number(split[0],number),split[1])
        return bool(split and [key(r) for r in split[0]]==[key(r) for r in slot['label_refs']] and
                    [key(r) for r in split[1]]==[key(r) for r in slot['inline_value_refs']] and
                    (block['block_id'],a,z) in [key(r) for r in split[1]])
    if _italic(block):return False
    labels=[lookup[r['block_id']]for r in slot['label_refs']]
    return bool(_split_text_response(labels+[block]))
