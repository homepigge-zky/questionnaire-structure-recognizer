from questionnaire_structure_recognizer.source_notes import group_source_notes


def test_pdf_side_note_stays_one_group_after_its_own_question():
    location=lambda box:{'type':'pdf','page':1,'bbox':box}
    blocks={'label1':{'location':location([30,100,140,108])},'label2':{'location':location([30,126,140,134])}}
    questions=[{'id':'q1','field_id':'f1','source_order':11,'source_refs':[{'block_id':'label1'}]},
        {'id':'q2','field_id':'f2','source_order':16,'source_refs':[{'block_id':'label2'}]}]
    notes=[{'block_id':'n1','order':10,'role':'instruction','text':'(Fictional explanatory note', 'location':location([180,99,300,107])},
        {'block_id':'n2','order':13,'role':'instruction','text':'continues here.)','location':location([182,107,290,115])},
        {'block_id':'n3','order':15,'role':'instruction','text':'(Separate second note.)','location':location([180,126,300,134])}]
    result=group_source_notes(notes,questions,blocks)
    assert len(result)==2
    assert result[0]['text']=='(Fictional explanatory note\ncontinues here.)'
    assert result[0]['block_ids']==['n1','n2'] and result[0]['anchor_field_id']=='f1'
    assert 11<result[0]['order']<16<result[1]['order']
    assert result[1]['anchor_field_id']=='f2'
    assert len(notes)==3 and notes[0]['text']=='(Fictional explanatory note'
    notes[0]['anchor_field_id']='f1'
    mixed=group_source_notes(notes,questions,blocks)
    assert mixed[0]['block_ids']==['n1','n2'] and mixed[0]['anchor_field_id']=='f1'


def test_competing_rows_and_other_page_notes_are_not_guessed():
    note={'block_id':'n','order':1,'role':'instruction','text':'A note','location':{'type':'pdf','page':1,'bbox':[180,99,300,110]}}
    blocks={b:{'location':{'type':'pdf','page':1,'bbox':[30,100,140,108]}} for b in ['a','b']}
    questions=[{'id':b,'field_id':b,'source_order':2,'source_refs':[{'block_id':b}]} for b in blocks]
    assert 'anchor_field_id' not in group_source_notes([note],questions,blocks)[0]


def test_ocr_same_line_fragments_use_horizontal_order_and_keep_source_section():
    def note(bid,order,text,box):
        return {'block_id':bid,'order':order,'text':text,'role':'instruction',
                'location':{'type':'pdf','page':2,'bbox':box}}
    notes=[note('first',5,'Have each manufacturer complete',[75,225,500,237]),
           note('right',6,'for this product.',[234,237,560,250]),
           note('left',7,'a separate form',[75,238,240,250])]
    blocks={'section':{'order':4,'location':{'type':'pdf','page':2}}}
    questions=[{'id':'q','field_id':'f','source_order':10,'source_refs':[],
                'section':'Manufacturer information','form_definition':{'section_refs':[{'block_id':'section'}]}}]
    result=group_source_notes(notes,questions,blocks)
    assert len(result)==1
    assert result[0]['text']=='Have each manufacturer complete\na separate form for this product.'
    assert result[0]['block_ids']==['first','left','right']
    assert result[0]['section']=='Manufacturer information'
    assert notes[1]['text']=='for this product.'


def test_declaration_remains_readonly_under_its_source_section():
    notes=[{'block_id':'declaration','order':4,'role':'signed_declaration_prose',
            'text':'We confirm the following fictional requirements.',
            'location':{'type':'docx','paragraph':4}}]
    blocks={'section':{'order':3,'location':{'type':'docx','paragraph':3}}}
    questions=[{'id':'sign','field_id':'signature','source_order':5,'source_refs':[],
                'section':'Part C: Declaration','form_definition':{'section_refs':[{'block_id':'section'}]}}]
    result=group_source_notes(notes,questions,blocks)
    assert result[0]['section']=='Part C: Declaration'
    assert result[0]['role']=='signed_declaration_prose'
    assert len(questions)==1 and 'answer_text' not in result[0]


def test_conflicting_source_note_owners_are_not_reassigned_by_geometry():
    note={'block_id':'n','order':1,'text':'Source note','role':'instruction',
          'anchor_validation':'multiple_source_associations','related_field_ids':['a','b'],
          'location':{'type':'pdf','page':1,'bbox':[180,99,300,110]}}
    blocks={'a':{'order':2,'location':{'type':'pdf','page':1,'bbox':[30,100,140,108]}}}
    question={'id':'a','field_id':'a','source_order':2,'source_refs':[{'block_id':'a'}]}
    result=group_source_notes([note],[question],blocks)
    assert 'anchor_field_id' not in result[0]
    assert result[0]['related_field_ids']==['a','b'] and result[0]['text']=='Source note'
