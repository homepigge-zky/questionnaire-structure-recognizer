from questionnaire_structure_recognizer.validation.native_pdf_blank_tails import restore_native_pdf_blank_tails
from questionnaire_structure_recognizer.form_positions import build_form_definition


def test_repeated_ruled_rows_keep_blank_same_cell_tail_without_inventing_value():
    labels=['SECTION I:','Supplier / Contractor Name','Address','Telephone','Fax Number']
    blocks=[]
    for n,text in enumerate(labels):
        y=100+n*30
        blocks.append({'block_id':str(n),'kind':'table_cell','text':text,'order':n,'controls':[],
            'location':{'type':'pdf','page':1,'extraction':'native','table':1,'bbox':[40,y,540,y+30]},
            'structure':{'table_id':'p1-t1','native_text_spans':[{'start':0,'end':len(text),'bbox':[45,y+3,45+len(text)*5,y+13]}]}})
    lookup={b['block_id']:b for b in blocks};fields=[]
    restore_native_pdf_blank_tails(fields,blocks,lookup)
    assert [f['question_text_original'] for f in fields]==labels[1:]
    assert all(f['answer_status']=='blank' and f['answer_value_refs']==[] for f in fields)
    for f in fields:
        target=build_form_definition(f,lookup,'fictional','test')['fill_targets'][0]
        assert target['write_method']=='standard_pdf_response' and target['writable']
        assert target['output_modes']==['standard']


def test_isolated_short_text_in_wide_box_does_not_become_a_form():
    block={'block_id':'isolated','kind':'table_cell','text':'Example','order':1,'controls':[],
        'location':{'type':'pdf','page':1,'extraction':'native','table':1,'bbox':[40,100,540,130]},
        'structure':{'table_id':'t','native_text_spans':[{'start':0,'end':7,'bbox':[45,103,90,113]}]}}
    fields=[];restore_native_pdf_blank_tails(fields,[block],{'isolated':block});assert fields==[]
