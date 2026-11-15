"""Retire only source-proved instruction/section/owned-option remnants.

Missing answer positions alone never authorize removal. Every retired reference
is retained in the original source ledger with the physical role evidence.
"""
from collections import defaultdict
from copy import deepcopy
import re
from .pdf_furniture_ownership import _box, _closed, _protected, _unused
from .pdf_source_relations import source_text_inside_cell,source_cells_share_row
from .field_boundaries import ref

VERSION='source-nonanswerable-roles-v1-exact-owned-ranges-and-bounded-prose'
def walk(fields):
    for f in fields:
        yield f
        yield from walk(f.get('children',[]))
def page(b):return b.get('location',{}).get('page')
def compact(s):return re.sub(r'\s+','',s).casefold()
def _key(r):return r.get('block_id'),r.get('start'),r.get('end')
def _valid(r,lookup):
    a,z=r.get('start'),r.get('end')
    return type(a)is int and type(z)is int and 0<=a<z<=len(lookup.get(r.get('block_id'),{}).get('text',''))


def _repeated_occupied_banners(pdf,texts):
    """Three real adjoining occupied cells: issuer, title, page counter."""
    cells=[c for c in pdf if _closed(c)];groups=defaultdict(list)
    for middle in cells:
        p=page(middle);local=[t for t in texts if page(t)==p]
        height=max((_box(t)[3]for t in local),default=0)
        if _box(middle)[3]>.15*height:continue
        title=[t for t in local if source_text_inside_cell(t,middle,label=True)]
        if len(title)!=1 or not re.search(r'questionnaire|问卷',title[0]['text'],re.I):continue
        left=[c for c in cells if c is not middle and source_cells_share_row(c,middle)]
        right=[c for c in cells if c is not middle and source_cells_share_row(middle,c)]
        if len(left)!=1 or len(right)!=1:continue
        contents=[[t for t in local if source_text_inside_cell(t,c,label=True)]for c in(left[0],middle,right[0])]
        if any(len(x)!=1 for x in contents):continue
        label,heading,counter=(x[0]for x in contents)
        match=re.fullmatch(r'page(\d+)(?:of|/)(\d+)',compact(counter['text']),re.I)
        if not match or int(match[1])!=p or int(match[2])<3:continue
        if any(re.search(r'[?？:：_]|\.{3,}',t['text'])for t in(label,heading)):continue
        # Real controls outside these printed captions would make it a form,
        # not fixed furniture. Letter-contour objects remain in source data.
        regions=[c for c in pdf if c.get('controls') and page(c)==p and any(source_text_inside_cell(c,cell)for cell in(left[0],middle,right[0]))]
        if any(not any(source_text_inside_cell(c,t)for t in(label,heading,counter))for c in regions):continue
        signature=(compact(label['text']),compact(heading['text']),int(match[2]))
        groups[signature].append({'page':p,'labels':[label,heading,counter],'cells':[left[0],middle,right[0]]})
    marked={}
    for group in groups.values():
        if len({g['page']for g in group})<3:continue
        for g in group:
            proof={'kind':'three_occupied_header_cells_repeated_on_source_pages','cell_refs':[ref(c)for c in g['cells']],
                'peer_pages':sorted({x['page']for x in group}),'peer_caption_refs':[ref(x['labels'][0])for x in group]}
            for b in g['labels']:marked[b['block_id']]=('repeated_header_footer',proof)
    return marked


