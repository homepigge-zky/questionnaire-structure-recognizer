"""Split exact inline OCR source ranges in a repeated ruled form.

This does not correct handwriting. A textual boundary plus actual row borders
identifies the field; the observed value remains explicitly pending review.
"""
from copy import deepcopy
import re
from .field_boundaries import ref
from .form_completion import new_field

VERSION='ocr-inline-source-ranges-v4-conditional-prompts-remain-labels'
BILINGUAL=re.compile(r"^([A-Za-z][A-Za-z /().',\-]{1,50}[\u3400-\u9fff](?:[\u3400-\u9fff/／ \t（）()，、\-]|[A-Za-z](?=[，、]?[\u3400-\u9fff])){1,30})\s*([A-Za-z0-9+].+)$")
COLON=re.compile(r'^([^:：\n]{2,70}[:：])\s*(\S.+)$')
LABEL_ONLY=re.compile(r"^[A-Za-z][A-Za-z /().',\-]{1,50}[\u3400-\u9fff][\u3400-\u9fff/／ \t（）()，、\-]{1,30}[:：]?$")


def _metadata_cells(blocks):
    """A row of document/version/page controls is template furniture.

    Only the physically adjacent closed cells are classified. No page band,
    fuzzy OCR date match, or neighboring supplier field is excluded.
    """
    from .pdf_source_relations import scan_label_inside
    cells=[b for b in blocks if b.get('location',{}).get('type')=='pdf' and b.get('structure',{}).get('scan_region_kind')=='text_box']
    result=set()
    for anchor in cells:
        a=anchor['location']['bbox'];page=anchor['location']['page']
        row=sorted([c for c in cells if c['location']['page']==page and abs(c['location']['bbox'][1]-a[1])<5 and abs(c['location']['bbox'][3]-a[3])<5],key=lambda c:c['location']['bbox'][0])
        if not 3<=len(row)<=6 or any(abs(c['location']['bbox'][2]-d['location']['bbox'][0])>4 for c,d in zip(row,row[1:])):continue
        members=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('page')==page and any(scan_label_inside(b['location'].get('bbox',[]),c['location']['bbox']) for c in row)]
        compact=[re.sub(r'\s+','',b['text']).casefold() for b in members]
        if not all(any(re.fullmatch(p,t) for t in compact) for p in (r'documentno\.?[:：]?',r'versionno\.?[:：]?',r'page[:：]?')):continue
        evidence=[ref(c) for c in row]
        # A damaged first metadata-cell contour can retain its real top and
        # bottom rules. Extend only to their common span, anchored by the
        # three independently observed document/version/page cells.
        x0=min(c['location']['bbox'][0] for c in row);x1=max(c['location']['bbox'][2] for c in row)
        y0=min(c['location']['bbox'][1] for c in row);y1=max(c['location']['bbox'][3] for c in row)
        rules=[b for b in blocks if b.get('location',{}).get('page')==page and b.get('structure',{}).get('scan_region_kind')=='write_line' and
               b['location']['bbox'][0]<x0-10 and b['location']['bbox'][2]>=x1-4]
        tops=[b for b in rules if abs(b['location']['bbox'][3]-y0)<4];bottoms=[b for b in rules if abs(b['location']['bbox'][3]-y1)<4]
        if tops and bottoms:
            top=min(tops,key=lambda b:abs(b['location']['bbox'][3]-y0));bottom=min(bottoms,key=lambda b:abs(b['location']['bbox'][3]-y1))
            band=[max(top['location']['bbox'][0],bottom['location']['bbox'][0]),y0,min(top['location']['bbox'][2],bottom['location']['bbox'][2]),y1]
            members=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('page')==page and scan_label_inside(b['location'].get('bbox',[]),band)]
            evidence.extend([ref(top),ref(bottom)])
        for b in members:
            b.setdefault('structure',{}).update(resolved_layout_role='fixed_template_metadata',role_evidence='closed_document_version_page_row',
                                                metadata_row_refs=deepcopy(evidence))
            result.add(b['block_id'])
    return result


