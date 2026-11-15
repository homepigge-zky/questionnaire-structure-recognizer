"""A title's unresolved ink contour cannot answer its adjacent subtitle.

This is a relation check, not a classifier based on language or checkbox size.
It needs independently retained title references, a contiguous subtitle, an
actual form below them and no genuine response outside the title text.
"""
from copy import deepcopy
from .pdf_furniture_ownership import _box,_protected,_unused
from .field_boundaries import ref

VERSION='title-contour-relations-v1-source-header-boundaries'


def _inside(inner,outer):
    return outer[0]<=inner[0]<inner[2]<=outer[2] and outer[1]<=inner[1]<inner[3]<=outer[3]


def reconcile_title_contour_relations(fields,sections,lookup,non_qa):
    titles=[]
    for note in non_qa:
        if note.get('role')!='title':continue
        for rr in note.get('refs',[]):
            b=lookup.get(rr.get('block_id'),{})
            if (b.get('location',{}).get('type')=='pdf' and _box(b) and
                    rr.get('start')==0 and rr.get('end')==len(b.get('text','')) and b.get('text','').strip()):titles.append(b)
    section_ids={r.get('block_id') for s in sections for r in s.get('refs',[])}
    retired=[];marked=set()
    def clean(items):
        kept=[]
        for f in items:
            if _protected(f):kept.append(f);continue
            f['children']=clean(f.get('children',[]))
            labels=f.get('label_refs',[]);targets=f.get('answer_region_refs',[])
            if not _unused(f) or len(labels)!=1 or not targets:kept.append(f);continue
            label=lookup.get(labels[0].get('block_id'),{});lb=_box(label)
            if not lb or label.get('block_id') not in section_ids:kept.append(f);continue
            if labels[0].get('start')!=0 or labels[0].get('end')!=len(label.get('text','')):kept.append(f);continue
            page=label.get('location',{}).get('page');same=[t for t in titles if t['location'].get('page')==page]
            height=max((_box(b)[3] for b in lookup.values() if _box(b) and b.get('location',{}).get('page')==page),default=0)
            if lb[3]>.08*height:kept.append(f);continue
            leads=[t for t in same if abs(_box(t)[0]-lb[0])<3 and -2<=lb[1]-_box(t)[3]<=6]
            if len(leads)!=1:kept.append(f);continue
            lead=leads[0];main=[t for t in same if t is not lead and _box(t)[0]>=_box(lead)[2] and
                              abs(sum(_box(t)[1::2])-sum(_box(lead)[1::2]))<14]
            if len(main)!=1:kept.append(f);continue
            main=main[0];mb=_box(main)
            form=[b for b in lookup.values() if b.get('location',{}).get('page')==page and _box(b) and
                  b.get('structure',{}).get('scan_region_kind')=='text_box' and
                  _box(b)[1]>max(lb[3],mb[3]) and _box(b)[1]-max(lb[3],mb[3])<60 and
                  _box(b)[2]-_box(b)[0]>mb[2]-mb[0]]
            if not form:kept.append(f);continue
            regions=[lookup.get(r.get('block_id'),{}) for r in targets]
            valid=True
            for b in regions:
                bb=_box(b);controls=b.get('controls',[])
                if (not bb or b.get('location',{}).get('page')!=page or not _inside(bb,mb) or
                    b.get('text','').strip() or len(controls)!=1 or controls[0].get('type')!='checkbox' or
                    controls[0].get('selected') is not None or b.get('structure',{}).get('native_selection_verified') or
                    bb[3]-bb[1]>.65*(mb[3]-mb[1])):valid=False;break
            if not valid:kept.append(f);continue
            # The false option must be the neighbouring fixed title itself.
            # A real Yes/No or a separately labelled response is never retired.
            if not f.get('options') or any(o.get('selected') is not None or
                    not o.get('refs') or any(r.get('block_id') not in {lead['block_id'],main['block_id']} or
                    r.get('start')!=0 or r.get('end')!=len(lookup[r['block_id']]['text']) for r in o['refs'])
                    for o in f['options']):kept.append(f);continue
            proof={'version':VERSION,'kind':'subtitle_wrongly_paired_to_unresolved_contour_inside_adjacent_title',
                'label_ref':deepcopy(labels[0]),'lead_ref':ref(lead),'main_title_ref':ref(main),
                'region_refs':deepcopy(targets),'form_below_refs':[ref(b)for b in form],
                'source_records':[{'block_id':b['block_id'],'text':b['text'],'location':deepcopy(b['location'])}for b in [label,lead,main,*regions]]}
            non_qa.append({'role':'document_header','refs':deepcopy(labels),'text_original':label['text'],
                           'reason':'标题下的连续副标题；原答复引用位于旁侧标题字形内部，没有独立填写位置。','source_role_evidence':proof})
            label.setdefault('structure',{}).update(resolved_layout_role='document_header',title_contour_relation=deepcopy(proof))
            for region in regions:region.setdefault('structure',{}).update(
                title_contour_relation=deepcopy(proof),source_content_status='undetermined_title_content',
                scan_association_valid=False)
            marked.add(label['block_id']);retired.append({'field_id':f['field_id'],**proof})
        return kept
    fields[:]=clean(fields)
    sections[:]=[s for s in sections if not s.get('refs') or not all(r.get('block_id') in marked for r in s['refs'])]
    return {'version':VERSION,'retired':retired}
