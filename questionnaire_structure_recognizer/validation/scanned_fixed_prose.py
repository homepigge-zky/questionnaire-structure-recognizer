"""Source-backed fixed print furniture and questionnaire cover instructions.

No whole-page exclusion. Each retired reference retains its original text and
the particular repeated group or paragraph boundaries that establish its role.
"""
from collections import defaultdict
from copy import deepcopy
import re

from .field_boundaries import ref

VERSION='scan-fixed-prose-v3-retain-readonly-signed-source'


def resolve_scanned_fixed_prose(fields,blocks,lookup,source_hash=""):
    text=[b for b in blocks if b.get('location',{}).get('extraction')=='ocr' and b.get('text','').strip() and len(b['location'].get('bbox',[]))==4]
    pages=defaultdict(list)
    for b in text:pages[b['location']['page']].append(b)
    for local in pages.values():local.sort(key=lambda b:(b['location']['bbox'][1],b['location']['bbox'][0]))
    norm=lambda b:re.sub(r'\s+','',b['text']).casefold()
    marked={}
    def mark(items,role,reason,evidence):
        for b in items:
            marked[b['block_id']]=(role,reason,[ref(e) for e in evidence])

    # Printed audit metadata is a three-part same-page group, repeated over
    # multiple pages. An isolated business question about a print/effective
    # date does not meet this evidence test.
    footers=[]
    for page,local in pages.items():
        groups=[[b for b in local if re.match(pattern,norm(b))] for pattern in
                (r'^printedby[:：]',r'^printdate(?:\([^)]*\))?[:：]',r'^effectivedate[:：]')]
        if not all(len(g)==1 for g in groups):continue
        group=[g[0] for g in groups];ys=[sum(b['location']['bbox'][1::2])/2 for b in group]
        if max(ys)-min(ys)<35:footers.append(group)
    if len(footers)>=2:
        for group in footers:
            mark(group,'fixed_template_metadata','repeated_print_operator_date_group',group)
            page=group[0]['location']['page'];top=min(b['location']['bbox'][1] for b in group)
            statements=[b for b in pages[page] if 0<top-b['location']['bbox'][3]<65 and
                re.match(r'^thisinformationisconfidential|^theuserisresponsibleforusingtheappropriateversion',norm(b))]
            mark(statements,'instruction','fixed_print_confidentiality_or_version_notice',group)

    formids=defaultdict(list)
    for b in text:
        if re.fullmatch(r'form[-‐–]\d{5,}',norm(b)):formids[norm(b)].append(b)
    for group in formids.values():
        if len({b['location']['page'] for b in group})<2:continue
        ys=[b['location']['bbox'][1] for b in group]
        if max(ys)-min(ys)>15:continue
        for identity in group:
            local=pages[identity['location']['page']];ib=identity['location']['bbox']
            forms=[b for b in local if norm(b)=='form' and 10<ib[1]-b['location']['bbox'][3]<65]
            if len(forms)!=1:continue
            form=forms[0]
            titles=[b for b in local if b['location']['bbox'][1]>=form['location']['bbox'][3] and b['location']['bbox'][3]<=ib[1]+2 and
                    len(b['text'])>20 and b['location']['bbox'][2]-b['location']['bbox'][0]>100 and not re.search(r'[?？]|[:：]\s*$',b['text'])]
            if len(titles)!=1:continue
            mark([form,*titles,identity],'repeated_header_footer','repeated_form_identifier_with_title_row',[form,*titles,identity])
            # A populated, repeated two-cell band immediately below the
            # proven document identifier is fixed version metadata. Require
            # both unchanged texts across other pages at the same coordinates;
            # a lone date or ordinary business field never establishes this.
            from .scan_matrix_structure import contents
            from .pdf_source_relations import source_cells_share_row
            cells=[c for c in blocks if c.get('structure',{}).get('scan_region_kind')=='text_box'
                   and c.get('location',{}).get('page')==identity['location']['page']]
            below=[b for b in local if 0<=b['location']['bbox'][1]-ib[3]<25
                   and len(b['text'])<40 and not re.search(r'[?？:：]',b['text'])]
            for a in below:
                ab=a['location']['bbox']
                peers=[b for b in below if b is not a and b['location']['bbox'][0]>ab[2]
                       and abs(sum(b['location']['bbox'][1::2])-sum(ab[1::2]))<8]
                if len(peers)!=1:continue
                pair=[a,peers[0]]
                if any(len({b['location']['page'] for b in text if norm(b)==norm(x)
                    and all(abs(u-v)<4 for u,v in zip(b['location']['bbox'],x['location']['bbox']))})<2 for x in pair):continue
                owners=[[c for c in cells if contents(c,local)==[x]] for x in pair]
                if not any(source_cells_share_row(c,d) for c in owners[0] for d in owners[1]):continue
                mark(pair,'fixed_template_metadata','repeated_closed_metadata_row_below_document_identifier',
                     [form,*titles,identity,*pair])


    # Questionnaire titles immediately following a proved document metadata
    # row can also repeat without a separate FORM identifier.
    repeated_titles=defaultdict(list)
    for b in text:
        if 'questionnaire' in norm(b) and len(b['text'])<110 and not re.search(r'[?？:：]',b['text']):repeated_titles[norm(b)].append(b)
    for group in repeated_titles.values():
        if len({b['location']['page'] for b in group})<2:continue
        for title in group:
            local=pages[title['location']['page']];tb=title['location']['bbox']
            metadata=[b for b in local if b.get('structure',{}).get('resolved_layout_role')=='fixed_template_metadata' and
                      0<=tb[1]-b['location']['bbox'][3]<16]
            if len(metadata)<2:continue
            translations=[b for b in local if '问卷' in b['text'] and not re.search(r'[A-Za-z?？:：]',b['text']) and
                          0<=b['location']['bbox'][1]-tb[3]<8 and abs(sum(b['location']['bbox'][::2])-sum(tb[::2]))<60]
            mark([title,*translations],'repeated_header_footer','repeated_questionnaire_title_after_document_metadata',[*metadata,title])

    for page,local in pages.items():
        for idx,b in enumerate(local):
            # A specific document-reference section and the following Content
            # heading establish that this SOP is the form's governing document.
            # A business SOP question or a requested attachment remains a field.
            if re.fullmatch(r'\d+(?:\.\d+)*referenceofthegoverningprocedure',norm(b)):
                nexts=[x for x in local[idx+1:] if 0<x['location']['bbox'][1]-b['location']['bbox'][3]<40]
                sop=[x for x in nexts if re.match(r'^sop[-‐–]\d+',norm(x)) and not re.search(r'[?？]',x['text'])]
                if len(sop)==1:
                    after=[x for x in local if re.fullmatch(r'\d+(?:\.\d+)*content',norm(x)) and 0<x['location']['bbox'][1]-sop[0]['location']['bbox'][3]<40]
                    if len(after)==1:mark([b,sop[0]],'instruction','governing_document_reference_between_explicit_section_titles',[b,sop[0],after[0]])
            # A continuous cover paragraph between an attached-questionnaire
            # introduction and its explicit confidentiality sentence. Its
            # Chinese translation is retained as original explanatory prose.
            if not re.match(r'^(?:attached|aitached)pleasefindaquestionnaire',norm(b)):continue
            paragraph=[b];last=b
            for nxt in local[idx+1:idx+16]:
                nb=nxt['location']['bbox'];lb=last['location']['bbox'];bb=b['location']['bbox']
                if nb[1]-lb[3]>18 or abs(nb[0]-bb[0])>8 or nb[2]>bb[2]+25:break
                if nxt.get('controls'):break
                paragraph.append(nxt);last=nxt
                if re.search(r'(?:保密|机密)信息',nxt['text']):
                    mark(paragraph,'instruction','bounded_cover_paragraph_ending_in_confidentiality',[b,nxt]);break

    # Existing metadata cell classification has its own exact physical row
    # proof. Include it here so the explanatory content survives in non_qa.
    for b in text:
        if b.get('structure',{}).get('resolved_layout_role')=='fixed_template_metadata':
            marked.setdefault(b['block_id'],('fixed_template_metadata',b['structure'].get('role_evidence',''),b['structure'].get('metadata_row_refs',[])))
    protected={r['block_id'] for f in fields if f.get('manual_override') or f.get('answer_origin') in {'manual','knowledge','reviewed_knowledge'} for r in f.get('label_refs',[])}
    for bid in protected:marked.pop(bid,None)
    nonqa=[]
    for bid,(role,reason,evidence) in marked.items():
        b=lookup[bid];b.setdefault('structure',{}).update(resolved_layout_role=role,fixed_prose_evidence=evidence,fixed_prose_reason=reason,fixed_prose_version=VERSION)
        nonqa.append({'role':role,'refs':[ref(b)],'text_original':b['text'],'reason':reason})
    kept=[]
    for field in fields:
        refs=field.get('label_refs') or field.get('question_refs') or []
        if field.get('manual_override') or not any(r['block_id'] in marked for r in refs):kept.append(field);continue
        remaining=[r for r in refs if r['block_id'] not in marked]
        if not remaining:continue
        updated=deepcopy(field);updated.update(label_refs=remaining,question_refs=deepcopy(remaining),
            question_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in remaining))
        # Removing a fixed footer does not destroy the independent valid
        # answer association for the real business question that remains.
        kept.append(updated)
    fields[:]=kept
    from .readonly_signed_records import retain_signed_source_records
    nonqa.extend(retain_signed_source_records(fields,blocks,lookup,source_hash))
    return nonqa
