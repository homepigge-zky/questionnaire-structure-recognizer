"""A merged OCR line must not suppress a proven marked physical option."""
from PIL import Image, ImageDraw
import pytest
from questionnaire_structure_recognizer.parser_core.ocr import scan_form_regions


def fixture(kind='tick',peer=True,border=True):
    image=Image.new('RGB',(1200,1600),'white');draw=ImageDraw.Draw(image)
    if border:draw.rectangle((800,200,830,230),outline='black',width=2)
    if kind=='tick':draw.line([(799,219),(808,227),(831,201)],fill='black',width=4)
    elif kind=='cross':
        draw.line([(805,205),(825,225)],fill='black',width=3);draw.line([(805,225),(825,205)],fill='black',width=3)
    elif kind=='letter':draw.text((808,208),'B',fill='black')
    elif kind=='slash':draw.line([(805,225),(825,205)],fill='black',width=3)
    if peer:draw.rectangle((650,200,680,230),outline='black',width=2)
    text=[{'text':'Fictional alpha beta gamma choice labels','bbox':[640,198,1150,232],'confidence':.99}]
    return image,text


def test_merged_ocr_line_keeps_verified_tick_among_repeated_physical_boxes():
    image,text=fixture();boxes=[b for b in scan_form_regions(image,text,600) if b['kind']=='checkbox']
    marked=[b for b in boxes if b.get('selected') is True]
    assert len(boxes)==2 and len(marked)==1 and marked[0]['bbox'][0]>790
    assert marked[0]['selection_source']=='raster_two_stroke_tick'


@pytest.mark.parametrize('kind,peer,border',[('cross',True,True),('letter',True,True),('slash',True,True),('tick',False,True),('tick',True,False)])
def test_embedded_glyph_without_both_peer_box_and_verified_mark_never_becomes_selected(kind,peer,border):
    image,text=fixture(kind,peer,border)
    assert not any(b.get('selected') is True for b in scan_form_regions(image,text,600))
