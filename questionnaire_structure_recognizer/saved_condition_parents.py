"""Persist verified flat native dependencies without guessing from labels."""
from collections import Counter


def attach_native_condition_parents(fields, result, protected_ids=()):
    counts=Counter(str(f.get('field_id') or f.get('question_id'))for f,_ in fields)
    lookup={str(f.get('field_id') or f.get('question_id')):f for f,_ in fields}
    blocks={b['block_id']:b for b in result.get('source_blocks',[])}
    parents={str(f.get('field_id') or f.get('question_id')):parent for f,parent in fields}
    proposals={}
    for f,current_parent in fields:
        key=str(f.get('field_id') or f.get('question_id'));definition=f.get('form_definition')or{}
        condition=definition.get('applicability_condition')or{}
        if current_parent or key in protected_ids or counts[key]!=1:continue
        if condition.get('evidence')!='native_adjacent_named_below_control':continue
        parent_key=condition.get('parent_field_id');parent=lookup.get(parent_key)
        if not parent or parent_key==key or counts[parent_key]!=1:continue
        pd=parent.get('form_definition')or{}
        if any(d.get('validation_status')!='valid' or d.get('source_hash')!=result.get('source_hash')for d in (definition,pd)):continue
        expected=str(condition.get('equals_option')or'').casefold().strip()
        if sum(str(o.get('text_original')or o.get('text')or'').casefold().strip()==expected for o in parent.get('options',[]))!=1:continue
        refs=condition.get('source_refs')or[];labels=parent.get('label_refs')or[]
        if not refs or not condition.get('source_text'):continue
        texts=[]
        for ref in refs:
            block=blocks.get(ref.get('block_id'));start=ref.get('start');end=ref.get('end')
            if (not block or type(start)is not int or type(end)is not int or not 0<=start<end<=len(block.get('text',''))
                    or not any(l.get('block_id')==ref['block_id'] and l.get('start',0)<=start<end<=l.get('end',0)for l in labels)):
                break
            texts.append(block['text'][start:end])
        else:
            if ' '.join(' '.join(texts).split())==' '.join(str(condition['source_text']).split()):proposals[key]=parent_key
    graph={**parents,**proposals}
    for key in list(proposals):
        visited={key};node=graph.get(key)
        while node:
            if node in visited:
                proposals.pop(key,None);break
            visited.add(node);node=graph.get(node)
    return [(f,proposals.get(str(f.get('field_id')or f.get('question_id')),parent))for f,parent in fields]
