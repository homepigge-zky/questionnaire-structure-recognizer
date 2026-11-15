"""Explicit reviewer equivalence, bounded by the target's original context.

These grants never alter source OCR, answers or global text normalization.
Each grant is invalidated when the reviewed knowledge question/context/version
changes. Existing legacy aliases retain the original strict-context policy.
"""
import hashlib
import json
import uuid

from .semantic_fields import normalized_text, classify_field


SCHEMA = '''CREATE TABLE IF NOT EXISTS knowledge_alias_scopes(
 alias_id TEXT PRIMARY KEY, section TEXT NOT NULL, context TEXT NOT NULL,
 applicable_scope_json TEXT NOT NULL, knowledge_signature TEXT NOT NULL,
 target_evidence_json TEXT NOT NULL, reviewer TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(alias_id) REFERENCES knowledge_aliases(id));'''


def question_signature(question, task, definition):
    def refs(name):
        return question.get(name) if name in question else json.loads(question.get(name+'_json') or '[]')
    value = {'question_text':question['question_text'],'section':question.get('section',''),
        'context':definition.get('semantic_context',''),'source_hash':definition.get('source_hash'),
        'label_refs':refs('source_refs'),'answer_regions':refs('answer_regions'),
        'scope':{key:task.get(key,'') for key in ('product','business_scope','customer_name','company','factory')},
        'position_validation':{key:definition.get(key) for key in ('validation_status','fill_responsibility',
            'field_type','auto_fill_eligible','applicability_condition','requires_signatory_confirmation',
            'requires_current_person_confirmation','requires_current_attachment_confirmation')}}
    if definition.get('source_part_route'):value['source_part_route']=definition['source_part_route']
    if definition.get('whole_record_requirement'):value['whole_record_requirement']=definition['whole_record_requirement']
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def signature(knowledge):
    source = json.loads(knowledge.get('source_location_json') or '{}')
    value = {key:knowledge.get(key) for key in ('id','canonical_question','section','context','answer','answer_version','applicable_scope_json')}
    value['source'] = {key:source.get(key) for key in ('source_hash','field_id','task_version','version',
        'question_refs','label_refs','answer_refs','answer_region_refs','answer_value_refs','context_refs','record_identity')}
    if source.get('source_part_route'):value['source']['source_part_route']=source['source_part_route']
    if source.get('whole_record_requirement'):value['source']['whole_record_requirement']=source['whole_record_requirement']
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def load_scoped_aliases(db):
    result = {}
    for row in db.execute('SELECT a.knowledge_id,a.alias,s.* FROM knowledge_aliases a JOIN knowledge_alias_scopes s ON s.alias_id=a.id WHERE a.confirmed=1'):
        result.setdefault(row['knowledge_id'],[]).append(dict(row))
    return result


def semantic_conflict(knowledge, question, task, definition):
    q = classify_field(question['question_text'],question.get('section',''),definition.get('semantic_context',''),task=task)
    k = classify_field(knowledge['canonical_question'],knowledge.get('section',''),knowledge.get('context',''),scope=json.loads(knowledge.get('applicable_scope_json') or '{}'))
    for key in ('canonical_field_key','entity_type','entity_id'):
        if q.get(key) and k.get(key) and q[key]!=k[key]:return '原问题对应的标准字段或业务对象不同，不能确认等价。'
    for key in ('address_use','person_role','contact_role'):
        a,z=q['qualifiers'].get(key),k['qualifiers'].get(key)
        if a and z and a!=z:return '地址用途、人员角色或联系方式所属角色不同，不能确认等价。'
    return ''


def matches_scope(grants, knowledge, question, task, definition):
    """An explicit exact target-context grant, never fuzzy equivalence."""
    qtext = normalized_text(question['question_text'])
    section = normalized_text(question.get('section',''))
    context = normalized_text(definition.get('semantic_context',''))
    for grant in grants:
        if (normalized_text(grant['alias']) != qtext or grant['knowledge_signature'] != signature(knowledge)
                or normalized_text(grant['section']) != section or normalized_text(grant['context']) != context):
            continue
        if semantic_conflict(knowledge,question,task,definition):continue
        scope = json.loads(grant['applicable_scope_json'])
        if all(normalized_text(str(task.get(key,''))) == normalized_text(str(value)) for key,value in scope.items()):
            return True
    return False


