"""Bind explicit native PDF questions to observed same-cell blanks/choices."""
from copy import deepcopy
import re

from .field_boundaries import ref
from .form_completion import new_field
from .native_vector_forms import _protected, _write_choice
from ..parser_core.pdf_table_positions import VERSION as OBSERVATION_VERSION
from ..parser_core.pdf_vector_forms import verified_vector_control

VERSION = 'native-table-local-positions-v1'


def _inside(bb, cell):
    return len(bb) == len(cell) == 4 and cell[0] <= bb[0] < bb[2] <= cell[2] and cell[1] <= bb[1] < bb[3] <= cell[3]


def _evidence(block, proof, source_hash=None):
    bb=block.get('location',{}).get('bbox',[])
    if len(bb)!=4:return False
    expected=[('h',bb[1],[bb[0],bb[2]]),('h',bb[3],[bb[0],bb[2]]),('v',bb[0],[bb[1],bb[3]]),('v',bb[2],[bb[1],bb[3]])]
    return (proof.get('version') == OBSERVATION_VERSION and len(proof.get('source_hash', '')) == 64
            and (not source_hash or proof.get('source_hash') == source_hash)
            and proof.get('source_text') == block.get('text')
            and proof.get('cell_bbox') == block.get('location', {}).get('bbox')
            and proof.get('page') == block.get('location', {}).get('page')
            and block.get('kind') == 'table_cell' and block.get('structure', {}).get('layout_evidence') == 'visible_vector_borders'
            and len(proof.get('observed_cell_edges', [])) == 4
            and all(e.get('drawing_indices') and (e.get('orientation'),e.get('at'),e.get('span'))==v
                    for e,v in zip(proof['observed_cell_edges'],expected)))


def verified_native_table_slot(block, slot, lookup, source_hash=None):
    s = block.get('structure', {}); kind = slot.get('kind'); proof = slot.get('observation') or {}
    if not _evidence(block, proof, source_hash) or not _inside(slot.get('bbox', []), proof.get('cell_bbox', [])): return False
    if (not proof.get('blank') or proof.get('nonwhite_pixels') != 0 or len(proof.get('pixel_sha256', '')) != 64
            or proof.get('method') != 'local_native_pdf_render_no_ocr' or slot.get('bbox') != proof.get('bbox')): return False
    a, z = slot.get('start'), slot.get('end'); text = block.get('text', '')
    if not isinstance(a, int) or not isinstance(z, int) or not 0 <= a <= z <= len(text): return False
    if kind == 'native_table_question_tail':
        return (proof == s.get('native_question_blank_tail') and a == z == len(text)
                and slot.get('label_refs') == [ref(block)] and not s.get('native_prefix_choice_ids') and not s.get('native_unassociated_square_drawings')
                and re.search(r'[?？]|\bplease\s+(?:list|provide|describe|state|specify)\b|请(?:列|提供|描述|说明)', text, re.I)
                and all(x['bbox'][3] < slot['bbox'][1] for x in s.get('native_text_spans', [])))
    if kind != 'native_table_underlined_gap' or proof not in s.get('native_underlined_blank_gaps', []): return False
    if a != proof.get('start') or z != proof.get('end') or a == z or text[a:z].strip(): return False
    parent = lookup.get(slot.get('choice_region_id'), {})
    if not verified_vector_control(parent): return False
    refs = parent['controls'][0].get('caption_refs') or []
    if len(refs) != 1 or refs[0]['block_id'] != block['block_id']: return False
    options = [lookup.get(bid, {}) for bid in s.get('native_prefix_choice_ids', [])]
    if any(not verified_vector_control(b) for b in options) or parent not in options: return False
    options.sort(key=lambda b: b['location']['bbox'][1]); index = options.index(parent)
    top = parent['location']['bbox'][1] - 1
    bottom = options[index+1]['location']['bbox'][1] - 1 if index+1 < len(options) else block['location']['bbox'][3]
    return top <= proof['bbox'][1] and proof['bbox'][3] < bottom


