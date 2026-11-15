"""Source-bounded Word conditional headings; never infer a current condition."""
from collections import defaultdict
from copy import deepcopy
import json
import re

VERSION = 'word-local-conditional-scope-v2-explicit-decision'
REASON = '原文局部区域以条件引导，当前父条件尚未有可核验的答复关系；保留填写位置和历史候选'
SCHEMA = '''CREATE TABLE IF NOT EXISTS questionnaire_condition_decisions (
 question_id TEXT PRIMARY KEY REFERENCES questions(id), task_id TEXT NOT NULL,
 source_hash TEXT NOT NULL, header_ref_json TEXT NOT NULL, source_text TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('unknown','applicable','not_applicable')),
 reviewer TEXT NOT NULL, updated_at TEXT NOT NULL
);'''
TAIL = re.compile(r'(?:[，,;；]\s*)?(?:若有|如有|如果有|若无|如无|如果无|若無|如無|if\s+(?:yes|no))\s*[:：]\s*$', re.I)


def _full_row(block):
    s = block.get('structure') or {}
    return (block.get('location', {}).get('type') == 'docx'
            and not s.get('is_merge_continuation') and not s.get('merge_anchor_id')
            and s.get('table_id') and s.get('cell_id')
            and s.get('row_start') == s.get('row_end')
            and s.get('grid_col_start') == 1
            and int(s.get('grid_col_end') or 0) == int(s.get('table_column_count') or 0)
            and int(s.get('table_column_count') or 0) > 1)


def local_condition_scopes(blocks, fields, source_hash):
    """Return local scopes anchored to exact physical whole-row instructions.

    Only a leading question ending in an explicit 'if yes/no:' cue is used.
    Following two-cell label/response rows stop at a new whole-row heading,
    missing row, changed grid, or table boundary. No implicit parent response
    position is manufactured from the terminal colon.
    """
    if not re.fullmatch(r'[a-fA-F0-9]{64}', source_hash or ''):
        return {}
    lookup = {b['block_id']: b for b in blocks}
    tables = defaultdict(lambda: defaultdict(dict))
    for b in blocks:
        s = b.get('structure') or {}
        if (b.get('location', {}).get('type') != 'docx' or not s.get('table_id')
                or not s.get('cell_id') or s.get('is_merge_continuation') or s.get('merge_anchor_id')):
            continue
        tables[s['table_id']][s.get('row_start')][s['cell_id']] = b
    scopes = {}
    for header in blocks:
        text = str(header.get('text') or '').strip()
        if not _full_row(header) or not TAIL.search(text) or not re.search(r'是否|能否|\b(?:is|are|do|does|did|can|has|have)\b', text, re.I):
            continue
        # Native controls are a distinct supported form, not an empty heading.
        if header.get('controls'):
            continue
        structure = header['structure']; table_id = structure['table_id']
        row = int(structure['row_end']) + 1; last_row = row - 1
        width = int(structure['table_column_count']); response_rows = {}
        pattern = None
        while row in tables[table_id]:
            cells = sorted(tables[table_id][row].values(), key=lambda b: b['structure']['grid_col_start'])
            if len(cells) != 2 or any(_full_row(b) for b in cells):
                break
            left, right = cells; ls, rs = left['structure'], right['structure']
            if (not left.get('text', '').strip() or ls.get('grid_col_start') != 1
                    or ls.get('row_end') != row or rs.get('row_end') != row
                    or rs.get('grid_col_start') != ls.get('grid_col_end', 0) + 1
                    or rs.get('grid_col_end') != width):
                break
            signature = (ls['grid_col_end'], rs['grid_col_start'], rs['grid_col_end'])
            if pattern is not None and pattern != signature:
                break
            pattern = signature; response_rows[left['block_id']] = right['block_id']
            last_row = row; row += 1
        if not response_rows:
            continue
        header_ref = {'block_id': header['block_id'], 'start': 0, 'end': len(header['text'])}
        for field in fields:
            refs = field.get('label_refs') or field.get('question_refs') or []
            if not refs or any(not isinstance(r.get('start'), int) or not isinstance(r.get('end'), int)
                               or not 0 <= r['start'] < r['end'] <= len(lookup.get(r.get('block_id'), {}).get('text', '')) for r in refs):
                continue
            labels = {r['block_id'] for r in refs}
            role = 'condition_header' if labels == {header['block_id']} else 'dependent_field'
            if role == 'dependent_field':
                if len(labels) != 1 or not labels.issubset(response_rows):
                    continue
                regions = field.get('answer_region_refs') or []
                allowed = {response_rows[bid] for bid in labels}
                if not regions or not {r.get('block_id') for r in regions}.issubset(allowed):
                    continue
            scopes[field['field_id']] = {
                'version': VERSION, 'source_hash': source_hash, 'table_id': table_id,
                'header_ref': header_ref, 'condition_text_original': header['text'],
                'row_start': structure['row_start'], 'row_end': last_row,
                'role': role, 'status': 'unresolved', 'reason': REASON,
            }
    return scopes


