import json
import sqlite3
from copy import deepcopy

from questionnaire_structure_recognizer.form_part_routes import (
    source_part_routes, route_status, clear_inactive_derived_answers, incompatible_historical_route, dependency_order,
)


def block(i,text,page,y):
    return {'block_id':str(i),'order':i,'text':text,'location':{'type':'pdf','page':page,'bbox':[40,y,300,y+9]}}


def source():
    blocks = [block(1,'Is your company the manufacturer of the product?',1,100),
              block(2,'Yes',1,112),block(3,'No',1,112),
              block(4,'If Yes: Please complete Part A.',1,130),
              block(5,'If No: Please complete Part B, and have Part A completed by the original manufacturer.',1,147),
              block(6,'Part A',2,70),block(7,'A-1 Product',2,110),block(8,'Name of the product:',2,122),
              block(9,'A-2 Manufacturer (if not identical with supplier)',2,200),block(10,'E-Mail:',2,220),
              block(11,'A-3 Site size',2,270),block(12,'Area:',2,292),
              block(13,'Part B',3,70),block(14,'Name of the product:',3,120)]
    fs=[{'field_id':str(i),'question_text_original':blocks[i-1]['text'],
         'label_refs':[{'block_id':str(i),'start':0,'end':len(blocks[i-1]['text'])}],
         'form_definition':{'validation_status':'valid'}} for i in [1,8,10,12,14]]
    fs[0]['options']=[{'text_original':k,'refs':[{'block_id':str(i)}]} for i,k in [(2,'Yes'),(3,'No')]]
    return blocks,fs


def test_printed_branch_and_local_exception_end_at_the_next_numbered_subsection():
    blocks,fs=source();routes=source_part_routes(blocks,fs)
    assert routes['8']['accepted_parent_answers']==['yes']
    assert routes['10']['accepted_parent_answers']==[] and routes['10']['local_condition_refs']==['9']
    assert routes['12']['accepted_parent_answers']==['yes']
    assert routes['14']['accepted_parent_answers']==['no']
    assert incompatible_historical_route(routes['14'],routes['8'])
    assert not incompatible_historical_route(routes['8'],deepcopy(routes['8']))
    assert incompatible_historical_route({},routes['8'])


def test_intervening_question_uncertain_parent_and_multiple_parts_are_held():
    blocks,fs=source(); extra=block(15,'A separate approval is required.',1,124)
    assert all(r['unresolved'] for r in source_part_routes(blocks+[extra],fs).values())
    fs[0]['form_definition']['validation_status']='needs_review'
    assert all(r['unresolved'] for r in source_part_routes(blocks,fs).values())
    blocks,fs=source();blocks[4]['text']='If No: complete Part B and complete Part A.'
    assert all(r['unresolved'] for r in source_part_routes(blocks,fs).values())
    assert not source_part_routes([b for b in blocks if b['block_id'] not in {'4','5'}],fs)


def test_controller_is_processed_before_its_fields_even_with_reversed_model_output():
    rows=[{'field_id':'part-a','form_definition_json':json.dumps({'source_part_route':{'parent_field_id':'controller'}})},
          {'field_id':'misc','form_definition_json':'{}'}, {'field_id':'controller','form_definition_json':'{}'}]
    assert [r['field_id'] for r in dependency_order(rows)] == ['controller','part-a','misc']
    assert [r['field_id'] for r in rows] == ['part-a','misc','controller']


