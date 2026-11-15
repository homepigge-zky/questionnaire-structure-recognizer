"""Materialize only native paragraph ranges corroborated by rendered lanes."""
from copy import deepcopy
import re
from .field_boundaries import ref
from ..parser_core.filled_word_layout import label_groups


def _protected(field):
    return bool(field.get('manual_override') or field.get('answer_origin') in {'manual','knowledge'}
        or field.get('origin') in {'manual','reviewed_knowledge'} or any(_protected(c) for c in field.get('children',[])))


def restore_filled_word_lanes(questions, blocks, lookup, new_field):
    families={}
    for answer in blocks:
        for mapping in answer.get('structure',{}).get('filled_lane_alignment',[]):
            families.setdefault((mapping['label_block'],mapping['answer_block']),[]).append(mapping)
    for (label_id,answer_id),mappings in families.items():
        if any(_protected(f) and any(r['block_id']==label_id for r in f.get('label_refs',[])) for f in questions):continue
        label,answer=lookup[label_id],lookup[answer_id]
        labels=label_groups(label)
        if len(labels)<2:continue
        source_paragraphs={p['paragraph_id']:p for p in answer['structure']['paragraph_spans']}
        by_label={(m['label_start'],m['label_end']):m for m in mappings}
        fields=[];used=set()
        for index,group in enumerate(labels):
            field=new_field(label,ref(label,group['start'],group['end']))
            mapping=by_label.get((group['start'],group['end']))
            next_start=labels[index+1]['start'] if index+1<len(labels) else len(label['text'])
            if label['text'][group['end']:next_start].strip():
                mapping=None
            if mapping:
                valid=all(pid in source_paragraphs and pid not in used for pid in mapping['answer_paragraphs'])
                if not valid:
                    field.update(answer_status='unresolved',review_status='needs_review',review_reasons=['版面引用无效或重复使用，保留字段但拒绝该填写位置'])
                    fields.append(field)
                    continue
                for pid in mapping['answer_paragraphs']:
                    p=source_paragraphs[pid];used.add(pid)
                    slot_id=pid+':rendered-value'
                    slot={'slot_id':slot_id,'kind':'native_rendered_paragraph_value' if p['text'].strip() else 'blank_paragraph_candidate',
                        'start':p['start'],'end':p['end'],'paragraph_id':pid,'paragraph_index':p['paragraph_index'],
                        'evidence':deepcopy(mapping)}
                    answer['structure'].setdefault('logical_slots',[]).append(slot)
                    region=ref(answer,p['start'],p['end'],slot_id)
                    field['answer_region_refs'].append(region)
                    if p['text'].strip():field['answer_value_refs'].append(ref(answer,p['start'],p['end']))
                field['answer_refs']=deepcopy(field['answer_value_refs'])
                field['answer_text_original']='\n'.join(answer['text'][r['start']:r['end']] for r in field['answer_value_refs'])
                field['answer_status']='filled' if field['answer_value_refs'] else 'blank'
                field['boundary_evidence']='rendered_native_parallel_paragraphs'
            else:
                field.update(answer_status='unresolved',review_status='needs_review',review_reasons=['该字段的左右原生段落尚未得到唯一版面对应，未采用相邻文字作为答案'])
            fields.append(field)
        if not fields:continue
        first_value=min((p['start'] for pid,p in source_paragraphs.items() if pid in used),default=0)
        context=[ref(answer,p['start'],p['end']) for p in source_paragraphs.values() if p['text'].strip() and p['end']<=first_value and p['paragraph_id'] not in used]
        for field in fields:field['context_refs']=deepcopy(context)
        # Keep rejected grouping in diagnostics; replace only this physical
        # family. Equal labels elsewhere retain their separate identities.
        displaced=[f for f in questions if any(r['block_id']==label_id for r in f.get('label_refs',[]))]
        label['structure']['replaced_compound_groups']=[{'field_id':f.get('field_id'),'label_refs':f.get('label_refs'),
            'answer_value_refs':f.get('answer_value_refs')} for f in displaced]
        questions[:]=[f for f in questions if f not in displaced]+fields