def _choice_plan(cell, blocks, lookup):
    if cell.get('structure',{}).get('native_unassociated_square_drawings'):return None
    ids = cell.get('structure', {}).get('native_prefix_choice_ids') or []
    controls = [lookup.get(bid, {}) for bid in ids]
    if len(controls) < 2 or len(set(ids)) != len(ids) or any(not verified_vector_control(b) for b in controls): return None
    if not _evidence(cell, cell['structure'].get('native_table_position_evidence', {})): return None
    cb = cell['location']['bbox']; source_hash = cell['structure']['native_table_position_evidence']['source_hash']
    for b in controls:
        e = b['controls'][0]['mark_evidence']; refs = b['controls'][0]['caption_refs']
        if (not _inside(b['location']['bbox'], cb) or b['location']['page'] != cell['location']['page']
                or not _evidence(cell, e.get('cell_evidence', {}), source_hash)
                or len(refs) != 1 or refs[0]['block_id'] != cell['block_id']): return None
    first = min(r['start'] for b in controls for r in b['controls'][0]['caption_refs'])
    prefix = cell['text'][:first].rstrip(); labels = []
    if prefix and prefix.endswith((':', '：')) or prefix and '\n' in prefix and re.search(r'[:：]', prefix):
        labels = [ref(cell, 0, len(prefix))]
    else:
        s = cell['structure']; lefts = []
        for b in blocks:
            bs = b.get('structure', {}); loc = b.get('location', {}); bb = loc.get('bbox', [])
            if (b.get('kind') == 'table_cell' and loc.get('page') == cell['location']['page']
                    and bs.get('layout_evidence') == 'visible_vector_borders' and bs.get('table_id') == s.get('table_id')
                    and bs.get('grid_col_end', -2) + 1 == s.get('grid_col_start')
                    and len(bb) == 4 and abs(bb[2]-cb[0]) < 1 and abs(bb[1]-cb[1]) < 1 and abs(bb[3]-cb[3]) < 1
                    and b.get('text','').strip()): lefts.append(b)
        if len(lefts) != 1 or not re.search(r'[?？]|\b(?:indicate|select|choose)\b|选择|什么', lefts[0]['text'], re.I): return None
        labels = [ref(lefts[0])]
    return labels, controls


def verified_native_table_choice(ref_, field, lookup):
    proof = field.get('native_table_choice') or {}; cell = lookup.get(proof.get('response_cell_id'), {})
    plan = _choice_plan(cell, list(lookup.values()), lookup) if cell else None
    if not plan or field.get('boundary_version') != VERSION or plan[0] != field.get('label_refs'): return False
    if ref_ not in field.get('answer_region_refs', []): return False
    control = next((b for b in plan[1] if b['block_id'] == ref_.get('block_id')), None)
    return bool(control and any(o.get('answer_region_refs') == [ref_] and o.get('refs') == control['controls'][0]['caption_refs']
                               and o.get('selected') is control['controls'][0]['selected'] for o in field.get('options', [])))


