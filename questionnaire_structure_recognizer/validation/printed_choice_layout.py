"""Avoid two visible boxes when an authorised choice already has a square bullet."""
from docx.oxml.ns import qn


def _square_bullet(paragraph, document):
    properties=paragraph._p.pPr
    numbering=properties.numPr if properties is not None else None
    style=paragraph.style;seen=set()
    while numbering is None and style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        ppr=style.element.find(qn('w:pPr'))
        numbering=ppr.find(qn('w:numPr')) if ppr is not None else None
        style=style.base_style
    if numbering is None:return None
    identifier=numbering.find(qn('w:numId'));level=numbering.find(qn('w:ilvl'))
    if identifier is None or identifier.get(qn('w:val')) in {None,'0'}:return None
    num_id=identifier.get(qn('w:val'));level_id=level.get(qn('w:val'),'0') if level is not None else '0'
    root=document.part.numbering_part.element
    instance=next((n for n in root.findall(qn('w:num')) if n.get(qn('w:numId'))==num_id),None)
    if instance is None:return None
    abstract_ref=instance.find(qn('w:abstractNumId'))
    if abstract_ref is None:return None
    abstract=next((n for n in root.findall(qn('w:abstractNum')) if n.get(qn('w:abstractNumId'))==abstract_ref.get(qn('w:val'))),None)
    if abstract is None:return None
    definition=next((n for n in abstract.findall(qn('w:lvl')) if n.get(qn('w:ilvl'))==level_id),None)
    override=next((n for n in instance.findall(qn('w:lvlOverride')) if n.get(qn('w:ilvl'))==level_id),None)
    if override is not None and override.find(qn('w:lvl')) is not None:definition=override.find(qn('w:lvl'))
    if definition is None:return None
    fmt=definition.find(qn('w:numFmt'));text=definition.find(qn('w:lvlText'))
    if fmt is None or fmt.get(qn('w:val'))!='bullet' or text is None or text.get(qn('w:val')) not in {'□','☐'}:return None
    return num_id


def suppress_duplicate_choice_bullet(document, container, container_kind, target, block):
    """Only a whole, source-verified option paragraph may suppress its own bullet.

    Global numbering definitions and every other paragraph stay untouched.
    Decimal lists, unknown/private glyphs and mixed prose are never removed.
    """
    label=target.get('preserve_option_label');reference=target.get('ref',{})
    if not label or target.get('write_method')!='replace_text_range':return None
    matches=[p for p in block.get('structure',{}).get('paragraph_spans',[])
             if p.get('text','').strip()==label and p.get('start',-1)<=reference.get('start',-2)
             and reference.get('end',-1)<=p.get('end',-2)]
    if len(matches)!=1:return None
    info=matches[0];index=info.get('paragraph_index',0)-1
    if container_kind=='paragraph':paragraph=container
    elif 0<=index<len(container.paragraphs):paragraph=container.paragraphs[index]
    else:return None
    previous=_square_bullet(paragraph,document)
    if previous is None:return None
    paragraph._p.get_or_add_pPr().get_or_add_numPr().get_or_add_numId().val=0
    return {'kind':'printed_choice_square_bullet_suppressed','target_id':target.get('target_id'),
            'paragraph_index':index+1,'num_id_before':previous,'num_id_after':'0',
            'changed_properties':['pPr/numPr/numId'],'reason':'源选项方框项目符号由显式勾选符号替代，避免一项显示两个方框；其他编号不变。'}
