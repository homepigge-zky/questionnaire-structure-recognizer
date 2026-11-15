from questionnaire_structure_recognizer.form_positions import infer_field_type


def kind(label, unit=''):
    return infer_field_type({'question_text_original': label, 'unit_text_original': unit}, {})[0]


def test_process_area_and_update_questions_are_not_numbers_or_dates():
    assert kind('Are the process areas dedicated for one product? If not what are the other products being manufactured.') not in {'integer', 'decimal', 'date'}
    assert kind('Describe how you update procedures and count defective records.') == 'long_text'
    assert kind('What qualification is required for personnel in the storage area?') != 'decimal'
    assert kind('Cleaning & maintenance of dispensing equipment, dispensing area.') != 'decimal'
    assert kind('Specify the area') != 'decimal'
    assert kind('Records indicating material name, Lot No., quantity, manufacturer name, suppliers name, material code and date received.') != 'date'


def test_explicit_quantity_and_date_fields_keep_their_types():
    assert kind('Site area') == 'decimal'
    assert kind('Area', 'm²') == 'decimal'
    assert kind('Number of employees') == 'integer'
    assert kind('Certificate expiry date') == 'date'
    assert kind('场地面积') == 'decimal'
    assert kind('Sr. No.') == 'integer'
    assert kind('S. No.') == 'integer'
    assert kind('Equipment serial number') != 'integer'
    assert kind('Sample quantity') == 'decimal'
    assert kind('No. of Warehouses') == 'integer'
    assert kind('No. of employees') == 'integer'
    assert kind('Area in square feet') == 'decimal'
    assert kind('Telephone No.') == 'contact'
