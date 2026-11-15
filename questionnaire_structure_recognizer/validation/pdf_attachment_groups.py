"""Physical PDF yes/no fields with independently requested attachment groups.

The copy request, not a coincident baseline beside a licence date, owns the
attachment controls. Every association can be rebuilt from the same source.
"""
from copy import deepcopy
import re

from .field_boundaries import ref
from .form_completion import new_field
from .native_vector_forms import _write_choice, _choice_set, _protected, _overlap
from ..parser_core.pdf_vector_forms import verified_vector_control

VERSION='pdf-mixed-response-v2-native-choices-fill-lines-attachments'


def _box(block):
    return block.get('location',{}).get('bbox',[])


def _plan(label,response,blocks,lookup):
    ls,rs=label.get('structure',{}),response.get('structure',{})
    lb,rb=_box(label),_box(response)
    if (label.get('kind')!='table_cell' or response.get('kind')!='table_cell'
        or label.get('location',{}).get('type')!='pdf'
        or label.get('location',{}).get('extraction')!='native'
        or label['location'].get('page')!=response.get('location',{}).get('page')
        or ls.get('layout_evidence')!='visible_vector_borders' or rs.get('layout_evidence')!='visible_vector_borders'
        or not ls.get('table_id') or ls['table_id']!=rs.get('table_id')
        or (ls.get('row_start'),ls.get('row_end'))!=(rs.get('row_start'),rs.get('row_end'))
        or len(lb)!=4 or len(rb)!=4 or abs(lb[2]-rb[0])>1
        or abs(lb[1]-rb[1])>1 or abs(lb[3]-rb[3])>1):return None
    controls=[b for b in blocks if b.get('location',{}).get('page')==label['location']['page']
              and b.get('structure',{}).get('physical_region_kind')=='native_vector_checkbox'
              and all(rb[0]-1<=x<=rb[2]+1 for x in _box(b)[::2])
              and all(rb[1]-1<=y<=rb[3]+1 for y in _box(b)[1::2])]
    if not controls or not all(verified_vector_control(b)for b in controls):return None
    if any(any(r['block_id']!=response['block_id']for r in b['structure']['native_vector_caption_refs'])for b in controls):return None
    rows=[]
    for b in sorted(controls,key=lambda b:b['controls'][0]['mark_evidence']['caption_bbox'][1]):
        box=b['controls'][0]['mark_evidence']['caption_bbox'];cy=sum(box[1::2])/2
        if rows and abs(rows[-1][0]-cy)<2:rows[-1][1].append(b)
        else:rows.append((cy,[b]))
    if len(rows)not in {1,2} or not _choice_set(rows[0][1],lookup):return None
    if len(rows)==2:
        captions=[lookup[r['block_id']]['text'][r['start']:r['end']].strip().lower()for b in rows[1][1]
                  for r in b['structure']['native_vector_caption_refs']]
        if sorted(captions)!=['copy attached','n/a']:return None
    # An untranslated native Chinese counterpart may accompany Copy attached;
    # it is not another independent answer nor evidence of a selected state.
    covered={i for b in controls for r in b['structure']['native_vector_caption_refs']for i in range(r['start'],r['end'])}
    remainder=''.join(c for i,c in enumerate(response['text'])if i not in covered).strip()
    if re.sub(r'\s+','',remainder) not in {'','复印件已附'}:return None
    spans=sorted(ls.get('native_text_spans',[]),key=lambda s:s['start'])
    text=label['text']
    if not spans or any(not(0<=s['start']<s['end']<=len(text))for s in spans):return None
    # Every source character must belong to an observed native line.
    if any(c.strip() and not any(s['start']<=i<s['end']for s in spans)for i,c in enumerate(text)):return None
    copy_lines=[i for i,s in enumerate(spans) if re.search(r'\bplease\s+provide\s+(?:a\s+)?copy\b',text[s['start']:s['end']],re.I)]
    if (len(rows)==2 and len(copy_lines)!=1)or(len(rows)==1 and copy_lines):return None
    copy_index=copy_lines[0]if copy_lines else len(spans)
    if copy_index==0:return None
    copy_refs=ref(label,spans[copy_index]['start'],spans[-1]['end'])if copy_lines else None
    if any(re.search(r'_{2,}|[?？]',text[s['start']:s['end']])for s in spans[copy_index:]):return None
    slots=[];first_slot=copy_index
    for i,s in enumerate(spans[:copy_index]):
        raw=text[s['start']:s['end']];matches=list(re.finditer(r'_{2,}',raw))
        if not matches:continue
        if len(matches)!=1 or not raw[:matches[0].start()].strip() or raw[matches[0].end():].strip():return None
        first_slot=min(first_slot,i);m=matches[0]
        slots.append({'label_ref':ref(label,s['start'],s['start']+m.start()),
                      'region_ref':ref(label,s['start']+m.start(),s['start']+m.end()),'source_line':deepcopy(s)})
    if slots and len(slots)!=copy_index-first_slot:return None
    if not slots and not copy_lines:return None
    main_ref=ref(label,spans[0]['start'],spans[first_slot-1]['end'])
    if not re.search(r'[?？]',text[main_ref['start']:main_ref['end']]):return None
    return {'main_ref':main_ref,'copy_ref':copy_refs,'text_slots':slots,
            'main_controls':rows[0][1],'copy_controls':rows[1][1]if copy_lines else [],'response_id':response['block_id']}


