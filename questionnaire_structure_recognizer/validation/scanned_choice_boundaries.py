"""Constrain scan choice labels to their observed, closed local form row.

This does not repair OCR or infer a tick. Adjacent closed cells delimit one
question and its response groups; explicit copy requests own attachment boxes.
"""
from copy import deepcopy
import re

from .field_boundaries import ref
from .form_completion import new_field
from .pdf_source_relations import source_cells_share_row, source_text_inside_cell
from .selection_evidence import selected_scan_caption

VERSION='scan-choice-boundaries-v3-independent-source-question-rows'
COPY=re.compile(r'please\s*provide\s*(?:a\s*)?copy',re.I)
FOLLOWUP=re.compile(r'^(?:[il]f\s*yes\b|[il]fyes[,，:]?|please\s*provide|est\s*license\s*date|date\s*of\s*(?:last|inspection))',re.I)


def _box(b):return b.get('location',{}).get('bbox',[])


def _walk(fields):
    for f in fields:
        yield f
        yield from _walk(f.get('children',[]))


def _protected(f):return f.get('manual_override') or f.get('answer_origin')in{'manual','knowledge','reviewed_knowledge'}


def _targets(f,lookup):
    refs=[r for o in f.get('options',[])for r in o.get('answer_region_refs',[])]
    if not refs or any(lookup.get(r.get('block_id'),{}).get('structure',{}).get('scan_region_kind')!='checkbox'for r in refs):return []
    return refs


def _source_text(refs,lookup):
    return '\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']]for r in refs)


def _set_field(f,labels,options,left,right,role,lookup):
    old={k:deepcopy(f.get(k))for k in ('field_id','label_refs','answer_region_refs','options')}
    fid='scan-row-choice:'+labels[0]['block_id']+':'+str(labels[0]['start'])+':'+role
    f.update(field_id=fid,question_id=fid,label_refs=deepcopy(labels),question_refs=deepcopy(labels),
        question_text_original=_source_text(labels,lookup),options=deepcopy(options),
        answer_region_refs=[deepcopy(r)for o in options for r in o['answer_region_refs']],
        answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='blank',
        physical_label_region_ref=ref(left),physical_response_region_ref=ref(right),
        boundary_evidence='closed_scan_choice_groups',boundary_version=VERSION,
        scan_row_evidence={'label_cell_ref':ref(left),'response_cell_ref':ref(right),'role':role},
        review_reasons=[],review_status='parsed')
    f.setdefault('boundary_history',[]).append(old)
    for option in f['options']:
        for rr in option['answer_region_refs']:
            region=lookup[rr['block_id']];control=(region.get('controls')or[{}])[0]
            option['selected']=control.get('selected');option['selection_source']=control.get('source')
            region.setdefault('structure',{}).update(scan_association_valid=True,scan_field_id=fid,
                scan_option_caption_refs=deepcopy(option['refs']))
    selected=[r for o in f['options']if o['selected']is True for r in o['refs']]
    unknown=any(o['selected']is None for o in f['options'])
    if len(selected)==1 and not unknown and selected_scan_caption(selected[0],f,lookup):
        f.update(answer_status='filled',answer_value_refs=deepcopy(selected),answer_refs=deepcopy(selected),
            answer_text_original=_source_text(selected,lookup),answer_pairing_evidence='physical_selected_box_and_source_caption')
    elif selected or unknown:
        f.update(answer_status='unresolved',review_status='needs_review',review_reasons=['答复位置已按实体单元格核对；选中状态尚不完整或不唯一，请检查原页。'])
    for rr in labels:
        lookup[rr['block_id']].setdefault('structure',{})['protected_label_ranges']=[{'start':rr['start'],'end':rr['end']}]


def _copy_options(texts,right,blocks):
    local=[b for b in texts if source_text_inside_cell(b,right,label=True)]
    copies=[b for b in local if re.fullmatch(r'\s*Copy\s+attached\s*',b['text'],re.I)]
    if len(copies)!=1:return []
    copy=copies[0];cb=_box(copy);cy=(cb[1]+cb[3])/2
    na=[b for b in local if re.fullmatch(r'\s*N\s*/?\s*A\s*',b['text'],re.I)and abs(sum(_box(b)[1::2])/2-cy)<7]
    if len(na)!=1:return []
    marks=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='checkbox'and source_text_inside_cell(b,right)]
    result=[];used=set()
    for caption in [copy,na[0]]:
        bb=_box(caption);y=sum(bb[1::2])/2
        # Source glyph boxes may include the checkbox itself. The mark must
        # still be at the right end of this exact caption, never another row.
        candidates=[m for m in marks if abs(sum(_box(m)[1::2])/2-y)<7 and -12<=_box(m)[0]-bb[2]<20]
        if len(candidates)!=1 or candidates[0]['block_id']in used:return []
        mark=candidates[0];used.add(mark['block_id'])
        result.append({'refs':[ref(caption)],'text_original':caption['text'].strip(),'answer_region_refs':[ref(mark)]})
    return result


