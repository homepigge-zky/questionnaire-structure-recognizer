"""Source-proved PDF contents, margin furniture and value/option roles.

Retain every source block. Only derived question entries wholly covered by a
proved role are retired. Text equality alone does not deduplicate business data.
"""
from collections import defaultdict
from copy import deepcopy
import re

from .field_boundaries import ref
from .form_completion import new_field

VERSION='native-pdf-prose-boundaries-v2-bounded-page-list-continuation'


def _protected(f):
    return f.get('manual_override') or f.get('answer_origin') in {'manual','knowledge','reviewed_knowledge'} or f.get('review_status') in {'confirmed','human_corrected'}


def _flat(fields):
    for f in fields:
        yield f
        yield from _flat(f.get('children',[]))


def _page_list_continuations(fields, blocks):
    """An unfinished bracketed list across a two-column page split.

    Require the last/first table rows, matching real column boundaries,
    consecutive list letters at the same printed indent, a closing bracket,
    and the next independent decimal question. No standalone list item is
    excluded merely because it starts with a letter.
    """
    pages=defaultdict(list); protected=set()
    def protect(items, inherited=False):
        for f in items:
            guard=inherited or _protected(f)
            if guard:
                protected.update(r['block_id'] for key in ('label_refs','question_refs','answer_region_refs','answer_value_refs') for r in f.get(key,[]))
            protect(f.get('children',[]),guard)
    protect(fields)
    for b in blocks:
        loc=b.get('location',{});s=b.get('structure',{})
        if (b.get('kind')=='table_cell' and loc.get('type')=='pdf' and loc.get('extraction')=='native'
                and isinstance(loc.get('page'),int) and len(loc.get('bbox',[]))==4
                and s.get('layout_evidence')=='visible_vector_borders' and s.get('table_column_count')==2):
            pages[loc['page']].append(b)
    number=re.compile(r'^\s*(\d+(?:\.\d+)+)[.]?\s*(?:\n|\s)')
    letters=re.compile(r'(?m)^\s*([a-z])[.)]\s*')
    def box(b):return b['location']['bbox']
    def row(b,items):
        s=b['structure']
        return sorted([x for x in items if x['structure'].get('table_id')==s['table_id'] and
                       x['structure'].get('row_start')==s['row_start']],key=lambda x:box(x)[0])
    def marker_x(b,start,end):
        spans=[x for x in b['structure'].get('native_text_spans',[]) if x['start']<=start and x['end']>=end and len(x.get('bbox',[]))==4]
        return spans[0]['bbox'][0] if len(spans)==1 else None
    for page,items in pages.items():
        after=pages.get(page+1,[])
        if not after:continue
        bottoms=[b for b in items if abs(box(b)[3]-max(box(x)[3] for x in items))<2]
        tops=[b for b in after if abs(box(b)[1]-min(box(x)[1] for x in after))<2]
        for parent_block in bottoms:
            previous=parent_block.get('text','');m=number.match(previous)
            opening=previous.rfind('[')
            if not m or opening<0 or opening<previous.rfind(']') or parent_block['block_id'] in protected:continue
            prior_letters=list(letters.finditer(previous,opening))
            if not prior_letters:continue
            pr=row(parent_block,items)
            if len(pr)!=2 or pr[0] is not parent_block:continue
            owners=[f for f in _flat(fields) if any(r['block_id']==parent_block['block_id'] for r in f.get('label_refs',[]))]
            if len(owners)!=1 or _protected(owners[0]):continue
            owner=owners[0]
            if not owner.get('answer_region_refs') or any(r['block_id']!=pr[1]['block_id'] for r in owner['answer_region_refs']):continue
            for continuation in tops:
                text=continuation.get('text','');ls=list(letters.finditer(text))
                if (not ls or text[:ls[0].start()].strip() or not text.rstrip().endswith(']')
                        or text.count(']')!=1 or '[' in text or continuation['block_id'] in protected):continue
                cr=row(continuation,after)
                if (len(cr)!=2 or cr[0] is not continuation or cr[1].get('text','').strip()
                        or cr[1].get('controls') or cr[1]['block_id'] in protected
                        or any(abs(box(a)[i]-box(b)[i])>2 for a,b in zip(pr,cr) for i in (0,2))):continue
                states=[ord(x.group(1)) for x in [prior_letters[-1],*ls]]
                if any(b!=a+1 for a,b in zip(states,states[1:])):continue
                pa=prior_letters[-1];ca=ls[0]
                px=marker_x(parent_block,pa.start(1),pa.end(1)+1);cx=marker_x(continuation,ca.start(1),ca.end(1)+1)
                if px is None or cx is None or abs(px-cx)>3:continue
                later=[b for b in after if b['structure'].get('table_id')==continuation['structure']['table_id']
                       and b['structure'].get('row_start')==continuation['structure']['row_start']+1
                       and abs(box(b)[0]-box(continuation)[0])<2]
                if len(later)!=1:continue
                nxt=number.match(later[0].get('text',''));parts=m[1].split('.')
                if not nxt or nxt[1]!='.'.join(parts[:-1]+[str(int(parts[-1])+1)]):continue
                # Any source control in the continuation row means it may be
                # independently answerable, even if its text cell is empty.
                if any(b.get('controls') and b.get('location',{}).get('page')==page+1
                       and len(b['location'].get('bbox',[]))==4
                       and box(cr[0])[0]<=box(b)[0]<box(cr[1])[2] and box(cr[0])[1]<=box(b)[1]<box(cr[0])[3]
                       for b in blocks):continue
                yield owner,parent_block,continuation,cr[1],later[0]


