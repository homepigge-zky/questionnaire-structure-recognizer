"""Choice records stay attached to their physical row and column header."""
from copy import deepcopy
import re
from .field_boundaries import ref
from .form_completion import new_field
from .pdf_source_relations import scan_label_inside
from .selection_evidence import selected_scan_caption
from ..parser_core.pdf_caption_fragments import verified_caption_crop

VERSION='scan-record-choices-v2-complete-source-selections'


def restore_scanned_record_choices(fields,blocks,lookup):
    from .scanned_cells import _replace_owned, _owned_by_reviewed
    cells=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='text_box']
    headers=[b for b in blocks if b.get('structure',{}).get('resolved_layout_role')=='record_column_header' and
             not re.fullmatch(r'no\.?|序号|编号',b.get('text','').strip(),re.I)]
    for header in headers:
        page=header['location']['page'];hb=header['location']['bbox']
        containing=[c for c in cells if c['location']['page']==page and scan_label_inside(hb,c['location']['bbox'])]
        if not containing:continue
        hc=min(containing,key=lambda c:(c['location']['bbox'][2]-c['location']['bbox'][0])*(c['location']['bbox'][3]-c['location']['bbox'][1]));cb=hc['location']['bbox']
        rows=sorted([c for c in cells if c['location']['page']==page and c['location']['bbox'][1]>=cb[3]-8 and
            abs(c['location']['bbox'][0]-cb[0])<4 and abs(c['location']['bbox'][2]-cb[2])<4],key=lambda c:c['location']['bbox'][1])
        chain=[];bottom=cb[3]
        for cell in rows:
            if abs(cell['location']['bbox'][1]-bottom)>8:break
            chain.append(cell);bottom=cell['location']['bbox'][3]
        if len(chain)<2:continue
        for index,cell in enumerate(chain):
            marks=[b for b in blocks if b.get('structure',{}).get('scan_region_kind')=='checkbox' and b['location']['page']==page and scan_label_inside(b['location']['bbox'],cell['location']['bbox'])]
            if not 2<=len(marks)<=16:continue
            options=[];owned=set()
            bands=[]
            for mark in sorted(marks,key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0])):
                mb=mark['location']['bbox'];cy=(mb[1]+mb[3])/2
                band=next((band for band in bands if abs(band[0]-cy)<8),None)
                if band:band[1].append(mark)
                else:bands.append([cy,[mark]])
            ordered=[mark for _,band in bands for mark in sorted(band,key=lambda b:b['location']['bbox'][0])]
            for mark in ordered:
                evidence=mark.get('structure',{}).get('scan_option_caption_crop') or {};refs=evidence.get('caption_refs',[])
                if evidence.get('container_ref',{}).get('block_id')!=cell['block_id'] or len(refs)!=1 or not verified_caption_crop(mark,refs[0],lookup):break
                control=(mark.get('controls') or [{}])[0];rr=refs[0];value=lookup[rr['block_id']]['text'][rr['start']:rr['end']]
                options.append({'refs':deepcopy(refs),'text_original':value,'selected':control.get('selected'),
                    'selection_source':control.get('source'),'answer_region_refs':[ref(mark)]});owned.add(rr['block_id'])
            if len(options)!=len(marks):continue
            region_ids={b['block_id'] for b in marks}
            if _owned_by_reviewed(fields,region_ids|owned):continue
            fid='scan-record-choice:'+cell['block_id'];f=new_field(header,ref(header))
            f.update(field_id=fid,question_id=fid,label_refs=[ref(header)],question_refs=[ref(header)],question_text_original=header['text'],
                options=options,answer_region_refs=[r for o in options for r in o['answer_region_refs']],answer_value_refs=[],answer_refs=[],
                answer_text_original='',answer_status='blank',boundary_evidence='closed_record_choice_column',boundary_version=VERSION,
                physical_response_region_ref=ref(cell),record_header_ref=ref(header),record_column_region_refs=[ref(c) for c in chain],
                record_group_id=hc['block_id'],record_instance_id=cell['block_id'],record_row_index=index,
                review_status='model_proposed',review_reasons=[],
                record_relation_notes=['记录按原页实体行定位；不同记录不能按相同列标题互相复用答案。'])
            for option in options:
                mark=lookup[option['answer_region_refs'][0]['block_id']]
                mark['structure'].update(scan_association_valid=True,scan_field_id=fid,scan_option_caption_refs=deepcopy(option['refs']))
            selected=[rr for option in options if option['selected'] is True for rr in option['refs']]
            if selected and all(isinstance(o['selected'],bool) for o in options) and all(selected_scan_caption(rr,f,lookup) for rr in selected):
                f.update(answer_status='filled',answer_value_refs=deepcopy(selected),answer_refs=deepcopy(selected),
                    answer_text_original='\n'.join(lookup[rr['block_id']]['text'][rr['start']:rr['end']] for rr in selected),
                    answer_pairing_evidence='physical_selected_box_and_source_caption')
            if any(option['selected'] is None for option in options):f.update(answer_status='unresolved',review_status='needs_review')
            _replace_owned(fields,owned,lookup)
            for bid in owned:
                structure=lookup[bid].setdefault('structure',{});structure.pop('protected_label_ranges',None);structure['resolved_layout_role']='option'
            fields.append(f)
