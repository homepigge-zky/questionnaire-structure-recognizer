"""Source-local personnel scopes, with no company- or document-name rules."""
from questionnaire_structure_recognizer.form_positions import build_form_definition
from questionnaire_structure_recognizer.semantic_fields import classify_field


def cell(key, text, row, column=1):
    return {'block_id': key, 'text': text, 'kind': 'table_cell', 'order': row * 3 + column,
            'location': {'type': 'docx', 'part': 'word/document.xml', 'table': 1, 'row': row, 'cell': column},
            'structure': {'table_id': 't1', 'row_start': row, 'row_end': row,
                          'grid_col_start': column, 'grid_col_end': column, 'cell_id': key,
                          'blank_state': 'blank' if not text else 'filled'}}


def definition(blocks, key, start=0, end=None):
    source = next(b for b in blocks if b['block_id'] == key)
    end = len(source['text']) if end is None else end
    field = {'field_id': key, 'question_text_original': source['text'][start:end],
             'label_refs': [{'block_id': key, 'start': start, 'end': end}],
             'answer_region_refs': [], 'pairing_validation': 'valid', 'answer_status': 'blank'}
    return build_form_definition(field, {b['block_id']: b for b in blocks}, 'fictional', 'test')


def test_contact_metadata_inherits_source_role_but_not_following_business_question():
    blocks = [cell('qa', 'Primary Quality Assurance Contact', 8),
              cell('name', 'Name: __________________', 9),
              cell('job', 'Job Title: ______________', 10),
              cell('phone', 'Phone No.: _____________', 11),
              cell('fax', 'Facsimile No.: __________', 12),
              cell('next', 'Do you validate the process?', 13),
              cell('other', 'Name:', 14)]
    for key, end in [('name', 5), ('job', 10), ('phone', 10), ('fax', 14)]:
        result = definition(blocks, key, end=end)
        assert result['semantic_context'] == 'Primary Quality Assurance Contact'
        assert not result['requires_current_person_confirmation']
        assert not result['requires_signatory_confirmation']
        semantic = classify_field(next(b['text'][:end] for b in blocks if b['block_id'] == key), context=result['semantic_context'])
        if key != 'fax':
            assert semantic['qualifiers'].get('person_role', semantic['qualifiers'].get('contact_role')) == 'quality_contact'
    assert not definition(blocks, 'other')['semantic_context']


def test_audited_by_separate_rows_are_current_people_not_quality_contacts():
    blocks = [cell('audited', 'Audited by', 28), cell('blank', '', 29),
              cell('name', 'Name:', 30), cell('blank2', '', 31),
              cell('sign', 'Signature:', 32), cell('blank3', '', 33), cell('date', 'Date:', 34)]
    for key in ['name', 'date']:
        result = definition(blocks, key)
        assert result['requires_current_person_confirmation'] or result['requires_signatory_confirmation']
        assert not result['auto_fill_eligible']


def test_same_cell_auditor_and_approval_title_ranges_keep_current_context():
    text = 'Auditor (s): ______\nName:\nTitle:\n*Approved: ______\nName:\nTitle:'
    source = cell('signing', text, 6)
    spans = []; offset = 0
    for index, line in enumerate(text.splitlines(), 1):
        spans.append({'start': offset, 'end': offset + len(line), 'text': line, 'paragraph_index': index})
        offset += len(line) + 1
    source['structure']['paragraph_spans'] = spans
    for span in spans:
        if span['text'] in ['Name:', 'Title:']:
            result = definition([source], 'signing', span['start'], span['end'])
            assert result['requires_current_person_confirmation']
            assert not result['auto_fill_eligible']
            assert '_' not in result['display_context']
            assert result['display_context'] in ['Auditor (s):', '*Approved:']


def test_unrelated_auditor_business_sentence_does_not_protect_contact_metadata():
    text = 'Describe how auditors review personnel training.\nName:\nTitle:'
    source = cell('business', text, 1)
    source['structure']['paragraph_spans'] = [{'start': 0, 'end': text.index('\n'), 'text': text.splitlines()[0], 'paragraph_index': 1}]
    for label in ['Name:', 'Title:']:
        start = text.index(label)
        assert not definition([source], 'business', start, start + len(label))['requires_current_person_confirmation']
