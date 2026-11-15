"""Bind a nested control table to its actual containing response cell."""
from copy import deepcopy
from .field_boundaries import ref, _s, _row, _col, _end, NUMBER
from .form_completion import new_field
from .native_choices import PLAIN_CHOICES, _control_options

VERSION='nested-response-controls-v1-physical-container'


def _walk(fields):
    for f in fields:
        yield f
        yield from _walk(f.get('children',[]))


def _protected(f):
    return f.get('manual_override') or f.get('answer_origin') in {'manual','knowledge'}


def _inside(child, container):
    a,b=child.get('location',{}),container.get('location',{})
    return a.get('part')==b.get('part') and str(a.get('table','')).startswith(f"{b.get('table')}.{b.get('row')}.{b.get('cell')}.")


def valid_nested_response(label, target, lookup):
    container=lookup.get(_s(target).get('nested_response_container_id'),{})
    return bool(container and _inside(target,container) and
                _s(container).get('table_id')==_s(label).get('table_id') and
                _row(container)==_row(label) and _end(label)+1==_col(container) and
                _s(target).get('nested_response_label_id')==label.get('block_id') and
                PLAIN_CHOICES.fullmatch(target.get('text','')) and target.get('controls'))


def restore_nested_response_controls(fields, blocks, lookup):
    cells=[b for b in blocks if b.get('kind')=='table_cell' and b.get('location',{}).get('type')=='docx'
           and not _s(b).get('is_merge_continuation')]
    for container in cells:
        if container.get('text','').strip() or container.get('controls'):continue
        descendants=[b for b in cells if _inside(b,container)]
        choices=[b for b in descendants if PLAIN_CHOICES.fullmatch(b.get('text','')) and b.get('controls')
                 and all(c.get('type')=='checkbox' for c in b['controls'])]
        if not choices:continue
        labels=[b for b in cells if _s(b).get('table_id')==_s(container).get('table_id') and
                _row(b)==_row(container) and _end(b)+1==_col(container) and b.get('text','').strip()
                and not b.get('controls') and not NUMBER.fullmatch(b['text'].strip())
                and not PLAIN_CHOICES.fullmatch(b['text'])]
        if len(labels)!=1:continue
        label=labels[0];affected={label['block_id'],container['block_id'],*(b['block_id'] for b in descendants)}
        touching=[f for f in _walk(fields) if any(r['block_id'] in affected for r in f.get('label_refs',[]))]
        if any(_protected(f) for f in touching):continue
        parent=next((f for f in touching if f.get('label_refs')==[ref(label)]),None)
        if parent is None:
            parent=new_field(label,ref(label),[]);fields.append(parent)
        if len(choices)!=1 or any(b.get('text','').strip() and b not in choices for b in descendants):
            parent.update(answer_region_refs=[],answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='unresolved',options=[],
                          review_reasons=['嵌套答复表包含多组控件，尚无各组对应标签的依据'])
            continue
        area=choices[0];options,issue=_control_options(area,0,len(area['text']),area['controls'])
        if issue or len(options)!=len(area['controls']):continue
        _s(area).update(nested_response_container_id=container['block_id'],nested_response_label_id=label['block_id'],
                        semantic_role_hint='option',protected_label_ranges=[])
        selected=[o for o in options if o['selected'] is True]
        parent.update(options=options,answer_region_refs=[deepcopy(r) for o in options for r in o['answer_region_refs']],
                      answer_value_refs=[deepcopy(r) for o in selected for r in o['refs']],
                      answer_refs=[deepcopy(r) for o in selected for r in o['refs']],
                      answer_text_original='\n'.join(o['text_original'] for o in selected),
                      answer_status='unresolved' if any(o['selected'] is None for o in options) else 'filled' if selected else 'blank',
                      boundary_evidence='native_nested_response_controls',boundary_version=VERSION,review_reasons=[])
        def keep(items):
            result=[]
            for f in items:
                if f is not parent and f.get('label_refs') and all(r['block_id']==area['block_id'] for r in f['label_refs']):continue
                f['children']=keep(f.get('children',[]));result.append(f)
            return result
        fields[:]=keep(fields)
