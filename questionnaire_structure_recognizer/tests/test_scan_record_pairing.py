from copy import deepcopy
import pytest
from questionnaire_structure_recognizer.validation.scanned_record_pairing import verified_scan_record_pairing
from questionnaire_structure_recognizer.validation.scanned_subfields import restore_scanned_subfields
from questionnaire_structure_recognizer.validation.validator import _apply_pairing_validation


def fixture():
    def b(bid,text,box,cell=False):
        s={'source_text_is_ocr':True}
        if cell:
            x,y,z,w=box;s={'scan_region_kind':'text_box','source_method':'raster_geometry',
                'closed_region_evidence':{'method':'observed_four_sided_polygon_v1','polygon':[[x,y],[z,y],[z,w],[x,w]],'edge_ink_coverage':[1.,1.,1.,1.]}}
        return {'block_id':bid,'kind':'ocr_text','text':text,'order':0,'location':{'type':'pdf','page':1,'extraction':'ocr','bbox':box},'structure':s,'style':{'ocr_confidence':.99},'controls':[]}
    blocks=[b('head','Item',[5,5,80,20]),b('hc','',[0,0,100,25],True),b('row1','',[0,25,100,55],True),b('row2','',[0,55,100,85],True),b('v1','EXAMPLE_A',[5,30,90,45]),b('v2','N/A',[5,60,90,75])]
    blocks[0]['structure']['resolved_layout_role']='record_column_header';lookup={x['block_id']:x for x in blocks};fields=[]
    restore_scanned_subfields(fields,blocks,lookup)
    return fields,lookup


def test_record_pairing_is_separate_from_unresolved_business_record_identity():
    fields,lookup=fixture()
    assert all(verified_scan_record_pairing(f,lookup) for f in fields)
    _apply_pairing_validation(fields,lookup)
    assert all(f['pairing_validation']=='valid' for f in fields)
    assert all(f['record_identity_status']=='ambiguous' for f in fields)
    assert all(f['source_observation']['status']=='pending_review' for f in fields)


@pytest.mark.parametrize('mutation',['wrong_row','value_outside','changed_header','broken_edge','wrong_index','missing_chain','cross_page'])
def test_source_record_pairing_rechecks_every_row_and_value(mutation):
    fields,lookup=fixture();f=deepcopy(fields[0]);lookup=deepcopy(lookup)
    if mutation=='wrong_row':f['answer_region_refs'][0]['block_id']='row2'
    if mutation=='value_outside':lookup['v1']['location']['bbox']=[5,60,90,75]
    if mutation=='changed_header':lookup['head']['location']['bbox']=[110,5,180,20]
    if mutation=='broken_edge':lookup['row2']['structure']['closed_region_evidence']['edge_ink_coverage'][0]=.1
    if mutation=='wrong_index':f['record_row_index']=1
    if mutation=='missing_chain':lookup['row1']['structure']['record_column_region_refs']=[]
    if mutation=='cross_page':lookup['row2']['location']['page']=2
    assert not verified_scan_record_pairing(f,lookup)
