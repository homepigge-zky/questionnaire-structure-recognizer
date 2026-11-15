"""Bind printed conditional Part instructions to native headings and choices."""
import json
import re
from functools import lru_cache
from pathlib import Path

from .choice_identity import selected_labels


HEADER = re.compile(r'^part\s+([a-z]|\d{1,2})[.:]?$', re.I)
DIRECTIVE = re.compile(r'^if\s+(yes|no)\s*[:,]\s*(?:please\s+)?(?:complete|fill\s+in)\s+part\s+([a-z]|\d{1,2})\b(.*)$', re.I)
LOCAL_NUMBER = re.compile(r'^([a-z])\s*[-–.]\s*\d+', re.I)
CONDITION = re.compile(r'\b(?:applicable|only\s+if|if\s+(?:yes|no|different|not)|for\s+.+?\s+only)\b|(?:仅适用|适用于|如为|若为|如果)|\(\s*if\s*(?:not\s*(?:identical|the\s*same)|different\s*(?:from|than))', re.I)


def different_contact_scope(first, second):
    return first != second and bool(CONDITION.search(first) or CONDITION.search(second))


def alternate_entity_condition(section):
    return bool(re.search(r'\(\s*if\s*(?:not\s*(?:identical\s*(?:with|to)|the\s*same\s*as)|different\s*(?:from|than))', section or '', re.I))


def explicit_followup_polarity(label):
    """Recognize a dependent prompt, without matching the tail of a main question."""
    text=(label or '').strip()
    english=re.match(r'^if\s*(yes|no)(?=\s*[:,，：;；.。]|\s+(?:please|describe|give|provide|specify|which|when|where|explain)\b)',text,re.I)
    if english:return english.group(1).casefold()
    chinese=re.match(r'^(?:如果|若|如)(?:为|為|回答)?(是|否)(?=\s*[:,，：;；。]|请|請)',text)
    if chinese:return {'是':'yes','否':'no'}[chinese.group(1)]
    # A named antecedent (e.g. 如有外包，是否...) is still conditional.
    # Require a leading conditional clause, its delimiter and a dependent
    # question/request. This never guesses the parent from nearby text or the
    # model's conditional_hint; an absent verified relation stays unresolved.
    named=re.match(r'^(?:如果|假如|若|如)\s*(不存在|没有|沒有|未有|无|無|存在|有)'
                   r'[^，,：:。；;?？\n]{0,100}[，,：:]\s*'
                   r'(?:是否|请|請|能否|如何|说明|說明|提供|填写|填寫)',text)
    if named:return 'yes' if named.group(1) in {'有','存在'} else 'no'
    return ''


def _text(block):
    return ' '.join(block.get('text', '').strip().split())


def _position(block):
    loc = block.get('location') or {}
    box = loc.get('bbox') or []
    if loc.get('type') == 'pdf' and len(box) == 4:
        return (loc['page'], box[1], box[0], block.get('order',0))
    return (0, block.get('order',0), 0, block.get('order',0))