def reconcile_nonanswerable_source_roles(fields,sections,lookup,non_qa):
    pdf=[b for b in lookup.values()if b.get('location',{}).get('type')=='pdf' and _box(b)]
    if not pdf:return {'version':VERSION,'retired':[]}
    texts=[b for b in pdf if b.get('text','').strip()]
    all_fields=list(walk(fields));owned=defaultdict(list);marked=_repeated_occupied_banners(pdf,texts);retired=[]
    for owner in all_fields:
        if owner.get('pairing_validation')!='valid' or _protected(owner):continue
        for option in owner.get('options',[]):
            refs=option.get('refs',[]);targets=[lookup.get(r.get('block_id'),{})for r in option.get('answer_region_refs',[])]
            if not refs or not targets or not all(_valid(r,lookup)for r in refs):continue
            if not all(_box(b) and any(c.get('type')=='checkbox'for c in b.get('controls',[]))for b in targets):continue
            if len({page(lookup[r['block_id']])for r in refs}|{page(b)for b in targets})!=1:continue
            for r in refs:owned[_key(r)].append(owner['field_id'])

    # Rebuild the explicit attachment-list scope from source geometry. Do not
    # trust a bare model instruction role, arbitrary words or a missing slot.
    for lead in texts:
        if not re.fullmatch(r'(?:please)?(?:attach|enclose)tothecompletedquestionnaire[:：]?',compact(lead['text'])):continue
        lb=_box(lead);last=lb
        following=sorted((b for b in texts if page(b)==page(lead) and _box(b)[1]>=lb[3]),key=lambda b:(_box(b)[1],_box(b)[0]))
        for b in following:
            bb=_box(b)
            if bb[1]-last[3]>30 or bb[1]-lb[1]>120:break
            if re.search(r'[?？]',b['text']) or (b['text'].rstrip().endswith((':','：'))and bb[0]<lb[0]+10):break
            if not lb[0]<bb[0]<lb[0]+45:continue
            marked[b['block_id']]=('attachment_requirement',{'kind':'explicit_indented_attachment_list','lead_ref':ref(lead)})
            last=bb

    # An occupied, wide, closed title band containing an explicit Part/Section
    # designation is a chapter, not a write-in cell. Ordinary centered labels
    # or a word such as "Name" do not satisfy this source proof.
    for b in texts:
        if not re.fullmatch(r'(?:part|section|chapter)\s+[A-Z0-9IVX]+',b['text'].strip(),re.I):continue
        bb=_box(b);bands=[]
        for band in pdf:
            if page(band)!=page(b) or not _closed(band) or not source_text_inside_cell(b,band,label=True):continue
            cb=_box(band);width=cb[2]-cb[0]
            if width<4*(bb[2]-bb[0]) or cb[3]-cb[1]>2*(bb[3]-bb[1]):continue
            if abs(sum(bb[::2])-sum(cb[::2]))>width*.1:continue
            contained=[t for t in texts if page(t)==page(b) and source_text_inside_cell(t,band,label=True)]
            controls=[c for c in pdf if c.get('controls') and page(c)==page(b) and source_text_inside_cell(c,band)]
            if any(not source_text_inside_cell(c,b)for c in controls):continue
            if len(contained)==1:bands.append(band)
        if len(bands)==1:
            marked[b['block_id']]=('section_header',{'kind':'explicit_part_title_in_closed_occupied_band','band_ref':ref(bands[0])})

    # Cross-page instructions need the final body line on the first page and
    # the first independently located, unnumbered field on the next page.
    # Repeated source header/footer text can be excluded by exact text+position.
    repeated=set()
    repeated_text=defaultdict(list)
    for b in texts:repeated_text[compact(b['text'])].append(b)
    for b in texts:
        bb=_box(b)
        if b.get('structure',{}).get('resolved_layout_role') in {'repeated_header_footer','fixed_template_approval'}:
            repeated.add(b['block_id'])
        if any(page(c)!=page(b) and all(abs(x-y)<4 for x,y in zip(bb,_box(c)))
               for c in repeated_text[compact(b['text'])]):repeated.add(b['block_id'])
    for f in all_fields:
        refs=f.get('label_refs') or [];labels=[lookup[r['block_id']]for r in refs if _valid(r,lookup)]
        if len(labels)!=1 or len(refs)!=1 or not _unused(f) or f.get('answer_region_refs') or _protected(f):continue
        b=labels[0];bb=_box(b)
        if not bb or not re.fullmatch(r'(?:incaseof|intheeventof).+[:：]',compact(b['text'])):continue
        heights=[_box(c)[3]for c in texts if page(c)==page(b)]
        if not heights or bb[1]<.8*max(heights):continue
        lower=[c for c in texts if page(c)==page(b) and _box(c)[1]>=bb[3] and c['block_id']not in repeated]
        if lower:continue
        candidates=[q for q in all_fields if q is not f and not _protected(q) and q.get('pairing_validation')=='valid'
            and q.get('answer_region_refs') and q.get('label_refs') and
            all(page(lookup.get(r.get('block_id'),{}))==page(b)+1 and _box(lookup[r['block_id']])for r in q['label_refs'])]
        if not candidates:continue
        candidates.sort(key=lambda q:min(_box(lookup[r['block_id']])[1]for r in q['label_refs']))
        first=candidates[0]
        if first.get('number_refs'):continue
        fb=_box(lookup[first['label_refs'][0]['block_id']])
        prior=[c for c in texts if page(c)==page(b)+1 and _box(c)[3]<fb[1] and c['block_id']not in repeated]
        if prior:continue
        rr=ref(b)
        for key in('context_refs','instruction_refs'):
            if not any(_key(r)==_key(rr)for r in first.setdefault(key,[])):first[key].append(deepcopy(rr))
        first['source_conditional_context']={'version':VERSION,'refs':[deepcopy(rr)],'relation':'preceding_page_final_condition_to_next_first_unnumbered_response'}
        if not first.get('explicit_condition'):
            first['explicit_condition']={'kind':'source_context_condition','status':'unresolved','source_text':b['text'],
                'source_refs':[deepcopy(rr)],'parent_field_id':None}
        marked[b['block_id']]=('conditional_context',{'kind':'cross_page_conditional_introduction','owner_field_id':first['field_id']})

    def clean(fs):
        kept=[]
        for f in fs:
            if _protected(f):kept.append(f);continue
            f['children']=clean(f.get('children',[]))
            if not _unused(f):kept.append(f);continue
            refs=f.get('label_refs') or f.get('question_refs') or []
            if not refs or not all(_valid(r,lookup)for r in refs):kept.append(f);continue
            owners=[set(owned.get(_key(r),[]))-{f['field_id']}for r in refs]
            owner_ids=set.intersection(*owners)if owners else set()
            # Wrong response links may point to another caption of this same
            # parent. Any unowned/real response target prevents retirement.
            if len(owner_ids)==1 and all(set(owned.get(_key(r),[]))&owner_ids for r in f.get('answer_region_refs',[])):
                retired.append({'field_id':f['field_id'],'role':'owned_choice_option','owner_field_id':next(iter(owner_ids)),'refs':deepcopy(refs)})
                continue
            if f.get('answer_region_refs') or not all(r['block_id']in marked for r in refs):kept.append(f);continue
            if f.get('options'):
                # Model text-only "options" can be other bullets in this same
                # explicitly bounded attachment list. Keep all six originals;
                # an actual checkbox or selected value prevents retirement.
                option_refs=[r for o in f['options']for r in o.get('refs',[])]
                if (any(o.get('selected') is True or o.get('answer_region_refs') or not o.get('refs') for o in f['options'])
                    or not all(_valid(r,lookup) and marked.get(r['block_id'],('',{}))[0]=='attachment_requirement'for r in refs+option_refs)
                    or len({_key(marked[r['block_id']][1]['lead_ref'])for r in refs+option_refs})!=1):kept.append(f);continue
                refs=sorted(refs+option_refs,key=lambda r:(_box(lookup[r['block_id']])[1],_box(lookup[r['block_id']])[0]))
            roles={marked[r['block_id']][0]for r in refs}
            if len(roles)!=1:kept.append(f);continue
            role=next(iter(roles));evidence=[deepcopy(marked[r['block_id']][1])for r in refs]
            text='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']]for r in refs)
            note={'role':role,'refs':deepcopy(refs),'text_original':text,'reason':'原文位置证明为固定说明/章节，不是独立答复题','source_role_evidence':{'version':VERSION,'proofs':evidence}}
            non_qa.append(note)
            for r in refs:lookup[r['block_id']].setdefault('structure',{}).update(resolved_layout_role=role,nonanswerable_source_evidence=deepcopy(note['source_role_evidence']))
            if role=='section_header' and not any(n.get('refs')==refs for n in sections):sections.append({'section_id':'source-section:'+f['field_id'],'refs':deepcopy(refs),'text_original':text,'source_scope_evidence':deepcopy(note['source_role_evidence'])})
            retired.append({'field_id':f['field_id'],'role':role,'refs':deepcopy(refs),'proofs':evidence})
        return kept
    fields[:]=clean(fields)
    from .native_pdf_nonanswerable_roles import reconcile_native_pdf_readonly_roles
    native=reconcile_native_pdf_readonly_roles(fields,lookup,non_qa)
    return {'version':VERSION,'retired':retired,'native_pdf_roles':native}