def test_legacy_source_routes_recovered_only_from_exact_version_file_hash_and_character_refs(tmp_path):
    from questionnaire_structure_recognizer.form_part_routes import historical_route
    blocks,fields=source()
    old=tmp_path/'immutable.json'
    old.write_text(json.dumps({'source_hash':'fictional-fingerprint','source_blocks':blocks,'questions':fields}),encoding='utf8')
    with sqlite3.connect(tmp_path/'business.db') as db:
        db.row_factory=sqlite3.Row
        db.executescript('CREATE TABLE versions(task_id,version,result_file);CREATE TABLE tasks(id,source_hash);')
        db.execute('INSERT INTO versions VALUES(?,?,?)',('history',1,old.name))
        db.execute('INSERT INTO tasks VALUES(?,?)',('history','fictional-fingerprint'))
        src={'task_id':'history','task_version':1,'field_id':'8','source_hash':'fictional-fingerprint',
             'question_refs':fields[1]['label_refs']}
        recovered=historical_route(db,src,'', '')
        assert recovered['part']=='A' and not recovered['unresolved']
        assert incompatible_historical_route(source_part_routes(blocks,fields)['14'],recovered)
        assert historical_route(db,{**src,'source_hash':'different'})['unresolved']
        assert historical_route(db,{**src,'question_refs':[{'block_id':'8','start':0,'end':1}]})['unresolved']
        assert historical_route(db,{**src,'field_id':'old-missing-field'})['unresolved']
        assert historical_route(db,{**src,'task_version':99},'Part B: other entity')['unresolved']
        assert historical_route(db,{},'General supplier contacts') is None
        assert historical_route(db,{**src,'field_id':'1','question_refs':fields[0]['label_refs']}) is None
        assert historical_route(db,{'source_part_route':recovered})==recovered


def test_orphan_explicit_followup_needs_a_verified_parent_but_main_question_remains_available():
    from questionnaire_structure_recognizer.workflow import resolved_applicability
    with sqlite3.connect(':memory:') as db:
        assert resolved_applicability(db,{'question_text':'If Yes: Please describe the process:'},{})=='unresolved'
        assert resolved_applicability(db,{'question_text':'若否，请说明理由'},{})=='unresolved'
        assert resolved_applicability(db,{'question_text':'Does this apply? If yes, give the details.'},{})=='applicable'
        assert resolved_applicability(db,{'question_text':'If No: Give the reason'},{'applicability_condition':{'equals_option':'Yes'}})=='unresolved'


def test_saved_parent_choice_controls_active_part_and_export_keeps_manual_answers():
    blocks,fs=source();routes=source_part_routes(blocks,fs)
    with sqlite3.connect(':memory:') as db:
        db.row_factory=sqlite3.Row
        db.executescript('''CREATE TABLE questions(id,task_id,field_id,question_text,form_definition_json);
            CREATE TABLE answers(question_id,answer_text,selected_options_json,origin,manual_override,match_id,confirmed,updated_at);
            CREATE TABLE answer_options(question_id,text,value,option_order);
            CREATE TABLE matches(question_id,task_id,status,coverage_complete,reason);''')
        for field in fs:
            i=field['field_id'];definition={'source_part_route':routes[i]} if i in routes else {}
            db.execute('INSERT INTO questions VALUES(?,?,?,?,?)',(i,'task',i,field['question_text_original'],json.dumps(definition)))
            db.execute('INSERT INTO answers VALUES(?,?,?,?,?,?,?,?)',(i,'KEPT' if i=='12' else 'HISTORY','[]','manual' if i=='12' else 'reviewed_knowledge',int(i=='12'),'old',1,'t'))
        db.executemany('INSERT INTO answer_options VALUES(?,?,?,?)',[('1','Yes','yes-id',1),('1','No','no-id',2)])
        q={'task_id':'task'}
        assert route_status(db,q,routes['14'])=='unresolved'
        db.execute('UPDATE answers SET selected_options_json=? WHERE question_id=?',(json.dumps(['yes-id']),'1'))
        assert route_status(db,q,routes['8'])=='applicable' and route_status(db,q,routes['14'])=='not_applicable'
        clear_inactive_derived_answers(db,'task','now')
        assert db.execute("SELECT answer_text FROM answers WHERE question_id='14'").fetchone()[0]==''
        db.execute('UPDATE answers SET selected_options_json=? WHERE question_id=?',(json.dumps(['no-id']),'1'))
        assert route_status(db,q,routes['14'])=='applicable'
        clear_inactive_derived_answers(db,'task','now')
        assert db.execute("SELECT answer_text FROM answers WHERE question_id='8'").fetchone()[0]==''
        assert db.execute("SELECT answer_text FROM answers WHERE question_id='12'").fetchone()[0]=='KEPT'
        db.execute("UPDATE questions SET question_text='Another prompt' WHERE id='1'")
        assert route_status(db,q,routes['14'])=='unresolved'