def confirm_equivalence(db, task_id, question_id, payload, stamp):
    from .workflow import json_value, _scope_matches, knowledge_choice_selection, whole_record_coverage_reason
    if payload.get('confirmed_equivalent') is not True or not str(payload.get('reviewer','')).strip():
        raise ValueError('请明确确认两个问法在当前章节、父题和适用范围内完整等价，并填写审核人。')
    question = db.execute('SELECT * FROM questions WHERE id=? AND task_id=?',(question_id,task_id)).fetchone()
    task = db.execute('SELECT * FROM questionnaire_tasks WHERE id=?',(task_id,)).fetchone()
    record = db.execute('SELECT * FROM knowledge_records WHERE id=?',(payload.get('knowledge_id'),)).fetchone()
    if not question or not task or not record:raise ValueError('当前题目或历史答案不存在。')
    question,task,record = dict(question),dict(task),dict(record)
    if payload.get('answer_version') != record['answer_version']:raise RuntimeError('历史答案版本已变化，请重新查看后确认。')
    if record['review_status']!='approved' or not record['active'] or not record['allow_auto_reuse']:
        raise ValueError('历史答案必须已审核、有效并允许自动复用，才能确认同义关系。')
    if payload.get('expected_knowledge_signature') != signature(record):
        raise RuntimeError('历史问题、上下文或来源位置已变化，请重新查看后确认。')
    from .knowledge import answer_value_consistency_reasons
    from .source_pair_guards import reviewed_value_restrictions, restriction_reason
    codes = reviewed_value_restrictions(record['answer'], json_value(record.get('source_location_json'),{}).get('reuse_restrictions'), record['canonical_question'])
    if codes:
        raise ValueError(restriction_reason(codes))
    issues = answer_value_consistency_reasons(record['canonical_question'], record['answer'])
    if issues:
        raise ValueError(' '.join(issues))
    ok,reason = _scope_matches(json_value(record['applicable_scope_json'],{}),task)
    if not ok:raise ValueError(reason)
    definition = json_value(question['form_definition_json'],{})
    incomplete_record = whole_record_coverage_reason(definition,record)
    if incomplete_record:raise ValueError(incomplete_record)
    if payload.get('expected_question_signature') != question_signature(question,task,definition):
        raise RuntimeError('当前问题、填写位置或适用范围已变化，请刷新对照后重新确认。')
    conflict = semantic_conflict(record,question,task,definition)
    if conflict:raise ValueError(conflict)
    if (definition.get('validation_status')!='valid' or not definition.get('auto_fill_eligible')
            or definition.get('fill_responsibility')!='our_side'
            or definition.get('requires_signatory_confirmation') or definition.get('requires_current_person_confirmation')
            or definition.get('requires_current_attachment_confirmation') or definition.get('field_type')=='manual_signature'):
        raise ValueError('当前填写位置、责任或条件尚未通过校验，不能授权自动复用。')
    from .workflow import resolved_applicability
    from .form_part_routes import incompatible_historical_route, historical_route
    historical = historical_route(db,json_value(record.get('source_location_json'),{}),record['section'],record['context'])
    route=definition.get('source_part_route')
    if resolved_applicability(db,question,definition)!='applicable' or incompatible_historical_route(route,historical):
        raise ValueError('当前分部的填写条件或历史分部不同，不能授权自动复用。')
    knowledge_choice_selection(db,question_id,record['answer'])
    document = db.execute('SELECT source_hash FROM documents WHERE task_id=?',(task_id,)).fetchone()
    scope = {key:task[key] for key in ('product','business_scope','customer_name') if task.get(key)}
    evidence = {'task_id':task_id,'question_id':question_id,'field_id':question['field_id'],
        'source_hash':document['source_hash'] if document else None,'question_text':question['question_text'],
        'section':question['section'],'context':definition.get('semantic_context',''),
        'label_refs':json_value(question['source_refs_json'],[]),
        'context_refs':definition.get('display_context_refs',[]),
        'knowledge_id':record['id'],'answer_version':record['answer_version'],
        'review_reason':str(payload.get('review_reason',''))[:3000]}
    alias_id = uuid.uuid4().hex
    scope_json = json.dumps(scope,ensure_ascii=False,sort_keys=True)
    db.execute('INSERT INTO knowledge_aliases VALUES(?,?,?,?,?,?)',(alias_id,record['id'],question['question_text'],definition.get('semantic_context',''),scope_json,1))
    db.execute('INSERT INTO knowledge_alias_scopes VALUES(?,?,?,?,?,?,?,?)',(alias_id,question['section'],definition.get('semantic_context',''),scope_json,signature(record),json.dumps(evidence,ensure_ascii=False),str(payload['reviewer']).strip(),stamp))
    return {'alias_id':alias_id,'knowledge_id':record['id'],'answer_version':record['answer_version'],
            'section':question['section'],'context':definition.get('semantic_context',''),'applicable_scope':scope}
