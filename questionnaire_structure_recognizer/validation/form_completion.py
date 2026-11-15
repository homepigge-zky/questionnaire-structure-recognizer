"""Source-backed local forms that do not require another model request.

This module never fabricates values. Locations are physical cells, paragraph
slots, or explicit source ranges. The same source ledger is sent to the model
and used to build and validate the write whitelist.
"""
from collections import defaultdict
from copy import deepcopy
import re

from .field_boundaries import ref, _s, _row, _col, _end, UNITS

VERSION = 'local-form-completion-v18-retain-source-records-under-section-caption'
NUMBERED = re.compile(r'^\s*\d+(?:\.\d+)*\.?\s+\S')
INSTRUCTION = re.compile(r'to be (?:filled|completed)|for .* use only|由.+填写', re.I)
FIXED_PROSE_ROLES = {'table_of_contents', 'definition_prose', 'external_label_specification', 'contract_clause'}


def _mark_explicit_document_prose(blocks):
    """Retain fixed prose in the source ledger without inventing form fields.

    A colon or tab is only a potential slot. A list explicitly describing what
    belongs on a container label is not a request to fill this Word document.
    Scope is local and ends at the first non-list paragraph. Definitions and
    hand-written TOCs similarly require explicit surrounding evidence.
    """
    definitions = False
    definition_numbering = None
    contents = False
    external_labels = False
    opening = [b.get('text','').strip() for b in blocks if b.get('kind')=='paragraph' and b.get('text','').strip()][:12]
    contract_document = any(re.fullmatch(r'(?:Vendor |Supplier )?(?:Quality |Technical )?(?:Agreement|Contract)|(?:供应商)?(?:质量|技术)?(?:协议|合同)',t,re.I) for t in opening)
    for block_index, b in enumerate(blocks):
        if b.get('kind') != 'paragraph' or b.get('location', {}).get('part') != 'word/document.xml':
            if b.get('kind') == 'table_cell':
                external_labels = False
            continue
        s = _s(b); text = b.get('text', '').strip(); style = b.get('style', {})
        if not text:
            continue
        numbering = style.get('numbering', {})
        is_heading = bool(re.match(r'Heading\s*\d|标题\s*\d', style.get('paragraph_style', ''), re.I))
        # A plural answer-section caption introducing instructions and a
        # question table is fixed prose. An explicit write-in/control remains
        # a field; capitalization or a colon alone is never sufficient.
        if (not b.get('controls') and re.fullmatch(r'(?:ANSWERS|RESPONSES)\s+TO\s+(?:THE\s+)?QUESTIONS\s*[:：]?', text, re.I)
                and not any(x.get('kind') == 'inline_placeholder' for x in s.get('logical_slots', []))):
            following = [x for x in blocks[block_index + 1:] if x.get('text', '').strip()][:8]
            if (any(x.get('kind') == 'table_cell' for x in following)
                    and any(x.get('kind') == 'paragraph' and re.search(r'\b(?:questionnaire|questions|comments|instructions)\b', x.get('text',''), re.I) for x in following)):
                s.update(resolved_layout_role='section_header', nonfillable_evidence='answer_section_caption_before_instructions_and_question_table')
                continue
        if contract_document and not b.get('controls'):
            if (is_heading or numbering) and len(text)<70 and text.upper()==text and not re.search(r'[:：?？]|_{2,}',text):
                s['resolved_layout_role']='section_header'
            elif len(text)>75 and re.search(r'\b(?:will|shall|must|is responsible|purpose of this|agreement outlines)\b',text,re.I) and not re.search(r'[?？]|_{2,}|＿{2,}|\b(?:please|provide details|describe your|specify your)\b',text,re.I):
                s['resolved_layout_role']='contract_clause'
                s['nonfillable_evidence']='declarative_obligation_in_explicit_contract'
            elif re.fullmatch(r'AGREEMENT ENTERED INTO AND ACCEPTED BY\s*:',text,re.I):
                s['resolved_layout_role']='section_header'
        if re.fullmatch(r'(?:TABLE OF )?CONTENTS|目录', text, re.I):
            contents = True
            s['resolved_layout_role'] = 'table_of_contents'
            continue
        if contents:
            if re.search(r'[.．…·]{3,}\s*\d+\s*$', text):
                s['resolved_layout_role'] = 'table_of_contents'
                continue
            contents = False
        if re.fullmatch(r'(?:\d+[.、]?\s*)?(?:DEFINITIONS|TERMS AND DEFINITIONS|术语(?:和|与)?定义|定义)', text, re.I):
            definitions = True
            definition_numbering = (numbering.get('num_id'), numbering.get('level')) if numbering else None
            s['resolved_layout_role'] = 'section_header'
            continue
        if definitions:
            same_level = bool(numbering and definition_numbering == (numbering.get('num_id'), numbering.get('level')))
            if is_heading or same_level:
                definitions = False
            elif not b.get('controls') and not re.search(r'_{2,}|＿{2,}|[?？]|\b(?:please|provide|describe|specify)\b|请(?:填写|说明|提供)', text, re.I):
                # Only explicit definition prose; bare labels and deliberate
                # placeholders under this heading remain available for review.
                if re.search(r'[:：]\s*\S.{15,}', text) or re.search(r'\bdefined terms\b.*\bmeaning\b', text, re.I):
                    s['resolved_layout_role'] = 'definition_prose'
                    s['nonfillable_evidence'] = 'explicit_definitions_section'
        external_intro = (
            re.search(r'\b(?:containers?|packag(?:e|es|ing)|labels?)\b', text, re.I)
            and re.search(r'\b(?:must|shall|should)\b', text, re.I)
            and re.search(r'\b(?:label(?:l?ed)?|mark(?:ed)?|bear|contain|include)\b', text, re.I)
            and re.search(r'\bfollowing\b.*[:：]\s*$', text, re.I)
        ) or bool(re.search(r'(?:包装|容器|标签).*(?:必须|应当|应).*(?:标注|标明|包含).*(?:以下|如下).*[:：]\s*$', text))
        if external_intro:
            external_labels = True
            s['resolved_layout_role'] = 'external_label_specification'
            s['nonfillable_evidence'] = 'explicit_external_label_requirements'
            continue
        if external_labels:
            bare_labels = len(text) <= 180 and text.endswith((':', '：')) and not is_heading and not b.get('controls') and not re.search(r'_{2,}|＿{2,}|[?？]', text)
            if bare_labels:
                s['resolved_layout_role'] = 'external_label_specification'
                s['nonfillable_evidence'] = 'explicit_external_label_requirements'
            else:
                external_labels = False


def prepare_form_regions(blocks):
    _mark_explicit_document_prose(blocks)
    tables = defaultdict(list)
    for b in blocks:
        if b['kind']=='table_cell' and not _s(b).get('is_merge_continuation'):
            tables[_s(b).get('table_id')].append(b)
        s=b.setdefault('structure',{}); text=b.get('text','')
        if b.get('location',{}).get('type')=='docx':
            for m in re.finditer(r'(?P<label>Others?|其他)\s*[（(](?P<blank>[ \t]{2,})[）)]',text,re.I):
                s.setdefault('logical_slots',[]).append({'slot_id':s.get('cell_id',b['block_id'])+f":other-{m.start()}:inline-placeholder",'kind':'inline_placeholder','start':m.start('blank'),'end':m.end('blank'),'placeholder_text':m['blank'],'label_start':m.start('label'),'label_end':m.end('label'),'conditional_choice':'Other'})
        if b.get('location',{}).get('type')=='pdf':
            slots=s.setdefault('logical_slots',[])
            region_id=s.get('cell_id') or b['block_id']
            if UNITS.fullmatch(text.strip()):
                # A separate, bordered response cell containing only a unit
                # provides space before it. This is not a value reference.
                slots.append({'slot_id':region_id+':before-content','kind':'leading_blank_candidate','start':0,'end':0,'offset':0})
            unit_option = re.fullmatch(r'\s*(?P<unit>\S+)\s+N\s*/\s*A\s*[□☐☒☑]\s*',text,re.I)
            if b['kind']=='table_cell' and unit_option and UNITS.fullmatch(unit_option['unit']):
                slots.append({'slot_id':region_id+':before-content','kind':'leading_blank_candidate',
                    'start':unit_option.start('unit'),'end':unit_option.start('unit'),'offset':unit_option.start('unit'),
                    'numeric_with_na':True,'unit_start':unit_option.start('unit'),'unit_end':unit_option.end('unit')})
            for m in re.finditer(r'(?<!\w)(Ref|Comment|Name|Job Title|Date|Signature)\s*:',text,re.I):
                stop=len(text)
                following=re.search(r'\S',text[m.end():])
                # Only explicit blank whitespace/end of a prompt; do not
                # overwrite a populated reference or an adjacent option.
                if following:
                    stop=m.end()+following.start()
                    if stop-m.end()<2: continue
                slots.append({'slot_id':region_id+f':prompt-slot:{m.start()}', 'kind':'inline_prompt_candidate',
                              'start':m.end(),'end':stop,'offset':m.end(),'label_start':m.start(),'label_end':m.end()-1})
        # The native list marker belongs to the number cell even when it is
        # alphabetic (attachment lists), not just a decimal question number.
    for cells in tables.values():
        rows=defaultdict(list)
        for b in cells: rows[_row(b)].append(b)
        for row in rows.values(): row.sort(key=_col)
        appendix=next((b for b in cells if re.match(r'^APPENDIX[-–\s]+[A-Z]\s*:',b['text'],re.I) and _s(b).get('grid_col_start')==1 and _s(b).get('grid_col_end')==_s(b).get('table_column_count')),None)
        if appendix:
            _s(appendix)['resolved_layout_role']='section_header'
            for b in cells:_s(b)['section_path_block_ids']=[[appendix['block_id']]]
        for rn,row in rows.items():
            if len(row)>=3 and not row[0].get('text','').strip() and row[1].get('text','').strip():
                marks=[p for p in _s(row[0]).get('paragraph_spans',[]) if p.get('style',{}).get('numbering',{}).get('marker')]
                if len(marks)==1 and re.fullmatch(r'[A-Za-z][.)]?',marks[0]['style']['numbering']['marker'].strip()):
                    for b in row: _s(b).update(local_layout_type='numbered_question_answer',label_column=_col(row[1]),answer_columns=[_col(x) for x in row[2:]])
            if row[0].get('location',{}).get('type')!='pdf': continue
            if len(row)>=2 and NUMBERED.match(row[0].get('text','')) and all(not NUMBERED.match(b.get('text','')) for b in row[1:]):
                if any(not b.get('text','').strip() or UNITS.fullmatch(b.get('text','').strip()) or re.search(r'[□☐☒☑]',b.get('text','')) or re.fullmatch(r'(?:Ref|Comment)\s*:',b.get('text','').strip(),re.I) for b in row[1:]):
                    for b in row: _s(b).update(local_layout_type='numbered_question_answer',label_column=_col(row[0]),answer_columns=[_col(x) for x in row[1:]])
        if cells[0].get('location',{}).get('type')=='pdf':
            for rn,row in rows.items():
                if len(row)<2 or not all(b['text'].strip() and not b.get('controls') for b in row) or any(NUMBERED.match(b['text']) for b in row):continue
                signature=[(_col(b),_end(b)) for b in row];following=[]
                for later in range(rn+1,max(rows)+1):
                    nxt=rows.get(later,[])
                    if [(_col(b),_end(b)) for b in nxt]!=signature or any(b['text'].strip() for b in nxt):break
                    following.append(nxt)
                if len(following)>=2:
                    for b in row:_s(b).update(local_layout_type='record_table_header',record_header_row=rn)
                    for ri,rr in enumerate(following,1):
                        for b in rr:_s(b).update(local_layout_type='record_table',record_header_row=rn,record_index=ri)
    for b in blocks:
        s=_s(b)
        if 'logical_slots' in s:
            s['logical_slots']=list({slot['slot_id']:slot for slot in s['logical_slots']}.values())
    return blocks


def new_field(block, label_ref, regions=(), parent=None):
    fid=f"source-form:{block['block_id']}:{label_ref['start']}"
    return {'field_id':fid,'question_id':fid,'parent_question_id':parent,
        'label_refs':[label_ref], 'question_refs':[deepcopy(label_ref)],
        'question_text_original':block['text'][label_ref['start']:label_ref['end']],
        'section_refs':[],'section_text_original':'','context_label':'', 'normalized_label':'',
        'answer_region_refs':list(regions),'answer_value_refs':[],'answer_refs':[],
        'answer_text_original':'','answer_status':'blank' if regions else 'not_located',
        'unit_refs':[],'unit_text_original':'','children':[],'options':[],
        'instruction_refs':[],'attachment_refs':[],'attachment_text_original':'',
        'review_status':'model_proposed','review_reasons':[],'model_hints':{},
        'boundary_evidence':'native_explicit_form','boundary_version':VERSION}


def _choice_with_reference_control(block, paragraphs, blocks):
    """A reference control inside a question is separate from its choice lane.

    Require a unique adjacent native checkbox cell and independently located
    text controls. Never reinterpret a general populated neighbour as choices.
    """
    controls=block.get('controls',[])
    if not controls or any(c.get('type')!='text' or c.get('source')!='legacy_form_field'
                           or not c.get('legacy_result_supported') for c in controls):return None
    candidates=[b for b in blocks if b.get('kind')=='table_cell'
        and _s(b).get('table_id')==_s(block).get('table_id') and _row(b)==_row(block)
        and _col(b)==_end(block)+1 and len(b.get('controls',[]))>=2
        and all(c.get('type')=='checkbox' for c in b['controls'])]
    if len(candidates)!=1:return None
    from .native_choices import _control_options
    answer=candidates[0]
    options,issue=_control_options(answer,0,len(answer['text']),answer['controls'])
    if issue or len(options)!=len(answer['controls']):return None
    prompt=paragraphs[0]
    parent=new_field(block,ref(block,prompt['start'],prompt['end']),[ref(answer)])
    for control in controls:
        matched=[p for p in paragraphs[1:] if p['paragraph_index']==control.get('paragraph_index')]
        if len(matched)!=1:return None
        p=matched[0];value=str(control.get('current_value') or '').strip()
        start=end=p['end']
        if value and not control.get('placeholder'):
            if p['text'].count(value)!=1:return None
            start=p['start']+p['text'].index(value);end=start+len(value)
        label_end=start
        if not block['text'][p['start']:label_end].strip():return None
        identity=f"p{control['paragraph_index']}:i{control['control_index']}"
        region=ref(block,start,end,f"{block['block_id']}:control:{identity}")
        region.update(control_id=control.get('control_id'),control_index=control['control_index'],
                      control_paragraph_index=control['paragraph_index'])
        child=new_field(block,ref(block,p['start'],label_end),[region],parent['field_id'])
        if start<end:
            values=[ref(block,start,end)]
            child.update(answer_value_refs=values,answer_refs=deepcopy(values),
                         answer_text_original=block['text'][start:end],answer_status='filled')
        parent['children'].append(child)
    return parent