def _classify_fixed_instructions(fields,blocks,lookup):
    instructions=_metadata_cells(blocks)
    for b in blocks:
        if b.get('location',{}).get('type')!='pdf':continue
        text=b.get('text','');compact=re.sub(r'\s+','',text).casefold()
        if '?' in text or '？' in text:continue
        direct=(re.match(r'(?:please)?sendthecompletedquestionnaireto|ore-?mailthee-?copyto',compact) or
            re.match(r'请将问卷寄送给|或将电子版文件发送至',compact) or
            re.match(r'请.{0,20}完成所附问卷',compact) or
            re.search(r'(?:所附文件|所有信息).{0,20}(?:视为保密|保密信息)',compact))
        if direct:
            b.setdefault('structure',{}).update(resolved_layout_role='instruction',role_evidence='explicit_questionnaire_delivery_or_completion_instruction')
            instructions.add(b['block_id'])
    if not instructions:return
    kept=[]
    for old in fields:
        if old.get('manual_override'):kept.append(old);continue
        refs=old.get('label_refs') or old.get('question_refs') or []
        remaining=[rr for rr in refs if rr['block_id'] not in instructions]
        if len(remaining)==len(refs):kept.append(old)
        elif remaining:
            item=deepcopy(old);item.update(label_refs=remaining,question_refs=deepcopy(remaining),
                question_text_original='\n'.join(lookup[rr['block_id']]['text'][rr['start']:rr['end']] for rr in remaining),
                answer_region_refs=[],answer_refs=[],answer_value_refs=[],answer_text_original='',answer_status='unresolved',review_status='needs_review')
            kept.append(item)
    fields[:]=kept


