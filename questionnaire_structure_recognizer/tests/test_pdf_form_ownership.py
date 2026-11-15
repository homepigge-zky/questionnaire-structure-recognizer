from questionnaire_structure_recognizer.form_positions import infer_responsibility


def test_ocr_missing_spaces_do_not_erase_explicit_supplier_ownership():
    blocks=[{'block_id':'title','kind':'ocr_text','text':'VENDORQUESTIONNAIREFORSTARTINGMATERIALS','location':{'type':'pdf','page':1}},
        {'block_id':'q','kind':'ocr_text','text':'Manufacturing site address','location':{'type':'pdf','page':1}}]
    field={'question_text_original':blocks[1]['text'],'label_refs':[{'block_id':'q','start':0,'end':len(blocks[1]['text'])}]}
    assert infer_responsibility(field,{b['block_id']:b for b in blocks})[0]=='our_side'
    blocks.append({'block_id':'review','kind':'ocr_text','text':'Assessment by DQA/PQA:','location':{'type':'pdf','page':9}})
    # Review headings govern their own area, never a cover field eight pages earlier.
    blocks[1]['location']['page']=9
    field['section_refs']=[{'block_id':'review','start':0,'end':len(blocks[-1]['text'])}]
    assert infer_responsibility(field,{b['block_id']:b for b in blocks})[0]=='reviewer'


def test_generic_pdf_has_no_automatic_supplier_assumption():
    blocks={'q':{'block_id':'q','text':'Company address','kind':'ocr_text','location':{'type':'pdf','page':1}}}
    field={'question_text_original':'Company address','label_refs':[{'block_id':'q','start':0,'end':15}]}
    assert infer_responsibility(field,blocks)[0]=='unknown'


def test_explicit_vendor_qualification_title_has_source_owner_and_local_review_override():
    from questionnaire_structure_recognizer.source_ownership import document_owner_evidence
    blocks = {'title': {'block_id': 'title', 'order': 0, 'kind': 'ocr_text',
                       'text': 'VENDOR QUALIFICATION QUESTIONNAIRE FOR RAW MATERIAL / DILUENTS',
                       'location': {'type': 'pdf', 'page': 1}},
              'q': {'block_id': 'q', 'order': 6, 'kind': 'ocr_text', 'text': 'Commercial status',
                    'location': {'type': 'pdf', 'page': 1}}}
    field = {'question_text_original': 'Commercial status', 'label_refs': [{'block_id': 'q', 'start': 0, 'end': 17}]}
    assert document_owner_evidence(field, blocks) == [{'block_id': 'title', 'start': 0, 'end': len(blocks['title']['text'])}]
    assert infer_responsibility(field, blocks)[0] == 'our_side'
    blocks['review'] = {'block_id': 'review', 'order': 5, 'kind': 'ocr_text', 'text': 'To be filled by customer',
                        'location': {'type': 'pdf', 'page': 1}}
    field['section_refs'] = [{'block_id': 'review', 'start': 0, 'end': len(blocks['review']['text'])}]
    assert infer_responsibility(field, blocks)[0] == 'customer'
