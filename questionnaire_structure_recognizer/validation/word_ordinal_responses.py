"""Native numbered subquestions can keep their response after a control is removed."""
from collections import defaultdict
from copy import deepcopy
import re
from .field_boundaries import ref
from .form_completion import new_field
from .word_form_ownership import _protected, _walk, _replace

VERSION='native-ordinal-response-v1-source-bounded-conditional-table'

def restore_ordinal_responses(fields,blocks,lookup):
    tables=defaultdict(list); notes=[]
    for b in blocks:
        s=b.get('structure') or {}
        if b.get('kind')=='table_cell' and b.get('location',{}).get('type')=='docx' and not s.get('is_merge_continuation'):
            tables[s.get('table_id')].append(b)
    for tid,cells in tables.items():
        first=min(b['structure']['row_start'] for b in cells)
        row=sorted([b for b in cells if b['structure']['row_start']==first],key=lambda b:b['structure']['grid_col_start'])
        if len(row)!=2 or row[0]['text'].strip() or row[0].get('controls'):
            continue
        label=lookup.get(row[1]['structure'].get('preceding_block_id'),{})
        numbering=(label.get('style') or {}).get('numbering') or {}
        marker=numbering.get('marker','')
        if label.get('kind')!='paragraph' or not re.fullmatch('[a-y][.)]',marker):continue
        next_marker=chr(ord(marker[0])+1)
        following=[b for b in cells if b['structure']['row_start']==first+1 and b['structure']['grid_col_start']==row[0]['structure']['grid_col_start']]
        if len(following)!=1 or not re.match(r'^\s*'+next_marker+r'[.)]\s+',following[0]['text']):continue
        current=[f for f in _walk(fields) if any(r['block_id']==label['block_id'] for r in f.get('label_refs',[]))]
        if any(_protected(f) for f in current):continue
        answer=row[1]
        if not current and answer['text'].strip() and not answer.get('controls') and answer['structure'].get('content_state')=='text':
            # Only this first physical row is the response to the preceding
            # ordinal. Never skip its empty label lane to a later nonempty cell.
            f=new_field(label,ref(label),[ref(answer)])
            f.update(answer_text_original=answer['text'],answer_value_refs=[ref(answer)],answer_refs=[ref(answer)],
                     answer_status='filled',boundary_evidence='native_ordinal_plain_response',boundary_version=VERSION)
            _replace(fields,label,f);current=[f]
        if not current:continue
        preceding=[lookup[x] for x in answer['structure'].get('preceding_block_ids',[]) if x in lookup and x!=label['block_id']]
        if len(preceding)<2:continue
        condition=preceding[-1];prompt=preceding[-2]
        clause=re.fullmatch(r'\s*If\s+(not\s+)?([^:：?？]{5,100})\s*[:：]\s*',condition['text'],re.I)
        if not clause or len(clause[2].split())<2 or clause[2].strip().casefold() not in prompt['text'].casefold():continue
        parents=[f for f in _walk(fields) if f.get('label_refs')==[ref(prompt)] and {o.get('text_original','').strip().casefold() for o in f.get('options',[])}=={'yes','no'}]
        if len(parents)!=1 or _protected(parents[0]):continue
        parent=parents[0]
        if not parent.get('answer_region_refs') or not all(lookup.get(r['block_id'],{}).get('controls') for r in parent['answer_region_refs']):continue
        group=[f for f in fields if f in current or (f.get('label_refs') and all(lookup.get(r['block_id'],{}).get('structure',{}).get('table_id')==tid for r in f['label_refs']))]
        pseudo=[f for f in fields if f.get('label_refs') and all(r['block_id']==condition['block_id'] for r in f['label_refs'])]
        if not group or any(_protected(f) for f in group+pseudo) or any(f.get('answer_text_original') for f in pseudo):continue
        for child in group:
            child['parent_question_id']=parent['field_id']
            child['instruction_refs']=[ref(condition)]
            child['context_refs']=[ref(prompt)]
            child['explicit_condition']={'parent_field_id':parent['field_id'],'equals_option':'No' if clause[1] else 'Yes',
                'source_refs':[ref(condition)],'source_text':condition['text'],'status':'unresolved'}
        group.sort(key=lambda f:min(lookup[r['block_id']]['order'] for r in f['label_refs']))
        parent['children'].extend(f for f in group if f not in parent['children'])
        fields[:]=[f for f in fields if f not in group+pseudo]
        condition.setdefault('structure',{})['resolved_layout_role']='instruction'
        notes.append({'role':'instruction','refs':[ref(condition)],'text_original':condition['text'],'reason':'native conditional instruction before bounded ordinal table'})
    return notes
