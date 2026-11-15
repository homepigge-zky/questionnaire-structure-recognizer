from copy import deepcopy
import pytest

from questionnaire_structure_recognizer.source_text_review import create_source_text_review,apply_source_text_reviews
from questionnaire_structure_recognizer.validation.pdf_source_relations import validate_ocr_response_values


def fixture(reviewed=True):
    def block(bid,text,box,**structure):
        return {'block_id':bid,'kind':'ocr_text','text':text,'location':{'type':'pdf','page':1,'extraction':'ocr','bbox':box},
                'style':{'ocr_confidence':.55},'structure':{'source_text_is_ocr':True,**structure},'controls':[]}
    items=[block('label','Employees',[25,105,135,117]),block('value','T3ST VALUE',[205,105,285,117]),
           block('region','',[200,100,400,130],scan_region_kind='text_box',blank_state='unresolved')]
    if reviewed:
        r=create_source_text_review('a'*64,items,[{'block_id':'value','before':'T3ST VALUE','after':'TEST VALUE'}],
             reviewer='source-viewer',evidence={'method':'source_image_visual_review','note':'Visible original source checked.',
                 'pages':[{'page':1,'image_sha256':'b'*64}]})
        items,_=apply_source_text_reviews('a'*64,items,[r])
    field={'field_id':'f1','label_refs':[{'block_id':'label','start':0,'end':9}],
           'answer_region_refs':[{'block_id':'region','start':0,'end':0}],
           'answer_value_refs':[{'block_id':'value','start':0,'end':10}],
           'answer_text_original':items[1]['text'],'review_reasons':[]}
    return field,{b['block_id']:b for b in items}


def test_source_review_can_read_low_confidence_value_without_altering_native_confidence():
    field,lookup=fixture()
    assert validate_ocr_response_values(field,lookup,[field])
    assert lookup['value']['style']['ocr_confidence']==.55
    assert field['source_observation']['transcription_status']=='source_reviewed'
    assert field['source_observation']['status']=='pending_review'  # knowledge still requires review
    assert not field['source_review_reasons']


@pytest.mark.parametrize('mutation',['no_review','text','bbox','label','outside','zero_range','fake_flag'])
def test_review_does_not_bypass_current_text_position_or_field_association(mutation):
    field,lookup=fixture(mutation!='no_review')
    others=[field]
    if mutation=='text':lookup['value']['text']='EVIL VALUE'
    if mutation=='bbox':lookup['value']['location']['bbox'][0]+=1
    if mutation=='label':others.append({'label_refs':deepcopy(field['answer_value_refs'])})
    if mutation=='outside':lookup['region']['location']['bbox']=[410,100,500,130]
    if mutation=='zero_range':field['answer_value_refs'][0]['end']=0
    if mutation=='fake_flag':lookup['value']['structure']['source_text_review']={'reviewed':True}
    assert not validate_ocr_response_values(field,lookup,others)
