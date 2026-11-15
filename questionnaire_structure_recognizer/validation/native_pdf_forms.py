"""Source-coordinate boundaries for repeated native PDF label/value lanes.

No text is inferred. This recovers physical line spans inside unruled cells
and repeated paragraph columns; it never pairs by filtered list indices.
"""
from collections import defaultdict
from copy import deepcopy
import re
from .form_completion import new_field

VERSION='native-pdf-lanes-v2-independent-table-groups'


def _key(r):return (r['block_id'],r['start'],r['end'])
def _overlap(a,b):return a['block_id']==b['block_id'] and max(a.get('start',0),b.get('start',0))<min(a.get('end',0),b.get('end',0))


def restore_native_pdf_lanes(fields,blocks,lookup):
    from .native_table_group_boundaries import restore_native_table_groups
    restore_native_table_groups(fields,blocks,lookup)
    pages=defaultdict(list)
    for block in blocks:
        loc=block.get('location',{});text=block.get('text','');s=block.get('structure',{})
        if loc.get('type')!='pdf' or loc.get('extraction')!='native' or s.get('scan_page_context') or not text.strip():continue
        if block.get('controls') or s.get('native_option_groups'):continue
        spans=s.get('native_text_spans') or ([{'start':0,'end':len(text),'bbox':loc.get('bbox',[])}] if block.get('kind')=='paragraph' else [])
        for span in spans:
            a,b=span.get('start'),span.get('end');box=span.get('bbox',[])
            if not isinstance(a,int) or not isinstance(b,int) or not 0<=a<b<=len(text) or len(box)!=4:continue
            value=text[a:b].strip()
            if not value:continue
            pages[(loc.get('page'),s.get('table_id','outside'))].append({'ref':{'block_id':block['block_id'],'start':a,'end':b,'slot_id':''},'box':box,'text':value,'block':block})
    recovered=[];consumed=[];contexts=[]
    for key,items in pages.items():
        items.sort(key=lambda x:(x['box'][1],x['box'][0]))
        labels=[x for x in items if re.search(r'[A-Za-z\u4e00-\u9fff]',x['text']) and re.search(r'[:：]\s*$',x['text']) and len(x['text'])<150 and '\n' not in x['text']]
        candidates=[]
        for label in labels:
            box=label['box'];partners=[x for x in items if x is not label and abs(x['box'][0]-box[0])<3 and -4<=box[1]-x['box'][3]<4 and re.search(r'[\u0370-\u03ff\u4e00-\u9fff]',x['text']) and not re.search(r'[A-Za-z]',x['text'])]
            label_parts=sorted(partners+[label],key=lambda x:x['box'][1]);top=min(x['box'][1] for x in label_parts);right=max(x['box'][2] for x in label_parts)
            vals=[x for x in items if x not in label_parts and x not in labels and x['box'][0]>=right+2 and top-3<=(x['box'][1]+x['box'][3])/2<=box[3]+3 and
                not re.fullmatch(r'[\s☑☒☐□✓✔√]*|(?:yes|no|n/?a)(?:\s*/\s*(?:yes|no|n/?a))+',x['text'],re.I)]
            if len(vals)>1 and max(x['box'][0] for x in vals)-min(x['box'][0] for x in vals)<3:
                ordered=sorted(vals,key=lambda x:x['box'][1])
                if all(b['box'][1]-a['box'][1]>5 for a,b in zip(ordered,ordered[1:])):vals=ordered[:1]
            if len(vals)!=1:continue
            value=vals[0]
            candidates.append({'label':label,'labels':label_parts,'values':[value],'top':top,'group':(round(box[0]/10),round(value['box'][0]/10) if key[1]=='outside' else 'same_physical_table')})
        groups=defaultdict(list)
        for c in candidates:groups[c['group']].append(c)
        for group in groups.values():
            if len(group)<2:continue
            group.sort(key=lambda c:c['top'])
            # A repeated lane and explicit label suffix are both required.
            for n,c in enumerate(group):
                value=c['values'][0];next_top=group[n+1]['top'] if n+1<len(group) else value['box'][3]+28
                following=[x for x in items if x is not value and x not in labels and x not in c['labels'] and
                    abs(x['box'][0]-value['box'][0])<3 and value['box'][1]+5<x['box'][1] and value['box'][3]-4<=x['box'][1]<next_top-2 and not re.search(r'[:：]\s*$',x['text'])]
                cursor=value['box'][3]
                for x in following:
                    if x['box'][1]-cursor>5:break
                    c['values'].append(x);cursor=x['box'][3]
                lrefs=[deepcopy(x['ref']) for x in c['labels']];vrefs=[deepcopy(x['ref']) for x in c['values']]
                for vr,x in zip(vrefs,c['values']):
                    sid=f"native-pdf-response:{vr['block_id']}:{vr['start']}:{vr['end']}"
                    vr['slot_id']=sid
                    slot={'slot_id':sid,'kind':'native_pdf_source_value','start':vr['start'],'end':vr['end'],
                        'bbox':deepcopy(x['box']),'label_refs':deepcopy(lrefs),'evidence':'repeated_physical_label_value_lanes'}
                    slots=lookup[vr['block_id']].setdefault('structure',{}).setdefault('logical_slots',[])
                    if not any(s.get('slot_id')==sid for s in slots):slots.append(slot)
                f=new_field(c['label']['block'],lrefs[0],deepcopy(vrefs));fid='pdf-lane:'+c['label']['ref']['block_id']+':'+str(c['label']['ref']['start'])
                f.update(field_id=fid,question_id=fid,label_refs=lrefs,question_refs=deepcopy(lrefs),
                    question_text_original='\n'.join(x['text'] for x in c['labels']),answer_value_refs=vrefs,answer_refs=deepcopy(vrefs),
                    answer_text_original='\n'.join(x['text'] for x in c['values']),answer_status='filled',
                    boundary_evidence='native_pdf_repeated_coordinate_lane',boundary_version=VERSION,
                    source_locations={'question':[deepcopy(x['block']['location'])|{'bbox':x['box']} for x in c['labels']],
                        'answer':[deepcopy(x['block']['location'])|{'bbox':x['box']} for x in c['values']],
                        'answer_regions':[deepcopy(x['block']['location'])|{'bbox':x['box']} for x in c['values']]})
                # A section is a separate, actual heading above the lane. It
                # must have the nearby explicit numbered section marker, or
                # be the first heading inside the same physical table cell.
                first=group[0];headings=[x for x in items if x not in labels and x['box'][3]<first['top']+1 and first['top']-x['box'][3]<65 and x['box'][0]>=first['label']['box'][0]+10 and x['box'][0]<first['values'][0]['box'][0]+5 and len(x['text'])>12 and not re.search(r'[:：]\s*$',x['text'])]
                valid_heads=[x for x in headings if key[1]!='outside' or any(re.fullmatch(r'\d+(?:\.\d+)+',num['text']) and num['box'][0]<x['box'][0] and abs(num['box'][1]-x['box'][1])<16 for num in items)]
                if valid_heads:
                    h=max(valid_heads,key=lambda x:x['box'][1]);f.update(context_refs=[deepcopy(h['ref'])],context_label=h['text'],section_refs=[deepcopy(h['ref'])],section_text_original=h['text']);contexts.append(h['ref'])
                recovered.append(f);consumed.extend(lrefs+vrefs)
                for lr in lrefs:
                    lookup[lr['block_id']].setdefault('structure',{}).setdefault('protected_label_ranges',[]).append({'start':lr['start'],'end':lr['end']})
    if not recovered:return fields
    kept=[]
    for field in fields:
        refs=(field.get('label_refs') or field.get('question_refs') or [])+field.get('answer_value_refs',[])
        if not any(_overlap(r,c) for r in refs for c in consumed):kept.append(field);continue
        label_refs=field.get('label_refs') or field.get('question_refs') or []
        remaining=[r for r in label_refs if not any(_overlap(r,c) for c in consumed+contexts)]
        # Keep the raw model group in diagnostic source metadata, never as
        # a falsely usable answer. Unrecovered source labels remain visible.
        anchor=lookup.get(label_refs[0]['block_id'],{}) if label_refs else {}
        anchor.setdefault('structure',{}).setdefault('replaced_model_groups',[]).append(deepcopy(field))
        if remaining:
            f=deepcopy(field);f.update(label_refs=remaining,question_refs=deepcopy(remaining),
                question_text_original='\n'.join(lookup[r['block_id']]['text'][r['start']:r['end']] for r in remaining),
                answer_region_refs=[],answer_value_refs=[],answer_refs=[],answer_text_original='',answer_status='unresolved',review_status='needs_review',
                review_reasons=['原模型分组混入多个独立填写行；可确定的行已拆分，此残余范围仍需核对。'])
            kept.append(f)
    fields[:]=sorted(kept+recovered,key=lambda f:(lookup[f['label_refs'][0]['block_id']].get('order',0),f['label_refs'][0].get('start',0)))
    return fields