def complete_local_fields(questions, blocks, lookup):
    """Restore explicit fields before native choices and global validation."""
    replaced={b['block_id'] for b in blocks if _s(b).get('resolved_layout_role') in {'section_header', *FIXED_PROSE_ROLES}}; additions=[]
    def add(f):
        additions.append(f)
        for r in f['label_refs']:
            _s(lookup[r['block_id']]).setdefault('protected_label_ranges',[]).append({'start':r['start'],'end':r['end']})
    # Colon-only body paragraphs and numbered attachment items are distinct
    # fields. Context headings themselves are retained as document content.
    attachment_context=None
    return_address_mode=False
    for b in blocks:
        if b['kind']=='table_cell' or b['kind'] in {'title','heading'}:
            return_address_mode=False
        if b['kind']!='paragraph' or b.get('location',{}).get('textbox'): continue
        text=b.get('text',''); s=_s(b)
        if s.get('resolved_layout_role') in FIXED_PROSE_ROLES or s.get('resolved_layout_role')=='section_header':
            continue
        placeholders=[x for x in s.get('logical_slots',[]) if x['kind']=='inline_placeholder']
        annotated=[]
        for slot in placeholders:
            suffix=re.match(r'\s*[(（]+\s*(?P<label>[^()（）\n]{2,70}?)\s*[)）]',text[slot['end']:])
            lr=None
            if suffix and not re.fullmatch(r'(?:if )?(?:optional|required|applicable)|可选|必填|如适用',suffix['label'].strip(),re.I):
                start=slot['end']+suffix.start('label');end=slot['end']+suffix.end('label')
                lr=ref(b,start,end)
            elif slot.get('label_start') is None:
                # A directly attached noun is source evidence, unlike taking
                # the entire declaration sentence as a field label.
                prefix=re.search(r'\b(?:product|material|company name|manufactur(?:er|ing) address)\s*$',text[:slot['start']],re.I)
                if prefix:lr=ref(b,prefix.start(),prefix.end())
            if lr:
                f=new_field(b,lr,[ref(b,slot['start'],slot['end'],slot['slot_id'])])
                f['context_refs']=[ref(b)];f['context_label']=text
                f['boundary_evidence']='explicit_placeholder_with_source_label'
                annotated.append(f)
        if annotated and len(annotated)==len(placeholders):
            for f in annotated:add(f)
            replaced.add(b['block_id']);continue
        if re.search(r'please return the completed (?:form|questionnaire)',text,re.I):
            return_address_mode=True
        elif return_address_mode and (s.get('resolved_layout_role')=='table_of_contents' or re.fullmatch(r'INDEX|CONTENTS|TABLE OF CONTENTS',text.strip(),re.I)):
            return_address_mode=False
        if return_address_mode:
            # Printed mailing instructions/address are immutable context,
            # including an unfilled attention line for the receiving party.
            replaced.add(b['block_id']);s['resolved_layout_role']='return_instructions';continue
        if re.match(r'^Note\s*:',text,re.I) and re.search(r'(?:fill out|complete).{0,35}questionnaire',text,re.I) and not s.get('logical_slots'):
            replaced.add(b['block_id']);s['resolved_layout_role']='completion_instructions';continue
        if re.search(r'documents?.*(?:attached|attachment)|附件清单',text,re.I): attachment_context=b
        if attachment_context and re.match(r'^\s*\d+[.．]\s*',text):
            f=new_field(b,ref(b)); f['field_type']='attachment_reference'
            f['attachment_refs']=[ref(b)]; f['attachment_text_original']=text
            f['section_refs']=[ref(attachment_context)]; f['section_text_original']=attachment_context['text']
            slot={'slot_id':b['block_id']+':after-content','kind':'trailing_fill_candidate','offset':len(text),'start':len(text),'end':len(text)}
            s.setdefault('logical_slots',[]).append(slot)
            f['answer_region_refs']=[ref(b,len(text),len(text),slot['slot_id'])]; f['answer_status']='blank'
            add(f); replaced.add(b['block_id']); continue
        if attachment_context and text and not re.match(r'^\s*\d+[.．]',text) and not re.search(r'[\u3000-\u9fff]',text) and b['order']>attachment_context['order']+2:
            attachment_context=None
        slots=[x for x in s.get('logical_slots',[]) if x['kind']=='inline_prompt_candidate' and x.get('label_start') is not None]
        if len(slots)>1 and all(not text[x['start']:x['end']].strip() for x in slots):
            for slot in slots:
                f=new_field(b,ref(b,slot['label_start'],slot['label_end']),[ref(b,slot['start'],slot['end'],slot['slot_id'])])
                f['boundary_evidence']='multiple_explicit_body_prompt_slots'
                add(f)
            replaced.add(b['block_id']);continue
        if len(text)<100 and slots and len(slots)==1 and slots[0]['start']==len(text):
            slot=slots[0]; add(new_field(b,ref(b,slot['label_start'],slot['label_end']),[ref(b,slot['start'],slot['end'],slot['slot_id'])]));replaced.add(b['block_id'])

    # Bilingual signing lines: an English colon label followed immediately by
    # its aligned short caption and an explicitly underlined blank. Preserve
    # both source expressions; do not count the caption as a second question.
    paragraphs=[b for b in blocks if b['kind']=='paragraph' and b.get('location',{}).get('part')=='word/document.xml']
    for a,b in zip(paragraphs,paragraphs[1:]):
        if not re.fullmatch(r'[A-Za-z][A-Za-z ]{1,35}:\s*(?:\([^\n]{1,60}\))?',a['text']):continue
        if not re.fullmatch(r'[\u3000-\u9fff]{1,12}',b['text']):continue
        ps=_s(b).get('paragraph_spans',[])
        if len(ps)!=1 or not ps[0].get('has_underlined_runs') or not re.search(r'[ \u3000]{2,}$',ps[0].get('raw_text','')):continue
        slot={'slot_id':b['block_id']+':underline-slot','kind':'trailing_underlined_blank','start':len(b['text']),'end':len(b['text'])}
        _s(b).setdefault('logical_slots',[]).append(slot)
        f=new_field(a,ref(a),[ref(b,slot['start'],slot['end'],slot['slot_id'])])
        f['label_refs'].append(ref(b));f['question_refs'].append(ref(b));f['question_text_original']+='\n'+b['text']
        _s(b)['native_label_block_id']=a['block_id']
        additions[:]=[x for x in additions if x['label_refs'][0]['block_id'] not in {a['block_id'],b['block_id']}]
        add(f);replaced.update((a['block_id'],b['block_id']))

    for b in blocks:
        if b['kind']!='table_cell':continue
        s=_s(b); ps=s.get('paragraph_spans',[])
        # A dotted write-in after a complete prompt remains a separate field
        # inside a policy/approval cell. Fixed paragraphs around it are not values.
        for slot in s.get('logical_slots',[]):
            if slot['kind']!='inline_placeholder' or not re.fullmatch(r'[.．…·]{4,}',slot.get('placeholder_text','')):continue
            para=next((p for p in ps if p['start']<=slot['start']<p['end']),None)
            if para and b['text'][para['start']:slot['start']].strip():
                add(new_field(b,ref(b,para['start'],slot['start']),[ref(b,slot['start'],slot['end'],slot['slot_id'])]));replaced.add(b['block_id'])
        labels=[p for p in ps if NUMBERED.match(p.get('text','')) and len(p.get('text',''))>15]
        # Several complete numbered questions in one physical cell. A blank
        # paragraph after each is a different target, not a missing value.
        if len(labels)>=2 and s.get('column_span',1)==s.get('table_column_count',1):
            for i,p in enumerate(labels):
                end=labels[i+1]['paragraph_index'] if i+1<len(labels) else len(ps)+1
                slot=next((x for x in s.get('logical_slots',[]) if x['kind']=='inline_placeholder' and p['start']<=x['start']<p['end']),None)
                if slot:
                    lr=ref(b,p['start'],slot['start']); lr['end']=len(b['text'][:lr['end']].rstrip())
                else:
                    lr=ref(b,p['start'],p['end'])
                    slot=next((x for x in s.get('logical_slots',[]) if x['kind']=='blank_paragraph_candidate' and p['paragraph_index']<x['paragraph_index']<end),None)
                f=new_field(b,lr,[ref(b,slot['start'],slot['end'],slot['slot_id'])] if slot else [])
                add(f)
            replaced.add(b['block_id'])
        # A numbered table row may contain the question and its actual native
        # controls in the SAME cell. Only the first prose paragraph is the
        # label; checkbox paragraphs and blank write-in slots stay separate.
        ctrl_ps={c.get('paragraph_index') for c in b.get('controls',[]) if c.get('source')=='legacy_form_field'}
        meaningful=[p for p in ps if p.get('text','').strip()]
        if ctrl_ps and meaningful and meaningful[0]['paragraph_index'] not in ctrl_ps and not re.fullmatch(r'[_＿.·…\s]+',meaningful[0]['text']):
            p=meaningful[0]; opt_ps=[x for x in meaningful if x['paragraph_index'] in ctrl_ps]
            if opt_ps:
                separate=_choice_with_reference_control(b,meaningful,blocks)
                if separate:
                    _s(b)['protected_label_ranges']=[]
                    add(separate);replaced.add(b['block_id'])
                    continue
                f=new_field(b,ref(b,p['start'],p['end']),[ref(b,x['start'],x['end']) for x in opt_ps])
                _s(b)['protected_label_ranges']=[]
                for slot in s.get('logical_slots',[]):
                    if slot['kind']=='inline_placeholder' and slot['start']>=p['end']:
                        # Preserve a separate write-in position next to a
                        # checkbox; no guess about its current applicability.
                        para=next(x for x in ps if x['paragraph_index']==slot['paragraph_index'])
                        start=para['start']; end=slot['start']
                        if b['text'][start:end].strip():
                            child=new_field(b,ref(b,start,end),[ref(b,slot['start'],slot['end'],slot['slot_id'])],f['field_id'])
                            f['children'].append(child)
                add(f);replaced.add(b['block_id'])
        elif len(meaningful)==1 and s.get('number_column') is None and not b.get('controls'):
            p=meaningful[0]
            if re.match(r'(?:Indicate|Material\(s\)|请说明|请填写)',p['text'],re.I):
                slot=next((x for x in s.get('logical_slots',[]) if x['kind']=='blank_paragraph_candidate' and x['paragraph_index']>p['paragraph_index']),None)
                if slot:
                    f=new_field(b,ref(b,p['start'],p['end']),[ref(b,slot['start'],slot['end'],slot['slot_id'])]);add(f);replaced.add(b['block_id'])
    tables=defaultdict(list)
    for b in blocks:
        if b['kind']=='table_cell' and not _s(b).get('is_merge_continuation'):tables[_s(b).get('table_id')].append(b)
    for cells in tables.values():
        rows=defaultdict(list)
        for b in cells:rows[_row(b)].append(b)
        for rn,row in rows.items():
            row.sort(key=_col)
            labels=[b for b in row if b['text'].strip()]
            following=[rows.get(rn+i,[]) for i in (1,2)]
            if len(labels)==1 and re.search(r'(?:following|如下)\s*[:：]$',labels[0]['text'],re.I) and all(any(b.get('controls') for b in rr) and any(p.get('style',{}).get('numbering') for b in rr for p in _s(b).get('paragraph_spans',[])) for rr in following):
                replaced.update(b['block_id'] for b in row);_s(labels[0])['resolved_layout_role']='question_group_context'
            if len(row)==3 and row[0]['text'].strip() and not row[1]['text'].strip() and INSTRUCTION.search(row[2]['text']):
                f=new_field(row[0],ref(row[0]),[ref(row[1])]); f['instruction_refs']=[ref(row[2])]
                add(f);replaced.update(b['block_id'] for b in row)
        ordered=sorted(rows)
        if len(ordered)==3:
            top=sorted(rows[ordered[0]],key=_col);middle=rows[ordered[1]];bottom=rows[ordered[2]]
            if len(top)==2 and re.fullmatch(r'\d+(?:\.\d+)*',top[0]['text'].strip()) and len(middle)==len(bottom)==1 and middle[0]['text'].strip().endswith(':'):
                slot=next((s for s in _s(bottom[0]).get('logical_slots',[]) if s['kind']=='blank_paragraph_candidate' and s['paragraph_index']>1),None)
                if slot:
                    f=new_field(top[1],ref(top[1]),[ref(bottom[0],slot['start'],slot['end'],slot['slot_id'])])
                    f['instruction_refs']=[ref(middle[0])];_s(bottom[0])['native_label_block_id']=top[1]['block_id']
                    add(f);replaced.update(b['block_id'] for b in cells)
        for rn,row in rows.items():
            upper=rows.get(rn-1,[])
            if not upper or not re.search(r'questionnaire was evaluated', ' '.join(b['text'] for b in upper),re.I):continue
            option=next((b for b in row if b.get('controls')),None)
            if not option:continue
            label=next(b for b in upper if re.search(r'questionnaire was evaluated',b['text'],re.I))
            _s(option)['protected_label_ranges']=[{'start':p['start'],'end':p['end']} for p in _s(option).get('paragraph_spans',[]) if p['text'].strip()]
            f=new_field(label,ref(label),[ref(option)])
            f['instruction_refs']=[ref(b) for b in upper];_s(option)['native_label_block_id']=label['block_id']
            add(f);replaced.add(option['block_id'])
    # A number/title pair above a compound body is context. Do not manufacture
    # an unanswered field for a heading such as Personnel of the Supplier.
    for cells in tables.values():
        rows=defaultdict(list)
        for b in cells:rows[_row(b)].append(b)
        for rn,row in rows.items():
            row.sort(key=_col)
            if len(row)==2 and re.fullmatch(r'\d+(?:\.\d+)*',row[0]['text'].strip()) and rn+1 in rows:
                below=rows[rn+1]
                if any(len(_s(b).get('label_list',[]))>=2 for b in below):
                    replaced.update(b['block_id'] for b in row)
    from .native_choices import _control_options
    _complete_mixed_answer_groups(questions,lookup,replaced)
    _complete_nested_writeins(questions,blocks,lookup)
    _complete_choice_writeins(questions,lookup,replaced)
    # A numeric response followed by its unit and an N/A square exposes two
    # different input types. Do not let checkbox normalization erase the value
    # area. Only the actual response cell's unit/option grammar qualifies.
    for parent in questions:
        bids={r['block_id'] for r in parent.get('answer_region_refs',[])}
        bids.update(r['block_id'] for o in parent.get('options',[]) for r in o.get('refs',[]))
        for bid in bids:
            b=lookup[bid]
            for slot in _s(b).get('logical_slots',[]):
                if not slot.get('numeric_with_na'):continue
                label=lookup[parent['label_refs'][0]['block_id']]
                if _s(label).get('table_id')!=_s(b).get('table_id') or _row(label)!=_row(b) or _end(label)>=_col(b):continue
                if any(slot['slot_id']==r.get('slot_id') for c in parent.get('children',[]) for r in c.get('answer_region_refs',[])):continue
                child=new_field(label,deepcopy(parent['label_refs'][0]),[ref(b,slot['start'],slot['end'],slot['slot_id'])],parent['field_id'])
                child['field_id']=child['question_id']=parent['field_id']+':numeric-value'
                child['normalized_label']='数值 / Value'
                child['unit_refs']=[ref(b,slot['unit_start'],slot['unit_end'])]
                child['unit_text_original']=b['text'][slot['unit_start']:slot['unit_end']]
                child['explicit_condition']={'status':'unresolved','parent_field_id':parent['field_id'],'source_text':'N/A','not_selected_option':'N/A'}
                parent.setdefault('children',[]).append(child)
    for f in questions:
        existing={r.get('slot_id') for c in f.get('children',[]) for r in c.get('answer_region_refs',[])}
        bids={r['block_id'] for r in f.get('answer_region_refs',[])}
        bids.update(r['block_id'] for o in f.get('options',[]) for r in o.get('refs',[]))
        for bid in bids:
            b=lookup[bid]
            for slot in _s(b).get('logical_slots',[]):
                if slot.get('conditional_choice') and slot['slot_id'] not in existing:
                    child=new_field(b,ref(b,slot['label_start'],slot['label_end']),[ref(b,slot['start'],slot['end'],slot['slot_id'])],f['field_id'])
                    child['explicit_condition']={'status':'unresolved','parent_field_id':f['field_id'],'source_text':b['text'][slot['label_start']:slot['label_end']]}
                    f.setdefault('children',[]).append(child)
    for parent in questions:
        if any(r['block_id'] in replaced for r in parent.get('label_refs',[])):continue
        labels=parent.get('label_refs',[])
        if len(labels)!=1:continue
        label=lookup[labels[0]['block_id']];ls=_s(label)
        candidates=[b for b in tables.get(ls.get('table_id'),[]) if _row(b)==_row(label) and _col(b)>_end(label) and b.get('controls')]
        if len(candidates)!=1:continue
        answer=candidates[0]
        controls=[c for c in answer['controls'] if c.get('source')=='legacy_form_field']
        if len(controls)<4:continue
        opts,problem=_control_options(answer,0,len(answer['text']),controls)
        if problem or len(opts)!=len(controls):continue
        groups=[]
        for o in opts:
            if not groups or any(x['text_original'].strip().lower()==o['text_original'].strip().lower() for x in groups[-1]):groups.append([])
            groups[-1].append(o)
        paragraphs=[p for p in ls.get('paragraph_spans',[]) if p.get('text','').strip()]
        if paragraphs and paragraphs[0]['text'].rstrip().endswith(':'): paragraphs=paragraphs[1:]
        blank_slots=[x for x in _s(answer).get('logical_slots',[]) if x['kind']=='inline_placeholder']
        has_other_tail=len(paragraphs)==len(groups)+1 and len(blank_slots)==1 and paragraphs[-1]['text'].strip().endswith(':')
        if (len(groups)!=len(paragraphs) and not has_other_tail) or len(groups)<2:continue
        children=[]
        for p,group in zip(paragraphs,groups):
            a=group[0]['refs'][0]['start'];z=group[-1]['refs'][0]['end']
            f=new_field(label,ref(label,p['start'],p['end']),[ref(answer,a,z)],parent['field_id'])
            f['boundary_evidence']='ordered_native_choice_groups';children.append(f)
        if has_other_tail:
            p=paragraphs[-1];slot=blank_slots[0]
            children.append(new_field(label,ref(label,p['start'],p['end']),[ref(answer,slot['start'],slot['end'],slot['slot_id'])],parent['field_id']))
        parent.update(children=children,answer_region_refs=[],answer_refs=[],answer_value_refs=[],answer_text_original='',answer_status='blank',options=[],review_reasons=[],review_status='model_proposed')
        parent['protected_label_ranges']=[]
    # Numbered/bulleted subrequirements facing explicitly repeated write-in
    # lines form a composite response. Match the source list sequence only
    # when every item has exactly one distinct native line, with no leftovers.
    for parent in questions:
        refs=parent.get('label_refs',[])
        if len(refs)!=1 or refs[0]['block_id'] in replaced:continue
        label=lookup[refs[0]['block_id']];ls=_s(label)
        ps=[p for p in ls.get('paragraph_spans',[]) if p.get('style',{}).get('numbering')]
        if len(ps)<2:continue
        regions=[b for b in tables.get(ls.get('table_id'),[]) if _row(b)==_row(label) and _col(b)>_end(label)]
        if len(regions)!=1:continue
        a=regions[0]; slots=[x for x in _s(a).get('logical_slots',[]) if x['kind']=='inline_placeholder']
        if len(slots)!=len(ps):continue
        children=[new_field(label,ref(label,p['start'],p['end']),[ref(a,s['start'],s['end'],s['slot_id'])],parent['field_id']) for p,s in zip(ps,slots)]
        parent.update(children=children,answer_region_refs=[],answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='blank',options=[],review_reasons=[],review_status='model_proposed')
    # An option group plus a separate comment box is two response types. Do
    # not pass the comment box to the checkbox writer as one more option.
    for parent in questions:
        regions=parent.get('answer_region_refs',[])
        option_bids={r['block_id'] for o in parent.get('options',[]) for r in o.get('refs',[])}
        option_bids.update(r['block_id'] for o in parent.get('options',[]) for r in o.get('answer_region_refs',[]))
        option_bids.update(r['block_id'] for r in regions if lookup[r['block_id']].get('controls') or re.search(r'[□☐☑☒]',lookup[r['block_id']]['text']))
        if not option_bids:continue
        # Earlier choice normalization can already have narrowed the main
        # response to checkbox ranges. Recover a distinct comment column from
        # its actual column header and row, never from arbitrary nearby blanks.
        original=parent['label_refs'][0];label=lookup[original['block_id']]
        table_cells=tables.get(_s(label).get('table_id'),[])
        comment_headers={}
        existing={r['block_id'] for c in parent.get('children',[]) for r in c.get('answer_region_refs',[])}
        for b in table_cells:
            if _row(b)!=_row(label) or _col(b)<=_end(label) or b.get('controls') or b['block_id'] in existing:continue
            headers=[h for h in table_cells if _col(h)==_col(b) and _end(h)==_end(b) and _row(h)<_row(b) and re.fullmatch(r'(?:Comments?|Remarks?|备注|补充说明)(?:\s+If\s+(?:Ans\.?|Answer)\s+is\s+(?:Yes|No))?',h['text'].strip(),re.I)]
            if not headers:continue
            header=max(headers,key=_row)
            # A subsequent spanning section/header is a hard boundary.
            if any(_row(header)<_row(h)<_row(b) and _col(h)<=_col(label) and _end(h)>=_end(b) and h['text'].strip() for h in table_cells):continue
            comment_headers[b['block_id']]=header
            _s(b)['comment_header_block_id']=header['block_id']
            if b['block_id'] not in {r['block_id'] for r in regions}:regions.append(ref(b))
        # N/A (or Yes/No) entered in an explicitly headed comment column is
        # free text, not a third choice. Earlier lexical option discovery may
        # have proposed it, but the actual column header and physical row
        # establish a separate response. Preserve that value in its own child.
        comment_ids=set(comment_headers)
        option_bids-=comment_ids
        parent['options']=[o for o in parent.get('options',[]) if not
            (o.get('refs') and all(r['block_id'] in comment_ids for r in o['refs']))]
        for key in ('answer_refs','answer_value_refs'):
            parent[key]=[r for r in parent.get(key,[]) if r['block_id'] not in comment_ids]
        if comment_ids:
            parent['answer_text_original']='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in parent.get('answer_value_refs',[])).strip()
            parent['answer_status']='filled' if parent['answer_text_original'] else 'blank'
        extras=[r for r in regions if r['block_id'] not in option_bids and not lookup[r['block_id']]['text'].strip()]
        extras.extend(r for r in regions if r['block_id'] in comment_headers and lookup[r['block_id']]['text'].strip())
        extras=list({r['block_id']:r for r in extras}.values())
        for rr in extras:
            original=parent['label_refs'][0];label=lookup[original['block_id']]
            header=comment_headers.get(rr['block_id'])
            child=new_field(header or label,ref(header) if header else deepcopy(original),[deepcopy(rr)],parent['field_id'])
            child['field_id']=child['question_id']=parent['field_id']+':comment:'+rr['block_id']
            child['normalized_label']='Comment / 补充说明'
            child['context_refs']=deepcopy(parent['label_refs'])
            child['context_label']=parent['question_text_original']
            child['section_refs']=deepcopy(parent.get('section_refs',[]))
            child['section_text_original']=parent.get('section_text_original','')
            if header:
                condition=re.search(r'If\s+(?:Ans\.?|Answer)\s+is\s+(Yes|No)',header['text'],re.I)
                if condition:child['explicit_condition']={'parent_field_id':parent['field_id'],'equals_option':condition[1].title(),'status':'unresolved','source_text':condition[0],'source_refs':[ref(header)]}
            value=lookup[rr['block_id']]['text'].strip()
            if value:child.update(answer_value_refs=[deepcopy(rr)],answer_refs=[deepcopy(rr)],answer_text_original=value,answer_status='filled')
            parent.setdefault('children',[]).append(child)
        if extras:parent['answer_region_refs']=[r for r in regions if r not in extras]
    # Native PDF diagrams carry several text ranges per choice. These ranges
    # can be interleaved in extraction order but remain in the same enclosure.
    for b in blocks:
        groups=_s(b).get('native_option_groups',[])
        if not groups:continue
        first=min(g['control_start'] for g in groups)
        f=new_field(b,ref(b,0,first));f['question_text_original']=f['question_text_original'].strip()
        f['boundary_evidence']='native_diagram_options';f['answer_status']='blank'
        for g in groups:
            rr=[ref(b,r['start'],r['end']) for r in g['ranges'] if b['text'][r['start']:r['end']].strip()]
            ar=ref(b,g['control_start'],g['control_end'],f"{_s(b)['cell_id']}:choice:{g['control_start']}")
            f['options'].append({'refs':rr,'text_original':' '.join(b['text'][r['start']:r['end']].strip() for r in rr),'selected':g['selected'],'selection_source':'native_drawn_enclosure','answer_region_refs':[ar]})
            f['answer_region_refs'].append(ar)
        _s(b)['protected_label_ranges']=[];add(f);replaced.add(b['block_id'])
    # Entire-cell symbol lists after an explicit 'select completed sections'
    # prompt are one selection field, not a paragraph answered by its neighbour.
    from .native_choices import _glyph_options
    for b in blocks:
        if b.get('location',{}).get('type')!='pdf' or not re.match(r'^\s*[□☐☑☒]',b.get('text','')):continue
        preceding=[x for x in tables.get(_s(b).get('table_id'),[]) if _row(x)==_row(b)-1]
        if len(preceding)==1 and re.search(r'section|part|部分|章节',preceding[0]['text'],re.I):
            label=preceding[0];f=new_field(label,ref(label),[ref(b)])
            # Protect option wording, not the selectable square. An empty
            # list would fall back to an old whole-cell label hint.
            _s(b)['protected_label_ranges']=[{'start':r['start'],'end':r['end']} for option in _glyph_options(b,0,len(b['text'])) for r in option.get('refs',[])]
            _s(b)['native_label_block_id']=label['block_id'];add(f);replaced.add(b['block_id']);replaced.add(label['block_id'])
    _pdf_record_grids(tables,lookup,additions,replaced)
    # Full-width numbered introductions followed by their numbered children
    # are context, not unanswered text fields. Printed return instructions
    # similarly have no writable region; preserve their source classification.
    for cells in tables.values():
        if cells[0].get('location',{}).get('type')!='pdf':continue
        rows=defaultdict(list)
        for b in cells:rows[_row(b)].append(b)
        return_mode=False;notes_mode=False
        for rn,row in sorted(rows.items()):
            text=' '.join(b['text'] for b in row)
            if re.search(r'^Part I:\s*GENERAL NOTES',text,re.I):notes_mode=True
            if re.search(r'Please return the completed (?:form|questionnaire)',text,re.I):return_mode=True
            if return_mode:
                replaced.update(b['block_id'] for b in row)
                for b in row:_s(b)['resolved_layout_role']='return_instructions'
            if notes_mode and re.match(r'^1(?:[.\s])',text):
                replaced.update(b['block_id'] for b in row)
                for b in row:_s(b)['resolved_layout_role']='completion_instructions'
            if len(row)==1 and not row[0].get('controls'):
                b=row[0];number=re.match(r'^(\d+(?:\.\d+)*)\.?\s+',b['text'])
                below=rows.get(rn+1,[])
                next_text=below[0]['text'] if below else ''
                if number and re.match(re.escape(number[1])+r'\.\d',next_text):
                    replaced.add(b['block_id']);_s(b)['resolved_layout_role']='question_group_context'
                if number and re.search(r'\bIf (?:yes|no),?\s+please complete\b.*\bAPPENDIX[-\s]',b['text'],re.I) and not _s(b).get('logical_slots'):
                    replaced.add(b['block_id']);_s(b)['resolved_layout_role']='conditional_attachment_instruction'
    _complete_vertical_regions(questions,blocks,additions,replaced)
    _complete_local_column_records(blocks,additions,replaced)
    _complete_spanning_record_rows(blocks,questions,additions)
    _complete_separator_forms(blocks,additions,replaced)
    _complete_wide_table_prompts(blocks,additions,replaced)
    _complete_audit_closing_forms(blocks,additions,replaced)
    _complete_native_choice_rows(blocks,additions,replaced)
    _complete_choice_explanation_rows(blocks,additions,replaced)
    _complete_parallel_control_lanes(blocks,additions,replaced)
    _complete_segmented_cell_forms(blocks,lookup,additions,replaced,questions)
    _complete_body_record_lines(blocks, additions, replaced)
    _complete_printed_paragraph_choices(blocks, additions, replaced)
    _complete_left_choice_grids(blocks, additions, replaced)
    _complete_numbered_following_forms(blocks, additions, replaced)
    _complete_merged_choice_followups(blocks, additions, replaced)
    _complete_isolated_cell_prompts(blocks, additions, replaced)
    _exclude_signed_declaration_prose(blocks, additions, replaced)
    _exclude_form_captions(blocks, additions, replaced)
    _complete_contract_template_placeholders(blocks,additions,replaced)
    from .completed_response_regions import restore_comment_lanes, restore_printed_choice_lanes, exclude_printed_write_lines, separate_count_guidance
    restore_comment_lanes(blocks,additions,replaced,new_field)
    restore_printed_choice_lanes(blocks,additions,replaced,new_field)
    def native_record_container(field):
        # A source-backed table caption may also be a section heading. It
        # owns no answer: retain its separately located record cells, not a
        # model-created question at that caption.
        return (field.get('boundary_evidence') == 'native_record_table'
                and field.get('record_columns') and field.get('children')
                and not field.get('answer_region_refs')
                and all(_s(lookup[r['block_id']]).get('resolved_layout_role') == 'section_header'
                        for r in field.get('label_refs', []) if r['block_id'] in replaced))
    questions[:]=[f for f in questions if native_record_container(f) or
                  not any(r['block_id'] in replaced for r in f.get('label_refs',[]))]+additions
    from .filled_word_lanes import restore_filled_word_lanes, restore_long_response_prompts, restore_bilingual_native_choice_lanes
    restore_filled_word_lanes(questions,blocks,lookup,new_field)
    restore_long_response_prompts(questions,blocks,lookup,new_field)
    restore_bilingual_native_choice_lanes(questions,blocks,lookup,new_field)
    separate_count_guidance(questions,lookup)
    exclude_printed_write_lines(questions,lookup)
    _discard_shadowed_form_wrappers(questions,lookup)
    _link_nested_response_records(questions,lookup)
    _split_record_prompt_values(questions,lookup)
    _separate_shared_blank_compound_slots(questions,lookup)
    _group_merged_local_form_captions(questions,blocks,lookup)
    _group_nested_participant_record_captions(questions,blocks,lookup)
    _attach_record_object_context(questions,lookup)
    # An upstream grouping may place a fixed-prose child under an otherwise
    # valid field. Remove only that derived field, never its source text.
    def retain_business_fields(fields):
        retained=[]
        for f in fields:
            if not f.get('template_placeholder_candidates') and any(_s(lookup[r['block_id']]).get('resolved_layout_role') in FIXED_PROSE_ROLES for r in f.get('label_refs',[]) if r.get('block_id') in lookup):
                continue
            f['children']=retain_business_fields(f.get('children',[]))
            retained.append(f)
        return retained
    questions[:]=retain_business_fields(questions)
    questions.sort(key=lambda f:(lookup[f['label_refs'][0]['block_id']]['order'],f['label_refs'][0]['start']))


