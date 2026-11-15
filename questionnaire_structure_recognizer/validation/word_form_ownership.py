"""Bind native paragraph prompts and preserve physical table ownership.

No inferred answer text: only explicit native controls, aligned record columns,
and source-proven section/record boundaries can change derived relationships.
"""
from collections import defaultdict
from copy import deepcopy
import re
from .field_boundaries import ref, placeholder
from .form_completion import new_field

VERSION='word-native-form-ownership-v4-cross-table-peer-sections'

def _s(b): return b.get('structure') or {}
def _walk(fs):
    for f in fs:
        yield f
        yield from _walk(f.get('children',[]))
def _protected(f):
    return bool(f.get('manual_override') or f.get('human_corrected') or f.get('review_status') == 'human_corrected'
        or (f.get('form_definition') or {}).get('mapping_review_status') == 'human_corrected'
        or f.get('answer_origin') in {'manual','knowledge','reviewed_knowledge'} or any(_protected(c) for c in f.get('children',[])))
def _ids(f):
    return {r.get('block_id') for k in ('label_refs','answer_region_refs','answer_value_refs') for r in f.get(k,[])}
def _controls(b):
    controls = [c for c in b.get('controls',[]) if c.get('type')=='text' and c.get('source') in {'legacy_form_field','content_control'} and c.get('paragraph_index') and c.get('control_index')]
    # A whole-cell reference must not swallow fixed text beside the control.
    # Mixed-content controls need an independently bounded range resolver.
    if len(controls) != 1 or len(b.get('controls', [])) != 1: return []
    current = str(controls[0].get('current_value') or '').strip()
    if b.get('text', '').strip() != current: return []
    return controls
def _target(b,c):
    return {**ref(b,0,len(b.get('text','')),f"{b['block_id']}:control:p{c['paragraph_index']}:i{c['control_index']}"),
        'control_id':c.get('control_id'),'control_index':c['control_index'],'control_paragraph_index':c['paragraph_index']}
def _set_value(f,b):
    if b.get('text','').strip() and not placeholder(b,b['text']):
        f.update(answer_value_refs=[ref(b)],answer_refs=[ref(b)],answer_text_original=b['text'],answer_status='filled')
def _replace(fields,label,new):
    matching=[f for f in _walk(fields) if any(r.get('block_id')==label['block_id'] for r in f.get('label_refs',[]))]
    if any(_protected(f) for f in matching): return False
    # An already validated relationship owns its target. Do not multiply it.
    if any(f.get('answer_region_refs') or f.get('children') for f in matching): return False
    def retain(fs):
        out=[]
        for f in fs:
            f['children']=retain(f.get('children',[]))
            if f not in matching: out.append(f)
        return out
    fields[:]=retain(fields);fields.append(new);return True

