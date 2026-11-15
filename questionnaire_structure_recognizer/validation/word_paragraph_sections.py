"""Correct proven prose/list contamination in derived Word section scopes.

The native parser preserves nearby paragraphs as candidate context. That
proximity is not a section hierarchy. Only independently evidenced local
headings or bounded signing groups replace an existing derived scope here.
Original text, model output, answer targets and manual mappings remain intact.
"""
from collections import defaultdict
from copy import deepcopy
import re
from .word_readonly_context import attachment_list_ids

VERSION='word-paragraph-section-context-v1'
def _s(b):return b.get('structure')or{}
def _ref(b):return {'block_id':b['block_id'],'start':0,'end':len(b.get('text','')),'slot_id':''}
def _walk(fs):
 for f in fs:
  yield f
  yield from _walk(f.get('children',[]))

def reconcile_word_paragraph_sections(fields,blocks,lookup,assignments,sections,non_qa):
 from .word_form_ownership import _protected
 from ..form_positions import _signatory_source_refs
 roles=defaultdict(set)
 for a in assignments:
  rr=a.get('ref',a);bid=rr.get('block_id')
  if bid in lookup:roles[bid].add(a.get('role',a.get('semantic_role')))
 attached=attachment_list_ids(blocks)
 instructions={bid for bid,rs in roles.items()if rs=={'instruction'}}
 instructions|={b['block_id']for b in blocks if _s(b).get('resolved_layout_role')=='instruction'}
 actual=list(_walk(fields));owned={r['block_id']for f in actual for r in f.get('label_refs',[])if f.get('answer_region_refs')}
 option_ids={r['block_id']for f in actual for o in f.get('options',[])for r in o.get('refs',[])if r.get('block_id')in lookup and isinstance(r.get('start'),int)and isinstance(r.get('end'),int)and 0<=r['start']<r['end']<=len(lookup[r['block_id']].get('text',''))}
 owned|=option_ids
 tables=defaultdict(list)
 for b in blocks:
  if b.get('location',{}).get('type')=='docx' and b.get('kind')=='table_cell' and _s(b).get('table_id'):tables[_s(b)['table_id']].append(b)
 changes=[];proven_nonheadings=set(attached)
 def heading(b):
  bid=b['block_id'];style=b.get('style',{});number=style.get('numbering',{});text=b.get('text','').strip()
  if bid in attached|instructions|owned or b.get('controls')or not text or re.search(r'[?？☐☑☒□✓✔]',text)or 'option'in roles[bid]:return False
  if b.get('kind')in{'heading','title'}or re.match(r'^(?:heading|标题)\s*\d+$',style.get('paragraph_style',''),re.I):return True
  # A native non-bullet list marker is necessary but insufficient: retain
  # the independently returned section role or explicit heading style.
  return number.get('format')not in {None,'bullet'} and 'section'in roles[bid]
 def node(ids):
  refs=[_ref(lookup[bid])for bid in ids]
  native_numbers=[lookup[bid].get('style',{}).get('numbering',{})for bid in ids if lookup[bid].get('style',{}).get('numbering',{}).get('format')not in {None,'bullet'}]
  numbering=native_numbers[0]if len(native_numbers)==1 else{}
  original_number=str(numbering.get('marker','')).rstrip('.)、')if numbering.get('format')not in {None,'bullet'}else''
  return {'section_id':'section:'+('|'.join(ids)),'level':0,'refs':refs,'text_original':'\n'.join(lookup[bid]['text']for bid in ids),'original_number':original_number}
 def append_note(bid,role):
  rr=_ref(lookup[bid])
  def identity(r):return (r.get('block_id'),r.get('start'),r.get('end'),r.get('slot_id')or'')
  if not any(n.get('role')==role and [identity(r)for r in n.get('refs',[])]==[identity(rr)]for n in non_qa):non_qa.append({'role':role,'refs':[rr],'text_original':lookup[bid]['text'],'evidence':VERSION})
 for tid,cells in tables.items():
  earliest=min(cells,key=lambda b:b['order']);ctx=_s(earliest).get('preceding_block_ids',[])
  if not ctx or any(bid not in lookup or lookup[bid].get('kind')!='paragraph' or lookup[bid]['order']>=earliest['order']for bid in ctx):continue
  candidates=[lookup[bid]for bid in ctx];strong=[b for b in candidates if heading(b)]
  selected=None;why=None;scope_notes=set(instructions)&set(ctx)
  if strong and (set(ctx)&(attached|instructions)):
   leaf=max(strong,key=lambda b:b['order']);selected=[leaf['block_id']];why='native_local_heading_after_proven_nonheading_context'
   # Retain an existing source-linked parallel heading (e.g. separate
   # English/Chinese lines). Do not infer a translation, or discard the
   # second expression just because only the first carries native numbering.
   linked_nodes=[s for s in sections if any(rr.get('block_id')==leaf['block_id']for rr in s.get('refs',[]))]
   parallel_ids={rr['block_id']for s in linked_nodes for rr in s.get('refs',[])if rr.get('block_id')in ctx}
   for b in candidates:
    bid=b['block_id']
    if (bid!=leaf['block_id'] and bid in parallel_ids and bid not in attached|instructions|owned
        and not b.get('style',{}).get('numbering') and not b.get('controls')
        and roles[bid]=={'section'} and not re.search(r'[?？☐☑☒□✓✔]',b.get('text',''))):selected.append(bid)
   selected.sort(key=lambda bid:lookup[bid]['order'])
  elif attached&set(ctx):
   # The source signing group is physically separate from the preceding
   # attachment list. A local completion caption owns its metadata form.
   source_fields=[f for f in actual if f.get('label_refs')and _s(lookup.get(f['label_refs'][0]['block_id'],{})).get('table_id')==tid]
   sign_refs=[rr for f in source_fields for rr in _signatory_source_refs(f,lookup)]
   local_sign=any(_s(lookup.get(rr['block_id'],{})).get('table_id')==tid for rr in sign_refs)
   controls=sum(bool(b.get('controls'))for b in cells)
   rows=defaultdict(list)
   for cell in cells:
    if not _s(cell).get('is_merge_continuation'):rows[_s(cell).get('row_start')].append(cell)
   paired_rows=0
   for row in rows.values():
    row=sorted(row,key=lambda b:_s(b).get('grid_col_start',0))
    if (len(row)==2 and row[0].get('text','').strip()
        and _s(row[0]).get('grid_col_start')==1
        and _s(row[0]).get('grid_col_end',0)+1==_s(row[1]).get('grid_col_start')
        and _s(row[1]).get('grid_col_end')==_s(row[1]).get('table_column_count')):paired_rows+=1
   prompts=[b for b in candidates if b['block_id']not in attached and not b.get('style',{}).get('numbering') and re.search(r'[:：]\s*$',b.get('text','')) and _signatory_source_refs({'label_refs':[_ref(b)]},lookup)]
   if local_sign and (controls>=2 or paired_rows>=2) and prompts:
    prompt=max(prompts,key=lambda b:b['order']);tail=[b for b in candidates if b['order']>prompt['order']]
    # A later physical heading has priority; unknown/control-bearing tails
    # are not silently reclassified. Exact signed-by source wording is an
    # instruction and is separately retained, not a new header.
    if all(not b.get('controls') and not b.get('style',{}).get('numbering') and ('instruction'in roles[b['block_id']]or re.search(r'\b(?:completed|filled)\s+and\s+signed\s+by\b|填写并签署|填写并签字',b.get('text',''),re.I))for b in tail):
     selected=[prompt['block_id']];why='native_signature_table_after_bounded_attachment_list';scope_notes|={b['block_id']for b in tail}
  if selected is None:continue
  prohibited=(attached|scope_notes)&set(ctx);proven_nonheadings|=prohibited
  selected_node=node(selected)
  for f in actual:
   if _protected(f)or not f.get('label_refs'):continue
   label=lookup.get(f['label_refs'][0]['block_id'],{});old={r['block_id']for r in f.get('section_refs',[])}
   if _s(label).get('table_id')!=tid or not old or not old.issubset(set(ctx)):continue
   if f.get('native_paragraph_section_evidence',{}).get('version')==VERSION and f.get('section_refs')==selected_node['refs']:continue
   prior={k:deepcopy(f.get(k))for k in('section_refs','section_text_original','section_path','context_label')}
   ancestors=[]
   for ancestor in f.get('section_path',[]):
    refs=ancestor.get('refs')or[];ids={rr.get('block_id')for rr in refs}
    if (not refs or ids&old or ids&(prohibited|owned) or any(bid not in lookup for bid in ids)):continue
    if all(heading(lookup[bid])or roles[bid]&{'section','title'}for bid in ids):ancestors.append(deepcopy(ancestor))
   leaf=deepcopy(selected_node);leaf['level']=len(ancestors)
   f.update(section_refs=deepcopy(leaf['refs']),section_text_original=leaf['text_original'],section_path=ancestors+[leaf],context_label='\n'.join(n['text_original']for n in ancestors+[leaf]))
   f['context_refs']=list({(r['block_id'],r.get('start'),r.get('end')):r for r in deepcopy(f.get('context_refs',[]))+[_ref(lookup[bid])for bid in ctx if bid not in attached]}.values())
   f['instruction_refs']=list({(r['block_id'],r.get('start'),r.get('end')):r for r in deepcopy(f.get('instruction_refs',[]))+[_ref(lookup[bid])for bid in scope_notes]}.values())
   f['native_paragraph_section_evidence']={'version':VERSION,'reason':why,'table_id':tid,'candidate_context_ids':list(ctx),'heading_refs':deepcopy(selected_node['refs']),'excluded_nonheading_refs':[_ref(lookup[bid])for bid in ctx if bid in prohibited],'previous_derived_context':prior}
   changes.append({'field_id':f['field_id'],'reason':why,'from':prior.get('section_text_original'),'to':f['section_text_original']})
  for bid in scope_notes:append_note(bid,'instruction')
 for bid in attached:
  append_note(bid,'attachment_requirement')
  # This independently verified role remains available to diagnostics even
  # when the model never produced a removable question for the list item.
  _s(lookup[bid]).update(resolved_layout_role='attachment_requirement',nonfillable_evidence=VERSION)
 # Remove only independently proven non-heading roles from the chapter list;
 # retain source refs as explicit non-QA content, never delete raw blocks.
 sections[:]=[s for s in sections if not s.get('refs')or not all(r['block_id']in proven_nonheadings for r in s['refs'])]
 return {'version':VERSION,'changes':changes,'proven_nonheading_ids':sorted(proven_nonheadings)}