def _attach_record_object_context(fields,lookup):
    """Carry fixed row identities to each field and its logical subpositions.

    Row numbers identify positions, never reusable business entities. Repeated
    names in rows with no unique printed identity stay ambiguous for matching.
    """
    for parent in fields:
        contexts=defaultdict(list)
        for item in parent.get('record_context_values',[]):
            for source in item.get('refs',[]):
                b=lookup.get(source.get('block_id'),{})
                if (b and 0<=source.get('start',-1)<source.get('end',0)<=len(b.get('text',''))
                        and _s(b).get('fixed_record_context')):
                    contexts[item['record_index']].append(deepcopy(source))
        texts={index:'\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in refs)
               for index,refs in contexts.items()}
        counts=defaultdict(int)
        for text in texts.values():counts[re.sub(r'\s+',' ',text).casefold()]+=1

        def attach(field, inherited_index=None):
            index=field.get('record_index') or inherited_index
            if index is not None:
                field.setdefault('record_index',index)
                refs=contexts.get(index,[])
                regions=[lookup[r['block_id']] for r in field.get('answer_region_refs',[]) if r['block_id'] in lookup]
                # Only fixed context in the very same physical record row;
                # model references cannot borrow an adjacent person's role.
                refs=[r for r in refs if not regions or any(
                    _s(lookup[r['block_id']]).get('table_id')==_s(b).get('table_id') and
                    _row(lookup[r['block_id']])<=_row(b)<=_s(lookup[r['block_id']]).get('row_end',_row(lookup[r['block_id']]))
                    for b in regions)]
                for source in refs:
                    if source not in field.setdefault('context_refs',[]):field['context_refs'].append(source)
                identity=re.sub(r'\s+',' ',texts.get(index,'')).casefold()
                field['record_identity_status']='identified' if refs and counts[identity]==1 else 'ambiguous'
                field['record_identity_reason']='原文同一记录行有唯一职责或对象' if field['record_identity_status']=='identified' else '重复记录未提供唯一对象标识，不能仅凭列名自动复用其他行答案'
            for child in field.get('children',[]):attach(child,index)

        if parent.get('record_columns'):
            for child in parent.get('children',[]):attach(child)
        for child in parent.get('children',[]):
            if child.get('record_columns'):_attach_record_object_context([child],lookup)