def source_part_routes(blocks, fields):
    """A branch needs both printed instructions, both headings and a nearby parent.

    Ambiguous controllers/directives leave the affected part unresolved. A bare
    Part heading in an ordinary document is not a conditional branch.
    """
    text_blocks = sorted((b for b in blocks if _text(b)), key=_position)
    marks = []
    instructions = []
    lookup = {b['block_id']: b for b in blocks}
    for block in text_blocks:
        text = _text(block)
        directive = DIRECTIVE.fullmatch(text)
        heading = HEADER.fullmatch(text)
        if directive:
            instructions.append((block, directive))
        elif heading:
            marks.append((block, heading.group(1).upper()))
    if not instructions or not marks:
        return {}
    fields_at = []
    for f in fields:
        refs = f.get('label_refs') or f.get('question_refs') or []
        labels = [lookup[r['block_id']] for r in refs if r.get('block_id') in lookup and _text(lookup[r['block_id']])]
        if labels:
            fields_at.append((min(_position(b) for b in labels), f))
    fields_at.sort(key=lambda item: item[0])
    first = _position(instructions[0][0]); last = _position(instructions[-1][0])
    values = {m.group(1).lower(): m.group(2).upper() for _, m in instructions}
    named = {m.group(2).upper() for _, m in instructions}
    complete = (len(instructions) == 2 and set(values) == {'yes','no'} and len(named) == 2
                and first[0] == last[0] and named <= {tag for b,tag in marks if _position(b) > last})
    # A second instruction for the current actor is ambiguous. Only the
    # explicit delegation 'have Part ... completed by ...' has a different actor.
    complete = complete and all(not tail.strip(' .,;:') or re.match(
        r'^[,;\s]*(?:and\s+)?have\s+part\s+\w+\s+completed\s+by\b', tail, re.I)
        for _, m in instructions for tail in [m.group(3)])
    before = [(pos, f) for pos, f in fields_at if pos < first and pos[0] == first[0]]
    parent = before[-1][1] if before else {}
    labels = {str(o.get('text_original') or o.get('text','')).strip().lower() for o in parent.get('options') or []}
    if (not {'yes','no'} <= labels or (parent.get('form_definition') or {}).get('validation_status') != 'valid'
            or len(before) > 1 and before[-1][0] == before[-2][0]):
        complete = False
    if before:
        start = before[-1][0]
        # Do not connect instructions to a remote question or across independent prose.
        if first[1] - start[1] > (115 if first[0] else 12):
            complete = False
        label_blocks = [lookup[r['block_id']] for r in parent.get('label_refs') or [] if r['block_id'] in lookup]
        option_refs = [r['block_id'] for opt in parent.get('options') or [] for r in opt.get('refs') or []]
        # In Word matrices the printed option caption and its actual checkbox
        # are separate cells. A verified mark belongs to the parent, not to
        # unrelated intervening prose. Unknown text in that cell still blocks
        # a route rather than being silently treated as a checked option.
        mark_refs = {r['block_id'] for opt in parent.get('options') or []
                     for r in opt.get('answer_region_refs') or [] if r.get('block_id') in lookup
                     and re.fullmatch(r'[☒☑✓✔✕✖XxVv√]+', _text(lookup[r['block_id']]))}
        own = {b['block_id'] for b in label_blocks} | set(option_refs) | mark_refs
        controls = [lookup[r['block_id']]['location'].get('bbox') for opt in parent.get('options') or []
                    for r in opt.get('answer_region_refs') or [] if r.get('block_id') in lookup]
        def same_parent_content(b):
            if b['block_id'] in own:
                return True
            box = b['location'].get('bbox') or []
            if len(box) != 4:
                return False
            if _text(b).casefold() in labels and any(c and box[1] < c[3] and box[3] > c[1]
                    and min(abs(box[2]-c[0]),abs(box[0]-c[2])) <= 18 for c in controls):
                return True
            # OCR can see a checkbox as a single character. Its ink must lie
            # within one of this parent's actual control rectangles.
            if len(_text(b)) == 1 and any(c and box[0]>=c[0]-2 and box[1]>=c[1]-2
                    and box[2]<=c[2]+2 and box[3]<=c[3]+2 for c in controls):
                return True
            if re.fullmatch(r'[(（]?(?:[a-z]|\d{1,2})[.)）]', _text(b), re.I):
                return any(len(c)==4 and box[2] <= c[0] and box[1] < c[3] and box[3] > c[1]
                           for x in label_blocks for c in [x['location'].get('bbox') or []])
            return False
        if any(start < _position(b) < first and not same_parent_content(b) for b in text_blocks):
            complete = False
    headings = [(b, tag) for b, tag in marks if _position(b) > last]
    numbered = [(b, LOCAL_NUMBER.match(_text(b))) for b in text_blocks if LOCAL_NUMBER.match(_text(b))]
    routes = {}
    parent_text = parent.get('question_text_original', '')
    identifies_manufacturer = bool(re.search(r'\bis\s+your\s+company\s+the\s+manufacturer\s+of\b', parent_text, re.I))
    for pos, f in fields_at:
        preceding = [(b,tag) for b,tag in headings if _position(b) <= pos]
        if not preceding or preceding[-1][1] not in named:
            continue
        header, part = preceding[-1]
        accepted = [v for v,dest in values.items() if dest == part] if complete else []
        unresolved = not complete
        local_refs = []
        local = [b for b,m in numbered if m.group(1).upper() == part and _position(header) <= _position(b) <= pos]
        if local:
            compact = re.sub(r'\s+', '', _text(local[-1]).casefold())
            if 'manufacturer' in compact and re.search(r'\(if(?:notidenticalwith|differentfrom)(?:the)?supplier\)', compact):
                local_refs.append(local[-1]['block_id'])
                if identifies_manufacturer:
                    accepted = [v for v in accepted if v == 'no']
                else:
                    unresolved = True
        routes[f['field_id']] = {'part':part, 'parent_field_id':parent.get('field_id'),
            'parent_source_text':parent_text, 'accepted_parent_answers':accepted, 'unresolved':unresolved,
            'heading_block_id':header['block_id'], 'instruction_block_ids':[b['block_id'] for b,_ in instructions],
            'local_condition_refs':local_refs}
    return routes


