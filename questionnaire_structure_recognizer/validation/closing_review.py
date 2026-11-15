"""Protect local approval lines after the source's questionnaire closing.

This is evidence of a current participant, not inferred ownership of the
company named above the lines. Ordinary business contacts remain reusable.
"""
import re


def closing_participant_refs(field, lookup):
    labels=field.get('label_refs') or field.get('question_refs') or []
    if len(labels)!=1:
        return []
    ref=labels[0]; label=lookup.get(ref.get('block_id'),{}); text=label.get('text','')
    start,end=ref.get('start'),ref.get('end')
    if not (isinstance(start,int) and isinstance(end,int) and 0<=start<end<=len(text)):
        return []
    caption=text[start:end].strip()
    if not re.fullmatch(r'(?:[A-Za-z][A-Za-z /&-]{0,45})?(?:Manager|Head|Director|Reviewer|Approver)\s*[:：]?'
                        r'|[\u4e00-\u9fff]{0,10}(?:经理|經理|负责人|負責人|审核人|審核人)\s*[:：]?',caption,re.I):
        return []
    location=label.get('location',{}); box=location.get('bbox') or []; page=location.get('page')
    if location.get('type')!='pdf' or not isinstance(page,int) or len(box)!=4:
        return []
    targets=[lookup.get(r.get('block_id'),{}) for r in field.get('answer_region_refs',[])]
    if not targets or not any(t.get('structure',{}).get('scan_region_kind')=='write_line'
                              and t.get('location',{}).get('page')==page for t in targets):
        return []
    nearby=[]
    for block in lookup.values():
        loc=block.get('location',{}); bounds=loc.get('bbox') or []
        if loc.get('type')=='pdf' and loc.get('page')==page and len(bounds)==4 and 0<=box[1]-bounds[3]<=120:
            nearby.append(block)
    closings=[b for b in nearby if re.search(r'thankyouforcompleting(?:this|the)?questionnaire'
               r'|感[谢謝].*填[写寫].*[问問]卷',re.sub(r'\s+','',b.get('text','')),re.I)]
    for closing in closings:
        y=closing['location']['bbox'][3]
        organizations=[b for b in nearby if b['location']['bbox'][1]>y and
            re.search(r'\b(?:co\.?\s*,?\s*ltd\.?|limited|inc\.?|corporation)\b|公司',b.get('text',''),re.I)]
        if len(organizations)==1:
            return [{'block_id':b['block_id'],'start':0,'end':len(b['text'])}
                    for b in [closing,organizations[0]]]
    return []
