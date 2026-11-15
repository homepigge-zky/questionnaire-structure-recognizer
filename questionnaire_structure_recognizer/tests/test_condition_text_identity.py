"""Ordinary (non-numbered) source conditions must not survive semantic edits."""
import json
import sqlite3
import pytest
from questionnaire_structure_recognizer.workflow import resolved_applicability
from questionnaire_structure_recognizer.form_part_routes import clear_inactive_derived_answers


@pytest.mark.parametrize('changed', ['parent', 'child'])
@pytest.mark.parametrize('origin', ['reviewed_knowledge', 'manual', 'source_document'])
def test_changed_ordinary_condition_is_unresolved_and_only_derived_answer_is_cleared(changed, origin):
    with sqlite3.connect(':memory:')as db:
        db.row_factory=sqlite3.Row
        db.executescript('CREATE TABLE questions(id,task_id,field_id,parent_id,question_text,original_text,section,form_definition_json);'
                         'CREATE TABLE answers(question_id,answer_text,selected_options_json,origin,manual_override,match_id,confirmed,updated_at);'
                         'CREATE TABLE answer_options(question_id,text,value,option_order);'
                         'CREATE TABLE matches(question_id,task_id,status,coverage_complete,reason);')
        parent_text='Do you operate a dedicated fictional facility?'
        child_text='Which fictional products are manufactured there?'
        definition={'applicability_condition':{'parent_field_id':'source-parent','equals_option':'No'}}
        db.execute('INSERT INTO questions VALUES(?,?,?,?,?,?,?,?)',('parent','task','source-parent',None,parent_text,parent_text,'','{}'))
        db.execute('INSERT INTO questions VALUES(?,?,?,?,?,?,?,?)',('child','task','source-child','parent',child_text,child_text,'',json.dumps(definition)))
        db.execute('INSERT INTO answers VALUES(?,?,?,?,?,?,?,?)',('parent','No','["No"]','manual',1,None,1,'t'))
        db.execute('INSERT INTO answers VALUES(?,?,?,?,?,?,?,?)',('child','TEST_EXISTING_VALUE','[]',origin,int(origin=='manual'),'match',1,'t'))
        db.executemany('INSERT INTO answer_options VALUES(?,?,?,?)',[('parent','Yes','Yes',1),('parent','No','No',2)])
        question=dict(db.execute('SELECT * FROM questions WHERE id="child"').fetchone())
        assert resolved_applicability(db,question,definition)=='applicable'
        db.execute('UPDATE questions SET question_text=? WHERE id=?',('Changed business meaning requiring review',changed))
        question=dict(db.execute('SELECT * FROM questions WHERE id="child"').fetchone())
        assert resolved_applicability(db,question,definition)=='unresolved'
        clear_inactive_derived_answers(db,'task','now',resolved_applicability)
        current=db.execute('SELECT * FROM answers WHERE question_id="child"').fetchone()
        assert current['answer_text']==(''if origin=='reviewed_knowledge'else'TEST_EXISTING_VALUE')
        # Reverting the exact source wording restores a valid source relation;
        # it does not silently restore the cleared old answer.
        db.execute('UPDATE questions SET question_text=original_text WHERE id=?',(changed,))
        question=dict(db.execute('SELECT * FROM questions WHERE id="child"').fetchone())
        assert resolved_applicability(db,question,definition)=='applicable'