def _group_nested_participant_record_captions(questions,blocks,lookup):
    """A participant roster caption describes its physical nested record table.

    Word requires a blank paragraph around nested tables. It is not a second
    answer to the roster title. Keep later prompts in the same cell intact.
    """
    remove=set()
    for owner in blocks:
        if owner.get('kind')!='table_cell':continue
        first=next((p for p in _s(owner).get('paragraph_spans',[]) if p.get('text','').strip()),None)
        if not first or not re.search(r'\b(?:attendees|participants)\s*:\s*$',first['text'],re.I):continue
        loc=owner.get('location',{});prefix=f"{loc.get('table')}.{loc.get('row')}.{loc.get('cell')}."
        nested=[b for b in blocks if b.get('kind')=='table_cell' and str(b.get('location',{}).get('table','')).startswith(prefix)]
        tables={_s(b).get('table_id') for b in nested}
        if len(tables)!=1:continue
        headers=[b for b in nested if _s(b).get('local_layout_type')=='record_table_header']
        if len(headers)<2 or not any(re.fullmatch(r'(?:Name|姓名)',b['text'].strip(),re.I) for b in headers):continue
        members=[f for f in questions if f.get('record_columns') and f.get('label_refs') and all(_s(lookup.get(r['block_id'],{})).get('table_id') in tables for r in f['label_refs'])]
        if not members:continue
        context=ref(owner,first['start'],first['end'])
        def add_context(f):
            if context not in f.setdefault('context_refs',[]):f['context_refs'].append(context)
            existing=f.get('context_label','')
            f['context_label']=first['text']+((' · '+existing) if existing and existing!=first['text'] else '')
            for child in f.get('children',[]):add_context(child)
        for f in members:add_context(f)
        for f in questions:
            if f.get('children') or f.get('answer_value_refs') or f.get('options'):continue
            if all(r['block_id']==owner['block_id'] and first['start']<=r['start']<r['end']<=first['end'] for r in f.get('label_refs',[])) and f.get('label_refs'):
                remove.add(f['field_id'])
        _s(owner).setdefault('fixed_caption_ranges',[]).append({'start':first['start'],'end':first['end'],'role':'nested_record_caption'})
    questions[:]=[f for f in questions if f['field_id'] not in remove]


def _group_merged_local_form_captions(questions,blocks,lookup):
    """A merged caption beside independently labelled forms is their context."""
    remove=set()
    for owner in blocks:
        s=_s(owner);first=_row(owner);last=s.get('row_end',first)
        if owner.get('kind')!='table_cell' or last<=first or not owner['text'].strip() or owner.get('controls'):continue
        members=[];covered=set()
        for f in questions:
            if len(f.get('label_refs',[]))!=1:continue
            b=lookup.get(f['label_refs'][0]['block_id'])
            if not b or _s(b).get('table_id')!=s.get('table_id') or not first<=_row(b)<=last or _col(b)<=_end(owner):continue
            if not f.get('answer_region_refs') or any(r['block_id']!=b['block_id'] for r in f['answer_region_refs']):continue
            if not any(slot.get('kind') in {'inline_prompt_candidate','inline_placeholder','blank_paragraph_candidate'} for slot in _s(b).get('logical_slots',[])):continue
            members.append(f);covered.add(_row(b))
        if len(members)<2 or covered!=set(range(first,last+1)):continue
        for f in members:
            context=f.setdefault('context_refs',[])
            if not any(r['block_id']==owner['block_id'] for r in context):context.append(ref(owner))
            f['context_label']=owner['text']
        _s(owner)['resolved_layout_role']='field_group_context'
        remove.add(owner['block_id'])
    questions[:]=[f for f in questions if not (not f.get('children') and not f.get('answer_value_refs') and any(r['block_id'] in remove for r in f.get('label_refs',[])))]


def _complete_contract_template_placeholders(blocks, additions, replaced):
    """Expose contract variables without authorizing replacement of prose.

    Standalone cover labels retain their text and gain an end-of-label slot.
    Inline variable words are proposals only: the source range must be
    explicitly confirmed through the separate mapping review API.
    """
    body=[b for b in blocks if b.get('location',{}).get('part')=='word/document.xml']
    opening=[b['text'].strip() for b in body if b.get('kind')=='paragraph' and b['text'].strip()][:12]
    if not any(re.fullmatch(r'[A-Za-z -]{0,50}(?:Agreement|Contract)',t,re.I) for t in opening):return
    role_words=r'(?:Vendor|Supplier|Manufacturer|Customer|Buyer)'
    standalone=re.compile(role_words+r' (?:Name(?: & Address)?|Address)$')
    variables=re.compile(r'\b(?:'+role_words+r' (?:Name|Address)|(?:Material|Product) Name)\b|<[A-Z][A-Z0-9 _/-]{1,40}>')
    for bi,b in enumerate(body):
        text=b['text'];s=_s(b)
        if b.get('controls'):continue
        own_cover=bool(standalone.fullmatch(text.strip()) and (b.get('kind')=='paragraph' or s.get('table_column_count')==1))
        nearby_company=next((x for x in reversed(body[:bi]) if x['text'].strip()),None)
        customer_owner=next((x for x in body if s.get('table_column_count')==1 and _s(x).get('table_id')==s.get('table_id') and _row(x)<_row(b) and re.search(r'\b(?:Ltd\.?|Limited|Inc\.?)',x.get('text',''))),None)
        if b.get('kind')=='paragraph' and nearby_company and re.search(r'\b(?:Ltd\.?|Limited|Inc\.?)',nearby_company['text']):customer_owner=nearby_company
        customer_address=bool(text.strip()=='Address' and customer_owner)
        if own_cover or customer_address:
            a=len(text);slot_id=f"{s.get('cell_id') or b['block_id']}:contract-cover:after-label"
            slot={'kind':'inline_prompt_candidate','slot_id':slot_id,'start':a,'end':a,'offset':a,'label_start':0,'label_end':a,'prompt_text':text}
            if len(s.get('paragraph_spans',[]))==1:slot.update(paragraph_index=1,paragraph_id=s['paragraph_spans'][0]['paragraph_id'])
            s.setdefault('logical_slots',[]).append(slot);s['protected_label_ranges']=[{'start':0,'end':a}]
            f=new_field(b,ref(b),[ref(b,a,a,slot_id)]);f['boundary_evidence']='contract_cover_label_with_local_response'
            if own_cover:
                party=text.strip().split()[0].casefold()
                s.update(column_owner_role='our_side' if party in {'vendor','supplier','manufacturer'} else 'customer',
                         column_owner_ref=b['block_id'],owner_evidence='explicit_contract_cover_party_label')
            if customer_address:
                s.update(column_owner_role='customer',column_owner_ref=customer_owner['block_id'])
                f['context_refs']=[ref(customer_owner)]
            additions[:]=[f for f in additions if not any(r['block_id']==b['block_id'] for r in f.get('label_refs',[]))]
            additions.append(f);replaced.add(b['block_id']);continue
        if b.get('kind')!='paragraph':continue
        context=bool(re.search(r'\bagreement\b.*\b(?:entered|between)\b',text,re.I) or re.search(r'\b(?:Material|Product) Name\s+\(PRODUCT\)',text))
        spans=[m for m in variables.finditer(text) if context or m[0].startswith('<')]
        if not spans:continue
        for m in spans:
            a,z=m.span();slot_id=f"{s.get('cell_id') or b['block_id']}:template-placeholder:{a}:{z}"
            slot={'kind':'template_placeholder_candidate','slot_id':slot_id,'start':a,'end':z,'placeholder_text':m[0]}
            if not any(x.get('slot_id')==slot_id for x in s.get('logical_slots',[])):s.setdefault('logical_slots',[]).append(slot)
            f=new_field(b,ref(b,a,z));reason='合同模板中的变量文字；需确认此原文范围允许替换，不能按已有答案或普通题目标签写入'
            f.update(answer_status='unresolved',review_status='needs_review',review_reasons=[reason],
                template_placeholder_candidates=[{'block_id':b['block_id'],'start':a,'end':z,'expected_text':m[0],'reason':reason}],
                boundary_evidence='unconfirmed_contract_template_variable')
            additions.append(f)
        replaced.add(b['block_id'])


def _separate_shared_blank_compound_slots(fields, lookup):
    """Several printed labels may face one empty paragraph with line breaks.

    Native paragraph IDs alone cannot distinguish those logical lines. Keep
    the established compound relation, but give each child a distinct line in
    the verified right-hand empty cell. Never split a populated/control cell.
    """
    for parent in fields:
        children=parent.get('children',[])
        _separate_shared_blank_compound_slots(children,lookup)
        if len(children)<2 or any(c.get('children') or len(c.get('label_refs',[]))!=1 or len(c.get('answer_region_refs',[]))!=1 for c in children):continue
        labels=[c['label_refs'][0] for c in children]
        regions=[c['answer_region_refs'][0] for c in children]
        if len({r['block_id'] for r in labels})!=1 or len({r['block_id'] for r in regions})!=1:continue
        label=lookup.get(labels[0]['block_id']);target=lookup.get(regions[0]['block_id'])
        if not label or not target or label is target or target['text'].strip() or target.get('controls'):continue
        if label.get('kind')!='table_cell' or target.get('kind')!='table_cell':continue
        if _s(label).get('table_id')!=_s(target).get('table_id') or _row(label)!=_row(target) or _col(target)<=_s(label).get('grid_col_end',_col(label)):continue
        if any(c.get('answer_value_refs') or c.get('answer_text_original','').strip() or c.get('boundary_evidence')!='aligned_native_paragraph_slots' for c in children):continue
        # Only repair genuinely colliding native targets, not separate blank
        # paragraphs which already have safe distinct locations.
        if len({(r.get('slot_id'),r.get('start'),r.get('end')) for r in regions})==len(regions):continue
        ranges=sorted((r['start'],r['end']) for r in labels)
        if any(a2<z1 for (a1,z1),(a2,z2) in zip(ranges,ranges[1:])):continue
        for index,child in enumerate(sorted(children,key=lambda c:c['label_refs'][0]['start'])):
            slot_id=f"{_s(target).get('cell_id') or target['block_id']}:logical-line:{index}"
            slot={'slot_id':slot_id,'kind':'blank_line_slot','start':0,'end':0,'offset':0,
                  'line_index':index,'line_count':len(children),'label_ref':deepcopy(child['label_refs'][0]),
                  'evidence':'aligned_native_compound_labels','expected_empty_cell':True}
            if not any(s.get('slot_id')==slot_id for s in _s(target).get('logical_slots',[])):
                _s(target).setdefault('logical_slots',[]).append(slot)
            child['answer_region_refs']=[ref(target,0,0,slot_id)]
            child['boundary_evidence']='native_parallel_prompt_lines'


def _complete_separator_forms(blocks, additions, replaced):
    """Keep punctuation-only divider cells out of answer values.

    Consecutive real blank cells after a colon are a local response lane.
    Its largest native grid interval is the write anchor, avoiding narrow
    layout cells. Multiple labels require equally many printed separators;
    each receives a stable logical line in the same originally empty cell.
    """
    tables=defaultdict(lambda:defaultdict(list))
    for b in blocks:
        if b.get('kind')=='table_cell' and b.get('location',{}).get('type')=='docx' and not _s(b).get('is_merge_continuation'):
            tables[_s(b).get('table_id')][_row(b)].append(b)
    for rows in tables.values():
        for rn,raw in rows.items():
            row=sorted(raw,key=_col)
            if len(row)<3 or not row[0]['text'].strip() or row[0].get('controls'):continue
            label=row[0];response=row
            if not re.fullmatch(r'[\s:：]+',row[1]['text']):
                below=sorted(rows.get(rn+1,[]),key=_col)
                if _s(label).get('row_end',rn)>rn and len(below)==len(row)-1 and _col(below[0])==_col(row[1]):below=[label,*below]
                if not below or len(below)!=len(row) or (below[0] is not label and below[0]['text'].strip()) or not re.fullmatch(r'[\s:：]+',below[1]['text']):continue
                if any(b['text'].strip() or b.get('controls') for b in row[1:]):continue
                response=below
            blank=response[2:]
            if any(b['text'].strip() or b.get('controls') or _s(b).get('blank_state')!='blank' for b in blank):continue
            meaningful=[p for p in _s(label).get('paragraph_spans',[]) if p.get('text','').strip()]
            separators=re.findall(r'[:：]',response[1]['text'])
            if len(meaningful)!=len(separators):continue
            separator_paragraphs=_s(response[1]).get('paragraph_spans',[])
            colon_paragraphs=[p for p in separator_paragraphs if p.get('text','').strip()]
            native_line_layout=(len(colon_paragraphs)==len(meaningful) and all(
                re.fullmatch(r'\s*[:：]\s*',p.get('text','')) and p.get('paragraph_index') is not None
                for p in colon_paragraphs))
            def width(b):
                x=_s(b).get('grid_x_twips') or [0,0]
                return x[1]-x[0]
            target=max(blank,key=width)
            if len(meaningful)>1 and any(not p.get('blank') for p in _s(target).get('paragraph_spans',[])):continue
            made=[]
            for index,p in enumerate(meaningful):
                rr=ref(target)
                if len(meaningful)>1:
                    slot_id=f"{_s(target)['cell_id']}:logical-line:{index}"
                    slot={'slot_id':slot_id,'kind':'blank_line_slot','start':0,'end':0,'offset':0,
                        'line_index':index,'line_count':len(meaningful),'label_ref':ref(label,p['start'],p['end']),
                        'evidence':'parallel_label_and_colon_lane','expected_empty_cell':True}
                    if native_line_layout:
                        slot.update(layout_paragraph_index=colon_paragraphs[index]['paragraph_index'],
                                    layout_paragraph_count=len(separator_paragraphs),
                                    separator_block_id=response[1]['block_id'])
                    _s(target).setdefault('logical_slots',[]).append(slot)
                    rr=ref(target,0,0,slot_id)
                f=new_field(label,ref(label,p['start'],p['end']),[rr])
                f.update(boundary_evidence='native_parallel_prompt_lines' if len(meaningful)>1 else 'native_colon_separator_lane',
                         response_lane_refs=[ref(b) for b in blank],context_refs=[])
                made.append(f)
            if _row(target)!=rn:_s(target)['native_label_block_id']=label['block_id']
            additions[:]=[f for f in additions if not any(r['block_id']==label['block_id'] for r in f.get('label_refs',[]))]
            additions.extend(made);replaced.update([label['block_id'],response[1]['block_id']])