def restore_word_form_ownership(fields,blocks,lookup):
    tables=defaultdict(list)
    for b in blocks:
        if b.get('location',{}).get('type')=='docx' and b.get('kind')=='table_cell' and _s(b).get('table_id'):
            tables[_s(b)['table_id']].append(b)
    for tid,cells in tables.items():
        physical=[b for b in cells if not _s(b).get('is_merge_continuation')]
        first=min((_s(b).get('row_start',99999) for b in physical),default=99999)
        row=[b for b in physical if _s(b).get('row_start')==first]
        ids={_s(b).get('preceding_block_id') for b in row}
        if len(ids)!=1: continue
        label=lookup.get(next(iter(ids)),{})
        if label.get('kind')!='paragraph' or not label.get('text','').strip(): continue
        # The parser's preceding pointer is verified against actual native order.
        start=min(b.get('order',0) for b in physical)
        intervening=[b for b in blocks if label.get('order',0)<b.get('order',0)<start and b.get('text','').strip()]
        if intervening: continue
        numbering=label.get('style',{}).get('numbering') or {}
        if not numbering and not re.search(r'[?？]|[:：]\s*$',label['text']): continue
        explicit=[(b,c) for b in row for c in _controls(b)]
        if len(explicit)==1 and all(not b.get('text','').strip() or _controls(b) for b in row):
            b,c=explicit[0]
            if any(b['block_id'] in _ids(f) and f.get('answer_region_refs') for f in _walk(fields)): continue
            f=new_field(label,ref(label),[_target(b,c)])
            f.update(boundary_evidence='native_paragraph_text_control',boundary_version=VERSION)
            f['context_refs']=[ref(lookup[x]) for x in _s(b).get('preceding_block_ids',[]) if x in lookup and x!=label['block_id']]
            _set_value(f,b);_replace(fields,label,f)
            continue
        # A real multi-column header above repeated native text controls.
        headers=[b for b in row if b.get('text','').strip() and not b.get('controls')]
        if len(headers)<2 or len(headers)>12: continue
        headers.sort(key=lambda b:_s(b).get('grid_col_start',0))
        aligned=defaultdict(list)
        for b in physical:
            cs=_controls(b)
            if len(cs)!=1 or _s(b).get('row_start',0)<=first: continue
            candidates=[(i,h) for i,h in enumerate(headers) if (_s(h).get('grid_col_start'),_s(h).get('grid_col_end'))==(_s(b).get('grid_col_start'),_s(b).get('grid_col_end'))]
            if len(candidates)==1: aligned[_s(b)['row_start']].append((candidates[0][0],candidates[0][1],b,cs[0]))
        if not aligned or any(len(v)!=len(headers) for v in aligned.values()): continue
        regions={b['block_id'] for group in aligned.values() for _,_,b,_ in group}
        if any(_protected(f) or f.get('answer_region_refs') for f in _walk(fields) if regions & _ids(f)): continue
        root=new_field(label,ref(label));root.update(boundary_evidence='native_record_table',boundary_version=VERSION,answer_status='blank',record_context_values=[])
        root['record_columns']=[{'label':h['text'],'label_ref':ref(h),'grid_col_start':_s(h)['grid_col_start'],'grid_col_end':_s(h)['grid_col_end']} for h in headers]
        for index,(native_row,group) in enumerate(sorted(aligned.items()),1):
            active=any(b.get('text','').strip() and not placeholder(b,b['text']) for _,_,b,_ in group)
            for col,h,b,c in sorted(group):
                _s(h).update(local_layout_type='record_table_header')
                _s(b).update(local_layout_type='record_table',record_header_row=first,record_index=index)
                child=new_field(h,ref(h),[_target(b,c)],root['field_id'])
                fid=f"{root['field_id']}:record:{index}:column:{col+1}"
                child.update(field_id=fid,question_id=fid,record_index=index,record_column_index=col+1,record_slot_status='active' if active else 'reserved',boundary_version=VERSION)
                _set_value(child,b);root['children'].append(child)
        _replace(fields,label,root)
    _complete_owned_record_rows(fields,tables,lookup)
    _readonly_merged_row_labels(fields,tables,lookup)
    return _section_and_caption_rows(fields,tables,lookup)


def _complete_owned_record_rows(fields,tables,lookup):
    for root in list(fields):
        columns=root.get('record_columns',[])
        if root.get('boundary_evidence')!='native_record_table' or not columns or _protected(root): continue
        header=lookup.get(columns[0]['label_ref']['block_id'],{});hs=_s(header);tid=hs.get('table_id')
        if tid not in tables: continue
        owned={r['block_id'] for c in root.get('children',[]) for r in c.get('answer_region_refs',[])}
        cells=tables[tid];rows=defaultdict(list)
        for b in cells:
            if not _s(b).get('is_merge_continuation') and _s(b).get('row_start',0)>hs.get('row_start',0): rows[_s(b)['row_start']].append(b)
        # Extend only a physical row-spanning record already owned by this root.
        last=max((_s(lookup.get(bid,{})).get('row_end',0) for bid in owned if _s(lookup.get(bid,{})).get('row_end',0)>_s(lookup.get(bid,{})).get('row_start',0)),default=0)
        for row,group in sorted(rows.items()):
            if row>last: continue
            for b in group:
                if b['block_id'] in owned: continue
                matching=[(i,c) for i,c in enumerate(columns) if (c['grid_col_start'],c['grid_col_end'])==(_s(b).get('grid_col_start'),_s(b).get('grid_col_end'))]
                if len(matching)!=1: continue
                if any(_protected(f) and b['block_id'] in _ids(f) for f in _walk(fields)): continue
                i,column=matching[0];h=lookup[column['label_ref']['block_id']]
                index=row-hs['row_start']
                _s(b).update(local_layout_type='record_table',record_header_row=hs['row_start'],record_index=index)
                c=new_field(h,deepcopy(column['label_ref']),[ref(b)],root['field_id'])
                fid=f"{root['field_id']}:record:{index}:column:{i+1}"
                c.update(question_id=fid,field_id=fid,record_index=index,record_column_index=i+1,record_slot_status='active',boundary_version=VERSION)
                _set_value(c,b);root['children'].append(c);owned.add(b['block_id'])
        headers={c['label_ref']['block_id'] for c in columns}
        # Restrict de-duplication to this actual record's source positions.
        def retain(items):
            out=[]
            for f in items:
                if f is root or _protected(f): out.append(f);continue
                touched=_ids(f)
                if touched and touched<=owned|headers and touched&owned: continue
                f['children']=retain(f.get('children',[]));out.append(f)
            return out
        fields[:]=retain(fields)


