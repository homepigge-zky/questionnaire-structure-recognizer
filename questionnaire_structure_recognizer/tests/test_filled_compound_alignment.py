from questionnaire_structure_recognizer.parser_core.filled_word_layout import associate_lanes


def test_alignment_uses_rendered_positions_not_nonempty_array_indices():
    labels = [dict(start=0,end=5,paragraph_id='l1'),dict(start=6,end=12,paragraph_id='l2'),dict(start=13,end=18,paragraph_id='l3')]
    paragraphs = [dict(paragraph_id='a1',start=0,end=9,text='TEST_NAME'),
                  dict(paragraph_id='a2',start=9,end=9,text=''),
                  dict(paragraph_id='a3',start=10,end=20,text='TEST_PHONE')]
    positions = {'l1':(0,40,100),'l2':(0,40,150),'l3':(0,40,200),
                 'a1':(0,200,100),'a2':(0,200,150),'a3':(0,200,200)}
    result = associate_lanes(labels,paragraphs,positions)
    assert [r['answer_paragraphs'] for r in result] == [['a1'],['a2'],['a3']]


def test_alignment_retains_multiline_value_and_rejects_ambiguous_empty_region():
    labels=[dict(start=0,end=5,paragraph_id='l1'),dict(start=6,end=12,paragraph_id='l2')]
    paragraphs=[dict(paragraph_id='a1',text='Address line 1'),dict(paragraph_id='a2',text='Address line 2'),dict(paragraph_id='a3',text=''),dict(paragraph_id='a4',text='')]
    positions={'l1':(0,40,100),'l2':(0,40,150),'a1':(0,200,100),'a2':(0,200,120),'a3':(0,200,149),'a4':(0,200,151)}
    result=associate_lanes(labels,paragraphs,positions)
    assert result[0]['answer_paragraphs']==['a1','a2']
    assert len(result)==1


def test_last_label_before_page_break_keeps_own_value():
    labels=[dict(start=0,end=5,paragraph_id='l1'),dict(start=6,end=12,paragraph_id='l2')]
    paragraphs=[dict(paragraph_id='a1',text='TEST_QC'),dict(paragraph_id='a2',text='TEST_QA')]
    positions={'l1':(0,40,710),'l2':(1,40,150),'a1':(0,200,716),'a2':(1,200,150)}
    assert [x['answer_paragraphs'] for x in associate_lanes(labels,paragraphs,positions)]==[['a1'],['a2']]