def attach_local_conditions(blocks, fields, source_hash):
    scopes = local_condition_scopes(blocks, fields, source_hash)
    parents={scope['header_ref']['block_id']:fid for fid,scope in scopes.items()if scope['role']=='condition_header'}
    for field in fields:
        scope = scopes.get(field['field_id'])
        definition = field.get('form_definition') or {}
        if not scope or definition.get('source_hash') != source_hash:
            continue
        definition['source_local_condition'] = deepcopy(scope)
        # This is a source constraint, not removal of a writable child region.
        definition['auto_fill_eligible'] = False
        field['auto_fill_eligible'] = False
        if scope['role']=='dependent_field' and scope['header_ref']['block_id'] in parents:
            definition['applicability_condition']={'kind':'explicit_local_condition_decision',
                'parent_field_id':parents[scope['header_ref']['block_id']],
                'source_text':scope['condition_text_original'],'header_ref':deepcopy(scope['header_ref'])}
            definition['auto_fill_eligible_if_condition_met']=bool(definition.get('validation_status')=='valid'
                and definition.get('fill_responsibility')=='our_side' and definition.get('fill_targets')
                and all(t.get('writable')for t in definition['fill_targets']))
            field['applicability_condition']=deepcopy(definition['applicability_condition'])
        definition['validation_reasons'] = list(dict.fromkeys(definition.get('validation_reasons', []) + [REASON]))
        if scope['role'] == 'condition_header' and definition.get('mapping_review_status') != 'human_corrected':
            # The colon introduces the following rows; it is not an implicit
            # Yes/No slot appended inside the protected heading itself.
            for target in definition.get('fill_targets', []):
                if (target.get('block_id') == scope['header_ref']['block_id']
                        and target.get('write_method') == 'append_after_label'):
                    target['writable'] = False
                    target['validation_reason'] = '原文冒号引出条件子区域，没有独立父题填写位置'
            if not any(t.get('writable') for t in definition.get('fill_targets', [])):
                definition['fillable_status'] = 'not_located'
                definition['allowed_write_methods'] = []
                field['fillable_status'] = 'not_located'
                field['allowed_write_methods'] = []
            if field.get('boundary_evidence')=='native_conditional_group' or definition.get('conditional_group'):
                definition.update(field_type='conditional_group',conditional_group=True,fill_targets=[],
                    fillable_status='condition_container',allowed_write_methods=[],auto_fill_eligible_if_condition_met=False,
                    validation_status='valid',validation_reasons=['原文条件分组：此标题没有答复位置；适用决定仅控制子项，不写入 Word。'])
                field.update(field_type='conditional_group',fillable_status='condition_container')
    return scopes


def group_local_conditions(fields,blocks,source_hash):
    """Make a no-write container and retain all five/six/etc. physical children."""
    from .validation.word_form_ownership import _protected
    lookup={b['block_id']:b for b in blocks}
    scopes=local_condition_scopes(blocks,fields,source_hash)
    # Native extraction may classify the whole-row prompt as a heading. Its
    # exact physical child scope still establishes a read-only container.
    parent_blocks={s['header_ref']['block_id'] for s in scopes.values() if s['role']=='condition_header'}
    for scope in list(scopes.values()):
        bid=scope['header_ref']['block_id']
        if bid in parent_blocks:continue
        local=[f for f in fields if scopes.get(f.get('field_id'),{}).get('header_ref')==scope['header_ref']]
        if not local or any(_protected(f) for f in local):continue
        child=local[0]
        from .validation.form_completion import new_field
        parent=new_field(lookup[bid],deepcopy(scope['header_ref']),[])
        parent['section_refs']=[deepcopy(r) for r in child.get('section_refs',[]) if r.get('block_id')!=bid]
        parent['section_text_original']='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in parent['section_refs'])
        fields.insert(fields.index(child),parent)
        scopes[parent['field_id']]={**deepcopy(scope),'role':'condition_header'}
        parent_blocks.add(bid)
    for parent in list(fields):
        scope=scopes.get(parent.get('field_id'),{})
        if scope.get('role')!='condition_header' or _protected(parent):continue
        children=[f for f in fields if scopes.get(f.get('field_id'),{}).get('role')=='dependent_field'
            and scopes[f['field_id']]['header_ref']==scope['header_ref']]
        if not children or any(_protected(child) for child in children):continue
        parent.update(children=children,options=[],answer_region_refs=[],answer_value_refs=[],answer_refs=[],
            answer_text_original='',answer_status='not_present',boundary_evidence='native_conditional_group',
            boundary_version=VERSION,review_reasons=[])
        for child in children:
            child['parent_question_id']=parent['field_id']
            if not _protected(child):
                child['context_refs']=list(child.get('context_refs',[]))+[deepcopy(scope['header_ref'])]
        fields[:]=[f for f in fields if f not in children]