def restore_ocr_inline_fields(fields,blocks,lookup):
    protected={r['block_id'] for f in fields if f.get('manual_override') or f.get('answer_origin') in {'manual','knowledge','reviewed_knowledge'}
               for r in f.get('label_refs',[])+f.get('answer_value_refs',[])+f.get('answer_region_refs',[])}
    _classify_fixed_instructions(fields,blocks,lookup)
    lines=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='write_line' and b['location']['bbox'][2]-b['location']['bbox'][0]>180]
    recovered=[];consumed=set()
    answer_ids={rr['block_id'] for f in fields for rr in f.get('answer_value_refs',[])}
    for b in blocks:
        loc=b.get('location',{});text=b.get('text','');s=b.get('structure',{});bb=loc.get('bbox',[])
        if loc.get('extraction')!='ocr' or not 5<len(text)<220 or len(bb)!=4 or b.get('controls'):continue
        if b['block_id'] in protected or b['block_id'] in answer_ids or s.get('resolved_layout_role') in {'existing_answer','option','instruction','section_header','fixed_template_metadata'}:continue
        if re.match(r'\s*(?:note|instructions?|remarks?|https?|printed by|print date)\b',text,re.I):continue
        match=COLON.match(text) or BILINGUAL.match(text)
        if not match:continue
        label=match.group(1).rstrip();value=match.group(2).strip();start=match.start(2)
        if not value or re.search(r'[?？]|\b(?:please|must|shall)\b',label,re.I):continue
        # "If No: How can you ...?" is one customer prompt. The condition
        # colon never authorizes treating the rest of that prompt as a value.
        # A genuinely observed "If No: N/A" remains a separate supported case.
        conditional_prefix=re.fullmatch(r'(?:if(?:yes|no)|若[有无無是否]|如[有无無是否])[:：]',re.sub(r'\s+','',label),re.I)
        if conditional_prefix and (re.search(r'[?？]',value) or re.match(r'(?:how|what|why|when|where|which|who|is|are|do|does|can|could|has|have|please|provide|explain|describe|state|specify)\b|请|如何|怎样|是否',value,re.I)):continue
        if re.fullmatch(r'(?:SECTION[IVXLCDM0-9]*|第.{1,10}(?:章|节|部分))[:：]?',re.sub(r'\s+','',label),re.I):continue
        # An OCR letter embedded inside a Chinese label is not an evidenced
        # label/value delimiter. Leave the full source unresolved instead.
        if re.search(r'[\u3400-\u9fff]',value[:8]):continue
        page=loc.get('page');cy=(bb[1]+bb[3])/2
        column=[x for x in lines if x['location']['page']==page and abs(x['location']['bbox'][0]-bb[0])<20 and x['location']['bbox'][2]>=bb[2]-5]
        from .pdf_source_relations import scan_label_inside
        closed=[c for c in blocks if c.get('structure',{}).get('scan_region_kind')=='text_box' and c['location']['page']==page and
                c.get('structure',{}).get('closed_region_evidence') and 14<c['location']['bbox'][3]-c['location']['bbox'][1]<45 and
                scan_label_inside(bb,c['location']['bbox'])]
        tops=[x for x in column if x['location']['bbox'][3]<=cy and cy-x['location']['bbox'][3]<32]
        bottoms=[x for x in column if x['location']['bbox'][3]>cy and x['location']['bbox'][3]-cy<32]
        if len(closed)==1:
            rowrefs=[ref(closed[0])]
        else:
            if len(column)<4 or not tops or not bottoms:continue
            top=max(tops,key=lambda x:x['location']['bbox'][3]);bottom=min(bottoms,key=lambda x:x['location']['bbox'][3])
            if not 14<bottom['location']['bbox'][3]-top['location']['bbox'][3]<45:continue
            if abs(top['location']['bbox'][2]-bottom['location']['bbox'][2])>15:continue
            rowrefs=[ref(top),ref(bottom)]
        label_ref=ref(b,0,len(label));value_ref=ref(b,start,start+len(value));sid=f'ocr-inline:{b["block_id"]}:{start}'
        region_ref=deepcopy(value_ref);region_ref['slot_id']=sid
        s.setdefault('logical_slots',[]).append({'slot_id':sid,'kind':'ocr_inline_existing_value','start':value_ref['start'],
            'end':value_ref['end'],'expected_source_text':text,'label_refs':[deepcopy(label_ref)],
            'evidence':'explicit_inline_delimiter_in_repeated_ruled_rows','row_border_refs':rowrefs,
            'bbox_precision':'whole_ocr_line_with_exact_character_range','bbox':deepcopy(bb)})
        f=new_field(b,label_ref,[region_ref]);fid='ocr-inline-field:'+b['block_id']
        f.update(field_id=fid,question_id=fid,label_refs=[label_ref],question_refs=[deepcopy(label_ref)],question_text_original=label,
            answer_region_refs=[region_ref],answer_value_refs=[value_ref],answer_refs=[deepcopy(value_ref)],answer_text_original=value,
            answer_status='filled',review_status='needs_review',boundary_evidence='ocr_inline_delimiter_with_row_borders',boundary_version=VERSION,
            review_reasons=['已按原文分隔和表格行边界拆分标签与已有文字；手写/OCR值必须对照原页审核，未自动纠字。'])
        f['source_observation']={'origin':'source_document','status':'pending_review','answer_value_refs':[deepcopy(value_ref)]}
        from .reviewed_transcription import resolve_transcription_reason
        resolve_transcription_reason(f,lookup,{'已按原文分隔和表格行边界拆分标签与已有文字；手写/OCR值必须对照原页审核，未自动纠字。'})
        s['protected_label_ranges']=[{'start':0,'end':len(label)}]
        recovered.append(f);consumed.add(b['block_id'])
    # In a shared ruled row, a second explicitly delimited field terminates
    # the first label's writing band. Preserve separately OCR-read fragments
    # on its right; never take text beyond that next real label or row border.
    for following in list(recovered):
        nb=lookup[following['label_refs'][0]['block_id']];nl=nb['location'];box=nl['bbox'];ncy=(box[1]+box[3])/2
        slot=next((x for x in nb['structure'].get('logical_slots',[]) if x['slot_id']==following['answer_region_refs'][0].get('slot_id')),None)
        borders=[lookup[r['block_id']] for r in (slot or {}).get('row_border_refs',[])]
        if len(borders)!=2 or any(x.get('structure',{}).get('scan_region_kind')!='write_line' for x in borders):continue
        upper,lower=sorted(borders,key=lambda x:x['location']['bbox'][3]);top=upper['location']['bbox'][3];bottom=lower['location']['bbox'][3]
        labels=[b for b in blocks if b['block_id'] not in consumed|protected and b.get('location',{}).get('page')==nl['page'] and
                b.get('location',{}).get('extraction')=='ocr' and LABEL_ONLY.fullmatch(b.get('text','')) and
                abs(b['location']['bbox'][0]-box[0])<10 and top<sum(b['location']['bbox'][1::2])/2<ncy-8]
        if len(labels)!=1:continue
        label=labels[0];lb=label['location']['bbox'];lcy=(lb[1]+lb[3])/2
        values=[b for b in blocks if b['block_id'] not in consumed|protected and b is not label and b.get('text','').strip() and
                b.get('location',{}).get('page')==nl['page'] and b.get('location',{}).get('extraction')=='ocr' and
                b['location']['bbox'][0]>lb[2]+3 and b['location']['bbox'][2]<=min(upper['location']['bbox'][2],lower['location']['bbox'][2])+3 and
                top<sum(b['location']['bbox'][1::2])/2<ncy-4 and abs(sum(b['location']['bbox'][1::2])/2-lcy)<12 and
                not (COLON.match(b['text']) or BILINGUAL.match(b['text']) or LABEL_ONLY.fullmatch(b['text']))]
        if not values:continue
        values.sort(key=lambda b:(round((sum(b['location']['bbox'][1::2])/2)/12),b['location']['bbox'][0]))
        refs=[ref(b) for b in values];regions=[]
        for b,vr in zip(values,refs):
            sid='ocr-row-value:'+b['block_id'];rr={**vr,'slot_id':sid};regions.append(rr)
            b.setdefault('structure',{}).update(protected_label_ranges=[],resolved_layout_role='existing_answer')
            b['structure'].setdefault('logical_slots',[]).append({'slot_id':sid,'kind':'ocr_inline_existing_value','start':0,'end':len(b['text']),
                'expected_source_text':b['text'],'label_refs':[ref(label)],'row_border_refs':[ref(upper),ref(lower)],
                'next_field_label_ref':deepcopy(following['label_refs'][0]),'evidence':'same_ruled_row_before_next_explicit_field',
                'bbox_precision':'exact_ocr_value_block','bbox':deepcopy(b['location']['bbox'])})
        f=new_field(label,ref(label),regions);fid='ocr-ruled-field:'+label['block_id']
        f.update(field_id=fid,question_id=fid,answer_region_refs=regions,answer_value_refs=refs,answer_refs=deepcopy(refs),
                 answer_text_original='\n'.join(b['text'] for b in values),answer_status='filled',review_status='needs_review',
                 boundary_evidence='ocr_split_row_value_before_next_inline_field',boundary_version=VERSION,
                 review_reasons=['已依据实际行边界与下一字段边界恢复分段原文；OCR值保留待人工校读，未自动纠字。'],
                 source_observation={'origin':'source_document','status':'pending_review','answer_value_refs':deepcopy(refs)})
        label.setdefault('structure',{})['protected_label_ranges']=[{'start':0,'end':len(label['text'])}]
        recovered.append(f);consumed.update([label['block_id']]+[b['block_id'] for b in values])
    if not recovered:return
    kept=[]
    for old in fields:
        refs=old.get('label_refs') or old.get('question_refs') or []
        if not refs or not any(rr['block_id'] in consumed for rr in refs):kept.append(old);continue
        left=[rr for rr in refs if rr['block_id'] not in consumed]
        lookup[refs[0]['block_id']].setdefault('structure',{}).setdefault('replaced_model_groups',[]).append(deepcopy(old))
        if left:
            item=deepcopy(old);item.update(label_refs=left,question_refs=deepcopy(left),question_text_original='\n'.join(lookup[rr['block_id']]['text'][rr['start']:rr['end']] for rr in left),
                answer_text_original='',answer_refs=[],answer_value_refs=[],answer_region_refs=[],answer_status='unresolved',review_status='needs_review')
            kept.append(item)
    fields[:]=kept+recovered
    fields.sort(key=lambda f:lookup.get((f.get('label_refs') or [{}])[0].get('block_id'),{}).get('order',0))