def route_status(db, question, route):
    if not route:
        return 'applicable'
    if route.get('unresolved') or not route.get('parent_field_id'):
        return 'unresolved'
    parent = db.execute('SELECT q.id,q.question_text,a.selected_options_json FROM questions q JOIN answers a ON a.question_id=q.id WHERE q.task_id=? AND q.field_id=?',
                        (question['task_id'], route['parent_field_id'])).fetchone()
    if not parent or parent['question_text'] != route['parent_source_text']:
        return 'unresolved'
    opts = [dict(x) for x in db.execute('SELECT text,value FROM answer_options WHERE question_id=? ORDER BY option_order',(parent['id'],))]
    try:
        chosen = selected_labels(opts,json.loads(parent['selected_options_json']))
    except (ValueError, TypeError):
        return 'unresolved'
    if len(chosen) != 1 or chosen[0].strip().casefold() not in {'yes','no'}:
        return 'unresolved'
    return 'applicable' if chosen[0].strip().casefold() in route['accepted_parent_answers'] else 'not_applicable'


def incompatible_historical_route(current, historic):
    if not historic:
        return False
    if not current or historic.get('unresolved'):
        return True
    return any(current.get(key) != historic.get(key) for key in ('part','parent_source_text','accepted_parent_answers'))


def _identity(refs):
    return sorted((r.get('block_id',''),r.get('start',-1),r.get('end',-1)) for r in refs or [])


@lru_cache(maxsize=24)
def _archived_routes(path, modified_ns, size, source_hash):
    # The persisted result is the original immutable analysis version. Keep
    # only the small routing index in memory, keyed by its physical revision.
    result = json.loads(Path(path).read_text(encoding='utf8'))
    if result.get('source_hash') != source_hash:
        return {},{}
    fields = []
    stack = list(result.get('questions') or [])
    while stack:
        field = stack.pop()
        fields.append(field);stack.extend(field.get('children') or [])
    routes = source_part_routes(result.get('source_blocks') or [], fields)
    index, labels = {},{}
    for field in fields:
        route = routes.get(field.get('field_id'))
        if not route:continue
        refs = field.get('label_refs') or field.get('question_refs') or []
        index[field['field_id']] = (_identity(refs),route)
        for ref in refs:labels[ref['block_id']]=route['part']
    headings = {v['heading_block_id']:v['part'] for v in routes.values()}
    active = None
    for block in sorted(result.get('source_blocks') or [], key=_position):
        if HEADER.fullmatch(_text(block)):
            active=headings.get(block['block_id'])
        if active:labels[block['block_id']]=active
    return index,labels


def historical_route(db, source, section='', context='', source_cache=None):
    """Use the exact archived source or hold known legacy Part data for review."""
    if source.get('source_part_route'):
        return source['source_part_route']
    declared = re.search(r'(?:^|\n)\s*part\s+([a-z]|\d{1,2})(?:\s*[:.)-]|\s|$)',
                         f'{section}\n{context}',re.I)
    known = declared.group(1).upper() if declared else None
    cache = source_cache if source_cache is not None else {}
    key = source.get('task_id'),source.get('task_version')
    if all(key) and source.get('field_id'):
        if 'versions_available' not in cache:
            names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            cache['versions_available']={'versions','tasks'} <= names
        if key not in cache:
            version = (db.execute('SELECT v.result_file,t.source_hash FROM versions v JOIN tasks t ON t.id=v.task_id WHERE v.task_id=? AND v.version=?',key).fetchone()
                       if cache['versions_available'] else None)
            index, labels, digest = {},{},None
            dbfile = next((r[2] for r in db.execute('PRAGMA database_list') if r[1]=='main'),'')
            if version and dbfile:
                base = Path(dbfile).resolve().parent
                path = (base/version['result_file']).resolve()
                if path.is_relative_to(base) and path.is_file():
                    try:
                        stat=path.stat();digest=version['source_hash']
                        index,labels=_archived_routes(str(path),stat.st_mtime_ns,stat.st_size,digest)
                    except (OSError,ValueError,KeyError,TypeError):
                        pass
            cache[key]=index,labels,digest
        index,labels,digest=cache[key]
        refs=source.get('question_refs') or source.get('label_refs') or []
        exact=index.get(source['field_id'])
        if exact:
            expected,route=exact
            known=route['part']
            if refs and _identity(refs)==expected and source.get('source_hash',digest)==digest:
                return route
        for ref in refs:
            if ref.get('block_id') in labels:known=labels[ref['block_id']]
    if known:
        return {'part':known,'unresolved':True,'reason':'historical_part_mapping_requires_source_review'}
    return None


