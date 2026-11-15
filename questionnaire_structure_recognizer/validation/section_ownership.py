"""Source-span ownership of derived sections, without a word blacklist."""
from copy import deepcopy
VERSION='section-ownership-v3-native-section-before-model-response'


def reconcile_response_sections(fields,sections,lookup):
    def flat(items):
        for item in items:
            yield item
            yield from flat(item.get('children',[]))
    def valid(ref):
        a,z=ref.get('start'),ref.get('end');b=lookup.get(ref.get('block_id'),{})
        return isinstance(a,int) and isinstance(z,int) and 0<=a<z<=len(b.get('text',''))
    def text(refs):return '\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in refs if valid(r))
    def native_heading_range(ref):
        if not valid(ref):return False
        block=lookup[ref['block_id']];s=block.get('structure',{})
        if (block.get('location',{}).get('type')!='docx' or
            s.get('resolved_layout_role')!='section_header' or
            s.get('local_section_evidence')!='native_numbered_full_width_table_sequence' or
            s.get('grid_col_start')!=1 or s.get('grid_col_end')!=s.get('table_column_count') or
            s.get('table_column_count',0)<2 or s.get('row_span',1)!=1):return False
        # The source reader has already verified a numbered, single physical
        # full-width chapter row in a sequence. Restrict ownership to its
        # actual numbered paragraph; a heading-like model role is insufficient.
        return any(p.get('style',{}).get('numbering',{}).get('num_id') and
            p.get('style',{}).get('numbering',{}).get('marker') and
            isinstance(p.get('start'),int) and isinstance(p.get('end'),int) and
            p['start']<=ref['start']<ref['end']<=p['end'] for p in s.get('paragraph_spans',[]))
    for field in flat(fields):
        bad_options=[o for o in field.get('options',[]) if any(native_heading_range(r) for r in o.get('refs',[]))]
        bad_values=[r for r in field.get('answer_value_refs',[]) if native_heading_range(r)]
        if not bad_options and not bad_values:continue
        field['rejected_response_sources']={'version':VERSION,
            'option_refs':[deepcopy(r) for o in bad_options for r in o.get('refs',[])],
            'answer_value_refs':deepcopy(bad_values),
            'reason':'源文编号通栏章节被错误关联为选项或答案；保留章节并拒绝该派生关联。'}
        field['options']=[o for o in field.get('options',[]) if o not in bad_options]
        field.update(pairing_validation='invalid',validation_status='invalid',review_status='needs_review')
        reason=field['rejected_response_sources']['reason']
        for key in ('validation_reasons','review_reasons'):
            if reason not in field.setdefault(key,[]):field[key].append(reason)
        if not (field.get('manual_override') or field.get('answer_origin') in {'manual','knowledge','reviewed_knowledge'}):
            if bad_values:
                field['unvalidated_answer_refs']=deepcopy(field.get('answer_value_refs',[]))
                field['observed_answer_text']=field.get('answer_text_original','')
                field.update(answer_value_refs=[],answer_refs=[],answer_text_original='')
            field['answer_status']='unresolved'
    spans={}
    for field in flat(fields):
        if field.get('pairing_validation')!='valid':continue
        source=[('answer_value',r) for r in field.get('answer_value_refs',[])]
        source.extend(('option_caption',r) for option in field.get('options',[]) for r in option.get('refs',[]))
        for role,ref in source:
            if valid(ref):spans.setdefault(ref['block_id'],[]).append({'ref':ref,'role':role,'field_id':field['field_id']})
    def conflicts(ref):
        if not valid(ref):return []
        return [owned for owned in spans.get(ref['block_id'],[]) if
                max(ref['start'],owned['ref']['start'])<min(ref['end'],owned['ref']['end'])]
    removed=[]
    def clean(nodes):
        kept=[]
        for node in nodes:
            refs=node.get('refs',[]);bad=[r for r in refs if conflicts(r)]
            if not bad:kept.append(node);continue
            removed.append({'section_id':node.get('section_id'),'refs':deepcopy(bad),
                'response_owners':[deepcopy(x) for r in bad for x in conflicts(r)]})
            remaining=[r for r in refs if r not in bad]
            if remaining:
                node=deepcopy(node);node.update(refs=remaining,text_original=text(remaining));kept.append(node)
        return kept
    sections[:]=clean(sections)
    # Reuse only an existing native heading proof: bold chapter number,
    # co-located title, and the source's subordinate question numbers. This
    # is distinct from a model proposing an arbitrary previous value as a
    # section, and never consults a different questionnaire or knowledge row.
    from .native_pdf_prose_boundaries import VERSION as PROSE_VERSION
    headings=[];seen=set()
    for block in lookup.values():
        structure=block.get('structure',{});proof=structure.get('native_prose_evidence') or {}
        refs=proof.get('section_refs') or [];key=tuple((r.get('block_id'),r.get('start'),r.get('end')) for r in refs)
        if key in seen or not refs or proof.get('version')!=PROSE_VERSION or structure.get('resolved_layout_role')!='section':continue
        if any(not valid(r) or conflicts(r) for r in refs):continue
        originals=[lookup[r['block_id']] for r in refs]
        if any(b.get('location',{}).get('extraction')!='native' or b.get('structure',{}).get('resolved_layout_role')!='section' or
               b.get('structure',{}).get('native_prose_evidence',{}).get('section_refs')!=refs for b in originals):continue
        seen.add(key)
        node={'section_id':'section:'+','.join(r['block_id'] for r in refs),'refs':deepcopy(refs),'level':0,
              'text_original':' '.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in refs),
              'source_scope_evidence':{'version':VERSION,'kind':'native_numbered_heading_with_subordinate_rows'}}
        headings.append((min(b.get('order',0) for b in originals),node))
    headings.sort(key=lambda x:x[0])
    changed=[]
    for field in flat(fields):
        old_path=field.get('section_path',[]);path=clean(old_path)
        refs=field.get('section_refs',[]);bad=[r for r in refs if conflicts(r)]
        restored=None
        if not path and (not refs or bad):
            labels=[lookup.get(r.get('block_id'),{}) for r in field.get('label_refs',[])]
            if labels and all(b.get('location',{}).get('extraction')=='native' for b in labels):
                preceding=[h for order,h in headings if order<min(b.get('order',0) for b in labels)]
                if preceding:restored=deepcopy(preceding[-1]);path=[restored]
        if not bad and path==old_path and restored is None:continue
        if bad:
            field['rejected_section_sources']={'version':VERSION,'refs':deepcopy(bad),
                'reason':'已校验的答复原值或选项原文范围不能同时作为章节传播；原始内容未删除。'}
        field['section_path']=path
        # A surviving ancestor is already source-backed. With none, retain
        # unknown scope rather than guessing a nearby section or company.
        refs=path[-1]['refs'] if path else [r for r in refs if r not in bad]
        old_text=field.get('section_text_original','')
        field['section_refs']=deepcopy(refs);field['section_text_original']=path[-1]['text_original'] if path else text(refs)
        if restored:
            field['section_scope_evidence']=deepcopy(restored['source_scope_evidence'])
            if not any(node.get('refs')==refs for node in sections):sections.append(deepcopy(restored))
        if field.get('context_label')==old_text:field['context_label']=field['section_text_original']
        changed.append(field['field_id'])
    return {'version':VERSION,'removed_section_sources':removed,'updated_fields':changed}