def _field(label,lr,plan,role,parent=None):
    f=new_field(label,lr,[],parent)
    fid=f"pdf-attachment:{label['block_id']}:{lr['start']}:{role}"
    f.update(field_id=fid,question_id=fid,boundary_evidence='native_pdf_attachment_group',boundary_version=VERSION,
             attachment_group_evidence={'role':role,'response_id':plan['response_id'],'main_ref':deepcopy(plan['main_ref'])})
    return f


def restore_pdf_attachment_groups(fields,blocks,lookup):
    cells=[b for b in blocks if b.get('kind')=='table_cell' and b.get('location',{}).get('type')=='pdf']
    for label in cells:
        plans=[p for b in cells if b is not label and (p:=_plan(label,b,blocks,lookup))]
        if len(plans)!=1:continue
        p=plans[0];labelref=ref(label)
        if any(_protected(f) and any(_overlap(r,labelref)for r in f.get('label_refs',[]))for f in fields):continue
        parent=_field(label,p['main_ref'],p,'main');_write_choice(parent,p['main_controls'],lookup)
        for item in p['text_slots']:
            child=_field(label,item['label_ref'],p,'text',parent['field_id'])
            rr=deepcopy(item['region_ref']);sid=f"native-pdf-explicit-line:{label['block_id']}:{rr['start']}:{rr['end']}"
            slot={'slot_id':sid,'kind':'native_pdf_explicit_fill_line','start':rr['start'],'end':rr['end'],
                  'label_ref':deepcopy(item['label_ref']),'source_line':deepcopy(item['source_line']),
                  'response_id':p['response_id'],'version':VERSION}
            slots=label['structure'].setdefault('logical_slots',[])
            if not any(s.get('slot_id')==sid for s in slots):slots.append(slot)
            rr['slot_id']=sid
            child.update(answer_region_refs=[rr],answer_status='blank',context_refs=[deepcopy(p['main_ref'])])
            parent['children'].append(child)
        if p['copy_ref']:
            child=_field(label,p['copy_ref'],p,'attachment',parent['field_id']);_write_choice(child,p['copy_controls'],lookup)
            child['context_refs']=[deepcopy(p['main_ref'])];parent['children'].append(child)
        consumed=[labelref,ref(lookup[p['response_id']])]
        kept=[]
        for old in fields:
            refs=old.get('label_refs',[])
            covered=refs and all(any(c['block_id']==r['block_id'] and c['start']<=r['start']<=r['end']<=c['end']for c in consumed)for r in refs)
            if covered and not _protected(old):
                label['structure'].setdefault('attachment_group_previous_fields',[]).append(deepcopy(old))
            else:kept.append(old)
        fields[:]=kept+[parent]
    fields.sort(key=lambda f:(lookup.get((f.get('label_refs')or[{}])[0].get('block_id'),{}).get('order',0),(f.get('label_refs')or[{}])[0].get('start',0)))
    return fields