def _section_and_caption_rows(fields,tables,lookup):
    rows={};candidates=[];nonqa=[]
    for tid,cells in tables.items():
        grouped=defaultdict(list)
        for b in cells:
            if not _s(b).get('is_merge_continuation'):grouped[_s(b).get('row_start',0)].append(b)
        rows[tid]=grouped
        for rn,group in grouped.items():
            filled=[b for b in group if b.get('text','').strip()]
            if len(filled)!=2 or any(b.get('controls') for b in group):continue
            filled.sort(key=lambda b:_s(b).get('grid_col_start',0));mark,title=filled
            if not re.fullmatch(r'[A-Z][.)]?',mark['text'].strip()) or not 2<len(title['text'].strip())<=100:continue
            if not (title['text'].isupper() or any(_s(b).get('local_layout_type')=='record_table_header' for n,g in grouped.items() if rn<n<=rn+2 for b in g)):continue
            candidates.append((tid,rn,group,mark,title))
    # Multiple independent alphabetic sections, not a single letter data value.
    if len({m['text'].strip().rstrip('.)') for _,_,_,m,_ in candidates})<2:return nonqa
    ordered=sorted(candidates,key=lambda item:item[3].get('order',0))
    qualified={};runs=[]
    for candidate in ordered:
        part=candidate[3].get('location',{}).get('part')
        letter=candidate[3]['text'].strip().rstrip('.)')
        if runs and runs[-1][-1][3].get('location',{}).get('part')==part and ord(letter)==ord(runs[-1][-1][3]['text'].strip().rstrip('.)'))+1:
            runs[-1].append(candidate)
        else:runs.append([candidate])
    for run in runs:
        if len(run)<2:continue
        for candidate in run:
            index=ordered.index(candidate);mark,title=candidate[3],candidate[4]
            later=[c[3].get('order',0) for c in ordered[index+1:] if c[3].get('location',{}).get('part')==mark.get('location',{}).get('part')]
            qualified[title['block_id']]={'kind':'consecutive_native_alphabetic_peer_rows','series_id':run[0][3]['block_id'],
                'start_order':mark.get('order',0),'end_order':min(later) if later else max(b.get('order',0) for b in lookup.values() if b.get('location',{}).get('part')==mark.get('location',{}).get('part'))+1,
                'part':mark.get('location',{}).get('part')}
    for tid,rn,group,mark,title in candidates:
        groupids={b['block_id'] for b in group}
        if any(_protected(f) and _ids(f)&groupids for f in _walk(fields)):continue
        refs=[ref(mark),ref(title)]
        _s(title)['native_section_marker_ref']=ref(mark)
        if title['block_id'] in qualified:_s(title)['native_peer_section_range']=qualified[title['block_id']]
        for b in group:_s(b)['resolved_layout_role']='section_header'
        nonqa.append({'role':'section','refs':refs,'text_original':mark['text']+' '+title['text'],'reason':'native alphabetic section row'})
        following=rows[tid].get(rn+1,[])
        nextrow=rows[tid].get(rn+2,[])
        instruction=[b for b in following if b.get('text','').strip()]
        instructionids=set()
        if len(instruction)==1 and not any(b.get('controls') for b in following) and any(_s(b).get('local_layout_type')=='record_table_header' for b in nextrow):
            b=instruction[0]
            if _s(b).get('grid_col_end',0)>_s(b).get('grid_col_start',0) and re.match(r'please\s+(?:complete|provide|list)\b|请',b['text'],re.I):
                instructionids.add(b['block_id']);nonqa.append({'role':'instruction','refs':[ref(b)],'text_original':b['text'],'reason':'native instruction spanning record header'})
        def retain(items):
            out=[]
            for f in items:
                if _protected(f):out.append(f);continue
                labels={r['block_id'] for r in f.get('label_refs',[])}
                if labels and labels<=groupids|instructionids and not f.get('children') and not f.get('answer_region_refs'):continue
                f['options']=[o for o in f.get('options',[]) if not any(r['block_id'] in instructionids for r in o.get('refs',[]))]
                f['children']=retain(f.get('children',[]));out.append(f)
            return out
        fields[:]=retain(fields)
        # Caption belongs to subsequent native fields, not a free standing answer.
        nextsection=min((n for t,n,_,_,_ in candidates if t==tid and n>rn),default=10**9)
        for f in _walk(fields):
            anchors=[lookup.get(r['block_id'],{}) for r in f.get('label_refs',[])]
            if any(_s(b).get('table_id')==tid and rn<_s(b).get('row_start',0)<nextsection for b in anchors):
                f['section_refs']=deepcopy(refs)
                if f.get('record_columns') and instructionids:
                    f['instruction_refs']=[ref(lookup[bid]) for bid in instructionids]
    return nonqa