def _numbered_gutter_lines(parent, texts, local, left):
    """OCR can join a gutter number and the question on one source line.

    Recover only a previously referenced numbered interrogative with nearly
    all its extent inside this row and an explicit following continuation.
    No neighboring instruction, independent question or row is admitted.
    """
    lb=_box(left);page=left.get('location',{}).get('page')
    known={r.get('block_id')for r in parent.get('label_refs',[])}
    for previous in parent.get('boundary_history',[]):
        known.update(r.get('block_id')for r in previous.get('label_refs',[])or[])
    interrogative=r'(?:is|are|was|were|has|have|do|does|did|can|could|will|would)\b'
    found=[]
    for b in texts:
        if b in local or b['block_id']not in known or b.get('location',{}).get('page')!=page:continue
        text=b['text'].strip();bb=_box(b)
        if not re.match(r'^\d+(?:[.]\d+)*[.)]?\s+'+interrogative,text,re.I):continue
        if len(bb)!=4 or not(lb[1]<=bb[1]<bb[3]<=lb[3]and bb[0]<lb[0]<bb[2]<=lb[2]+2):continue
        if (bb[2]-lb[0])/max(1,bb[2]-bb[0])<.85:continue
        following=[x for x in local if _box(x)[1]>=bb[3]-2 and _box(x)[1]-bb[3]<=20]
        if not following:continue
        tail=min(following,key=lambda x:(_box(x)[1],_box(x)[0]))
        tailtext=tail['text'].strip()
        if not re.match(r'^[a-z]',tailtext)or re.match(interrogative,tailtext,re.I)or FOLLOWUP.match(tailtext):continue
        if not re.search(r'[?？]$',tailtext)or re.search(r'[?？]',text):continue
        found.append(b)
    return found if len(found)==1 else []


