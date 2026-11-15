"""Conditional underlines remain distinct from their shared matrix question."""
from copy import deepcopy
import re

from .field_boundaries import ref
from .form_completion import new_field
from .pdf_source_relations import source_text_inside_cell
from .scanned_local_tables import _contents,_physical_contains,_manual,_install,_independent_matrix_prompt

VERSION='scan-local-prompts-v1-source-conditional-rule-boundaries'
CONDITION=re.compile(r'^\s*if\s+(yes|no)\b',re.I)


def conditional_rule_plans(blocks):
    texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr']
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box']
    rules=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='write_line' and b['structure'].get('line_style')=='solid']
    plans=[]
    for cell in cells:
        local=[t for t in _contents(cell,texts) if source_text_inside_cell(t,cell,label=True)]
        conditions=[t for t in local if CONDITION.match(t['text']) and re.search(r'[:：?？]\s*$',t['text'])]
        main=_independent_matrix_prompt(local,{t['block_id'] for t in conditions})
        if not main or not conditions:continue
        for label in conditions:
            bb=label['location']['bbox'];cy=(bb[1]+bb[3])/2
            possible=[r for r in rules if _physical_contains(r,cell) and
                      0<=r['location']['bbox'][0]-bb[2]<12 and
                      r['location']['bbox'][1]-3<=cy<=r['location']['bbox'][3]+3 and
                      r['location']['bbox'][2]<cell['location']['bbox'][2]-8]
            if len(possible)==1:plans.append((cell,label,possible[0],main))
    return plans


def restore_conditional_rule_fields(fields,blocks,lookup):
    plans=conditional_rule_plans(blocks);added=[]
    for cell,label,rule,main in plans:
        ids={label['block_id'],rule['block_id']}
        if any(_manual(f) and any(r['block_id'] in ids for r in f.get('label_refs',[])+f.get('answer_region_refs',[])) for f in fields):continue
        if rule.get('structure',{}).get('blank_state')!='blank':continue
        f=new_field(label,ref(label),[ref(rule)]);fid='scan-conditional-rule:'+label['block_id']
        f.update(field_id=fid,question_id=fid,question_text_original=label['text'],label_refs=[ref(label)],question_refs=[ref(label)],
                 answer_status='blank',answer_refs=[],answer_value_refs=[],answer_text_original='',
                 boundary_evidence='closed_scan_source_subfield',boundary_version=VERSION,inline_rule_evidence_ref=ref(rule),
                 context_refs=[ref(b) for b in main],physical_label_region_ref=ref(cell))
        # A leading condition belongs to a preceding row, not the later
        # independent question sharing its cell. Record that distinction; the
        # condition remains unresolved until an explicit parent is validated.
        before=label['location']['bbox'][1]<main[0]['location']['bbox'][1]
        parent=None
        if not before:parent='scan-local-cell:'+cell['block_id']
        else:
            from .scan_closed_local_grids import shares_horizontal_border
            cb=cell['location']['bbox']
            preceding=[c for c in blocks if c.get('structure',{}).get('scan_region_kind')=='text_box' and
                       (c.get('structure',{}).get('closed_region_evidence') or {}).get('polygon') and
                       c.get('location',{}).get('page')==cell['location']['page'] and
                       abs(c['location']['bbox'][0]-cb[0])<4 and abs(c['location']['bbox'][2]-cb[2])<4 and
                       shares_horizontal_border(c,cell,cb[0],cb[2])]
            if len(preceding)==1:
                texts=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr']
                if _independent_matrix_prompt(_contents(preceding[0],texts),set()):parent='scan-local-cell:'+preceding[0]['block_id']
        f['explicit_condition']={'status':'unresolved','source_text':label['text'],'source_refs':[ref(label)],
            'parent_field_id':parent,'equals_option':CONDITION.match(label['text']).group(1).capitalize(),
            'source_relationship':'preceding_row_condition' if before else 'same_cell_preceding_question',
            'reason':'按原文保留条件，父题未作答时不自动决定是否适用。'}
        if parent:f['parent_question_id']=parent
        if _install(fields,f,[label],[],lookup):
            rule['structure'].update(scan_association_valid=True,scan_field_id=fid)
            added.append(fid)
    return {'version':VERSION,'restored_fields':added}
