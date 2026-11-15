"""Source-bound conditional Part headings and explicit historical exceptions.

This only withholds automatic reuse. It does not infer an entity's role,
invent a parent choice, rewrite N/A, or remove a historical answer.
"""
import json
import re

HEADER = re.compile(r'^part\s*[-–—:]?\s*([a-z]|\d{1,2})\s*(?:[.:]\s*)?(?:\(\s*(.+)\s*\))?$', re.I)
CONDITIONAL = re.compile(r'^(?:applicable\s+(?:for|to)|only\s+(?:for|if)|if)\b', re.I)
EXCEPTION = re.compile(r'(?:^|(?<=[.!?\n])\s*)part\s*[-–—:]?\s*([a-z]|\d{1,2})\s+is\s+not\s+applicable\s+(?:as|because|since)\b[^\n.!?]+[.!?]?', re.I)
CODE = 'source_part_exemption_requires_current_confirmation'

def _slice(ref, lookup):
    b=lookup.get(ref.get('block_id'),{})
    a,z=ref.get('start'),ref.get('end');text=b.get('text','')
    if type(a) is int and type(z) is int and 0<=a<z<=len(text):return b,text[a:z]
    return {},''

def native_part_headers(field, lookup):
    """Both a valid original span and native table-context relation are required."""
    definition=field.get('form_definition') or {}
    label_refs=field.get('label_refs') or field.get('question_refs') or []
    labels=[b for r in label_refs for b,text in [_slice(r,lookup)] if text]
    if not labels:return []
    found=[]
    for ref in field.get('section_refs') or definition.get('section_refs') or []:
        h,text=_slice(ref,lookup);m=HEADER.fullmatch(' '.join(text.split()))
        if not m or h.get('location',{}).get('type')!='docx' or 'paragraph' not in h.get('location',{}):continue
        if not all(b.get('location',{}).get('type')=='docx' and
                   b.get('location',{}).get('part')==h['location'].get('part') and
                   b.get('order',-1)>h.get('order',float('inf')) and
                   h.get('block_id') in b.get('structure',{}).get('preceding_block_ids',[])
                   for b in labels):continue
        found.append((h,m.group(1).upper(),m.group(2) or ''))
    return found

def part_exception_refs(field, lookup):
    headers=native_part_headers(field,lookup)
    if len(headers)!=1:return []
    header,tag,_=headers[0];refs=[]
    for block in lookup.values():
        loc=block.get('location',{})
        if loc.get('type')!='docx' or loc.get('part')!=header['location'].get('part'):continue
        for match in EXCEPTION.finditer(block.get('text','')):
            if match.group(1).upper()!=tag:continue
            # Match starts after punctuation/space, and stores only the exact
            # declaration, never an empty span falling back to the whole block.
            start=match.start()
            while start<match.end() and block['text'][start].isspace():start+=1
            refs.append({'block_id':block['block_id'],'start':start,'end':match.end()})
    return refs

def unbound_saved_part_condition(db, question, definition):
    if definition.get('source_part_route') or definition.get('applicability_condition'):
        return False
    refs=definition.get('section_refs') or []
    labels=question.get('source_refs')
    if labels is None:
        labels=json.loads(question.get('source_refs_json') or '[]')
    if not refs or not labels:return False
    lookup={}
    for bid in {r.get('block_id') for r in refs+labels if r.get('block_id')}:
        row=db.execute('SELECT block_id,order_no,text,location_json,structure_json FROM document_blocks WHERE task_id=? AND block_id=?',
                       (question['task_id'],bid)).fetchone()
        if row:
            lookup[bid]={'block_id':row['block_id'],'order':row['order_no'],'text':row['text'],
                         'location':json.loads(row['location_json']),'structure':json.loads(row['structure_json'])}
    return any(CONDITIONAL.search(tail) for _,_,tail in native_part_headers({'label_refs':labels,'section_refs':refs},lookup))
