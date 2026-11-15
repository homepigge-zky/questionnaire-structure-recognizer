"""Resolve explicit native 'If yes, answer N' routes within one Word table.

The source number and response regions must be unique. This never synthesizes
answers, moves fields, or treats a model-suggested condition as source evidence.
"""
from collections import defaultdict
from copy import deepcopy
import re
from .word_form_ownership import _protected, _walk

VERSION = 'native-numbered-condition-route-v1'
_ROUTE = re.compile(r'\bif\s*,?\s*(yes|no)\s*[,;:]?\s*(?:please\s+)?(?:answer|complete|respond\s+to)\s+(?:(?:the\s+)?(?:question|item)\s*|the\s+)?(\d+(?:\.\d+)*)[.)]?\s*$', re.I | re.M)
_NUMBER = re.compile(r'^\s*(\d+(?:\.\d+)*)[.)]?(?=\s|$)')


def _labels(field, lookup):
    result=[]
    for reference in field.get('label_refs',[]):
        block=lookup.get(reference.get('block_id'),{})
        text=block.get('text','');start=reference.get('start');end=reference.get('end')
        if (block.get('kind')!='table_cell' or block.get('location',{}).get('type')!='docx'
            or not isinstance(start,int) or not isinstance(end,int) or not 0<=start<end<=len(text)):
            return []
        result.append((block,reference,text[start:end]))
    return result


def _response_in_table(field,table_id,lookup):
    regions=field.get('answer_region_refs',[])
    return bool(regions) and all(lookup.get(r.get('block_id'),{}).get('structure',{}).get('table_id')==table_id for r in regions)


def restore_numbered_condition_routes(fields, blocks, lookup):
    rows=[]
    for field in _walk(fields):
        labels=_labels(field,lookup)
        tables={b.get('structure',{}).get('table_id') for b,_,_ in labels}
        if len(tables)!=1 or None in tables:continue
        tid=next(iter(tables))
        if not _response_in_table(field,tid,lookup):continue
        text='\n'.join(t for _,_,t in labels)
        number=_NUMBER.match(text)
        rows.append((field,labels,tid,number.group(1) if number else None))
    proposals=defaultdict(list)
    for parent,labels,tid,_ in rows:
        if _protected(parent):continue
        routes=[(match,reference) for _,reference,text in labels for match in _ROUTE.finditer(text)]
        if len(routes)!=1:continue
        match,reference=routes[0];choice=match[1].title();number=match[2]
        options={str(o.get('text_original','')).strip().casefold() for o in parent.get('options',[])}
        if choice.casefold() not in options or not {'yes','no'}<=options:continue
        candidates=[f for f,_,t,n in rows if t==tid and n==number and f is not parent]
        for child in candidates:
            if _protected(child):continue
            condition={'parent_field_id':parent['field_id'] if len(candidates)==1 else None,
                'equals_option':choice,'source_refs':[deepcopy(reference)],'source_text':match[0].strip(),
                'status':'unresolved','evidence':'unique_native_question_number_route' if len(candidates)==1 else 'ambiguous_native_question_number_route',
                'route_version':VERSION,'target_original_number':number,
                'parent_source_text':'\n'.join(t for _,_,t in labels),
                'child_source_text':'\n'.join(t for _,_,t in _labels(child,lookup))}
            proposals[child['field_id']].append((child,condition))
    for alternatives in proposals.values():
        child=alternatives[0][0]
        if child.get('explicit_condition') and child['explicit_condition'].get('route_version')!=VERSION:continue
        condition=alternatives[0][1]
        if len(alternatives)>1:
            condition['parent_field_id']=None;condition['evidence']='multiple_native_numbered_routes'
            condition['source_refs']=[r for _,c in alternatives for r in c['source_refs']]
        child['explicit_condition']=condition


def restore_saved_numbered_conditions(db, task_id):
    """Upgrade only verified source relationships; keep all saved answer values."""
    import json
    task=db.execute('SELECT source_hash FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not task:return
    blocks=[]
    for row in db.execute('SELECT block_id,kind,text,order_no,location_json,structure_json FROM document_blocks WHERE task_id=?',(task_id,)):
        blocks.append({'block_id':row['block_id'],'kind':row['kind'],'text':row['text'],'order':row['order_no'],
                       'location':json.loads(row['location_json']),'structure':json.loads(row['structure_json'])})
    lookup={b['block_id']:b for b in blocks};fields=[];rows={}
    for raw in db.execute('SELECT * FROM questions WHERE task_id=?',(task_id,)):
        row=dict(raw);definition=json.loads(row['form_definition_json'])
        if (definition.get('source_hash')!=task['source_hash'] or row['question_text']!=row['original_text']
            or row.get('review_status')=='human_corrected' or definition.get('mapping_review_status')=='human_corrected'):
            continue
        references=json.loads(row['source_refs_json']);regions=json.loads(row['answer_regions_json'])
        if references!=definition.get('label_refs'):continue
        field={'field_id':row['field_id'],'label_refs':references,'answer_region_refs':regions,
               'options':[{'text_original':r['text']}for r in db.execute('SELECT text FROM answer_options WHERE question_id=?',(row['id'],))],
               'form_definition':definition}
        labels=_labels(field,lookup)
        original=' '.join(str(row['original_text']).split())
        if not labels or ' '.join('\n'.join(t for _,_,t in labels).split())!=original:continue
        old_condition=definition.get('applicability_condition')
        if old_condition:field['explicit_condition']=deepcopy(old_condition)
        fields.append(field);rows[field['field_id']]=row
    restore_numbered_condition_routes(fields,blocks,lookup)
    for field in fields:
        condition=field.get('explicit_condition')
        if not condition or condition.get('route_version')!=VERSION:continue
        row=rows[field['field_id']];definition=field['form_definition']
        parent=rows.get(condition.get('parent_field_id'));parent_id=parent['id'] if parent else None
        if row.get('parent_id') not in (None,parent_id):
            # A previously established different relationship is ambiguous.
            condition={**condition,'parent_field_id':None,'evidence':'conflicting_saved_parent'}
            parent_id=row['parent_id']
        condition={**condition,'source_hash':task['source_hash']}
        definition['auto_fill_eligible_if_condition_met']=bool(definition.get('auto_fill_eligible') or definition.get('auto_fill_eligible_if_condition_met'))
        definition['auto_fill_eligible']=False
        definition['applicability_condition']=condition
        if definition!=json.loads(row['form_definition_json']) or row.get('parent_id')!=parent_id:
            db.execute('UPDATE questions SET parent_id=?,form_definition_json=? WHERE id=?',
                       (parent_id,json.dumps(definition,ensure_ascii=False),row['id']))
