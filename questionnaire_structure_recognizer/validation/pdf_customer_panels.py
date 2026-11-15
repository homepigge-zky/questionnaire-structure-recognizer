"""Retain physically bounded customer-only comment and signing regions.

The owner comes from an explicit named issuer section, not a model hint or a
keyword such as Comments. Empty source cells are located before any fields are
created; no neighbouring white space or line is selected as a substitute.
"""
from copy import deepcopy
import re
from .field_boundaries import ref
from .pdf_furniture_ownership import _closed
from .scan_closed_local_grids import (box,page,contents,shares_horizontal_border,
    field,install,blank)
from .pdf_source_relations import source_cells_share_row
from ..source_ownership import pdf_named_issuer_section_scope

VERSION='pdf-customer-panels-v1-closed-caption-over-answer'


def reconcile_customer_panel_positions(fields,blocks,lookup):
    report={'version':VERSION,'restored':[],'protected':[]}
    cells=[b for b in blocks if b.get('location',{}).get('type')=='pdf' and _closed(b)]
    texts=[b for b in blocks if b.get('location',{}).get('type')=='pdf'
           and b.get('text','').strip() and len(b.get('location',{}).get('bbox',[]))==4]
    def below(header):
        hb=box(header)
        return [c for c in cells if c is not header and page(c)==page(header)
            and abs(box(c)[0]-hb[0])<6 and abs(box(c)[2]-hb[2])<6
            and shares_horizontal_border(header,c,max(hb[0],box(c)[0]),min(hb[2],box(c)[2]))
            and blank(c,texts)]
    def owner(label):
        f={'label_refs':[ref(label)]}
        result=pdf_named_issuer_section_scope(f,lookup)
        return f.get('_pdf_owner_evidence_refs',[]) if result and result[0]=='customer' else []
    def annotate(f,proof,evidence):
        f['customer_panel_evidence']={'version':VERSION,**proof,'owner_refs':deepcopy(evidence)}
        f['section_refs']=[deepcopy(evidence[0])]
        f['section_text_original']=lookup[evidence[0]['block_id']]['text']
        f['section_path']=[{'section_id':'source-owner:'+evidence[0]['block_id'],
            'refs':[deepcopy(evidence[0])],'text_original':f['section_text_original'],'level':0}]

    # A short colon caption occupies a complete thin bordered row. Its next
    # full-width cell is independently empty and shares the observed rule.
    for header in cells:
        hb=box(header);labels=contents(header,texts)
        if not (hb[2]-hb[0]>250 and hb[3]-hb[1]<45 and len(labels)==1):continue
        label=labels[0];text=label['text'].strip()
        if not (text.endswith((':','：')) and len(text)<100 and not re.search(r'[?？_□☐☑☒]',text)):continue
        evidence=owner(label);targets=below(header)
        if not evidence or len(targets)!=1 or box(targets[0])[3]-box(targets[0])[1]<30:continue
        f=field(header,labels,targets,suffix=':customer-comment')
        proof={'kind':'customer_caption_above_closed_blank_cell','caption_cell_ref':ref(header),
               'answer_cell_ref':ref(targets[0])}
        annotate(f,proof,evidence)
        install(fields,[f],[header],targets,texts,lookup,proof,report)

    # One shared signing caption, two or more separate role columns, then
    # corresponding blank cells. The role columns remain context, so the
    # exact original Sign./Date caption can be used by multiple instances.
    for shared in cells:
        labels=contents(shared,texts);sb=box(shared)
        if len(labels)!=1 or not re.fullmatch(r'sign(?:ature)?(?:and)?date|signature',
                re.sub(r'\W+','',labels[0]['text'].casefold())):continue
        label=labels[0];evidence=owner(label)
        if not evidence or sb[2]-sb[0]<250 or sb[3]-sb[1]>35:continue
        columns=[c for c in cells if c is not shared and page(c)==page(shared)
            and 50<box(c)[2]-box(c)[0]<sb[2]-sb[0]-20 and box(c)[3]-box(c)[1]<35
            and shares_horizontal_border(shared,c,box(c)[0],box(c)[2]) and contents(c,texts)]
        columns.sort(key=lambda c:box(c)[0])
        if not 2<=len(columns)<=4 or abs(box(columns[0])[0]-sb[0])>5 or abs(box(columns[-1])[2]-sb[2])>5:continue
        if any(not source_cells_share_row(a,b) for a,b in zip(columns,columns[1:])):continue
        targets=[below(c) for c in columns]
        if any(len(ts)!=1 for ts in targets):continue
        targets=[ts[0] for ts in targets]
        if len({c['block_id'] for c in targets})!=len(columns):continue
        replacement=[]
        for column,target in zip(columns,targets):
            role=contents(column,texts)
            f=field(shared,[label],[target],suffix=':customer-sign:'+target['block_id'],context=role)
            f['signatory_context_refs']=[ref(label)]
            f['record_context_refs']=[ref(t) for t in role]
            f['record_context']='\n'.join(t['text'] for t in role)
            proof={'kind':'customer_shared_signing_header_over_role_columns',
                'signing_header_ref':ref(shared),'role_cell_ref':ref(column),'answer_cell_ref':ref(target)}
            annotate(f,proof,evidence);replacement.append(f)
        proof={'kind':'customer_shared_signing_header_over_role_columns','signing_header_ref':ref(shared),
               'role_cell_refs':[ref(c) for c in columns],'answer_cell_refs':[ref(c) for c in targets]}
        if install(fields,replacement,[shared,*columns],targets,texts,lookup,proof,report):
            # Shared header was previously paired to an unrelated rule. Keep
            # that rule as immutable source evidence, but never as this field's
            # current answer position. install preserves all manual fields.
            pass
    # Report counts by actual positions, not by invocation groups.
    report['restored_groups']=deepcopy(report['restored'])
    report['restored']=[{'field_id':f['field_id'],'target_refs':deepcopy(f['answer_region_refs'])}
        for f in fields if f.get('customer_panel_evidence',{}).get('version')==VERSION]
    return report
