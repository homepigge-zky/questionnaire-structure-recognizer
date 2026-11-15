"""A source transcription review resolves text doubt, never field geometry."""


def reviewed_values(field,lookup):
    from ..source_text_review import verified_reviewed_text
    refs=field.get('answer_value_refs',[])
    if not refs:return False
    for rr in refs:
        block=lookup.get(rr.get('block_id'),{});a,z=rr.get('start'),rr.get('end')
        if (type(a) is not int or type(z) is not int or not 0<=a<z<=len(block.get('text',''))
                or not verified_reviewed_text(block)):return False
    return True


def resolve_transcription_reason(field,lookup,allowed_reasons):
    """Call only after independently checking this field's physical pairing.

    Keep unmatched warnings and the separate import/knowledge approval state.
    """
    if not reviewed_values(field,lookup):return False
    field.setdefault('source_observation',{}).update(transcription_status='source_reviewed',knowledge_approval=False)
    field['source_observation']['source_review_digests']=sorted({lookup[r['block_id']]['structure']['source_text_review']['review_digest'] for r in field['answer_value_refs']})
    for key in ('review_reasons','source_review_reasons'):
        field[key]=[reason for reason in field.get(key,[]) if reason not in allowed_reasons]
    if not field.get('review_reasons'):field['review_status']='model_proposed'
    return True
