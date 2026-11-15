"""Restore checkbox lists inside an actual closed raster form cell."""
from copy import deepcopy
import re

from .field_boundaries import ref
from .form_completion import new_field
from .pdf_source_relations import _inside, scan_label_inside
from .selection_evidence import selected_scan_caption

VERSION='scan-option-lists-v2-complete-source-selections'


def verified_multiple_selection(field,lookup):
    """Validate native stacked checkbox geometry before allowing multiple values."""
    evidence=field.get('selection_mode_evidence') or {}
    options=field.get('options') or []
    if field.get('selection_mode')!='multiple_allowed' or evidence.get('kind')!='physical_stacked_checkbox_list' or not 2<=len(options)<=12:return False
    if re.search(r'which\s+description|choose\s+one|select\s+one|单选|哪种|哪種',field.get('question_text_original',''),re.I):return False
    container=lookup.get(evidence.get('container_ref',{}).get('block_id'),{});cl=container.get('location',{});cb=cl.get('bbox',[])
    if cl.get('type')!='pdf' or container.get('structure',{}).get('source_method')!='raster_geometry' or len(cb)!=4:return False
    refs=[rr for o in options for rr in o.get('answer_region_refs',[])]
    if refs!=evidence.get('option_region_refs') or len(refs)!=len(options) or len({r['block_id'] for r in refs})!=len(refs):return False
    targets=[lookup.get(rr['block_id'],{}) for rr in refs]
    for option,target in zip(options,targets):
        ts=target.get('structure',{});tl=target.get('location',{});tb=tl.get('bbox',[])
        if tl.get('page')!=cl.get('page') or ts.get('scan_region_kind')!='checkbox' or len(tb)!=4 or not _inside(tb,cb):return False
        if not cb[0]-2<=tb[0]<=cb[0]+25 or not target.get('controls') or not all(c.get('type')=='checkbox' for c in target['controls']):return False
        band=ts.get('scan_option_caption_band') or {}
        if band.get('container_ref')!=evidence['container_ref'] or band.get('caption_refs')!=option.get('refs') or ts.get('scan_option_caption_refs')!=option.get('refs'):return False
        for rr in option.get('refs',[]):
            b=lookup.get(rr.get('block_id'),{});bb=b.get('location',{}).get('bbox',[])
            if not (isinstance(rr.get('start'),int) and isinstance(rr.get('end'),int) and 0<=rr['start']<rr['end']<=len(b.get('text',''))):return False
            if len(bb)!=4 or not scan_label_inside(bb,cb) or not band.get('start_y',-999)<=(bb[1]+bb[3])/2<band.get('end_y',-999):return False
    return max(t['location']['bbox'][0] for t in targets)-min(t['location']['bbox'][0] for t in targets)<=8