def _complete_wide_table_prompts(blocks, additions, replaced):
    tables=defaultdict(lambda:defaultdict(list))
    for b in blocks:
        if b.get('kind')=='table_cell' and b.get('location',{}).get('type')=='docx' and not _s(b).get('is_merge_continuation'):
            tables[_s(b).get('table_id')][_row(b)].append(b)
    for rows in tables.values():
        for row in rows.values():
            right=max((_s(b).get('grid_x_twips',[0,0])[1] for b in row),default=0)
            for b in row:
                s=_s(b);text=b['text']
                if b.get('controls') or s.get('resolved_layout_role') in FIXED_PROSE_ROLES:continue
                slots=[x for x in s.get('logical_slots',[]) if x.get('kind') in {'inline_placeholder','inline_prompt_candidate'} and x.get('label_start') is not None]
                if len(slots)!=1:continue
                covered=set()
                for slot in slots:covered.update(range(slot['label_start'],slot['label_end']));covered.update(range(slot['start'],slot['end']))
                if any(ch.isalnum() and i not in covered for i,ch in enumerate(text)):continue
                has_placeholder=any(x['kind']=='inline_placeholder' for x in slots)
                x=s.get('grid_x_twips',[0,0]);wide=right and x[1]-x[0]>=right*.5
                if not has_placeholder and (not wide or any(other['text'].strip() or other.get('controls') for other in row if other is not b)):continue
                if re.search(r'\bI hereby (?:attest|certify)|\bhandwritten\b|\bNote\s*:',text,re.I):continue
                made=[new_field(b,ref(b,slot['label_start'],slot['label_end']),[ref(b,slot['start'],slot['end'],slot['slot_id'])]) for slot in slots]
                s['protected_label_ranges']=[{'start':slot['label_start'],'end':slot['label_end']} for slot in slots]
                for f in made:f['boundary_evidence']='native_explicit_table_prompt'
                additions[:]=[f for f in additions if not any(r['block_id']==b['block_id'] for r in f.get('label_refs',[]))]
                additions.extend(made);replaced.add(b['block_id'])


def _complete_audit_closing_forms(blocks, additions, replaced):
    tables=defaultdict(lambda:defaultdict(list))
    for b in blocks:
        if b.get('kind')=='table_cell' and not _s(b).get('is_merge_continuation'):
            tables[_s(b).get('table_id')][_row(b)].append(b)
    binary=re.compile(r'^\s*(?P<yes>[A-Za-z][A-Za-z ]{1,35}?)\s*/\s*(?P<no>Not\s+(?P=yes))\s*$',re.I)
    for rows in tables.values():
        recent=None
        for rn,raw in sorted(rows.items()):
            row=sorted(raw,key=_col)
            # Every title in a parallel signing row has its own native slot;
            # a neighbouring Date label is not somebody's existing signature.
            words=[b for b in row if b['text'].strip()]
            completed_signing_cells = set()
            for b in words:
                slots = [s for s in _s(b).get('logical_slots', [])
                         if s.get('kind') == 'inline_prompt_candidate' and s.get('label_start') is not None]
                if len(slots) < 2 or b.get('controls') or b.get('location', {}).get('type') != 'docx':
                    continue
                captions = [b['text'][s['label_start']:s['label_end']].strip(' :：') for s in slots]
                signs = [s for s, caption in zip(slots, captions)
                         if re.fullmatch(r'sign(?:ature)?(?:\s*(?:/|&|and)\s*date)?', caption, re.I)]
                metadata = r'(?:printed\s+)?name|department\s*/\s*title|job\s*title|title|position|date|place'
                metadata += r'|(?:completed|filled|prepared|reviewed|approved)\s+by(?:\s*\(name\))?'
                metadata += r'|(?:reviewer|auditor)\s+by\s+[a-z ]{1,35}\s*\(name\)'
                metadata += r'|sign(?:ature)?(?:\s*(?:/|&|and)\s*date)?'
                if not signs or any(not re.fullmatch(metadata, caption, re.I) for caption in captions):
                    continue
                if any(b['text'][s['start']:s['end']].strip() for s in slots):
                    continue
                covered = set()
                for s in slots: covered.update(range(s['label_start'], s['end']))
                if any(ch.isalnum() and i not in covered for i, ch in enumerate(b['text'])):
                    continue
                # The native parser already located each blank colon prompt.
                # Preserve all prompts instead of taking only the final one.
                additions[:] = [f for f in additions if not any(r['block_id'] == b['block_id'] for r in f.get('label_refs', []))]
                _s(b)['protected_label_ranges'] = [{'start': s['label_start'], 'end': s['label_end']} for s in slots]
                for slot in slots:
                    f = new_field(b, ref(b, slot['label_start'], slot['label_end']),
                                  [ref(b, slot['start'], slot['end'], slot['slot_id'])])
                    f['boundary_evidence'] = 'native_multiline_signing_prompts'
                    f['context_refs'] = [ref(b, slots[0]['label_start'], slots[0]['label_end'])]
                    f['signatory_context_refs'] = [ref(b, s['label_start'], s['label_end']) for s in signs]
                    additions.append(f)
                completed_signing_cells.add(b['block_id']); replaced.add(b['block_id'])
            # Mirrored party signing columns have their own explicit prompts;
            # the empty gutter between them is spacing, not a response cell.
            mirrored=False
            if len(row)==3 and len(words)==2 and row[1] not in words:
                prompt_sets=[[s for s in _s(b).get('logical_slots',[]) if s.get('kind')=='inline_prompt_candidate'] for b in words]
                signatures=[tuple(re.sub(r'\s+',' ',b['text'][s['label_start']:s['label_end']].strip()).casefold() for s in slots) for b,slots in zip(words,prompt_sets)]
                mirrored=bool(signatures[0] and signatures[0]==signatures[1] and all(re.search(r'\bsignee\b|\bsign(?:ature)?\s*[/&]',x,re.I) for x in signatures[0]))
                if mirrored:
                    for b,slots in zip(words,prompt_sets):
                        additions[:]=[f for f in additions if not any(r['block_id']==b['block_id'] for r in f.get('label_refs',[]))]
                        _s(b)['protected_label_ranges']=[{'start':s['label_start'],'end':s['label_end']} for s in slots]
                        for slot in slots:
                            f=new_field(b,ref(b,slot['label_start'],slot['label_end']),[ref(b,slot['start'],slot['end'],slot['slot_id'])])
                            f['boundary_evidence']='native_mirrored_signing_columns';additions.append(f)
                        replaced.add(b['block_id'])
                    _s(row[1])['resolved_layout_role']='signing_column_gutter'
                    continue
            if recent is not None and rn==_row(recent)+1 and len(words)==1 and re.fullmatch(r'from the [\w -]{2,35}',words[0]['text'].strip(),re.I):
                _s(words[0])['resolved_layout_role']='label_continuation';replaced.add(words[0]['block_id'])
            signing=any(re.search(r'\bsignature\s*:',b['text'],re.I) for b in words)
            if signing and len(words)>=2 and all(len(b['text'])<100 and not b.get('controls') for b in words):
                for b in words:
                    if b['block_id'] in completed_signing_cells: continue
                    slot=next((s for s in _s(b).get('logical_slots',[]) if s.get('kind') in {'inline_prompt_candidate','trailing_fill_candidate'} and s.get('end',s.get('offset'))==len(b['text'])),None)
                    if not slot:continue
                    lr=ref(b,slot.get('label_start',0),slot.get('label_end',len(b['text'])))
                    a=slot.get('start',slot.get('offset'));z=slot.get('end',a)
                    f=new_field(b,lr,[ref(b,a,z,slot['slot_id'])]);f['boundary_evidence']='native_parallel_signing_prompts'
                    f['context_refs']=[ref(x) for x in words if x is not b]
                    additions[:]=[x for x in additions if not any(r['block_id']==b['block_id'] for r in x.get('label_refs',[]))]
                    additions.append(f);replaced.add(b['block_id'])
            if len(row)==2:
                label,choice=row;match=binary.fullmatch(choice['text'])
                if match and not choice.get('controls'):
                    if label['text'].strip():recent=label
                    if recent is None or rn-_row(recent)>4:continue
                    f=new_field(recent,ref(recent));f['field_id']=f['question_id']=f['field_id']+':choice:'+choice['block_id']
                    f.update(boundary_evidence='native_binary_option_lane',normalized_label=choice['text'].strip(),answer_status='blank',context_refs=[ref(choice)],options=[])
                    for group in ['yes','no']:
                        a,z=match.span(group);slot_id=f"{choice['block_id']}:printed-option:{a}"
                        _s(choice).setdefault('logical_slots',[]).append({'kind':'printed_option','slot_id':slot_id,'start':a,'end':z,'option_label':choice['text'][a:z]})
                        rr=ref(choice,a,z,slot_id)
                        f['options'].append({'refs':[ref(choice,a,z)],'answer_region_refs':[rr],'text_original':choice['text'][a:z],'selected':False,'selection_source':'printed_choice_list'})
                        f['answer_region_refs'].append(rr)
                    additions.append(f);replaced.add(recent['block_id']);replaced.add(choice['block_id'])
                elif recent is not None and rn==_row(recent)+1 and re.fullmatch(r'from the [\w -]{2,35}',label['text'].strip(),re.I) and not choice['text'].strip():
                    _s(label)['resolved_layout_role']='label_continuation';replaced.add(label['block_id'])
            for b in words:
                text=b['text'].strip()
                fixed=bool(re.fullmatch(r'\(?handwritten signature.*\)?',text,re.I) or re.match(r'I hereby (?:attest|certify)\b.*:\s*$',text,re.I))
                if fixed and any(abs(_row(c)-rn)<=5 and re.search(r'\bsignature\s*:',c['text'],re.I) for rr in rows.values() for c in rr):
                    _s(b)['resolved_layout_role']='signature_instruction';replaced.add(b['block_id'])
                    additions[:]=[f for f in additions if not any(r['block_id']==b['block_id'] for r in f.get('label_refs',[]))]


def _complete_local_column_records(blocks, additions, replaced):
    """Recover local column headers backed by aligned native blank records.

    Two explicit layouts qualify: an empty corner above named row owners,
    or a vertically merged context cell beside column names and a blank row.
    Never infer arbitrary adjacent filled cells as headers.
    """
    tables=defaultdict(lambda:defaultdict(list))
    for b in blocks:
        if b.get('kind')=='table_cell' and b.get('location',{}).get('type')=='docx' and not _s(b).get('is_merge_continuation'):
            tables[_s(b).get('table_id')][_row(b)].append(b)
    for rows in tables.values():
        for rn,raw in rows.items():
            row=sorted(raw,key=_col)
            if len(row)<3:continue
            corner,headers=row[0],row[1:]
            if any(not b['text'].strip() or b.get('controls') or len(b['text'])>100 for b in headers):continue
            merged_context=bool(corner['text'].strip() and _s(corner).get('row_end',rn)>rn)
            empty_corner=not corner['text'].strip() and not corner.get('controls')
            if not merged_context and not empty_corner:continue
            records=[]
            for next_rn in range(rn+1,max(rows)+1):
                rr=sorted(rows.get(next_rn,[]),key=_col)
                if merged_context:
                    if next_rn>_s(corner).get('row_end',rn):break
                    owner=corner;targets=rr
                else:
                    if len(rr)!=len(row) or not rr[0]['text'].strip():break
                    owner=rr[0];targets=rr[1:]
                if len(targets)!=len(headers):break
                if any((_col(h),_end(h))!=(_col(t),_end(t)) or t['text'].strip() or t.get('controls') or _s(t).get('blank_state')!='blank' for h,t in zip(headers,targets)):break
                records.append((owner,targets))
            if not records or (empty_corner and len(records)<2):continue
            for h in headers:_s(h).update(local_layout_type='record_table_header',record_header_row=rn)
            replaced.update(b['block_id'] for b in row)
            for index,(owner,targets) in enumerate(records,1):
                replaced.add(owner['block_id'])
                for col,(header,target) in enumerate(zip(headers,targets),1):
                    _s(target).update(local_layout_type='record_table',record_header_row=rn,record_index=index)
                    f=new_field(header,ref(header),[ref(target)])
                    f['field_id']=f['question_id']=f['field_id']+':record:'+target['block_id']
                    f.update(record_index=index,record_column_index=col,record_slot_status='reserved',
                        context_refs=[ref(owner)],context_label=owner['text'],boundary_evidence='native_local_column_record')
                    additions.append(f)


def _complete_spanning_record_rows(blocks, questions, additions):
    """Retain blank records beneath a physically merged record owner."""
    tables=defaultdict(list)
    for b in blocks:
        if b.get('kind')=='table_cell' and not _s(b).get('is_merge_continuation'):
            tables[_s(b).get('table_id')].append(b)
    def flatten(fs):
        for f in fs:yield f;yield from flatten(f.get('children',[]))
    existing={r['block_id'] for f in flatten([*questions,*additions]) for r in f.get('answer_region_refs',[])}
    for cells in tables.values():
        for owner in cells:
            s=_s(owner);rn=_row(owner);last=s.get('row_end',rn)
            if last<=rn or not owner['text'].strip():continue
            header_row=s.get('record_header_row')
            if not header_row:continue
            headers=[b for b in cells if _row(b)==header_row and _col(b)>_end(owner) and _s(b).get('local_layout_type')=='record_table_header']
            if len(headers)<2:continue
            for row in range(rn+1,last+1):
                for index,h in enumerate(sorted(headers,key=_col),2):
                    matches=[b for b in cells if _row(b)==row and (_col(b),_end(b))==(_col(h),_end(h))]
                    if len(matches)!=1:continue
                    target=matches[0]
                    if target['block_id'] in existing or target['text'].strip() or target.get('controls') or _s(target).get('blank_state')!='blank':continue
                    _s(target).update(local_layout_type='record_table',record_header_row=header_row,record_index=row-header_row)
                    f=new_field(h,ref(h),[ref(target)])
                    f['field_id']=f['question_id']=f['field_id']+':record:'+target['block_id']
                    f.update(record_index=row-header_row,record_column_index=index,record_slot_status='reserved',
                        context_refs=[ref(owner)],context_label=owner['text'],boundary_evidence='native_spanning_owner_record')
                    additions.append(f);existing.add(target['block_id'])


def _complete_native_choice_rows(blocks, additions, replaced):
    from .native_choices import _control_options
    tables=defaultdict(lambda:defaultdict(list))
    for b in blocks:
        if b.get('kind')=='table_cell' and not _s(b).get('is_merge_continuation'):
            tables[_s(b).get('table_id')][_row(b)].append(b)
    for rows in tables.values():
        for raw in rows.values():
            row=sorted(raw,key=_col)
            if len(row)!=2:continue
            label,answer=row
            controls=[c for c in answer.get('controls',[]) if c.get('source')=='legacy_form_field' and c.get('type')=='checkbox']
            ps=[p for p in _s(answer).get('paragraph_spans',[]) if p.get('text','').strip()]
            if not label['text'].strip() or label.get('controls') or len(controls)<2 or any(not any(c.get('paragraph_index')==p['paragraph_index'] for c in controls) for p in ps):continue
            if len([p for p in _s(label).get('paragraph_spans',[]) if p.get('text','').strip()])!=1:continue
            if any(s.get('kind')=='inline_placeholder' for s in _s(label).get('logical_slots',[])):continue
            if any(s.get('kind')=='inline_placeholder' for s in _s(answer).get('logical_slots',[])):continue
            options,issue=_control_options(answer,0,len(answer['text']),controls)
            if issue or len(options)!=len(controls):continue
            f=new_field(label,ref(label),[ref(answer)])
            f['boundary_evidence']='native_control_option_row'
            additions[:]=[x for x in additions if not any(r['block_id'] in {label['block_id'],answer['block_id']} for r in x.get('label_refs',[]))]
            additions.append(f);replaced.update([label['block_id'],answer['block_id']])


