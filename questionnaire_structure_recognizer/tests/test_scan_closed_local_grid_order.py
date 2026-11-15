"""Local recovery must not reorder unrelated record rows or untouched fields."""
from copy import deepcopy

from questionnaire_structure_recognizer.validation import scan_closed_local_grids as grids


def block(block_id, order, text='', x=0):
    return {'block_id': block_id, 'text': text, 'order': order,
            'location': {'type': 'pdf', 'page': 1, 'extraction': 'ocr',
                         'bbox': [x, 10, x+30, 30]}, 'structure': {}}


def field(field_id, labels, regions=(), **kwargs):
    return {'field_id': field_id,
            'label_refs': [grids.ref(b) for b in labels],
            'question_refs': [grids.ref(b) for b in labels],
            'answer_region_refs': [grids.ref(b) for b in regions],
            'answer_value_refs': [], 'answer_refs': [],
            'answer_status': 'blank', 'answer_text_original': '', **kwargs}


def test_no_recovery_preserves_row_major_records_exactly():
    columns = [block('column-'+str(i), i, 'column '+str(i)) for i in range(3)]
    fields = [field('row-%s-column-%s' % (r, c), [columns[c]], record_index=r)
              for r in range(1, 4) for c in range(3)]
    before = deepcopy(fields)
    original_objects = list(fields)
    report = grids.reconcile_scanned_local_grids(fields, columns, {b['block_id']: b for b in columns})
    assert report['restored'] == []
    assert fields == before
    assert all(a is b for a, b in zip(fields, original_objects))
    assert [f['record_index'] for f in fields] == [1,1,1,2,2,2,3,3,3]


def local_fixture():
    # Source order is deliberately not current record order. Recovery is not
    # authorized to turn an existing row-major result into column-major data.
    prefix = block('prefix', 70, 'first unrelated record')
    label_cell = block('label-cell', 20, x=0)
    label = block('label', 21, 'local prompt', x=4)
    label['location']['bbox'] = [4, 14, 26, 26]
    response = block('response', 22, x=30)
    middle = block('middle', 5, 'next unrelated record')
    suffix = block('suffix', 1, 'last unrelated record')
    residual = block('residual', 23, 'another independent instruction')
    blocks = [prefix, label_cell, label, response, middle, suffix, residual]
    new = field('restored-local', [label], [response])
    return blocks, new


def install(blocks, fields, new):
    lookup = {b['block_id']: b for b in blocks}
    report = {'restored': [], 'protected': []}
    assert grids.install(fields, [new], [lookup['label-cell']], [lookup['response']],
                         [lookup['label']], lookup, {'kind': 'test-physical-local-cell'}, report)
    return report


def test_local_recovery_replaces_at_first_affected_root_without_global_sort():
    blocks, new = local_fixture(); by_id = {b['block_id']: b for b in blocks}
    fields = [field('prefix', [by_id['prefix']]), field('old-local', [by_id['label']]),
              field('middle', [by_id['middle']]), field('suffix', [by_id['suffix']])]
    unrelated = [fields[0], fields[2], fields[3]]
    install(blocks, fields, new)
    assert [f['field_id'] for f in fields] == ['prefix', 'restored-local', 'middle', 'suffix']
    assert [f for f in fields if f is not new] == unrelated
    assert all(fields[i] is old for i, old in zip([0, 2, 3], unrelated))


def test_recovery_inserts_before_retained_residual_at_same_boundary():
    blocks, new = local_fixture(); by_id = {b['block_id']: b for b in blocks}
    fields = [field('prefix', [by_id['prefix']]),
              field('mixed', [by_id['label'], by_id['residual']]),
              field('suffix', [by_id['suffix']])]
    install(blocks, fields, new)
    assert [f['field_id'] for f in fields] == ['prefix', 'restored-local', 'mixed', 'suffix']
    assert fields[2]['label_refs'] == [grids.ref(by_id['residual'])]
    assert fields[2]['answer_status'] == 'unresolved'


def test_nested_owned_child_does_not_move_its_unrelated_siblings_or_roots():
    blocks, new = local_fixture(); by_id = {b['block_id']: b for b in blocks}
    retained = field('retained-child', [by_id['middle']])
    parent = field('parent', [by_id['residual']], children=[
        retained, field('old-local', [by_id['label']])])
    fields = [field('prefix', [by_id['prefix']]), parent,
              field('suffix', [by_id['suffix']])]
    install(blocks, fields, new)
    assert [f['field_id'] for f in fields] == ['prefix', 'restored-local', 'parent', 'suffix']
    assert parent['children'] == [retained]
    assert parent['children'][0] is retained


def test_discovered_local_group_uses_source_order_only_to_choose_insertion_gap():
    blocks, new = local_fixture(); by_id = {b['block_id']: b for b in blocks}
    fields = [field('suffix', [by_id['suffix']]), field('prefix', [by_id['prefix']]),
              field('middle', [by_id['middle']])]
    before = list(fields)
    install(blocks, fields, new)
    assert [f['field_id'] for f in fields] == ['suffix', 'restored-local', 'prefix', 'middle']
    assert [f for f in fields if f is not new] == before


def test_manual_nested_field_blocks_recovery_without_reordering_anything():
    blocks, new = local_fixture(); by_id = {b['block_id']: b for b in blocks}
    fields = [field('prefix', [by_id['prefix']]), field('manual', [by_id['residual']],
        manual_override=True, children=[field('old-local', [by_id['label']])]),
        field('suffix', [by_id['suffix']])]
    before = deepcopy(fields); report = {'restored': [], 'protected': []}
    assert not grids.install(fields, [new], [by_id['label-cell']], [by_id['response']],
        [by_id['label']], by_id, {'kind': 'test-physical-local-cell'}, report)
    assert fields == before
    assert len(report['protected']) == 1


def test_two_consecutive_local_replacements_keep_other_record_rows_in_order():
    blocks, new = local_fixture(); by_id = {b['block_id']: b for b in blocks}
    blocks_b = deepcopy(blocks)
    for b in blocks_b:
        b['block_id'] += '-b'
        b['order'] += 100
        b['location']['bbox'] = [v+100 if i % 2 == 0 else v for i, v in enumerate(b['location']['bbox'])]
    lookup = {b['block_id']: b for b in blocks+blocks_b}
    new_b = field('restored-b', [lookup['label-b']], [lookup['response-b']])
    records = [field('record-'+str(r)+'-'+str(c), [by_id['middle'] if c == 1 else by_id['suffix']], record_index=r)
               for r in range(1, 3) for c in range(1, 3)]
    fields = [records[0], field('old-a', [by_id['label']]), *records[1:3],
              field('old-b', [lookup['label-b']]), records[3]]
    install(blocks, fields, new)
    report = {'restored': [], 'protected': []}
    assert grids.install(fields, [new_b], [lookup['label-cell-b']], [lookup['response-b']],
        [lookup['label-b']], lookup, {'kind': 'second-local-cell'}, report)
    expected = ['record-1-1', 'restored-local', 'record-1-2', 'record-2-1', 'restored-b', 'record-2-2']
    assert [f['field_id'] for f in fields] == expected
    assert [f['record_index'] for f in fields if 'record_index' in f] == [1,1,2,2]
    before = deepcopy(fields)
    install(blocks, fields, deepcopy(new))
    assert fields == before, 'Re-entering the same proven local recovery must preserve field data and order.'