def decision_status(db,question,definition):
    scope=definition.get('source_local_condition') or {}
    condition=definition.get('applicability_condition') or {}
    parent_field=question.get('field_id') if scope.get('role')=='condition_header' else condition.get('parent_field_id')
    if scope.get('source_hash')!=definition.get('source_hash') or not parent_field:return 'unresolved'
    parent=db.execute('SELECT q.*,d.decision,d.source_hash AS decision_source,d.header_ref_json,d.source_text AS decision_text '
        'FROM questions q LEFT JOIN questionnaire_condition_decisions d ON d.question_id=q.id '
        'WHERE q.task_id=? AND q.field_id=?',(question.get('task_id'),parent_field)).fetchone()
    if not parent:return 'unresolved'
    pd=json.loads(parent['form_definition_json']);normalize=lambda s:' '.join(str(s or '').split())
    if (not pd.get('conditional_group') or pd.get('validation_status')!='valid' or pd.get('fill_targets')
        or parent['decision_source']!=definition.get('source_hash') or parent['decision_text']!=scope.get('condition_text_original')
        or json.loads(parent['header_ref_json'] or '{}')!=scope.get('header_ref')
        or normalize(parent['question_text'])!=normalize(parent['original_text'])
        or normalize(question.get('question_text'))!=normalize(question.get('original_text'))):return 'unresolved'
    if scope.get('role')=='dependent_field' and question.get('parent_id')!=parent['id']:return 'unresolved'
    return parent['decision'] if parent['decision'] in {'applicable','not_applicable'} else 'unresolved'


def save_decision(db, question, decision, reviewer, stamp):
    """A task decision controls existing children; it is never a document answer."""
    if decision not in {'unknown', 'applicable', 'not_applicable'}:
        raise ValueError('适用状态必须为尚未确定、适用或不适用')
    definition = json.loads(question['form_definition_json'])
    scope = definition.get('source_local_condition') or {}
    ref = scope.get('header_ref') or {}
    source = db.execute('SELECT t.source_hash,b.text FROM tasks t JOIN document_blocks b ON b.task_id=t.id '
                        'WHERE t.id=? AND b.block_id=?', (question['task_id'], ref.get('block_id'))).fetchone()
    same_text = lambda a,b: ' '.join(str(a or '').split()) == ' '.join(str(b or '').split())
    if (not source or not definition.get('conditional_group') or definition.get('validation_status') != 'valid'
            or definition.get('fill_targets') or scope.get('role') != 'condition_header'
            or source['source_hash'] != definition.get('source_hash') or scope.get('source_hash') != source['source_hash']
            or ref.get('start') != 0 or ref.get('end') != len(source['text'])
            or source['text'] != scope.get('condition_text_original')
            or not same_text(question['question_text'], question['original_text'])
            or not same_text(question['original_text'], source['text'])
            or definition.get('fill_responsibility') in {'customer', 'reviewer', 'shared'}):
        raise ValueError('原文条件分组、填写责任或来源位置已改变，不能应用此适用决定；请核对原文')
    db.execute('INSERT INTO questionnaire_condition_decisions VALUES(?,?,?,?,?,?,?,?) '
               'ON CONFLICT(question_id) DO UPDATE SET source_hash=excluded.source_hash,header_ref_json=excluded.header_ref_json,'
               'source_text=excluded.source_text,decision=excluded.decision,reviewer=excluded.reviewer,updated_at=excluded.updated_at',
               (question['id'], question['task_id'], source['source_hash'], json.dumps(ref,ensure_ascii=False),
                source['text'], decision, reviewer, stamp))


def restore_saved_local_conditions(db, task_id):
    """Bind existing saved tasks from their persisted immutable source blocks."""
    task = db.execute('SELECT source_hash FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not task:
        return
    source_hash = task['source_hash']
    blocks = [dict(row) for row in db.execute('SELECT block_id,text,order_no,location_json,structure_json,controls_json FROM document_blocks WHERE task_id=?', (task_id,))]
    for b in blocks:
        b['order'] = b.pop('order_no')
        for key, default in [('location', {}), ('structure', {}), ('controls', [])]:
            b[key] = json.loads(b.pop(key + '_json') or json.dumps(default))
    rows = [dict(row) for row in db.execute('SELECT id,field_id,source_refs_json,answer_regions_json,form_definition_json FROM questions WHERE task_id=?', (task_id,))]
    fields = [{'field_id': q['field_id'], 'label_refs': json.loads(q['source_refs_json']),
               'answer_region_refs': json.loads(q['answer_regions_json']),
               'form_definition': json.loads(q['form_definition_json'])} for q in rows]
    attach_local_conditions(blocks, fields, source_hash)
    for row, field in zip(rows, fields):
        new = json.dumps(field['form_definition'], ensure_ascii=False)
        if json.loads(row['form_definition_json']) != field['form_definition']:
            db.execute('UPDATE questions SET form_definition_json=? WHERE id=?', (new, row['id']))