def restore_scanned_choice_boundaries(fields, blocks, lookup):
    # A single observed cell can contain several distinct bilingual questions.
    # Re-evaluate each exact physical option group against complete source runs,
    # rather than requiring only one Yes/No group in the enclosing rectangle.
    from .scanned_question_rows import source_choice_partition
    for f in _walk(fields):
        if _protected(f):continue
        targets=_targets(f,lookup)
        partition=source_choice_partition(targets,blocks,lookup)
        if not partition:continue
        if f.get('label_refs')==partition['label_refs']:continue
        oldid=f['field_id']
        _set_field(f,partition['label_refs'],f['options'],
                   lookup[partition['label_cell_ref']['block_id']],
                   lookup[partition['response_cell_ref']['block_id']],'main',lookup)
        f['source_question_partition']=deepcopy(partition)
        for child in _walk(fields):
            if child.get('parent_question_id')==oldid:child['parent_question_id']=f['field_id']
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box'and b.get('structure',{}).get('source_method')=='raster_geometry']
    texts=[b for b in blocks if b.get('location',{}).get('extraction')=='ocr'and b.get('text','').strip()]
    flat=list(_walk(fields));nonqa=[];remove=set();claimed=set()
    for right in cells:
        lefts=[b for b in cells if b is not right and source_cells_share_row(b,right)]
        if len(lefts)!=1:continue
        left=lefts[0]
        groups=[f for f in flat if _targets(f,lookup)and all(source_text_inside_cell(lookup[r['block_id']],right)for r in _targets(f,lookup))]
        main=[f for f in groups if {re.sub(r'\s+','',o.get('text_original','')).casefold()for o in f['options']}in({'yes','no'},{'yes','no','n/a'},{'yes','no','na'})]
        if len(main)!=1 or _protected(main[0])or any(_protected(f)for f in groups):continue
        parent=main[0]
        local=sorted([b for b in texts if source_text_inside_cell(b,left,label=True)],key=lambda b:(_box(b)[1],_box(b)[0]))
        if not local:continue
        overhang=_numbered_gutter_lines(parent,texts,local,left)
        local=sorted(local+overhang,key=lambda b:(_box(b)[1],_box(b)[0]))
        stop=next((i for i,b in enumerate(local)if FOLLOWUP.match(b['text'].strip())),len(local))
        primary=local[:stop]
        if not primary or not any(re.search(r'[?？]',b['text'])for b in primary):continue
        if any(re.match(r'\s*(?:SECTION\s*[IVX\d]+|第.{1,8}部分)',b['text'],re.I)for b in primary):continue
        # Two independent English question starts in one physical cell require
        # finer row/paragraph grouping. A single closed outer box is not enough.
        starts=[b for b in primary if re.match(r'^\s*(?:is|are|was|were|has|have|do|does|did|can|could|will|would)[A-Za-z\s]',b['text'],re.I)]
        if len(starts)>1:continue
        labels=[ref(b)for b in primary];oldlabels=deepcopy(parent.get('label_refs',[]));oldid=parent['field_id']
        copies=[i for i,b in enumerate(local)if COPY.search(b['text'])]
        opts=_copy_options(texts,right,blocks)
        if labels==oldlabels and parent.get('answer_region_refs')==_targets(parent,lookup)and not(len(copies)==1 and opts):continue
        _set_field(parent,labels,parent['options'],left,right,'main',lookup)
        claimed.update(b['block_id']for b in primary)
        for f in flat:
            if f.get('parent_question_id')==oldid:f['parent_question_id']=parent['field_id']
        # Content outside the physical label cell stays available as context;
        # it must never be concatenated into this row's actual question.
        outside=[r for r in oldlabels if r['block_id']not in {b['block_id']for b in primary}and not source_text_inside_cell(lookup[r['block_id']],left,label=True)]
        parent['context_refs']=[r for r in parent.get('context_refs',[])if r['block_id']not in {b['block_id']for b in overhang}]
        if outside:parent.setdefault('context_refs',[]).extend(r for r in outside if r not in parent.get('context_refs',[]))
        if len(copies)!=1 or not opts:continue
        ci=copies[0];copylabels=[ref(local[ci])]
        # A following Chinese source translation belongs to the same request;
        # an independent question or answer is never absorbed by proximity.
        for b in local[ci+1:]:
            if re.match(r'^\s*[\u3400-\u9fff]',b['text'])and re.search(r'复印|複印',b['text'])and not re.search(r'[?？]',b['text']):copylabels.append(ref(b))
            else:break
        targets={r['block_id']for o in opts for r in o['answer_region_refs']}
        owners=[f for f in groups if f is not parent and {r['block_id']for r in _targets(f,lookup)}==targets]
        if len(owners)>1 or any(_protected(f)for f in owners):continue
        child=owners[0]if owners else new_field(local[ci],copylabels[0])
        # A malformed model tree may put the main question beneath its copy
        # request. Moving that ancestor under this descendant would create an
        # unreachable cycle. Keep the original tree and require local review.
        if any(node is parent for node in _walk([child])):
            child.setdefault('review_reasons',[]).append('主问题与附件的父子结构冲突；已保留原文分组，需核对。')
            child['review_status']='needs_review'
            continue
        containers=[(None,fields)]+[(f,f.get('children',[]))for f in flat]
        mounts=[(owner,items)for owner,items in containers if any(item is child for item in items)]
        if any(owner is not None and _protected(owner)for owner,_ in mounts):continue
        # A source-target repair can move an old model child, but must not
        # leave the same object mounted under two parents. Other children stay.
        for _,items in mounts:items[:]=[item for item in items if item is not child]
        _set_field(child,copylabels,opts,left,right,'attachment',lookup)
        child.update(parent_question_id=parent['field_id'],context_refs=deepcopy(parent['label_refs']),
            attachment_group_evidence={'role':'attachment','source':'closed_scan_choice_groups'})
        if child not in parent.setdefault('children',[]):parent['children'].append(child)
        claimed.update(r['block_id']for r in copylabels)
        # Retire only redundant model fields whose complete label belongs to
        # this exact copy request. Unparsed registration/date text remains.
        for f in flat:
            if f is child or f is parent or _protected(f):continue
            rs=f.get('label_refs',[])
            if rs and all(r['block_id']in {x['block_id']for x in copylabels}for r in rs):remove.add(id(f))
    def prune(items):
        items[:]=[f for f in items if id(f)not in remove]
        for f in items:prune(f.get('children',[]))
    prune(fields)
    # Rebuild protections from actual remaining labels; an earlier broad
    # model group may have incorrectly protected an option or answer caption.
    for bid in claimed:
        lookup[bid].setdefault('structure',{})['protected_label_ranges']=[{'start':r['start'],'end':r['end']}for f in _walk(fields)for r in f.get('label_refs',[])if r['block_id']==bid]
    return nonqa


def current_scan_attachment_evidence(field,lookup):
    if field.get('boundary_evidence')!='closed_scan_choice_groups'or field.get('boundary_version')!=VERSION or field.get('scan_row_evidence',{}).get('role')!='attachment':return []
    left=lookup.get(field.get('physical_label_region_ref',{}).get('block_id'),{});right=lookup.get(field.get('physical_response_region_ref',{}).get('block_id'),{})
    if not source_cells_share_row(left,right)or not any(COPY.search(lookup.get(r['block_id'],{}).get('text','')[r['start']:r['end']])for r in field.get('label_refs',[])):return []
    texts=[b for b in lookup.values()if b.get('text','').strip()and b.get('location',{}).get('extraction')=='ocr']
    options=_copy_options(texts,right,list(lookup.values()))
    if not options or {r['block_id']for o in options for r in o['answer_region_refs']}!={r['block_id']for r in field.get('answer_region_refs',[])}:return []
    return deepcopy(options[0]['refs'])