def _readonly_merged_row_labels(fields,tables,lookup):
    """A native assessment category spanning choice rows is fixed context.

    Require the category header, physical vertical merge, multiple choice
    columns in every spanned row, and an actual review-section source. Neither
    the value's words nor an LLM responsibility hint can trigger this rule.
    """
    from .native_choices import _glyph_options
    for root in fields:
        columns=root.get('record_columns',[])
        if root.get('boundary_evidence')!='native_record_table' or not columns or _protected(root):continue
        first=columns[0];header=lookup.get(first['label_ref']['block_id'],{})
        if not re.fullmatch(r'项目|检查项目|审核项目|item|criteria?|topic|category',header.get('text','').strip(),re.I):continue
        section_refs = root.get('section_refs') or []
        if not section_refs:
            # Native completion runs before the final section attachment. Use
            # the parser's immediate source paragraph, never a model role.
            preceding = lookup.get(_s(header).get('preceding_block_id'), {})
            if preceding.get('location', {}).get('type') == 'docx' and preceding.get('kind') in {'paragraph','heading'}:
                section_refs = [ref(preceding)]
        section=' '.join(lookup.get(r.get('block_id'),{}).get('text','')[r.get('start',0):r.get('end',0)] for r in section_refs)
        if not re.search(r'审核|审查|review|assessment',section,re.I):continue
        tid=_s(header).get('table_id');cells=tables.get(tid,[])
        for child in list(root.get('children',[])):
            labels=child.get('label_refs',[]);regions=child.get('answer_region_refs',[])
            if _protected(child) or len(labels)!=1 or labels[0].get('block_id')!=header['block_id'] or len(regions)!=1:continue
            b=lookup.get(regions[0].get('block_id'),{});bs=_s(b)
            if not b.get('text','').strip() or b.get('controls') or bs.get('table_id')!=tid or bs.get('vertical_merge')!='restart':continue
            start,end=bs.get('row_start',0),bs.get('row_end',0)
            if not start<end or bs.get('grid_col_start')!=first['grid_col_start'] or bs.get('grid_col_end')!=first['grid_col_end']:continue
            choice_rows={rn:[cell for cell in cells if _s(cell).get('row_start')==rn and not _s(cell).get('is_merge_continuation')
                and _s(cell).get('grid_col_start',0)>bs['grid_col_end'] and len(_glyph_options(cell,0,len(cell.get('text',''))))>=2]
                for rn in range(start,end+1)}
            if any(len(v)<2 for v in choice_rows.values()):continue
            bs['fixed_record_context']=True
            for rn in range(start,end+1):
                index=rn-_s(header)['row_start']
                entry={'record_index':index,'column_index':1,'text':b['text'],'refs':[ref(b)],
                    'shared_row_label':True,'evidence':'native_merged_assessment_category_over_choice_rows'}
                if not any(v.get('record_index')==index and v.get('refs')==entry['refs'] for v in root.setdefault('record_context_values',[])):
                    root['record_context_values'].append(entry)
            root['children'].remove(child)