def restore_native_table_positions(fields, blocks, lookup):
    recovered = []; consumed = []
    def walk(items):
        for item in items:
            yield item
            yield from walk(item.get('children',[]))
    choice_plans={b['block_id']:p for b in blocks if (p:=_choice_plan(b,blocks,lookup))}
    choice_label_ids={r['block_id'] for p in choice_plans.values() for r in p[0]}
    for cell in blocks:
        s = cell.get('structure', {}); cb = cell.get('location', {}).get('bbox', [])
        if not s.get('native_table_position_evidence'): continue
        if any(_protected(f) and any(r['block_id'] == cell['block_id'] for r in f.get('label_refs', [])+f.get('answer_region_refs',[])) for f in walk(fields)): continue
        plan = choice_plans.get(cell['block_id'])
        if plan:
            labels, controls = plan; fid = 'native-prefix-choice:' + cell['block_id']
            f = new_field(lookup[labels[0]['block_id']], labels[0]); f.update(field_id=fid, question_id=fid,
                label_refs=deepcopy(labels), question_refs=deepcopy(labels), question_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in labels),
                boundary_evidence='native_table_prefix_choices', boundary_version=VERSION,
                native_table_choice={'response_cell_id':cell['block_id']}, physical_form_region={'source':'native_table_prefix_choices'})
            _write_choice(f, controls, lookup); recovered.append(f); consumed.extend(labels)
            consumed.append(ref(cell))
            controls.sort(key=lambda b:b['location']['bbox'][1])
            for index, control in enumerate(controls):
                top=control['location']['bbox'][1]-1
                bottom=controls[index+1]['location']['bbox'][1]-1 if index+1<len(controls) else cb[3]
                gaps=[g for g in s.get('native_underlined_blank_gaps',[]) if top<=g['bbox'][1] and g['bbox'][3]<bottom]
                if not gaps: continue
                rr=[];lr=[]
                for gap in gaps:
                    span=next((sp for sp in s.get('native_text_spans',[]) if sp['start']<=gap['start']<gap['end']<=sp['end']),None)
                    if not span: continue
                    label=ref(cell,span['start'],gap['start']);sid=f"native-underline:{cell['block_id']}:{gap['start']}:{gap['end']}"
                    slot={'slot_id':sid,'kind':'native_table_underlined_gap','start':gap['start'],'end':gap['end'],
                          'bbox':deepcopy(gap['bbox']),'label_refs':[label],'choice_region_id':control['block_id'],'observation':deepcopy(gap)}
                    if not verified_native_table_slot(cell,slot,lookup): continue
                    s['logical_slots']=[x for x in s.get('logical_slots',[]) if x.get('slot_id')!=sid]+[slot]
                    rr.append(ref(cell,gap['start'],gap['end'],sid));lr.append(label)
                if not rr: continue
                cid=fid+':supplement:'+control['block_id'];child=new_field(cell,lr[0],rr,parent=fid)
                child.update(field_id=cid,question_id=cid,label_refs=lr,question_refs=deepcopy(lr),
                    question_text_original='\n'.join(cell['text'][r['start']:r['end']].strip() for r in lr),answer_status='blank',
                    boundary_evidence='native_choice_underlined_supplement',boundary_version=VERSION,context_refs=deepcopy(labels),
                    explicit_condition={'status':'unresolved','parent_field_id':fid,'equals_option':'\n'.join(cell['text'][r['start']:r['end']] for r in control['controls'][0]['caption_refs']),
                                        'source_text':cell['text'][lr[0]['start']:lr[0]['end']]})
                f['children'].append(child)
            # Keep every additional native expression/instruction from the
            # same choice cell. Caption geometry proves the selectable values;
            # it does not authorize dropping the adjacent bilingual wording.
            used=[r for item in [f]+f['children'] for key in ('label_refs','answer_region_refs') for r in item.get(key,[])
                  if r['block_id']==cell['block_id']]
            used.extend(r for o in f['options'] for r in o.get('refs',[]) if r['block_id']==cell['block_id'])
            cursor=0;notes=[]
            for a,z in sorted([(r['start'],r['end']) for r in used]+[(len(cell['text']),len(cell['text']))]):
                if a>cursor:
                    raw=cell['text'][cursor:a]
                    if re.search(r'[A-Za-z\u3400-\u9fff]',raw):
                        start=cursor+len(raw)-len(raw.lstrip());end=a-len(raw)+len(raw.rstrip())
                        notes.append(ref(cell,start,end))
                cursor=max(cursor,z)
            f['instruction_refs']=notes
            continue
        if cell['block_id'] in choice_label_ids: continue
        # A white tail after already sourced response text is not evidence
        # that the entire field is unanswered. Keep that prior relationship
        # for ordinary pairing validation instead of replacing its value.
        if any(any(v.get('block_id')==cell['block_id'] and isinstance(v.get('start'),int) and isinstance(v.get('end'),int)
                   and 0<=v['start']<v['end']<=len(cell.get('text','')) and cell['text'][v['start']:v['end']].strip()
                   for v in old.get('answer_value_refs',[])) for old in walk(fields)):continue
        proof=s.get('native_question_blank_tail') or {}
        sid='native-table-tail:'+cell['block_id'];slot={'slot_id':sid,'kind':'native_table_question_tail',
            'start':len(cell.get('text','')),'end':len(cell.get('text','')),'bbox':deepcopy(proof.get('bbox',[])),
            'label_refs':[ref(cell)],'observation':deepcopy(proof)}
        if not verified_native_table_slot(cell,slot,lookup): continue
        s['logical_slots']=[x for x in s.get('logical_slots',[]) if x.get('slot_id')!=sid]+[slot]
        rr=ref(cell,len(cell['text']),len(cell['text']),sid)
        f=new_field(cell,ref(cell),[rr]);fid='native-table-tail:'+cell['block_id']
        f.update(field_id=fid,question_id=fid,answer_status='blank',boundary_evidence='native_closed_cell_question_tail',boundary_version=VERSION)
        recovered.append(f);consumed.append(ref(cell))
    if not recovered:return fields
    kept=[]
    for old in fields:
        refs=old.get('label_refs') or []
        if not _protected(old) and refs and all(any(r['block_id']==c['block_id'] and c['start']<=r['start']<=r['end']<=c['end'] for c in consumed) for r in refs):
            lookup[refs[0]['block_id']].setdefault('structure',{}).setdefault('native_table_position_replaced_groups',[]).append(deepcopy(old))
        else:kept.append(old)
    fields[:]=kept+recovered
    for f in walk(recovered):
        for rr in f.get('label_refs',[]):lookup[rr['block_id']].setdefault('structure',{})['protected_label_ranges']=[{'start':r['start'],'end':r['end']}for item in walk(recovered) for r in item.get('label_refs',[])if r['block_id']==rr['block_id']]
    return fields