def _complete_choice_explanation_rows(blocks,additions,replaced):
    """A printed conditional explanation belongs after the local options."""
    from .native_choices import PLAIN_CHOICES
    tables=defaultdict(lambda:defaultdict(list))
    for b in blocks:
        if b.get('kind')=='table_cell' and not _s(b).get('is_merge_continuation'):
            tables[_s(b).get('table_id')][_row(b)].append(b)
    for rows in tables.values():
        for raw in rows.values():
            row=sorted(raw,key=_col)
            if len(row)!=2:continue
            label,answer=row;ps=[p for p in _s(label).get('paragraph_spans',[]) if p['text'].strip()]
            if len(ps)==1 and len(label['text'].splitlines())==2:
                first,second=label['text'].splitlines();ps=[{'text':first,'start':0,'end':len(first)},
                    {'text':second,'start':len(first)+1,'end':len(label['text'])}]
            if len(ps)!=2 or answer.get('controls') or not PLAIN_CHOICES.fullmatch(answer['text']):continue
            condition=re.match(r'If\s+(Yes|No)\s*[,;:]?\s*(?:please\s+)?(?:explain|describe|specify|provide)\b',ps[1]['text'],re.I)
            if not condition:continue
            slot=next((s for s in _s(answer).get('logical_slots',[]) if s.get('kind')=='trailing_fill_candidate' and s.get('offset')==len(answer['text'])),None)
            if not slot:continue
            parent=new_field(label,ref(label,ps[0]['start'],ps[0]['end']),[ref(answer)])
            child=new_field(label,ref(label,ps[1]['start'],ps[1]['end']),[ref(answer,slot['offset'],slot['offset'],slot['slot_id'])],parent['field_id'])
            child.update(boundary_evidence='conditional_response_after_local_choice_list',
                explicit_condition={'parent_field_id':parent['field_id'],'equals_option':condition[1].title(),'source_text':ps[1]['text'],'status':'unresolved'})
            parent['children']=[child]
            _s(label)['protected_label_ranges']=[{'start':p['start'],'end':p['end']} for p in ps]
            additions[:]=[f for f in additions if not any(r['block_id']==label['block_id'] for r in f.get('label_refs',[]))]
            additions.append(parent);replaced.add(label['block_id'])


def _complete_parallel_control_lanes(blocks, additions, replaced):
    """Associate parallel paragraphs only inside one physical table row.

    A label lane next to a control-only lane is not a prefilled answer.
    Extra unlabelled controls are retained diagnostically, never assigned an
    invented meaning. Native paragraph identity is required for each option.
    """
    tables=defaultdict(lambda:defaultdict(list))
    for b in blocks:
        if b.get('kind')=='table_cell' and not _s(b).get('is_merge_continuation'):
            tables[_s(b).get('table_id')][_row(b)].append(b)
    for rows in tables.values():
        for row in rows.values():
            row=sorted(row,key=_col)
            if len(row)!=3:continue
            prompt,labels,control=row
            controls=[c for c in control.get('controls',[]) if c.get('source')=='legacy_form_field' and c.get('type')=='checkbox']
            ps=[p for p in _s(labels).get('paragraph_spans',[]) if p.get('text','').strip()]
            if not prompt['text'].strip() or labels.get('controls') or control['text'].strip() or len(ps)<2 or len(controls)<len(ps):continue
            by_pi=defaultdict(list)
            for c in controls:by_pi[c.get('paragraph_index')].append(c)
            if any(len(by_pi[p['paragraph_index']])!=1 for p in ps):continue
            f=new_field(prompt,ref(prompt));f.update(boundary_evidence='native_parallel_control_lanes',options=[],answer_status='blank')
            used=set()
            for p in ps:
                c=by_pi[p['paragraph_index']][0];used.add((c['paragraph_index'],c['control_index']))
                lr=ref(labels,p['start'],p['end'])
                slots=[s for s in _s(labels).get('logical_slots',[]) if s.get('kind')=='inline_placeholder' and s.get('paragraph_index')==p['paragraph_index']]
                if slots:
                    lr['end']=len(labels['text'][:slots[0]['start']].rstrip())
                    child=new_field(labels,deepcopy(lr),[ref(labels,s['start'],s['end'],s['slot_id']) for s in slots],f['field_id'])
                    child['explicit_condition']={'parent_field_id':f['field_id'],'source_text':labels['text'][lr['start']:lr['end']],'status':'unresolved'}
                    f['children'].append(child)
                identity=f"p{c['paragraph_index']}:i{c['control_index']}"
                rr=ref(control,0,0,f"{control['block_id']}:control:{identity}")
                rr.update(control_id=c.get('control_id'),control_index=c['control_index'],control_paragraph_index=c['paragraph_index'])
                f['options'].append({'refs':[lr],'answer_region_refs':[rr],'text_original':labels['text'][lr['start']:lr['end']],
                    'selected':c.get('selected'),'selection_source':'native_control'})
                f['answer_region_refs'].append(rr)
                if c.get('selected') is True:f['answer_value_refs'].append(deepcopy(lr))
            f['answer_refs']=deepcopy(f['answer_value_refs'])
            f['answer_text_original']='\n'.join(labels['text'][r['start']:r['end']] for r in f['answer_value_refs'])
            f['answer_status']='unresolved' if any(o['selected'] is None for o in f['options']) else 'filled' if f['answer_value_refs'] else 'blank'
            extras=[c for c in controls if (c['paragraph_index'],c['control_index']) not in used]
            if extras:
                f['unmapped_control_refs']=[{'block_id':control['block_id'],'control_id':c.get('control_id'),'control_paragraph_index':c['paragraph_index'],'control_index':c['control_index']} for c in extras]
                reason=f'同一答复列另有 {len(extras)} 个无原文标签控件，未分配选项含义'
                f['pairing_evidence']='native_choice_regions'
                f['selection_review_reasons']=[reason]
                f['review_reasons'].append(reason)
            _s(control)['local_layout_type']='choice_matrix'
            additions[:]=[x for x in additions if not any(r['block_id'] in {prompt['block_id'],labels['block_id']} for r in x.get('label_refs',[]))]
            additions.append(f);replaced.update([prompt['block_id'],labels['block_id']])


def _complete_vertical_regions(questions,blocks,additions,replaced):
    tables=defaultdict(lambda:defaultdict(list))
    proposed_labels={r['block_id'] for f in [*questions,*additions] for r in f.get('label_refs',[])}
    for b in blocks:
        s=_s(b)
        if b.get('kind')=='table_cell' and b.get('location',{}).get('type')=='docx' and not s.get('is_merge_continuation'):
            tables[s.get('table_id')][_row(b)].append(b)
    for rows in tables.values():
        for rn,row in rows.items():
            below=rows.get(rn+1,[])
            if len(row)!=1 or len(below)!=1:continue
            label,target=row[0],below[0];ls,ts=_s(label),_s(target)
            if not label['text'].strip() or len(label['text'].strip())>160 or label.get('controls'):continue
            if target['text'].strip() or target.get('controls') or ts.get('content_state') not in {'empty','blank',None}:continue
            if label['block_id'] not in proposed_labels and len(ts.get('paragraph_spans',[]))<2:continue
            if (_col(label),_end(label))!=(_col(target),_end(target)):continue
            if ls.get('column_span',1)!=ls.get('table_column_count',1):continue
            if ts.get('blank_state') in {'structural_spacer','merge_continuation','unreadable'}:continue
            f=new_field(label,ref(label),[ref(target)])
            f['boundary_evidence']='full_width_prompt_followed_by_bordered_blank_row'
            additions[:]=[x for x in additions if not any(r['block_id']==label['block_id'] for r in x.get('label_refs',[]))]
            additions.append(f);replaced.add(label['block_id'])
            ls['protected_label_ranges']=[{'start':0,'end':len(label['text'])}]
    for f in questions:
        if f.get('children') or f.get('answer_region_refs') or f.get('answer_value_refs'):continue
        refs=f.get('label_refs',[])
        if len(refs)!=1:continue
        b=next((b for b in blocks if b['block_id']==refs[0]['block_id']),None)
        if not b or b.get('kind')!='table_cell' or b.get('controls'):continue
        s=_s(b);ps=s.get('paragraph_spans',[]);meaningful=[p for p in ps if p.get('text','').strip()]
        if len(meaningful)!=1 or len(meaningful[0]['text'])>120:continue
        p=meaningful[0]
        if refs[0]['start']!=p['start'] or refs[0]['end']!=p['end']:continue
        slot=next((x for x in s.get('logical_slots',[]) if x['kind']=='blank_paragraph_candidate' and x.get('paragraph_index',0)>p['paragraph_index']),None)
        if not slot:continue
        f['answer_region_refs']=[ref(b,slot['start'],slot['end'],slot['slot_id'])]
        f['answer_status']='blank';f['boundary_evidence']='confirmed_label_followed_by_native_blank_paragraph'


def _complete_segmented_cell_forms(blocks,lookup,additions,replaced,questions=()):
    """Local prompt/blank and prompt/checkbox groups inside one physical cell.

    Blank paragraph IDs establish the response position. Neither a subsequent
    heading nor another group's checkbox may become the preceding answer.
    """
    previous_context = None
    previous_table = None
    for b in blocks:
        if b.get('kind')!='table_cell' or b.get('location',{}).get('type')!='docx':continue
        s=_s(b);ps=s.get('paragraph_spans',[])
        if s.get('table_id')!=previous_table:
            previous_context=None;previous_table=s.get('table_id')
        meaningful=[p for p in ps if p.get('text','').strip()]
        ctrl_ps={c.get('paragraph_index') for c in b.get('controls',[]) if c.get('source')=='legacy_form_field'}
        prompts=[];groups=[]
        for index,p in enumerate(meaningful):
            pi=p['paragraph_index']
            if pi in ctrl_ps:continue
            following=meaningful[index+1:]
            next_p=following[0] if following else None
            if next_p and next_p['paragraph_index'] in ctrl_ps:
                members=[]
                for candidate in following:
                    if candidate['paragraph_index'] not in ctrl_ps:break
                    members.append(candidate)
                groups.append((p,members))
                continue
            if not p['text'].rstrip().endswith((':','：')) or len(p['text'])>120:continue
            next_index=next_p['paragraph_index'] if next_p else len(ps)+1
            slot=next((x for x in s.get('logical_slots',[]) if x['kind']=='blank_paragraph_candidate' and pi<x.get('paragraph_index',0)<next_index),None)
            if slot is None:
                slot=next((x for x in s.get('logical_slots',[]) if x['kind']=='inline_prompt_candidate' and x.get('paragraph_index')==pi and x['start']==x['end']==p['end']),None)
            if slot:prompts.append((p,slot))
        # Leave simple one-label forms to the ordinary boundary resolver. This
        # path is for several independent regions within the same physical cell.
        if len(prompts)+len(groups)<2:continue
        # In a label column, intervening blank paragraphs may be alignment
        # spacing for the response cell to the right. Native controls prove a
        # same-cell form; otherwise require a cell spanning its local row.
        if not groups and s.get('column_span',1)!=s.get('table_column_count',1):continue
        existing=[f for f in [*questions,*additions] if any(r['block_id']==b['block_id'] for r in f.get('label_refs',[]))]
        additions[:]=[f for f in additions if not any(r['block_id']==b['block_id'] for r in f.get('label_refs',[]))]
        replaced.add(b['block_id'])
        s['protected_label_ranges']=[]
        local=[]
        for p,slot in prompts:
            lr=ref(b,slot.get('label_start',p['start']),slot.get('label_end',p['end']))
            f=new_field(b,lr,[ref(b,slot['start'],slot['end'],slot['slot_id'])])
            local.append((p['paragraph_index'],f))
        for p,members in groups:
            f=new_field(b,ref(b,p['start'],p['end']),[ref(b,m['start'],m['end']) for m in members])
            f['boundary_evidence']='native_local_checkbox_group'
            local.append((p['paragraph_index'],f))
        group_ids={p['paragraph_index'] for p,_ in groups}
        for pi,f in sorted(local):
            if pi in group_ids:
                previous_context=deepcopy(f['label_refs'][0])
            elif previous_context:
                context=lookup[previous_context['block_id']]['text'][previous_context['start']:previous_context['end']]
                f['context_refs']=[deepcopy(previous_context)];f['context_label']=context
            s['protected_label_ranges'].extend({'start':r['start'],'end':r['end']} for r in f['label_refs'])
            additions.append(f)
        # Preserve separately located signature/placeholder fields that this
        # reconstruction did not replace. Whole-cell upstream guesses are not
        # admitted through this escape hatch.
        slot_ids={x['slot_id'] for x in s.get('logical_slots',[]) if x['kind']=='inline_placeholder'}
        for f in existing:
            refs=f.get('label_refs',[])
            if len(refs)!=1 or refs[0]['block_id']!=b['block_id']:continue
            lr=refs[0]
            if any(max(lr['start'],r['start'])<min(lr['end'],r['end']) for _,g in local for r in g['label_refs']):continue
            if any(r.get('slot_id') in slot_ids for r in f.get('answer_region_refs',[])):
                if not any(x['field_id']==f['field_id'] for x in additions):additions.append(f)


def _split_record_prompt_values(fields,lookup):
    """P:/F:/E: and similar preprinted sublabels are not existing values."""
    for f in fields:
        _split_record_prompt_values(f.get('children',[]),lookup)
        if not f.get('record_index') or f.get('children'):continue
        regions=f.get('answer_region_refs',[])
        if len(regions)!=1 or regions[0].get('block_id') not in lookup:continue
        b=lookup[regions[0]['block_id']];s=_s(b)
        lines=[line.strip() for line in b.get('text','').splitlines() if line.strip()]
        if len(lines)<2 or not all(re.fullmatch(r'[\w /-]{1,35}[:：]',line) for line in lines):continue
        slots=[x for x in s.get('logical_slots',[]) if x['kind']=='inline_prompt_candidate' and x.get('label_start') is not None]
        if len(slots)!=len(lines):continue
        children=[]
        for slot in slots:
            child=new_field(b,ref(b,slot['label_start'],slot['label_end']),[ref(b,slot['start'],slot['end'],slot['slot_id'])],f['field_id'])
            child['field_id']=child['question_id']=f['field_id']+':slot:'+slot['slot_id']
            child['context_refs']=deepcopy(f['label_refs']);child['context_label']=f['question_text_original']
            child['section_refs']=deepcopy(f.get('section_refs',[]));child['section_text_original']=f.get('section_text_original','')
            children.append(child)
        f.update(children=children,answer_region_refs=[],answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='blank')


def _complete_mixed_answer_groups(questions,lookup,replaced):
    """Keep native paragraph/line identity for mixed controls and write-ins."""
    for parent in questions:
        labels=parent.get('label_refs',[]);regions=parent.get('answer_region_refs',[])
        if len(labels)!=1 or parent.get('children'):continue
        label=lookup[labels[0]['block_id']];lp=_s(label).get('paragraph_spans',[])
        if label.get('kind')!='table_cell' or any(r.get('slot_id') for r in regions):continue
        source_regions=list({r['block_id']:lookup[r['block_id']] for r in regions}.values())
        if len(source_regions)==1:
            answer=source_regions[0];rp=_s(answer).get('paragraph_spans',[])
            ps=[p for p in lp if p['text'].strip()]
            if len(lp)==len(rp) and len(ps)>=2 and answer.get('controls'):
                children=[]
                for p in ps:
                    ap=next((a for a in rp if a['paragraph_index']==p['paragraph_index']),None)
                    if ap is None:break
                    slots=[s for s in _s(answer).get('logical_slots',[]) if s.get('paragraph_index')==ap['paragraph_index'] and s['kind'] in {'inline_placeholder','blank_paragraph_candidate'}]
                    if slots:ar=[ref(answer,s['start'],s['end'],s['slot_id']) for s in slots[:1]]
                    elif any(c.get('paragraph_index')==ap['paragraph_index'] for c in answer.get('controls',[])):ar=[ref(answer,ap['start'],ap['end'])]
                    else:break
                    children.append(new_field(label,ref(label,p['start'],p['end']),ar,parent['field_id']))
                if len(children)==len(ps):
                    parent.update(children=children,answer_region_refs=[],answer_value_refs=[],answer_refs=[],answer_text_original='',options=[],answer_status='blank')
                    replaced.add(answer['block_id']);continue
        # Numbered empty lines are distinct reserved record positions. Their
        # printed Roman labels remain outside each writable placeholder.
        for answer in source_regions:
            ps=[p for p in _s(answer).get('paragraph_spans',[]) if p['text'].strip()]
            slots=[s for s in _s(answer).get('logical_slots',[]) if s['kind']=='inline_placeholder']
            if len(ps)>=3 and len(slots)==len(ps) and sum(bool(re.match(r'^(?:[ivxlcdm]+|\d+)[.)]',p['text'],re.I)) for p in ps)>=2:
                children=[]
                for i,s in enumerate(slots,1):
                    child=new_field(label,deepcopy(labels[0]),[ref(answer,s['start'],s['end'],s['slot_id'])],parent['field_id'])
                    child['field_id']=child['question_id']=parent['field_id']+f":record:{i}"
                    child.update(record_index=i,record_column_index=1,record_slot_status='reserved');children.append(child)
                parent.update(children=children,record_columns=[{'label':parent['question_text_original'],'label_ref':deepcopy(labels[0])}],answer_region_refs=[],answer_value_refs=[],answer_refs=[],answer_text_original='',options=[],answer_status='blank')
                break
        if parent.get('children'):continue
        # Separate visible response lines of one combined question. Keep the
        # original combined label instead of inventing semantic subquestions.
        if len(source_regions)>=1:
            lined=[b for b in source_regions if len([s for s in _s(b).get('logical_slots',[]) if s['kind']=='inline_placeholder'])>=2]
            if len(lined)==1 and all(b is lined[0] or not b['text'].strip() for b in source_regions):
                a=lined[0];slots=[s for s in _s(a)['logical_slots'] if s['kind']=='inline_placeholder'];children=[]
                for i,s in enumerate(slots,1):
                    child=new_field(label,deepcopy(labels[0]),[ref(a,s['start'],s['end'],s['slot_id'])],parent['field_id'])
                    child['field_id']=child['question_id']=parent['field_id']+f':response-line:{i}';child['normalized_label']=f'填写位置 {i}';children.append(child)
                for a in source_regions:
                    if a is lined[0]:continue
                    child=new_field(label,deepcopy(labels[0]),[ref(a)],parent['field_id']);child['field_id']=child['question_id']=parent['field_id']+':response:'+a['block_id'];children.append(child)
                parent.update(children=children,answer_region_refs=[],answer_value_refs=[],answer_refs=[],answer_text_original='',options=[],answer_status='blank')