def restore_scanned_option_lists(questions,blocks,lookup):
    from .scanned_cells import _replace_owned, _owned_by_reviewed
    text=[b for b in blocks if b.get('text','').strip() and b.get('location',{}).get('extraction')=='ocr']
    marks=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='checkbox']
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box' and
        b['location']['bbox'][2]-b['location']['bbox'][0]>250]
    for cell in cells:
        cb=cell['location']['bbox'];page=cell['location']['page']
        local=[b for b in marks if b['location']['page']==page and _inside(b['location']['bbox'],cb)]
        if not 2<=len(local)<=12:continue
        local.sort(key=lambda b:b['location']['bbox'][1])
        if max(b['location']['bbox'][0] for b in local)-min(b['location']['bbox'][0] for b in local)>8:continue
        if any(not cb[0]-2<=b['location']['bbox'][0]<=cb[0]+25 for b in local):continue
        source=[b for b in text if b['location']['page']==page and scan_label_inside(b['location']['bbox'],cb)]
        first_y=local[0]['location']['bbox'][1]
        labels=[b for b in source if b['location']['bbox'][3]<first_y+1]
        if not labels or any(re.fullmatch(r'\d+(?:\.\d+)*',b['text'].strip()) for b in labels):continue
        labels.sort(key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0]))
        options=[];owned={b['block_id'] for b in labels};followup=[]
        for index,mark in enumerate(local):
            mb=mark['location']['bbox'];cy=(mb[1]+mb[3])/2
            stop=local[index+1]['location']['bbox'][1]-2 if index+1<len(local) else cb[3]
            rows=sorted([b for b in source if b['block_id'] not in owned and cy-8<=(b['location']['bbox'][1]+b['location']['bbox'][3])/2<stop
                and b['location']['bbox'][0]>=mb[0]-5],key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0]))
            captions=[]
            for row in rows:
                if re.match(r'\s*(?:\(?\*|if\b|\(?\s*explain\b|如果|若|\(?明\s*Explain|答\s*Ans\s*[:：])',row['text'],re.I):
                    followup.append(row);break
                captions.append(row)
            if not captions or abs((captions[0]['location']['bbox'][1]+captions[0]['location']['bbox'][3])/2-cy)>8:break
            refs=[]
            for row in captions:
                start=0
                if row is captions[0] and row['text'][:1] in {'□','☐','☑','☒','口','√','✓','✔'}:start=1
                refs.append(ref(row,start,len(row['text'])))
            control=(mark.get('controls') or [{}])[0]
            option={'refs':refs,'text_original':'\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in refs),
                'selected':control.get('selected'),'selection_source':control.get('source'),'answer_region_refs':[ref(mark)]}
            options.append(option);owned.update(b['block_id'] for b in captions)
            mark['structure'].update(scan_option_caption_refs=deepcopy(refs),scan_option_caption_band={
                'method':'closed_cell_stacked_checkbox_caption_band','container_ref':ref(cell),
                'start_y':cy-8,'end_y':stop,'next_mark_ref':ref(local[index+1]) if index+1<len(local) else None,
                'caption_refs':deepcopy(refs)})
        if len(options)!=len(local):continue
        if _owned_by_reviewed(questions,owned|{b['block_id'] for b in local}):continue
        fid='scan-option-list:'+cell['block_id'];f=new_field(labels[0],ref(labels[0]))
        f.update(field_id=fid,question_id=fid,label_refs=[ref(b) for b in labels],question_refs=[ref(b) for b in labels],
            question_text_original='\n'.join(b['text'] for b in labels),options=options,answer_region_refs=[ref(b) for b in local],
            answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='blank',boundary_evidence='closed_scan_option_list',boundary_version=VERSION,
            selection_mode='single' if re.search(r'which\s+description|choose\s+one|select\s+one|单选|哪种|哪種',labels[0]['text'],re.I) else 'multiple_allowed',
            selection_mode_evidence={'kind':'physical_stacked_checkbox_list','container_ref':ref(cell),'option_region_refs':[ref(b) for b in local]})
        for mark in local:mark['structure'].update(scan_association_valid=True,scan_field_id=fid)
        selected=[rr for option in options if option['selected'] is True for rr in option['refs']]
        if selected and all(isinstance(o['selected'],bool) for o in options) and all(selected_scan_caption(rr,f,lookup) for rr in selected):
            f.update(answer_status='filled',answer_value_refs=deepcopy(selected),answer_refs=deepcopy(selected),
                answer_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in selected),
                answer_pairing_evidence='physical_selected_box_and_source_caption')
        if any(o['selected'] is None for o in options):
            f.update(answer_status='unresolved',review_status='needs_review',review_reasons=['复选列表中部分实际框内符号尚未确认，未猜测选择状态。'])
        _replace_owned(questions,owned,lookup)
        for label in labels:label.setdefault('structure',{})['protected_label_ranges']=[{'start':0,'end':len(label['text'])}]
        for option in options:
            for rr in option['refs']:
                b=lookup[rr['block_id']];prior=b.setdefault('structure',{}).pop('protected_label_ranges',None)
                if prior:b['structure']['superseded_label_ranges']=prior
                b['structure']['resolved_layout_role']='option'
        questions.append(f)
