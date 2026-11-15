"""Split model groups crossing independent, physically bordered PDF rows."""
from copy import deepcopy
import re
from .field_boundaries import ref
from .form_completion import new_field

VERSION='native-table-groups-v1-independent-row-ownership'


def restore_native_table_groups(fields,blocks,lookup):
    from .native_vector_forms import _protected
    from .scanned_cells import _replace_owned
    added=[];remove=[]
    def native(b):
        return (b.get('kind')=='table_cell' and b.get('location',{}).get('extraction')=='native'
                and b.get('structure',{}).get('layout_evidence')=='visible_vector_borders'
                and not b.get('structure',{}).get('is_merge_continuation'))
    def table(b):return (b['location'].get('page'),b['structure'].get('table_id'))
    for old in list(fields):
        if _protected(old) or old.get('children') or old.get('answer_value_refs'):continue
        refs=old.get('label_refs',[]);labels=[lookup.get(r['block_id'],{}) for r in refs]
        if len(labels)<2 or len({b.get('block_id') for b in labels})!=len(labels):continue
        if not all(native(b) and r.get('start')==0 and r.get('end')==len(b['text']) for b,r in zip(labels,refs)):continue
        if len({table(b) for b in labels})!=1:continue
        # Several lines in one physical cell, bilingual spans and a composite
        # prompt with shared rows must not be decomposed this way.
        if len({(b['structure'].get('row_start'),b['structure'].get('row_end')) for b in labels})!=len(labels):continue
        if not all(re.search(r'[?？]|\bplease\s+(?:list|provide|describe|state)\b',b['text'],re.I) for b in labels):continue
        foreign=[]
        for option in old.get('options',[]):
            rr=option.get('refs',[])
            if len(rr)!=1:foreign=[];break
            r=rr[0];b=lookup.get(r['block_id'],{})
            if not native(b) or table(b)!=table(labels[0]) or b in labels or r.get('start')!=0 or r.get('end')!=len(b['text']):foreign=[];break
            if not re.search(r'[:：]\s*\n',b['text']):foreign=[];break
            foreign.append(b)
        if not foreign:continue
        owned={b['block_id'] for b in labels+foreign}
        if any(_protected(f) and any(r['block_id'] in owned for r in f.get('label_refs',[])) for f in fields):continue
        lookup[labels[0]['block_id']].setdefault('structure',{}).setdefault('replaced_model_groups',[]).append(deepcopy(old))
        for b in labels+foreign:
            region=[];bb=b['location']['bbox'];s=b['structure']
            right=[c for c in blocks if native(c) and table(c)==table(b) and not c['text'].strip()
                and c['structure'].get('blank_state')=='blank' and not c.get('controls')
                and c['structure'].get('row_start')==s.get('row_start') and c['structure'].get('row_end')==s.get('row_end')
                and abs(c['location']['bbox'][0]-bb[2])<1 and abs(c['location']['bbox'][1]-bb[1])<1
                and abs(c['location']['bbox'][3]-bb[3])<1]
            if len(right)==1 and b in labels:region=[ref(right[0])]
            f=new_field(b,ref(b),region);fid='native-table-field:'+b['block_id']
            f.update(field_id=fid,question_id=fid,options=[],answer_value_refs=[],answer_refs=[],answer_text_original='',
                answer_status='blank' if region else 'not_located',boundary_evidence='native_independent_bordered_rows',boundary_version=VERSION,
                replaced_group_id=old['field_id'],section_refs=deepcopy(old.get('section_refs',[])),
                context_refs=deepcopy(old.get('context_refs',[])))
            if not region:
                f.update(review_status='needs_review',review_reasons=['已按独立物理单元格拆出原文；填写片段或选项位置尚未逐项验证，保留原文待核对。'])
            b['structure'].setdefault('protected_label_ranges',[]).append({'start':0,'end':len(b['text'])})
            added.append(f)
        _replace_owned(fields,owned,lookup)
    fields.extend(added)
    return len(added)
