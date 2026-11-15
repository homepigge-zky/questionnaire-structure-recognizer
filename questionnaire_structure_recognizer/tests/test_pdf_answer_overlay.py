"""Mixed scan + native answer layer must retain both physical layers."""
import io
import fitz
from PIL import Image, ImageDraw

from questionnaire_structure_recognizer.parser_core import pdf_parser


def make_overlay(path):
    im = Image.new('RGB', (600, 800), 'white')
    draw = ImageDraw.Draw(im)
    draw.text((35, 100), 'Supplier name', fill='black')
    draw.line((210, 115, 540, 115), fill='black', width=2)
    data = io.BytesIO(); im.save(data, format='PNG')
    doc = fitz.open(); page = doc.new_page(width=600, height=800)
    page.insert_image(page.rect, stream=data.getvalue())
    page.insert_text((220, 110), 'TEST OVERLAY SUPPLIER', fontsize=10)
    doc.save(path); doc.close()


def test_answer_overlay_retains_native_values_and_scan_write_positions(tmp_path, monkeypatch):
    path = tmp_path / 'overlay.pdf'; make_overlay(path)
    monkeypatch.setattr(pdf_parser, 'ocr_available', lambda: True)
    monkeypatch.setattr(pdf_parser, 'ocr_recognize', lambda image: [
        {'text': 'Supplier name', 'bbox': [35*image.width/600, 100*image.height/800,
            130*image.width/600, 113*image.height/800], 'confidence': .99}])
    result = pdf_parser.extract_pdf(path, 'test-hash', 'test', ocr_enabled=True).model_dump()
    native = [b for b in result['source_blocks'] if b['text'] == 'TEST OVERLAY SUPPLIER']
    assert len(native) == 1 and native[0]['location']['extraction'] == 'native'
    regions = [b for b in result['source_blocks'] if b['structure'].get('scan_region_kind') == 'write_line']
    assert regions, 'Native answers must not suppress raster writing positions'
    assert regions[0]['structure']['overlay_value_block_ids'] == [native[0]['block_id']]
    assert regions[0]['structure']['blank_state'] == 'filled'
    assert any(b['text'] == 'Supplier name' and b['structure'].get('scan_page_context') for b in result['source_blocks'])
    from questionnaire_structure_recognizer.validation.scanned_forms import restore_scanned_fields
    blocks = result['source_blocks']; lookup = {b['block_id']: b for b in blocks}
    # The real failed model group included both an empty physical region and
    # the typed answer as its question. A valid native overlay must become the
    # original answer, not a second question with a conflicting protected label.
    false_refs=[{'block_id':regions[0]['block_id'],'start':0,'end':0},
        {'block_id':native[0]['block_id'],'start':0,'end':len(native[0]['text'])}]
    fields = [{'field_id':'bad-model-answer-as-question','label_refs':false_refs,'question_refs':false_refs,
        'question_text_original':native[0]['text'],'answer_region_refs':[]}]
    restore_scanned_fields(fields, blocks, lookup)
    assert len(fields) == 1
    assert fields[0]['question_text_original'] == 'Supplier name'
    assert fields[0]['answer_text_original'] == 'TEST OVERLAY SUPPLIER'
    assert fields[0]['answer_status'] == 'filled'
    from questionnaire_structure_recognizer.validation.validator import _apply_pairing_validation
    from questionnaire_structure_recognizer.form_positions import build_form_definition
    _apply_pairing_validation(fields,lookup)
    assert fields[0]['pairing_validation']=='valid'
    definition=build_form_definition(fields[0],lookup,'test-hash',result['parser_version'])
    assert definition['fill_targets'][0]['write_method']=='standard_pdf_response'
    assert definition['fill_targets'][0]['output_modes']==['standard']
    assert fields[0]['answer_text_original']=='TEST OVERLAY SUPPLIER'
    # Tampering a source ID or moving the text to an adjacent line never
    # qualifies merely because a flag claims an overlay relationship.
    native[0]['location']['page']=2
    _apply_pairing_validation(fields,lookup)
    assert fields[0]['pairing_validation']=='invalid'


def test_native_math_root_is_not_a_checkbox_without_physical_box(tmp_path, monkeypatch):
    path = tmp_path / 'overlay.pdf'; make_overlay(path)
    monkeypatch.setattr(pdf_parser, 'ocr_available', lambda: True)
    monkeypatch.setattr(pdf_parser, 'ocr_recognize', lambda image: [])
    result = pdf_parser.extract_pdf(path, 'test-hash', 'test', ocr_enabled=True).model_dump()
    assert not any(c.get('selected') is True for b in result['source_blocks'] for c in b['controls'])


def test_invisible_old_tick_does_not_select_both_answers():
    from questionnaire_structure_recognizer.parser_core.models import make_block
    image = Image.new('RGB',(600,800),'white'); draw=ImageDraw.Draw(image)
    draw.rectangle((100,100,120,115),outline='black')
    draw.rectangle((140,100,160,115),outline='black')
    draw.line((145,107,149,111,156,103),fill='black',width=2)
    regions=[]; values=[]
    for x in (100,140):
        regions.append(make_block('ocr_text','',0,{'bbox':[x,100,x+20,115]},
            structure={'scan_region_kind':'checkbox','blank_state':'blank'},controls=[{'selected':False}]))
        values.append(make_block('paragraph','√',0,{'bbox':[x+5,103,x+16,112]},structure={}))
    pdf_parser._attach_native_overlay(regions,values,image,600)
    assert [r.controls[0]['selected'] for r in regions] == [False,True]
    assert values[0].structure['resolved_layout_role']=='invisible_native_mark'