def resolve_native_pdf_prose(fields,blocks,lookup):
    native=[b for b in blocks if b.get('location',{}).get('extraction')=='native' and
            b.get('text','').strip() and len(b['location'].get('bbox',[]))==4]
    pages=defaultdict(list)
    for b in native:pages[b['location']['page']].append(b)
    covered=[];notes=[]
    def mark(b,role,evidence):
        r=ref(b);covered.append(r)
        b.setdefault('structure',{}).update(resolved_layout_role=role,native_prose_evidence={'version':VERSION,**evidence})
        notes.append({'role':role,'refs':[r],'text_original':b['text'],'reason':evidence['reason']})
    for owner,previous,continuation,empty,following in _page_list_continuations(fields,blocks):
        refs=deepcopy(owner.get('instruction_refs') or [])+[ref(continuation)]
        owner['instruction_refs']=list({(r['block_id'],r['start'],r['end']):r for r in refs}.values())
        evidence={'reason':'前页未结束的括号说明及连续字母列表，在相同实体表格列跨页延续，并在下一独立题号之前结束。',
                  'parent_field_id':owner['field_id'],'previous_ref':ref(previous),'continuation_ref':ref(continuation),
                  'empty_continuation_region_ref':ref(empty),'next_question_ref':ref(following),
                  'pages':[previous['location']['page'],continuation['location']['page']]}
        owner.setdefault('cross_page_instruction_evidence',[])
        if evidence not in owner['cross_page_instruction_evidence']:owner['cross_page_instruction_evidence'].append(deepcopy(evidence))
        mark(continuation,'instruction',evidence)
    # Section numbers require the separate bold heading and numbered source
    # questions below it. A numeric business value or a plain numbered prompt
    # never qualifies merely because it is short.
    for page,items in pages.items():
        def bold(b):
            runs=b.get('structure',{}).get('native_pdf_font_runs',[])
            return bool(runs) and all(r.get('flags',0)&16 for r in runs)
        for number in items:
            m=re.fullmatch(r'(\d+)[.)]',number['text'].strip())
            if not m or not bold(number) or number.get('structure',{}).get('native_closed_form_regions'):continue
            nb=number['location']['bbox']
            heads=[b for b in items if b is not number and bold(b) and b['location']['bbox'][0]>nb[2]+5
                   and nb[1]-2<=b['location']['bbox'][1]<=nb[3]+14]
            has_children=any(re.match(re.escape(m[1])+r'\.\d+',b['text'].strip()) and b['location']['bbox'][1]>nb[3] for b in items)
            letter_children={b['text'].strip().rstrip('.)') for b in items if re.fullmatch(r'[A-Z][.)]',b['text'].strip()) and
                             b['location']['bbox'][1]>nb[3] and b.get('structure',{}).get('native_closed_form_regions')}
            has_children=has_children or {'A','B','C'}<=letter_children
            if not heads or not has_children:continue
            for b in [number,*heads]:mark(b,'section',{'reason':'独立加粗章节号、同行标题及本节下级题号共同证明章节标题。','section_refs':[ref(x) for x in [number,*heads]]})
    # A full-width numbered table caption followed by a physical header row
    # and repeated columns is a record table. Reuse the exact header refs for
    # each physical row; do not turn the populated cells into new questions.
    tables=defaultdict(list)
    for b in native:
        if b.get('kind')=='table_cell':tables[(b['location']['page'],b.get('structure',{}).get('table_id'))].append(b)
    record_consumed=[]
    for (page,tid),cells in list(tables.items()):
        rows=defaultdict(list)
        for b in cells:rows[b['structure'].get('row_start')].append(b)
        ordered=sorted(rows)
        if len(ordered)<3 or len(rows[ordered[0]])!=1:continue
        title=rows[ordered[0]][0];headers=sorted(rows[ordered[1]],key=lambda b:b['location']['bbox'][0])
        if len(headers)<3 or not re.search(r'(?:^|\n)\d+\.\d+\.?\s*(?:\n|$)',title['text']):continue
        if not all(b['text'].strip() and b['structure'].get('native_pdf_font_runs') for b in headers):continue
        if not all(any(r.get('flags',0)&2 for r in b['structure']['native_pdf_font_runs']) for b in headers):continue
        data=[rows[n] for n in ordered[2:]]
        # Continuation needs the immediately following physical page, the
        # same column edges, no intervening new full-width table caption.
        for (p2,t2),cc in tables.items():
            if p2!=page+1:continue
            rr=defaultdict(list)
            for b in cc:rr[b['structure'].get('row_start')].append(b)
            first=sorted(rr[min(rr)],key=lambda b:b['location']['bbox'][0])
            if len(first)==len(headers) and all(abs(a['location']['bbox'][0]-b['location']['bbox'][0])<2 and abs(a['location']['bbox'][2]-b['location']['bbox'][2])<2 for a,b in zip(headers,first)):
                data.extend(rr[n] for n in sorted(rr))
        for index,row in enumerate(data,1):
            row=sorted(row,key=lambda b:b['location']['bbox'][0])
            if len(row)!=len(headers):continue
            for column,(header,value) in enumerate(zip(headers,row),1):
                if abs(header['location']['bbox'][0]-value['location']['bbox'][0])>=2 or abs(header['location']['bbox'][2]-value['location']['bbox'][2])>=2:continue
                # A question with independently established native options in
                # a record cell remains that question; never its header value.
                established=[f for f in _flat(fields) if f.get('boundary_evidence')=='native_vector_numbered_form' and any(r['block_id']==value['block_id'] for r in f.get('label_refs',[]))]
                if established:continue
                if any(_protected(f) and any(r['block_id']==value['block_id'] for r in f.get('label_refs',[])+f.get('answer_value_refs',[])) for f in _flat(fields)):continue
                value.setdefault('structure',{})['record_header_block_id']=header['block_id']
                value['structure'].update(local_layout_type='record_table',record_index=index,record_column_index=column)
                f=new_field(header,ref(header),[ref(value)]);fid='native-record:'+value['block_id']
                vals=[ref(value)] if value['text'].strip() else []
                f.update(field_id=fid,question_id=fid,answer_value_refs=vals,answer_refs=deepcopy(vals),
                         answer_text_original=value['text'],answer_status='filled' if vals else 'blank',
                         record_index=index,record_column_index=column,context_refs=[ref(title)],
                         context_label=title['text']+' · '+str(index),boundary_evidence='native_source_record_cell',boundary_version=VERSION)
                fields.append(f);record_consumed.append(ref(value))
        if record_consumed:
            for b in headers:mark(b,'table_header',{'reason':'完整跨列表题下方的列头，答复保留为各物理记录格。','caption_ref':ref(title)})
    covered.extend(record_consumed)
    # An explicit contents heading plus a right Page column and at least three
    # numbered caption/page rows proves a TOC. A numbered questionnaire alone
    # cannot satisfy this layout and is left untouched.
    for page,items in pages.items():
        headings=[b for b in items if re.fullmatch(r'INDEX|CONTENTS|TABLE OF CONTENTS|目录',b['text'].strip(),re.I)]
        page_columns=[b for b in items if re.fullmatch(r'Page|页码',b['text'].strip(),re.I)]
        for heading in headings:
            hb=heading['location']['bbox']
            for column in page_columns:
                pb=column['location']['bbox']
                if pb[0]<hb[2]+150 or not hb[1]<=pb[1]<hb[3]+60:continue
                row_ids=set();rows=[]
                for value in items:
                    vb=value['location']['bbox'];word=value['text'].strip()
                    if not word.isdigit() or not 1<=int(word)<=max(pages) or abs(vb[2]-pb[2])>12 or vb[1]<=pb[3]:continue
                    cy=(vb[1]+vb[3])/2
                    row=[b for b in items if b is not value and abs(sum(b['location']['bbox'][1::2])/2-cy)<3 and b['location']['bbox'][2]<vb[0]-50]
                    nums=[b for b in row if re.fullmatch(r'\d+[.)]',b['text'].strip())]
                    caps=[b for b in row if re.search(r'[A-Za-z\u4e00-\u9fff]',b['text'])]
                    if len(nums)!=1 or not caps or nums[0]['location']['bbox'][2]>=min(b['location']['bbox'][0] for b in caps):continue
                    rows.append([nums[0],*caps,value])
                if len(rows)<3:continue
                for b in [heading,column]+[b for row in rows for b in row]:
                    if b['block_id'] in row_ids:continue
                    row_ids.add(b['block_id']);mark(b,'table_of_contents',{'reason':'原页目录标题、Page列和重复编号/标题/页码行共同确认目录布局。','heading_ref':ref(heading),'page_column_ref':ref(column)})
    # Repeated margins need both repeated text and matching actual coordinates.
    # A block within a closed form/table is business content, even if repeated.
    repeated=defaultdict(list)
    for b in native:
        if b['kind']=='table_cell' or b.get('structure',{}).get('native_closed_form_regions'):continue
        page=b['location']['page'];bb=b['location']['bbox'];bottom=max(x['location']['bbox'][3] for x in pages[page])
        if bb[3]<bottom*.2:repeated[re.sub(r'\s+','',b['text']).casefold()].append(b)
    for key,items in repeated.items():
        if len(key)<4:continue
        for b in items:
            bb=b['location']['bbox'];peers=[x for x in items if x['location']['page']!=b['location']['page'] and
                abs(x['location']['bbox'][0]-bb[0])<8 and abs(x['location']['bbox'][1]-bb[1])<8]
            if len({x['location']['page'] for x in peers})>=2:
                mark(b,'repeated_header_footer',{'reason':'同一原生文字位于至少三页相同页边位置，且不在任何实体表单框内。','peer_refs':[ref(x) for x in peers]})
    # Native vector options are actual printed captions, regardless of whether
    # the model successfully grouped their surrounding compound question yet.
    from ..parser_core.pdf_vector_forms import verified_vector_control
    for b in blocks:
        if verified_vector_control(b):covered.extend(b.get('structure',{}).get('native_vector_caption_refs',[]))
    for page,items in pages.items():
        controls=[b for b in blocks if b['location'].get('page')==page and verified_vector_control(b)]
        for b in items:
            if not b['text'].strip().endswith((':','：')):continue
            bb=b['location']['bbox'];below=[c for c in controls if bb[3]<c['location']['bbox'][1]<bb[3]+150 and
                                         bb[0]-3<=c['location']['bbox'][0]<=bb[2]+3]
            if len(below)<9:continue
            letters=[x for x in items if re.fullmatch(r'[A-Z][.)]',x['text'].strip()) and x['location']['bbox'][0]<bb[0]-100 and
                     bb[3]<x['location']['bbox'][1]<bb[3]+150]
            if len(letters)>=3:mark(b,'table_header',{'reason':'右侧选择列标题，下面有至少三条独立字母题号及三组选项，保留标题但不生成答复字段。','row_number_refs':[ref(x) for x in letters]})
    for f in _flat(fields):
        if f.get('boundary_evidence')=='native_vector_numbered_form':covered.extend(f.get('answer_value_refs',[]))
    # Remaining literal fill-line strokes in a verified value block are
    # formatting, never a second field after the actual value was consumed.
    for b in native:
        spans=[r for r in covered if r['block_id']==b['block_id']]
        if spans:
            remainder=''.join(c for i,c in enumerate(b['text']) if not any(r['start']<=i<r['end'] for r in spans))
            if remainder and re.fullmatch(r'[\s_]+',remainder):covered.append(ref(b))
    def fully_covered(r):
        spans=sorted((max(r['start'],x['start']),min(r['end'],x['end'])) for x in covered
                     if x['block_id']==r['block_id'] and max(r['start'],x['start'])<min(r['end'],x['end']))
        cursor=r['start']
        for a,z in spans:
            if a>cursor and lookup[r['block_id']]['text'][cursor:a].strip():return False
            cursor=max(cursor,z)
        return cursor>=r['end'] or not lookup[r['block_id']]['text'][cursor:r['end']].strip()
    def retire(items):
        kept=[]
        for f in items:
            if _protected(f) or f.get('boundary_evidence')=='native_source_record_cell':kept.append(f);continue
            # An option caption can also label its own independent write-in.
            # Only source-verified native underlined blanks authorize keeping
            # that child; a claimed region or a nonempty string never does.
            from .native_table_positions import verified_native_table_slot
            regions=f.get('answer_region_refs',[])
            if regions and all((b:=lookup.get(r.get('block_id'),{})) and
                (slot:=next((s for s in b.get('structure',{}).get('logical_slots',[]) if s.get('slot_id')==r.get('slot_id')),None)) and
                slot.get('kind')=='native_table_underlined_gap' and r.get('start')==slot.get('start') and r.get('end')==slot.get('end') and
                verified_native_table_slot(b,slot,lookup) for r in regions):
                kept.append(f);continue
            retire(f.get('children',[]));refs=f.get('label_refs') or f.get('question_refs') or []
            if refs and all(fully_covered(r) for r in refs):
                for r in refs:lookup[r['block_id']].setdefault('structure',{}).setdefault('retired_derived_fields',[]).append(f['field_id'])
                kept.extend(f.get('children',[]));continue
            kept.append(f)
        items[:]=kept
    retire(fields)
    return notes