def _complete_nested_writeins(questions,blocks,lookup):
    for f in questions:
        refs=f.get('label_refs',[])
        if len(refs)!=1:continue
        label=lookup[refs[0]['block_id']];loc=label['location']
        if loc.get('type')!='docx' or not loc.get('table'):continue
        prefix=f"{loc['table']}.{loc['row']}.{loc['cell']}."
        nested=[b for b in blocks if b['kind']=='table_cell' and str(b['location'].get('table','')).startswith(prefix) and not _s(b).get('is_merge_continuation')]
        if not nested or any(b['text'].strip() or b.get('controls') or _s(b).get('table_column_count')!=1 for b in nested):continue
        if len({_s(b)['table_id'] for b in nested})!=1:continue
        for b in nested:_s(b)['native_parent_label_cell_id']=_s(label)['cell_id']
        regions=[ref(b) for b in nested]
        if f.get('options') or any(lookup[r['block_id']].get('controls') for r in f.get('answer_region_refs',[])):
            ps=[p for p in _s(label).get('paragraph_spans',[]) if p['text'].strip()]
            if len(ps)<2:continue
            child=new_field(label,ref(label,ps[-1]['start'],ps[-1]['end']),regions,f['field_id'])
            f.setdefault('children',[]).append(child)
            f['label_refs']=f['question_refs']=[ref(label,ps[0]['start'],ps[0]['end'])];f['question_text_original']=ps[0]['text']
        else:
            f.update(answer_region_refs=regions,answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='blank')


def _complete_choice_writeins(questions,lookup,replaced):
    """Keep an explicit follow-up line separate from native Yes/No controls.

    Only source paragraphs and native placeholder slots establish the link.
    Consecutive lines of a prose response remain one continuing answer.
    """
    used={r.get('slot_id') for f in questions for c in f.get('children',[]) for r in c.get('answer_region_refs',[])}
    for f in questions:
        refs=f.get('label_refs',[])
        if len(refs)!=1 or f.get('children') or refs[0]['block_id'] in replaced:continue
        label=lookup[refs[0]['block_id']]
        response=list({r['block_id']:lookup[r['block_id']] for r in f.get('answer_region_refs',[])}.values())
        if not any(b.get('controls') for b in response):continue
        slots=[]
        for b in [label]+[b for b in response if b is not label]:
            for s in _s(b).get('logical_slots',[]):
                if s['kind']=='inline_placeholder' and s['slot_id'] not in used and re.fullmatch(r'[_＿]+',s.get('placeholder_text','')):
                    slots.append((b,s))
        if not slots or len({b['block_id'] for b,s in slots})!=1:continue
        prose=[{'text':m.group(),'start':m.start(),'end':m.end()} for m in re.finditer(r'[^\n]+',label['text']) if m.group().strip() and not re.fullmatch(r'[_＿\s]+',m.group())]
        if len(prose)==2:
            prompt=ref(label,prose[1]['start'],prose[1]['end'])
            f['label_refs']=f['question_refs']=[ref(label,prose[0]['start'],prose[0]['end'])]
            f['question_text_original']=prose[0]['text']
        else:
            b=slots[0][0]
            tails=[p for p in _s(b).get('paragraph_spans',[]) if re.fullmatch(r'\(?please specify\)?',p['text'].strip(),re.I)]
            if len(tails)!=1:continue
            p=tails[0];prompt=ref(b,p['start'],p['end'])
            # Replace the inferred append-after-caption position with the
            # actual printed line immediately above that caption.
            replaced.add(b['block_id'])
        b=lookup[prompt['block_id']]
        child=new_field(b,prompt,[ref(a,s['start'],s['end'],s['slot_id']) for a,s in slots],f['field_id'])
        child['continuous_response_lines']=True
        f.setdefault('children',[]).append(child)
        _s(label)['protected_label_ranges']=[{'start':r['start'],'end':r['end']} for r in f['label_refs']]
        _s(b).setdefault('protected_label_ranges',[]).append({'start':prompt['start'],'end':prompt['end']})


def _pdf_record_grids(tables,lookup,additions,replaced):
    previous=None
    for tid,cells in tables.items():
        if not cells or cells[0].get('location',{}).get('type')!='pdf':continue
        rows=defaultdict(list)
        for b in cells:rows[_row(b)].append(b)
        for row in rows.values():row.sort(key=_col)
        header=None;body=[]
        for rn,row in sorted(rows.items()):
            below=rows.get(rn+1,[])
            # Many columns plus several repeated fixed row identifiers and
            # blank numeric cells: a record grid, never adjacent QA pairs.
            if len(row)>=5 and all(b['text'].strip() for b in row) and len(below)>=len(row):
                tail=[r for k,r in sorted(rows.items()) if k>rn and len(r)>=len(row) and sum(bool(b['text'].strip()) for b in r)<=2]
                if len(tail)>=3:header=row;body=tail;break
        continued=False
        if not header and previous:
            first=cells[0]; page=first['location']['page']
            if page==previous['page']+1:
                tail=[]
                for rn,row in sorted(rows.items()):
                    useful=[b for b in row if b['location']['bbox'][2]-b['location']['bbox'][0]>12]
                    if len(useful)==len(previous['columns'])+2 and sum(bool(b['text'].strip()) for b in useful)<=2:tail.append(useful)
                if len(tail)>=2:header=previous['headers'];body=tail;continued=True
        if not header:continue
        columns=header[1:] if not continued else previous['columns']
        # Fixed identifier columns may be a merged two-column header. The
        # actual response column geometry, not index parity, selects targets.
        if not continued and _end(header[0])==_col(header[0]):
            # Plain empty records such as Ref#/Description handled elsewhere.
            continue
        records=[]
        for row in body:
            matched=[]
            for h in columns:
                hb=h['location']['bbox']
                choices=[b for b in row if abs(b['location']['bbox'][0]-hb[0])<2 and abs(b['location']['bbox'][2]-hb[2])<2]
                if len(choices)!=1:break
                matched.append((h,choices[0]))
            if len(matched)!=len(columns):continue
            identity=[b for b in row if b['text'].strip() and b['block_id'] not in {r['block_id'] for _,r in matched}]
            if not identity:continue
            # The subheader EI/Class is not a material record: it is directly
            # below the merged header and defines its component columns.
            if not continued and _row(row[0])==_row(header[0])+1:continue
            records.append((identity,matched,row))
        if not records:continue
        root=new_field(header[0],ref(header[0]));root['field_id']=root['question_id']=f'record-grid:{tid}'
        root['record_columns']=[{'label':h['text'],'label_ref':ref(h)} for h in columns];root['answer_status']='blank'
        for index,(identity,pairs,row) in enumerate(records,1):
            for ci,(h,b) in enumerate(pairs,1):
                _s(h)['local_layout_type']='record_table_header'
                _s(b).update(local_layout_type='record_table',record_header_row=_row(h),record_header_block_id=h['block_id'])
                f=new_field(h,ref(h),[ref(b)],root['field_id'])
                f['field_id']=f['question_id']=f'record-grid:{tid}:{index}:{ci}'
                f['record_index']=index;f['record_column_index']=ci;f['record_slot_status']='active'
                f['context_refs']=[ref(x) for x in identity];f['section_text_original']=' / '.join(x['text'] for x in identity)
                f['context_label']=f['section_text_original'];root['children'].append(f)
        replaced.update(b['block_id'] for b in cells)
        additions.append(root)
        previous={'page':cells[0]['location']['page'],'headers':header,'columns':columns}


def _complete_body_record_lines(blocks, additions, replaced):
    """A text column heading followed by aligned multi-slot rows is a record.

    Requires consecutive native paragraphs, at least two rows, equal slot
    counts and identical slot starts. No labels are inferred from vocabulary.
    """
    body=[b for b in blocks if b.get('kind')=='paragraph' and b.get('location',{}).get('part')=='word/document.xml']
    for pos,header in enumerate(body):
        text=header.get('text','')
        headings=list(re.finditer(r'\S(?:.*?\S)?(?=\s{3,}|$)',text))
        if not 2<=len(headings)<=8 or any(re.search(r'[_＿?？]',h.group()) for h in headings):continue
        rows=[];slot_starts=None
        for b in body[pos+1:]:
            if b.get('location',{}).get('paragraph')!=header['location']['paragraph']+len(rows)+1:break
            slots=[s for s in _s(b).get('logical_slots',[]) if s.get('kind')=='inline_placeholder' and s.get('label_start') is None]
            if len(slots)!=len(headings) or re.sub(r'[_＿\s]','',b.get('text','')):break
            starts=[s['start'] for s in slots]
            if slot_starts is not None and starts!=slot_starts:break
            slot_starts=starts;rows.append((b,slots))
        if len(rows)<2:continue
        root=new_field(header,ref(header));root.update(boundary_evidence='native_body_record_columns',answer_status='blank')
        root['field_id']=root['question_id']='body-record:'+header['block_id']
        root['record_columns']=[{'label':h.group(),'label_ref':ref(header,h.start(),h.end())} for h in headings]
        root['children']=[]
        _s(header)['resolved_layout_role']='record_table_header'
        for ri,(b,slots) in enumerate(rows,1):
            for ci,(heading,slot) in enumerate(zip(headings,slots),1):
                child=new_field(header,ref(header,heading.start(),heading.end()),[ref(b,slot['start'],slot['end'],slot['slot_id'])],root['field_id'])
                child.update(field_id=f"{root['field_id']}:{ri}:{ci}",question_id=f"{root['field_id']}:{ri}:{ci}",record_index=ri,record_column_index=ci,record_slot_status='reserved',boundary_evidence='native_body_record_columns')
                root['children'].append(child)
        ids={header['block_id'],*(b['block_id'] for b,_ in rows)}
        additions[:]=[f for f in additions if not any(r['block_id'] in ids for r in f.get('label_refs',[]))]
        replaced.update(ids);additions.append(root)


def _complete_printed_paragraph_choices(blocks, additions, replaced):
    body=[b for b in blocks if b.get('kind')=='paragraph' and b.get('location',{}).get('part')=='word/document.xml']
    for index,b in enumerate(body):
        text=b.get('text','')
        tail=re.search(r'(?<!\w)(?:Yes[ \t]{2,}No(?:[ \t]{2,}N\s*/?\s*A)?|Yes\s*/\s*No(?:\s*/\s*N\s*/?\s*A)?)\s*$',text,re.I)
        if not tail or not text[:tail.start()].strip() or b.get('controls'):continue
        label_end=len(text[:tail.start()].rstrip())
        label_refs=[ref(b,0,label_end)];ids={b['block_id']}
        # A line break inside a question is supported only with an explicit
        # unfinished connective before the printed option suffix.
        if re.search(r'\b(?:its|with|of|to|the|and|or)\s*$',text[:label_end],re.I) and index+1<len(body):
            nxt=body[index+1];ntext=nxt.get('text','').strip()
            if nxt['location'].get('paragraph')==b['location']['paragraph']+1 and ntext and len(ntext)<180 and not _s(nxt).get('logical_slots') and not nxt.get('controls') and not re.search(r'[:：;；_＿]|\bYes\b|\bNo\b',ntext,re.I):
                label_refs.append(ref(nxt));ids.add(nxt['block_id'])
        f=new_field(b,label_refs[0]);f['label_refs']=f['question_refs']=label_refs
        f['question_text_original']='\n'.join(next(x for x in body if x['block_id']==r['block_id'])['text'][r['start']:r['end']] for r in label_refs)
        f.update(answer_status='blank',boundary_evidence='native_paragraph_option_list',options=[],answer_region_refs=[])
        _s(b)['protected_label_ranges']=[{'start':0,'end':label_end}]
        for match in re.finditer(r'Yes|No|N\s*/?\s*A',text[tail.start():],re.I):
            start,end=tail.start()+match.start(),tail.start()+match.end()
            slot_id=f"{b['block_id']}:printed-option:{start}"
            write_start=start
            while write_start>label_end and start-write_start<3 and text[write_start-1]==' ':write_start-=1
            slot={'slot_id':slot_id,'kind':'printed_option','start':write_start,'end':end,'option_label':text[start:end]}
            slots=_s(b).setdefault('logical_slots',[])
            if not any(s.get('slot_id')==slot_id for s in slots):slots.append(slot)
            rr=ref(b,write_start,end,slot_id)
            f['options'].append({'refs':[ref(b,start,end)],'answer_region_refs':[rr],'text_original':text[start:end],'selected':False,'selection_source':'printed_choice_list'})
            f['answer_region_refs'].append(rr)
        additions[:]=[item for item in additions if not any(r['block_id'] in ids for r in item.get('label_refs',[]))]
        replaced.update(ids);additions.append(f)


def _complete_left_choice_grids(blocks, additions, replaced):
    tables=defaultdict(list)
    for b in blocks:
        if b.get('kind')=='table_cell' and not _s(b).get('is_merge_continuation'):tables[_s(b).get('table_id')].append(b)
    for tid,cells in tables.items():
        rows=defaultdict(list)
        for b in cells:rows[_row(b)].append(b)
        for row in rows.values():row.sort(key=_col)
        for rn,row in sorted(rows.items()):
            prompt=next((b for b in row if re.search(r'\b(?:check|select|tick)\s+(?:which|all)|选择.*(?:适用|全部)',b.get('text',''),re.I)),None)
            if not prompt:continue
            following=rows.get(rn+1,[])
            instruction=next((b for b in following if re.search(r'\b(?:put|tick|mark)\b.*\b(?:applicable|appropriate)\s+(?:column|box)|勾选.*(?:栏|框)',b.get('text',''),re.I)),None)
            if not instruction:continue
            pairs=[];writeins=[];used={b['block_id'] for b in row+following}
            for nr in range(rn+2,max(rows)+1):
                nextrow=rows.get(nr,[])
                if len(nextrow)==2 and pairs:
                    box,label=nextrow
                    slot=next((s for s in _s(label).get('logical_slots',[]) if s.get('kind')=='inline_prompt_candidate' and s.get('start')==s.get('end')==len(label.get('text',''))),None)
                    if not box.get('text','').strip() and _s(box).get('blank_state')=='blank' and _end(box)+1==_col(label) and slot and re.search(r'\b(?:please specify|other)\b|其他|请说明',label['text'],re.I):
                        pairs.append((box,label));writeins.append((label,slot));used.update(b['block_id'] for b in nextrow);continue
                    break
                if len(nextrow)<4 or len(nextrow)%2:break
                local=[]
                for i in range(0,len(nextrow),2):
                    box,label=nextrow[i:i+2]
                    if box.get('text','').strip() or _s(box).get('blank_state')!='blank' or box.get('controls') or _end(box)+1!=_col(label):local=[];break
                    if not label.get('text','').strip():continue
                    if len(label['text'])>140 or _s(label).get('row_span',1)!=1:local=[];break
                    local.append((box,label))
                if len(local)<2:break
                pairs.extend(local);used.update(b['block_id'] for b in nextrow)
            if len(pairs)<4:continue
            f=new_field(prompt,ref(prompt));f.update(boundary_evidence='physical_left_choice_grid',answer_status='blank',options=[],answer_region_refs=[],instruction_refs=[ref(instruction)])
            for box,label in pairs:
                _s(box).update(local_layout_type='choice_matrix',native_choice_parent_block_id=prompt['block_id'],native_choice_label_block_id=label['block_id'])
                _s(label)['protected_label_ranges']=[{'start':0,'end':len(label['text'])}]
                rr=ref(box,0,0)
                f['options'].append({'refs':[ref(label)],'answer_region_refs':[rr],'text_original':label['text'],'selected':False,'selection_source':'blank_choice_grid'})
                f['answer_region_refs'].append(rr)
            for label,slot in writeins:
                slot['conditional_choice']=label['text']
                child=new_field(label,ref(label,slot['label_start'],slot['label_end']),[ref(label,slot['start'],slot['end'],slot['slot_id'])],f['field_id'])
                child['boundary_evidence']='physical_choice_writein';f['children'].append(child)
            additions[:]=[item for item in additions if not any(r['block_id'] in used for r in item.get('label_refs',[]))]
            replaced.update(used);additions.append(f)