def dependency_order(rows):
    """Match saved controllers first while preserving the public document order."""
    by_field = {r['field_id']:r for r in rows}
    ordered = []
    seen = set()
    def visit(row):
        field_id=row['field_id']
        if field_id in seen:return
        seen.add(field_id)
        definition=json.loads(row['form_definition_json'] or '{}')
        for key in ('applicability_condition','source_part_route'):
            parent_id=(definition.get(key) or {}).get('parent_field_id')
            if parent_id in by_field:visit(by_field[parent_id])
        ordered.append(row)
    for row in rows:visit(row)
    return ordered


def restore_existing_routes(db, task_id):
    """Recognize routes for older saved tasks, without reparsing their files."""
    from .local_condition_scopes import restore_saved_local_conditions
    restore_saved_local_conditions(db, task_id)
    from .validation.numbered_condition_routes import restore_saved_numbered_conditions
    restore_saved_numbered_conditions(db, task_id)
    if not db.execute("SELECT 1 FROM document_blocks WHERE task_id=? AND lower(trim(text)) LIKE 'if%part %' LIMIT 1",(task_id,)).fetchone():
        return
    if db.execute("SELECT 1 FROM questions WHERE task_id=? AND form_definition_json LIKE '%\"source_part_route\"%' LIMIT 1",(task_id,)).fetchone():
        return
    blocks = [{'block_id':r['block_id'],'order':r['order_no'],'text':r['text'],'location':json.loads(r['location_json'])}
              for r in db.execute('SELECT block_id,order_no,text,location_json FROM document_blocks WHERE task_id=?',(task_id,))]
    rows = [dict(r) for r in db.execute('SELECT * FROM questions WHERE task_id=?',(task_id,))]
    fields = []
    for row in rows:
        regions = json.loads(row['answer_regions_json'])
        options = [{'text':o['text'],'answer_region_refs':regions} for o in db.execute(
            'SELECT text FROM answer_options WHERE question_id=? ORDER BY option_order',(row['id'],))]
        fields.append({'field_id':row['field_id'],'label_refs':json.loads(row['source_refs_json']),'form_definition':json.loads(row['form_definition_json']),
                       'question_text_original':row['original_text'],'options':options})
    routes = source_part_routes(blocks, fields)
    for row in rows:
        if row['field_id'] in routes:
            definition = json.loads(row['form_definition_json'])
            definition['source_part_route'] = routes[row['field_id']]
            db.execute('UPDATE questions SET form_definition_json=? WHERE id=?',(json.dumps(definition,ensure_ascii=False),row['id']))


def clear_inactive_derived_answers(db, task_id, stamp, resolve=None):
    """A changed controller must take effect before an edit/export snapshot."""
    rows = db.execute("SELECT q.* FROM questions q JOIN answers a ON a.question_id=q.id WHERE q.task_id=? AND a.origin='reviewed_knowledge' AND a.manual_override=0",(task_id,)).fetchall()
    for row in rows:
        definition=json.loads(row['form_definition_json'])
        status = resolve(db,dict(row),definition) if resolve else route_status(db,row,definition.get('source_part_route'))
        if status == 'applicable':
            continue
        db.execute("UPDATE answers SET answer_text='',selected_options_json='[]',origin='blank',match_id=NULL,confirmed=0,updated_at=? WHERE question_id=?",(stamp,row['id']))
        db.execute("UPDATE matches SET status='candidate',coverage_complete=0,reason='当前分部的填写条件尚未满足' WHERE task_id=? AND question_id=? AND status='auto_filled'",(task_id,row['id']))
