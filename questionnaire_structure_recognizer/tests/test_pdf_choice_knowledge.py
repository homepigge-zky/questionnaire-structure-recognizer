from copy import deepcopy
from questionnaire_structure_recognizer.validation.selection_evidence import selected_scan_caption
from questionnaire_structure_recognizer.validation.validator import _apply_pairing_validation
from questionnaire_structure_recognizer.knowledge import extract_pending_entries


def test_visible_native_marks_authorize_source_captions_not_bare_ticks_as_knowledge():
    def block(ident,text,bbox,extraction='ocr_region',**structure):
        return {'block_id':ident,'text':text,'kind':'ocr_text','order':len(ident),'structure':structure,
            'controls':[],'location':{'type':'pdf','page':1,'bbox':bbox,'extraction':extraction}}
    def ref(ident,text):return {'block_id':ident,'start':0,'end':len(text)}
    caption=block('yes','Yes',[200,80,211,89]);lookup={'yes':caption};fields=[]
    for i in range(2):
        y=100+i*25;q=block(f'q{i}',f'Has procedure {i}?',[40,y,140,y+10]);v=block(f'v{i}','√',[202,y+2,210,y+10],extraction='native')
        region=block(f'r{i}','',[200,y,212,y+12],extraction='ocr',scan_region_kind='checkbox',
            scan_association_valid=True,native_selection_verified=True,overlay_value_block_ids=[v['block_id']],
            scan_option_caption_refs=[ref('yes','Yes')])
        region['controls']=[{'selected':True,'source':'native_mark_in_scan_rectangle','value_block_id':v['block_id']}]
        lookup.update({b['block_id']:b for b in [q,v,region]})
        f={'field_id':f'f{i}','question_text_original':q['text'],'label_refs':[ref(q['block_id'],q['text'])],
            'answer_region_refs':[ref(region['block_id'],'')],'answer_value_refs':[ref('yes','Yes')],
            'answer_text_original':'Yes','answer_status':'filled','options':[{'selected':True,'text_original':'Yes',
                'refs':[ref('yes','Yes')],'answer_region_refs':[ref(region['block_id'],'')]}]}
        fields.append(f)
    assert all(selected_scan_caption(ref('yes','Yes'),f,lookup) for f in fields)
    _apply_pairing_validation(fields,lookup)
    assert all(f['pairing_validation']=='valid' for f in fields)
    result={'questions':fields,'source_blocks':list(lookup.values())}
    drafts=extract_pending_entries({'id':'test','filename':'fictional.pdf'},1,result)
    assert len(drafts)==2 and all(d['answer']=='Yes' and not d['active'] for d in drafts)
    # Model-selected flags cannot override a physically unselected source.
    lookup['r0']['controls'][0]['selected']=False
    assert not selected_scan_caption(ref('yes','Yes'),fields[0],lookup)
    # Source captions in another physical column also cannot be reused.
    lookup['yes']['location']['bbox']=[240,80,251,89]
    assert not selected_scan_caption(ref('yes','Yes'),fields[1],lookup)
