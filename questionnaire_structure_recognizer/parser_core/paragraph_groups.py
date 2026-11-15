"""Replace only a declared contiguous native response paragraph group."""


def verified_paragraph_group(block,slot):
    if slot.get('evidence')!='separate_native_prompt_then_contiguous_response_paragraphs':return False
    group=slot.get('paragraph_group',[]);ps=block.get('structure',{}).get('paragraph_spans',[])
    if len(group)<2 or block.get('controls'):return False
    indices=[p.get('paragraph_index')for p in group]
    if not all(isinstance(i,int)and 0<i<=len(ps)for i in indices):return False
    if indices!=list(range(indices[0],indices[0]+len(indices))):return False
    for p in group:
        original=ps[p['paragraph_index']-1]
        if any(p.get(k)!=original.get(k)for k in ('paragraph_id','start','end')) or p.get('expected_text')!=original.get('text') or not original.get('text','').strip():return False
    text=block.get('text','');a,z=slot.get('start'),slot.get('end');prompt=slot.get('prompt_ref',{})
    return bool(a==group[0]['start'] and z==group[-1]['end'] and isinstance(a,int)and isinstance(z,int)and 0<=a<z<=len(text)
        and '\n'.join(p['expected_text']for p in group)==text[a:z]
        and prompt.get('block_id')==block.get('block_id')and 0<=prompt.get('start',-1)<prompt.get('end',-1)<=a)


def write_paragraph_group(cell,target,value):
    from copy import deepcopy
    from docx.text.paragraph import Paragraph
    from ..exporter import _replace_paragraph_range
    group=target.get('paragraph_group')or[]
    if len(group)<2:return False
    indices=[p.get('paragraph_index')for p in group]
    if not all(isinstance(i,int)and 0<i<=len(cell.paragraphs)for i in indices):return False
    if indices!=list(range(indices[0],indices[0]+len(indices))):return False
    paragraphs=[cell.paragraphs[i-1]for i in indices]
    for paragraph,entry in zip(paragraphs,group):
        if paragraph.text.strip()!=entry.get('expected_text'):return False
        if paragraph._p.xpath('.//w:sdt|.//w:fldChar|.//w:drawing|.//w:pict|.//w:tab'):return False
        if not paragraph.runs or ''.join(r.text for r in paragraph.runs)!=paragraph.text:return False
    # Prepare each exact paragraph copy before committing any mutation. A
    # failed range never leaves half of a response changed in the output.
    copies=[Paragraph(deepcopy(p._p),p._parent)for p in paragraphs]
    for i,paragraph in enumerate(copies):
        if not _replace_paragraph_range(paragraph,0,len(paragraph.text),value if i==0 else ''):return False
    for original,changed in zip(paragraphs,copies):original._p.getparent().replace(original._p,changed._p)
    return True