def verified_attachment_region(region,field,lookup):
    if field.get('boundary_evidence')!='native_pdf_attachment_group' or field.get('boundary_version')!=VERSION:return False
    e=field.get('attachment_group_evidence',{});labels=field.get('label_refs',[])
    if len(labels)!=1:return False
    label=lookup.get(labels[0]['block_id'],{});response=lookup.get(e.get('response_id'),{})
    p=_plan(label,response,list(lookup.values()),lookup)
    if not p or p['main_ref']!=e.get('main_ref'):return False
    role=e.get('role')
    lr=p['main_ref'] if role=='main' else p['copy_ref'] if role=='attachment' else None
    if lr!=labels[0]:return False
    for b in p['main_controls']if role=='main' else p['copy_controls']:
        if b['block_id']!=region.get('block_id'):continue
        return any(region in o.get('answer_region_refs',[]) and o.get('refs')==b['controls'][0]['caption_refs'] and
                   o.get('selected') is b['controls'][0]['selected'] for o in field.get('options',[]))
    return False


def verified_fill_line(block,slot,lookup):
    if slot.get('version')!=VERSION:return False
    p=_plan(block,lookup.get(slot.get('response_id'),{}),list(lookup.values()),lookup)
    return bool(p and any(x['label_ref']==slot.get('label_ref') and x['source_line']==slot.get('source_line') and
        (x['region_ref']['start'],x['region_ref']['end'])==(slot.get('start'),slot.get('end'))for x in p['text_slots']))


def standalone_enclosure_evidence(field,lookup):
    """A dedicated enclosure checkbox records files in this submission.

    A factual question that merely appends an attachment instruction remains
    a separate business question. Read the command from valid source ranges,
    never from a model-generated label or another question's nearby context.
    """
    options=field.get('options',[])
    names=[re.sub(r'\W','',str(o.get('text_original',''))).lower() for o in options]
    if len(options)!=3 or set(names)!={'yes','no','na'}:return []
    refs=field.get('label_refs') or field.get('question_refs') or []
    parts=[];pages=set()
    for r in refs:
        b=lookup.get(r.get('block_id'),{});text=b.get('text','');a,z=r.get('start'),r.get('end')
        if (b.get('location',{}).get('type')!='pdf' or type(a) is not int or type(z) is not int
            or not 0<=a<z<=len(text)):return []
        parts.append(text[a:z]);pages.add(b['location'].get('page'))
    if len(pages)!=1 or not parts:return []
    text='\n'.join(parts)
    prefix=r'^\s*(?:\d+(?:\.\d+)*[.)]\s*)?(?:(?:kindly|please)\s+)?'
    enclose=bool(re.match(prefix+r'enclose\b',text,re.I))
    # Attach can be a manufacturing instruction. It becomes a submission
    # declaration only with this source prompt's explicit Attached: suffix.
    attach=bool(re.match(prefix+r'attach\b[\s\S]*\battached\s*[:：]?\s*$',text,re.I))
    if not enclose and not attach:return []
    return deepcopy(refs)


def current_attachment_evidence(field,lookup):
    """A historical 'Copy attached' cannot prove this submission has a file."""
    from .pdf_submission_groups import attached_matrix_refs
    matrix_refs=attached_matrix_refs(field,lookup)
    if matrix_refs:return matrix_refs
    enclosure_refs=standalone_enclosure_evidence(field,lookup)
    if enclosure_refs:return enclosure_refs
    from ..source_pair_guards import reviewed_value_restrictions
    explicit=[]
    for r in field.get('instruction_refs') or []:
        text=lookup.get(r.get('block_id'),{}).get('text','')
        a,z=r.get('start'),r.get('end')
        if type(a) is int and type(z) is int and 0<=a<z<=len(text):
            if 'source_attachment_requires_current_confirmation' in reviewed_value_restrictions('',question=text[a:z]):
                explicit.append(deepcopy(r))
    if explicit:return explicit
    from .scanned_choice_boundaries import current_scan_attachment_evidence
    scan=current_scan_attachment_evidence(field,lookup)
    if scan:return scan
    if field.get('attachment_group_evidence',{}).get('role')!='attachment':return []
    regions=field.get('answer_region_refs',[])
    if not regions or not all(verified_attachment_region(r,field,lookup)for r in regions):return []
    return [deepcopy(r)for o in field.get('options',[]) if o.get('text_original','').strip().lower()=='copy attached'for r in o.get('refs',[])]