def _exclude_form_captions(blocks, additions, replaced):
    tables=defaultdict(list)
    for b in blocks:
        if b.get('kind')=='table_cell' and not _s(b).get('is_merge_continuation'):tables[_s(b).get('table_id')].append(b)
    for cells in tables.values():
        for b in cells:
            text=b.get('text','').strip();s=_s(b)
            grouped_title=(s.get('column_span',1)==s.get('table_column_count',0) and s.get('column_span',1)>1
                and len(text)<100 and re.search(r'(?:\binformation|\bdetails|信息|概况|资料)\s*[:;：；]?$',text,re.I)
                and not re.search(r'[?？]|\b(?:please|provide|describe|specify)\b|请(?:提供|填写|说明)',text,re.I)
                and not b.get('controls') and not any(slot.get('kind') in {'inline_placeholder','blank_paragraph_candidate','underlined_blank'} for slot in s.get('logical_slots',[])))
            following_rows={_row(x) for x in cells if _row(b)<_row(x)<=_row(b)+2
                and _s(x).get('local_layout_type')=='choice_matrix' and re.search(r'[?？]|\b(?:describe|explain|provide)\b',x.get('text',''),re.I)}
            if grouped_title and following_rows=={_row(b)+1,_row(b)+2}:
                s['resolved_layout_role']='section_header';replaced.add(b['block_id'])
                additions[:]=[f for f in additions if not any(r['block_id']==b['block_id'] for r in f.get('label_refs',[]))]
                continue
            if not text or len(text)>100 or b.get('controls') or text.endswith((':','：')):continue
            full_caption=_row(b)==1 and s.get('column_span',1)==s.get('table_column_count',0)
            signing_caption=bool(re.fullmatch(r'(?:audited|prepared|reviewed|approved|completed)\s+by',text,re.I))
            if not full_caption and not signing_caption:continue
            following=[x for x in cells if _row(b)<_row(x)<=_row(b)+(8 if signing_caption else 12) and x['text'].strip()]
            prompts=[x for x in following if any(slot.get('kind')=='inline_prompt_candidate' for slot in _s(x).get('logical_slots',[]))]
            if len(prompts)<2:continue
            if signing_caption and not any(re.search(r'\bsign(?:ature)?\s*:',x['text'],re.I) for x in prompts):continue
            s['resolved_layout_role']='section_header';replaced.add(b['block_id'])
            additions[:]=[f for f in additions if not any(r['block_id']==b['block_id'] for r in f.get('label_refs',[]))]
    ordered=sorted((b for b in blocks if b.get('location',{}).get('part')=='word/document.xml'),key=lambda b:b.get('order',0))
    for index,b in enumerate(ordered):
        text=b.get('text','').strip()
        if b.get('kind')!='paragraph' or b.get('controls'):continue
        following=next((x for x in ordered[index+1:] if x.get('text','').strip()),None)
        if not following:continue
        record_caption=bool(text.endswith(':') and len(text)<180 and following.get('kind')=='table_cell' and _s(following).get('local_layout_type')=='record_table_header')
        if not record_caption and not re.fullmatch(r'(?:.{1,55}\b(?:Details|Commitment)|[A-Za-z][A-Za-z &.]{1,60}\s+use\s+only)\s*:',text,re.I):continue
        is_caption=following.get('kind')=='table_cell' or bool(re.search(r'\b(?:shall|will|commit)\b',following.get('text',''),re.I)) or bool(re.search(r'\buse\s+only\s*:',text,re.I))
        if not is_caption:continue
        _s(b)['resolved_layout_role']='section_header';replaced.add(b['block_id'])
        additions[:]=[f for f in additions if not any(r['block_id']==b['block_id'] for r in f.get('label_refs',[]))]


def _discard_shadowed_form_wrappers(questions,lookup,native_targets=None):
    """An inferred whole form line cannot protect its own blank sub-slots.

    Only remove a wrapper when every non-punctuation source character is an
    explicit label or its placeholder. Unaccounted prose keeps the wrapper.
    """
    covered={}; slots_by_block={}
    for bid,b in lookup.items():
        slots=[s for s in _s(b).get('logical_slots',[]) if s.get('kind')=='inline_placeholder' and s.get('label_start') is not None]
        if not slots:continue
        chars=set()
        for s in slots:
            chars.update(range(s['label_start'],s['label_end']));chars.update(range(s['start'],s['end']))
        covered[bid]=chars;slots_by_block[bid]=slots
    if native_targets is None:
        native_targets=set()
        stack=list(questions)
        while stack:
            f=stack.pop();stack.extend(f.get('children',[]))
            touches_placeholder=any(r['block_id'] in slots_by_block and any(max(r['start'],s['start'])<min(r['end'],s['end']) for s in slots_by_block[r['block_id']]) for r in f.get('label_refs',[]))
            if not touches_placeholder:
                native_targets.update((r['block_id'],r.get('slot_id')) for r in f.get('answer_region_refs',[]) if r.get('slot_id'))
    retained=[];trimmed=set()
    for f in questions:
        _discard_shadowed_form_wrappers(f.get('children',[]),lookup,native_targets)
        refs=f.get('label_refs') or []
        overlaps_blank=any(r['block_id'] in slots_by_block and any(max(r['start'],s['start'])<min(r['end'],s['end']) for s in slots_by_block[r['block_id']]) for r in refs)
        complete=bool(refs) and all(r['block_id'] in covered and all(i in covered[r['block_id']] or not ch.isalnum() for i,ch in enumerate(lookup[r['block_id']]['text'][r['start']:r['end']],r['start'])) for r in refs)
        actual_replacements=all(all((r['block_id'],s['slot_id']) in native_targets for s in slots_by_block.get(r['block_id'],[])) for r in refs)
        values=f.get('answer_value_refs') or []
        values_are_other_forms=bool(values) and all(
            r['block_id'] not in {lr['block_id'] for lr in refs} and r['block_id'] in covered
            and all(i in covered[r['block_id']] or not ch.isalnum() for i,ch in enumerate(lookup[r['block_id']]['text'][r['start']:r['end']],r['start']))
            and all((r['block_id'],s['slot_id']) in native_targets for s in slots_by_block[r['block_id']]) for r in values)
        if overlaps_blank and complete and actual_replacements and not f.get('children') and (not values or values_are_other_forms) and not f.get('manual_override') and f.get('answer_origin') not in {'manual','knowledge'}:
            trimmed.update(r['block_id'] for r in refs);continue
        retained.append(f)
    questions[:]=retained
    for bid in trimmed:
        b=lookup[bid];blank_positions={i for s in slots_by_block[bid] for i in range(s['start'],s['end'])}
        spans=[];start=None
        for i,ch in enumerate(b['text']):
            if i not in blank_positions:
                if start is None:start=i
            elif start is not None:
                if b['text'][start:i].strip():spans.append({'start':start,'end':i})
                start=None
        if start is not None and b['text'][start:].strip():spans.append({'start':start,'end':len(b['text'])})
        _s(b)['protected_label_ranges']=spans


def _complete_numbered_following_forms(blocks, additions, replaced):
    tables=defaultdict(list)
    for b in blocks:
        if b.get('kind')=='table_cell' and not _s(b).get('is_merge_continuation'):tables[_s(b).get('table_id')].append(b)
    for cells in tables.values():
        rows=defaultdict(list)
        for b in cells:rows[_row(b)].append(b)
        active=None
        for rn,row in sorted(rows.items()):
            row.sort(key=_col)
            if len(row)!=2 or not re.fullmatch(r'\d+(?:\.\d+)*\.?',row[0].get('text','').strip()):continue
            label=row[1];text=label.get('text','')
            if re.search(r'please sign (?:the )?form below|please provide the following information',text,re.I):
                _s(label)['resolved_layout_role']='completion_instructions';replaced.add(label['block_id'])
                additions[:]=[f for f in additions if not any(r['block_id']==label['block_id'] for r in f.get('label_refs',[]))]
                if 'following information' in text.lower():active=(rn,label)
                continue
            if not active or rn>active[0]+2 or not re.search(r'\be\.g\.|\bfor example\b',text,re.I):continue
            # A numbered example-backed item immediately under an explicit
            # "provide following information" instruction has its answer in
            # the same full-width text cell, not the next independent row.
            slot=next((s for s in _s(label).get('logical_slots',[]) if s.get('kind')=='trailing_fill_candidate'),None)
            if not slot:continue
            f=new_field(label,ref(label),[ref(label,slot['offset'],slot['offset'],slot['slot_id'])])
            f['instruction_refs']=[ref(active[1])];f['boundary_evidence']='numbered_following_information'
            replaced.add(label['block_id']);additions.append(f)


def _complete_merged_choice_followups(blocks, additions, replaced):
    tables=defaultdict(list)
    for b in blocks:
        if b.get('kind')=='table_cell' and not _s(b).get('is_merge_continuation'):tables[_s(b).get('table_id')].append(b)
    for cells in tables.values():
        for b in cells:
            s=_s(b);text=b.get('text','')
            if not re.match(r'^\s*If\s+(?:yes|no)\s*[,;:]?',text,re.I) or b.get('controls'):continue
            if re.search(r'\b(?:complete|refer|follow|sign)\b.*\b(?:part|appendix|form|below)|following information',text,re.I):continue
            same=[x for x in cells if _row(x)==_row(b) and x['block_id']!=b['block_id'] and x.get('text','').strip()]
            if same:continue
            above=[x for x in cells if _row(x)==_row(b)-1 and _s(x).get('local_layout_type')=='choice_matrix' and _col(x)==_col(b) and x.get('text','').strip()]
            if len(above)!=1:continue
            options=_s(above[0]).get('choice_options',[])
            if options and s.get('column_span',1)==1:
                first=min(o['column_start'] for o in options);last=max(o['column_end'] for o in options)
                targets=[x for x in cells if _row(x)==_row(b) and (_col(x),_end(x))==(first,last) and _s(x).get('blank_state')=='blank']
                if len(targets)==1:
                    target=targets[0]
                    _s(target).update(local_layout_type='label_response_form',conditional_parent_block_id=above[0]['block_id'])
                    f=new_field(b,ref(b),[ref(target)]);f['context_refs']=[ref(above[0])]
                    f['boundary_evidence']='merged_choice_followup'
                    replaced.add(b['block_id']);additions.append(f)
                continue
            if s.get('column_span',1)<3:continue
            # The original author merged the question and option columns for
            # an explicit conditional response. Prefer a real blank paragraph;
            # otherwise use the evidenced end of this same response cell.
            slot=next((s for s in _s(b).get('logical_slots',[]) if s.get('kind')=='blank_paragraph_candidate'),None)
            slot=slot or next((s for s in _s(b).get('logical_slots',[]) if s.get('kind')=='trailing_fill_candidate'),None)
            if not slot:continue
            a=slot.get('start',slot.get('offset'));z=slot.get('end',a)
            f=new_field(b,ref(b),[ref(b,a,z,slot['slot_id'])]);f['context_refs']=[ref(above[0])]
            f['boundary_evidence']='merged_choice_followup';replaced.add(b['block_id']);additions.append(f)


def _complete_isolated_cell_prompts(blocks, additions, replaced):
    for b in blocks:
        s=_s(b)
        if b.get('kind')!='table_cell' or s.get('table_row_count')!=1 or s.get('table_column_count')!=1 or b.get('controls'):continue
        slots=[x for x in s.get('logical_slots',[]) if x.get('kind')=='inline_prompt_candidate' and x.get('label_start')==0 and x.get('start')==x.get('end')==len(b.get('text',''))]
        if len(slots)!=1:continue
        slot=slots[0];label=b['text'][slot['label_start']:slot['label_end']]
        if not label.strip() or len(label)>70 or re.search(r'\b(?:details|instructions?|declaration|commitment|use only)\b',label,re.I):continue
        f=new_field(b,ref(b,slot['label_start'],slot['label_end']),[ref(b,slot['start'],slot['end'],slot['slot_id'])])
        f['boundary_evidence']='isolated_native_prompt';replaced.add(b['block_id'])
        additions[:]=[x for x in additions if not any(r['block_id']==b['block_id'] for r in x.get('label_refs',[]))]
        additions.append(f)


def _exclude_signed_declaration_prose(blocks, additions, replaced):
    for b in blocks:
        text=b.get('text','')
        if b.get('kind')!='table_cell' or b.get('controls') or re.search(r'[?？]|_{2,}|＿{2,}',text):continue
        if not re.match(r'\s*We\b.{0,90}\b(?:confirm|declare|certify|commit)\b',text,re.I):continue
        if len(text.splitlines())<3 or len(re.findall(r'\b(?:shall|will|ensure)\b',text,re.I))<3:continue
        loc=b['location'];prefix=f"{loc.get('table')}."
        signing=[x for x in blocks if x.get('kind')=='table_cell' and str(x.get('location',{}).get('table','')).startswith(prefix) and re.fullmatch(r'\s*(?:sign(?:ature)?(?:\s*(?:and|&|/)\s*date)?|signed by)\s*',x.get('text',''),re.I)]
        if not signing:continue
        _s(b)['resolved_layout_role']='signed_declaration_prose';replaced.add(b['block_id'])
        additions[:]=[f for f in additions if not any(r['block_id']==b['block_id'] for r in f.get('label_refs',[]))]


def _link_nested_response_records(questions,lookup):
    attached=set()
    for parent in questions:
        if parent.get('answer_region_refs') or parent.get('children') or parent.get('options'):continue
        for rr in parent.get('instruction_refs',[]):
            b=lookup.get(rr['block_id'],{});loc=b.get('location',{})
            if b.get('kind')!='table_cell' or not re.match(r'\s*(?:note\s*[:;]|说明[:：]|备注[:：])',b.get('text',''),re.I):continue
            prefix=f"{loc.get('table')}.{loc.get('row')}.{loc.get('cell')}."
            nested=[f for f in questions if f is not parent and f.get('record_columns') and all(str(lookup[x['block_id']]['location'].get('table','')).startswith(prefix) for x in f.get('label_refs',[]))]
            if not nested:continue
            parent['children']=nested;parent['answer_status']='blank';parent['boundary_evidence']='physical_nested_response_records'
            if not parent.get('review_reasons'):parent['review_status']='model_proposed'
            for f in nested:f['parent_question_id']=parent['field_id'];attached.add(f['field_id'])
    questions[:]=[f for f in questions if f['field_id'] not in attached]