def restore_long_response_prompts(questions, blocks, lookup, new_field):
    """A bounded QA cell with one request and its list has one response lane.

    A colon introducing requirements is not an extra fill position. Explicit
    controls/placeholders or several separate requests are never collapsed.
    """
    for label in blocks:
        if any(_protected(f) and any(r['block_id']==label['block_id'] for r in f.get('label_refs',[])) for f in questions):continue
        s=label.get('structure',{})
        if label.get('kind')!='table_cell' or s.get('local_layout_type') not in {'question_answer','numbered_question_answer'}:continue
        if s.get('label_column')!=s.get('grid_col_start') or label.get('controls'):continue
        if any(slot.get('kind') in {'inline_placeholder','underlined_blank','inline_value_candidate'} for slot in s.get('logical_slots',[])):continue
        text=label.get('text','');lines=text.splitlines()
        requests=[line for line in lines if re.match(r'\s*(?:Please\s+(?:provide|describe|explain)|Describe\b|Explain\b|请(?:详细)?(?:说明|描述))',line,re.I)]
        if len(requests)!=1 or len(requests[0])<120 or len(lines)<3:continue
        targets=[b for b in blocks if b.get('kind')=='table_cell'
            and b.get('structure',{}).get('table_id')==s.get('table_id')
            and b.get('structure',{}).get('row_start')==s.get('row_start')
            and b.get('structure',{}).get('grid_col_start') in s.get('answer_columns',[])
            and not b.get('structure',{}).get('is_merge_continuation')]
        if len(targets)!=1 or targets[0].get('controls'):continue
        answer=targets[0]
        if answer.get('structure',{}).get('label_list'):continue
        field=new_field(label,ref(label),[ref(answer)])
        field['boundary_evidence']='bounded_qa_response_to_request_and_requirements'
        if answer['text'].strip():
            field.update(answer_text_original=answer['text'],answer_status='filled',answer_value_refs=[ref(answer)],answer_refs=[ref(answer)])
        old=[f for f in questions if any(r['block_id']==label['block_id'] for r in f.get('label_refs',[]))]
        label['structure']['replaced_prose_groups']=[{'field_id':f.get('field_id'),'label_refs':f.get('label_refs')} for f in old]
        questions[:]=[f for f in questions if f not in old]+[field]


def restore_bilingual_native_choice_lanes(questions, blocks, lookup, new_field):
    from .native_choices import _control_options
    for label in blocks:
        s=label.get('structure',{})
        if label.get('kind')!='table_cell' or s.get('local_layout_type') not in {'question_answer','numbered_question_answer'}:continue
        if s.get('label_column')!=s.get('grid_col_start') or label.get('controls'):continue
        if any(_protected(f) and any(r['block_id']==label['block_id'] for r in f.get('label_refs',[])) for f in questions):continue
        lines=label['text'].splitlines()
        english=[t for t in lines if re.search(r'[A-Za-z]{3}',t) and '?' in t]
        if len(english)!=1 or not re.search(r'[\u0370-\u03ff\u3040-\u9fff]',label['text']):continue
        if any(x.get('kind') in {'inline_placeholder','underlined_blank'} for x in s.get('logical_slots',[])):continue
        targets=[b for b in blocks if b.get('kind')=='table_cell'
            and b.get('structure',{}).get('table_id')==s.get('table_id')
            and b.get('structure',{}).get('row_start')==s.get('row_start')
            and b.get('structure',{}).get('grid_col_start') in s.get('answer_columns',[])
            and not b.get('structure',{}).get('is_merge_continuation')]
        if len(targets)!=1:continue
        answer=targets[0];controls=answer.get('controls',[])
        if len(controls)<2 or any(c.get('type')!='checkbox' for c in controls):continue
        if len({c.get('paragraph_index') for c in controls})!=1:continue
        options,issue=_control_options(answer,0,len(answer['text']),controls)
        if issue or len(options)!=len(controls):continue
        field=new_field(label,ref(label),[ref(answer)])
        field['boundary_evidence']='single_native_choice_lane_with_bilingual_prompt'
        old=[f for f in questions if any(r['block_id']==label['block_id'] for r in f.get('label_refs',[]))]
        label['structure']['replaced_choice_groups']=[{'field_id':f.get('field_id'),'label_refs':f.get('label_refs')} for f in old]
        questions[:]=[f for f in questions if f not in old]+[field]
