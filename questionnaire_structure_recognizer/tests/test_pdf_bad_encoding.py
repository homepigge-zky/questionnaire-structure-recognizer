import fitz
from questionnaire_structure_recognizer.parser_core import pdf_parser


def test_nonempty_broken_character_mapping_requires_ocr_or_explicit_failure(tmp_path,monkeypatch):
    path=tmp_path/'fictional-broken.pdf';d=fitz.open();p=d.new_page();p.insert_text((40,80),'Supplier name:');d.save(path);d.close()
    garbled='\x00\x01\x02\x07[94S59;:S7=7 '+('\x07\x0e\x01\x0btext '*20)
    monkeypatch.setattr(pdf_parser,'_native_lines',lambda *a,**k:[{'text':garbled,'chars':[],'bbox':[40,70,150,85],'size':12}])
    monkeypatch.setattr(pdf_parser,'ocr_available',lambda:True)
    monkeypatch.setattr(pdf_parser,'ocr_recognize',lambda image:[{'text':'Supplier name:','bbox':[40,70,150,85],'confidence':.99}])
    result=pdf_parser.extract_pdf(path,'test-sha','test',ocr_enabled=True).model_dump()
    assert result['metadata']['native_text_pages']==[]
    assert result['metadata']['unusable_native_text_pages']==[1]
    assert result['metadata']['ocr_pages']==[1]
    assert any(b['text']=='Supplier name:' for b in result['source_blocks'])
    assert not any('\x07' in b['text'] for b in result['source_blocks'])
    disabled=pdf_parser.extract_pdf(path,'test-sha','test',ocr_enabled=False).model_dump()
    assert disabled['extraction_status']=='failed'
    assert disabled['metadata']['unusable_native_text_pages']==[1]
    assert any(w['code']=='unusable_native_text_encoding' for w in disabled['warnings'])
