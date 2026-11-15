from questionnaire_structure_recognizer.parser_core.native_layout import unique_cell_sequence


def test_repeated_values_follow_unique_native_cell_order_not_other_columns():
    entries = [('title', 'Unique contact heading'), ('a', 'Salesperson'), ('b', 'Salesperson')]
    positions = {'title': [(0, 130, 100, 112)],
                 'a': [(0, 55, 120, 120), (0, 130, 124, 124), (0, 130, 148, 148)],
                 'b': [(0, 55, 120, 120), (0, 130, 124, 124), (0, 130, 148, 148)]}
    actual = unique_cell_sequence(entries, positions)
    assert actual['a'] == (0, 130, 124)
    assert actual['b'] == (0, 130, 148)


def test_ambiguous_repeated_occurrences_are_not_guessed():
    entries = [('a', 'Duplicate')]
    assert not unique_cell_sequence(entries, {'a': [(0, 20, 100, 100), (1, 20, 100, 100)]})


def test_native_order_can_continue_on_next_page():
    entries = [('a', 'First'), ('b', 'Second')]
    assert unique_cell_sequence(entries, {'a': [(0, 20, 700, 700)], 'b': [(1, 20, 100, 100)]})['b'] == (1, 20, 100)


def test_overlapping_paragraph_ranges_are_rejected():
    entries = [('a', 'Long paragraph'), ('b', 'Word inside it')]
    assert not unique_cell_sequence(entries, {'a': [(0, 20, 100, 120)], 'b': [(0, 20, 110, 110)]})


def test_numeric_paragraph_is_not_matched_inside_a_year():
    import fitz
    from questionnaire_structure_recognizer.parser_core.native_layout import _rendered_runs, _occurrences
    pdf=fitz.open();page=pdf.new_page();page.insert_text((30,50),'2017 17 170')
    occurrences=_occurrences('17',_rendered_runs(pdf))
    assert len(occurrences)==1


def test_closed_source_cell_excludes_later_pages_with_the_same_value():
    entries=[('heading','Unique title'),('value','N/A')]
    candidates={'heading':[(0,180,100,100)],'value':[(0,180,130,130),(1,180,130,130)]}
    assert unique_cell_sequence(entries,candidates,{0:[(175,90,400,150)]})['value']==(0,180,130)
