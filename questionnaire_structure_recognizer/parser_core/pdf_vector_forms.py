"""Read printed PDF form geometry and text styling without OCR or inference.

An outlined square is a checkbox only when native paint order and local layout
bind it to an explicit option caption. A checked value requires two actual
diagonal strokes; unrelated ink remains unknown, never a guessed checkmark.
"""
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

from .models import make_block

VERSION = 'pdf-native-vector-forms-v6-native-table-positions'
_MARK_EVIDENCE_VERSIONS = {VERSION, 'pdf-native-vector-forms-v5-bounded-blank-probes', 'pdf-native-vector-forms-v4-contiguous-caption-runs', 'pdf-native-vector-forms-v3-explicit-attachment-caption'}
CAPTION = re.compile(r'^(?:Yes|No|N\s*/?\s*A|Copy\s+attached|是|否|不适用|复印件已附)$', re.I)


def _inside(point, box, pad=0.3):
    return box[0]-pad <= point[0] <= box[2]+pad and box[1]-pad <= point[1] <= box[3]+pad


def _norm_map(text):
    chars, offsets = [], []
    for index, char in enumerate(text):
        for value in unicodedata.normalize('NFKC', char):
            if not value.isspace():chars.append(value);offsets.append(index)
    return ''.join(chars), offsets


def _visible(path):
    # Fill-only drawing records legitimately have no stroke opacity or colour.
    # They are background paint, never evidence of an outlined response area.
    opacity=path.get('stroke_opacity')
    color=path.get('color')
    return (isinstance(opacity,(int,float)) and .95 < opacity <= 1
            and isinstance(color,(list,tuple)) and bool(color) and min(color)<.8)


def _rect(path):
    rectangles = [list(item[1]) for item in path.get('items', []) if item[0] == 're']
    return rectangles[0] if len(rectangles) == 1 and len(path.get('items', [])) == 1 else None


def _closed_regions(drawings):
    horizontals, verticals, rectangles = [], [], []
    for index, drawing in enumerate(drawings):
        if not _visible(drawing):continue
        for item in drawing.get('items', []):
            if item[0] == 're':
                b=list(item[1])
                if b[2]-b[0]>100 and b[3]-b[1]>18:rectangles.append({'bbox':b,'drawing_indices':[index]})
            elif item[0] == 'l':
                a,z=item[1],item[2]
                if abs(a.y-z.y)<.5 and abs(a.x-z.x)>100:horizontals.append((min(a.x,z.x),max(a.x,z.x),(a.y+z.y)/2,index))
                if abs(a.x-z.x)<.5 and abs(a.y-z.y)>15:verticals.append(((a.x+z.x)/2,min(a.y,z.y),max(a.y,z.y),index))
    for top in horizontals:
        for bottom in horizontals:
            if bottom[2]-top[2]<18 or abs(top[0]-bottom[0])>1 or abs(top[1]-bottom[1])>1:continue
            left=[v for v in verticals if abs(v[0]-top[0])<1 and v[1]<=top[2]+1 and v[2]>=bottom[2]-1]
            right=[v for v in verticals if abs(v[0]-top[1])<1 and v[1]<=top[2]+1 and v[2]>=bottom[2]-1]
            if left and right:
                rectangles.append({'bbox':[top[0],top[2],top[1],bottom[2]],'drawing_indices':[top[3],bottom[3],left[0][3],right[0][3]]})
    unique={tuple(round(x,2)for x in r['bbox']):r for r in rectangles}
    return list(unique.values())


def _native_runs(page, blocks):
    all_chars=[]
    for trace in page.get_texttrace():
        if trace.get('type',0)==3 or trace.get('opacity',1)<=0:continue
        for code,_,origin,box in trace['chars']:
            if code<0:continue
            all_chars.append({'char':chr(code),'origin':list(origin),'bbox':list(box),
                'font':trace.get('font',''),'flags':trace.get('flags',0),'seqno':trace['seqno']})
    for block in blocks:
        box=block.get('location',{}).get('bbox',[]);text=block.get('text','')
        if len(box)!=4 or not text or block.get('location',{}).get('extraction')!='native':continue
        chars=[c for c in all_chars if _inside(((c['bbox'][0]+c['bbox'][2])/2,(c['bbox'][1]+c['bbox'][3])/2),box)]
        rows=[]
        for char in sorted(chars,key=lambda c:(c['origin'][1],c['origin'][0])):
            row=next((r for r in rows if abs(r[0]['origin'][1]-char['origin'][1])<1),None)
            if row is None:rows.append([char])
            else:row.append(char)
        ordered=[c for row in rows for c in sorted(row,key=lambda c:c['origin'][0])]
        flat=[]
        for char in ordered:
            for value in unicodedata.normalize('NFKC',char['char']):
                if not value.isspace():flat.append((value,char))
        expected,offsets=_norm_map(text)
        if ''.join(c[0]for c in flat)!=expected:continue
        runs=[]
        for index,(_,char) in enumerate(flat):
            key=(char['font'],char['flags'],char['seqno'])
            if runs and runs[-1]['key']==key and abs(runs[-1]['baseline']-char['origin'][1])<1:
                run=runs[-1];run['end']=offsets[index]+1
                run['bbox']=[min(run['bbox'][0],char['bbox'][0]),min(run['bbox'][1],char['bbox'][1]),max(run['bbox'][2],char['bbox'][2]),max(run['bbox'][3],char['bbox'][3])]
            else:runs.append({'key':key,'start':offsets[index],'end':offsets[index]+1,'bbox':char['bbox'][:],
                'baseline':char['origin'][1],'font':char['font'],'flags':char['flags'],'paint_seqno':char['seqno']})
        for run in runs:run.pop('key')
        block.setdefault('structure',{})['native_pdf_font_runs']=runs
    return all_chars


def _state(box, outline_index, drawings, text_chars):
    tolerance=min(1.0,(box[2]-box[0])*.15)
    corners=[(box[0],box[1]),(box[2],box[3]),(box[0],box[3]),(box[2],box[1])]
    def close(a,b):return math.hypot(a[0]-b[0],a[1]-b[1])<=tolerance
    positive=[];negative=[];unknown=[]
    for index,path in enumerate(drawings):
        if index==outline_index:continue
        rect=list(path['rect'])
        if path.get('fill') is not None and path.get('fill_opacity',1)>.95 and index>outline_index and all(_inside(p,rect,0)for p in corners):
            unknown.append(index);continue
        # A dot, tick, erased/covered box or curved mark is not an empty box.
        # Only the two exact diagonal strokes below establish checked status.
        interior=[box[0]+tolerance,box[1]+tolerance,box[2]-tolerance,box[3]-tolerance]
        intersects=rect[0]<interior[2] and rect[2]>interior[0] and rect[1]<interior[3] and rect[3]>interior[1]
        if intersects and path.get('fill') is not None and path.get('fill_opacity',1)>.95 and min(path['fill'])<.8:
            unknown.append(index)
        for item in path.get('items',[]):
            if not _visible(path):continue
            if item[0]!='l':
                if intersects and item[0]!='re':unknown.append(index)
                continue
            a,z=tuple(item[1]),tuple(item[2])
            if not (_inside(a,box,tolerance) and _inside(z,box,tolerance)):continue
            if (close(a,corners[0]) and close(z,corners[1])) or (close(z,corners[0]) and close(a,corners[1])):positive.append(index)
            elif (close(a,corners[2]) and close(z,corners[3])) or (close(z,corners[2]) and close(a,corners[3])):negative.append(index)
            elif _inside(((a[0]+z[0])/2,(a[1]+z[1])/2),interior,0):unknown.append(index)
    inside_text=any(c['char'].strip() and _inside(((c['bbox'][0]+c['bbox'][2])/2,(c['bbox'][1]+c['bbox'][3])/2),box,-.5)for c in text_chars)
    state=None if unknown or inside_text or bool(positive)!=bool(negative) else bool(positive and negative)
    return state,{'method':'native_rectangle_with_two_diagonals','outline_drawing_index':outline_index,
        'positive_diagonal_indices':positive,'negative_diagonal_indices':negative,'unclassified_ink_indices':unknown,'unclassified_text_inside':inside_text}


def _caption_runs(block):
    """Join only consecutive paint calls spelling one literal source caption.

    A PDF may paint N and o separately even inside a Yes/No/N/A cell. Both
    character offsets and physical paint/line continuity must agree; neither
    neighbouring words nor different lines can supply a missing letter.
    """
    runs = block.get('structure', {}).get('native_pdf_font_runs', [])
    text = block.get('text', '')
    found = {}
    for index, first in enumerate(runs):
        chain = [first]
        for next_run in [None, *runs[index + 1:]]:
            if next_run is not None:
                previous = chain[-1]
                if (next_run['paint_seqno'] != previous['paint_seqno'] + 1
                        or abs(next_run['baseline'] - first['baseline']) >= 1
                        or not -.3 <= next_run['bbox'][0] - previous['bbox'][2] < 3
                        or next_run['start'] < previous['end']
                        or text[previous['end']:next_run['start']].strip()):
                    break
                chain.append(next_run)
            start, end = first['start'], chain[-1]['end']
            while start < end and text[start].isspace(): start += 1
            while start < end and text[end - 1].isspace(): end -= 1
            caption = text[start:end]
            if CAPTION.fullmatch(caption):
                merged = {**chain[-1], 'start': start, 'end': end,
                          'bbox': [min(r['bbox'][0] for r in chain), min(r['bbox'][1] for r in chain),
                                   max(r['bbox'][2] for r in chain), max(r['bbox'][3] for r in chain)]}
                found[(start, end, merged['paint_seqno'])] = (merged, caption)
    return list(found.values())


def page_vector_evidence(page, blocks, page_number):
    if page.rotation:return [],[],{'skipped':'rotated native geometry requires a matching coordinate transform'}
    drawings=page.get_drawings();chars=_native_runs(page,blocks)
    blank_paint={t['seqno'] for t in page.get_texttrace() if t.get('type',0)==0 and t.get('opacity',1)==1
                 and t['chars'] and all(chr(c[0]).isspace() for c in t['chars'] if c[0]>=0)}
    captions=[]
    for block in blocks:
        captions.extend((block, run, caption) for run, caption in _caption_runs(block))
    controls=[]
    for index,path in enumerate(drawings):
        box=_rect(path)
        if not box or not _visible(path) or path.get('fill') is not None:continue
        width,height=box[2]-box[0],box[3]-box[1]
        if not(4<=width<=20 and 4<=height<=20 and .8<=width/height<=1.25):continue
        nearby=[(b,r,t) for b,r,t in captions if 1<=path['seqno']-r['paint_seqno']<=4 and
                all(n in blank_paint for n in range(r['paint_seqno']+1,path['seqno'])) and
                abs((r['bbox'][1]+r['bbox'][3])/2-(box[1]+box[3])/2)<18 and
                (-100<box[0]-r['bbox'][2]<35)]
        if len(nearby)!=1:continue
        block,run,caption=nearby[0];selected,evidence=_state(box,index,drawings,chars)
        evidence.update(outline_bbox=box,outline_paint_seqno=path['seqno'],caption_paint_seqno=run['paint_seqno'],caption_bbox=run['bbox'],version=VERSION,
                        intervening_blank_text_paint=list(range(run['paint_seqno']+1,path['seqno'])))
        location={'type':'pdf','page':page_number,'bbox':box,'coordinate_system':'PDF points, top-left origin',
            'extraction':'native_vector','drawing_index':index,'paint_seqno':path['seqno']}
        caption_ref={'block_id':block['block_id'],'start':run['start'],'end':run['end'],'slot_id':''}
        native=make_block('form_field','',0,location,structure={'physical_region_kind':'native_vector_checkbox',
            'blank_state':'filled' if selected is True else 'blank' if selected is False else 'unreadable',
            'native_vector_caption_refs':[caption_ref],'native_vector_version':VERSION},
            controls=[{'type':'checkbox','source':'native_pdf_vector','control_id':f"pdf-vector:{page_number}:{index}",
                'selected':selected,'mark_evidence':evidence,'caption_refs':[caption_ref]}]).model_dump()
        controls.append(native)
    regions=_closed_regions(drawings)
    for region in regions:
        material=json.dumps([page_number,region['bbox'],region['drawing_indices']],sort_keys=True)
        region.update(page=page_number,region_id='pdf-closed-'+hashlib.sha256(material.encode()).hexdigest()[:16],source='native_vector_closed_edges')
    return controls,regions,{'rectangular_options':len(controls),'closed_regions':len(regions)}


def _native_tail_blank_probes(page, blocks, regions, source_hash):
    """Observe actual white space; this does not decide that a field exists.

    Only an explicit native bullet ending in a colon inside a closed region
    is probed. The later structural validator must prove the complete matrix
    and terminal write-in relationship. Rendering is local, with no OCR.
    Any ink/image/fill in the proposed lane prevents a blank declaration.
    """
    import fitz
    for block in blocks:
        location=block.get('location',{});text=block.get('text','').strip();box=location.get('bbox',[])
        if (location.get('extraction')!='native' or len(box)!=4 or
            not re.match(r'^[-•]\s*\S',text) or not text.endswith((':','：'))):continue
        containing=[r for r in regions if r['bbox'][0]<=box[0]<box[2]<=r['bbox'][2]
                    and r['bbox'][1]<=box[1]<box[3]<=r['bbox'][3]]
        if not containing:continue
        region=min(containing,key=lambda r:(r['bbox'][2]-r['bbox'][0])*(r['bbox'][3]-r['bbox'][1]))
        # Stay below the native label's glyph bounds. Prior option rows can
        # have descenders/box edges overlapping its top baseline by a point.
        target=[box[2]+6,box[3]+2,region['bbox'][2]-3,region['bbox'][3]-3]
        if target[2]-target[0]<70 or target[3]-target[1]<8:continue
        image=page.get_pixmap(matrix=fitz.Matrix(2,2),clip=fitz.Rect(target),colorspace=fitz.csGRAY,alpha=False)
        pixels=image.samples
        # This is a strict white-space observation, not a percentage threshold
        # that could tolerate small checkbox marks or handwriting.
        blank=bool(pixels) and min(pixels)>=248
        block.setdefault('structure',{})['native_tail_blank_probe']={
            'version':'native-closed-tail-blank-probe-v1','source_hash':source_hash,
            'method':'local_native_pdf_render_no_ocr','page':location['page'],
            'closed_region_id':region['region_id'],'closed_bbox':list(region['bbox']),
            'label_text':block.get('text',''),'label_bbox':list(box),'bbox':target,
            'width':image.width,'height':image.height,'pixel_sha256':hashlib.sha256(pixels).hexdigest(),
            'blank':blank,'nonwhite_pixels':sum(v<248 for v in pixels)}
def enrich_native_vector_forms(path, native, password=''):
    import fitz
    if str(native.get('file_type','')).lower()!='pdf':return {'status':'not_applicable'}
    if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=native.get('source_hash'):raise ValueError('PDF source fingerprint mismatch')
    blocks=native['source_blocks'];additions=[];regions=[];pages=[]
    with fitz.open(path) as doc:
        if doc.needs_pass and not doc.authenticate(password):return {'status':'password_required'}
        for index,page in enumerate(doc,1):
            local=[b for b in blocks if b.get('location',{}).get('page')==index]
            controls,closed,info=page_vector_evidence(page,local,index)
            _native_tail_blank_probes(page,local,closed,native['source_hash'])
            from .pdf_table_positions import observe_table_positions
            prefix_controls=observe_table_positions(page,local,native['source_hash'],index)
            known={b['block_id'] for b in controls}
            controls.extend(b for b in prefix_controls if b['block_id'] not in known)
            info['native_prefix_options']=len(prefix_controls)
            additions.extend(controls);regions.extend(closed);pages.append({'page':index,**info})
    # Idempotent reinspection replaces only generated vector observations,
    # preserving text/OCR blocks and their identities. Old-version controls
    # must not win merely because they already have the same physical ID.
    current={b['block_id']:b for b in additions}
    blocks[:]=[current.pop(b['block_id'],b) for b in blocks
               if b.get('structure',{}).get('physical_region_kind')!='native_vector_checkbox' or b['block_id'] in current]
    blocks.extend(current.values())
    blocks.sort(key=lambda b:(b.get('location',{}).get('page',0),b.get('location',{}).get('bbox',[0,0])[1],b.get('location',{}).get('bbox',[0,0])[0],b.get('order',0)))
    for order,block in enumerate(blocks):block['order']=order
    for block in blocks:
        loc=block.get('location',{});box=loc.get('bbox',[])
        if len(box)!=4:continue
        local=[r for r in regions if r['page']==loc.get('page') and _inside(((box[0]+box[2])/2,(box[1]+box[3])/2),r['bbox'],1)]
        if local:
            block.setdefault('structure',{})['native_closed_form_regions']=local
            block['structure']['native_closed_form_region']=min(local,key=lambda r:(r['bbox'][2]-r['bbox'][0])*(r['bbox'][3]-r['bbox'][1]))
    info={'version':VERSION,'status':'complete','pages':pages,'controls':len(additions),'regions':len(regions)}
    native.setdefault('metadata',{})['native_vector_forms']=info
    return info


def verified_vector_control(block):
    controls=block.get('controls',[])
    if len(controls)!=1:return False
    control=controls[0];e=control.get('mark_evidence') or {};s=block.get('structure',{})
    if e.get('caption_after_outline'):
        from .pdf_table_positions import verified_prefix_control
        return verified_prefix_control(block)
    return bool(block.get('location',{}).get('extraction')=='native_vector' and s.get('physical_region_kind')=='native_vector_checkbox'
        and control.get('source')=='native_pdf_vector' and isinstance(control.get('selected'),bool)
        and e.get('method')=='native_rectangle_with_two_diagonals' and e.get('version') in _MARK_EVIDENCE_VERSIONS
        and isinstance(e.get('outline_paint_seqno'),int) and isinstance(e.get('caption_paint_seqno'),int)
        and 1<=e['outline_paint_seqno']-e['caption_paint_seqno']<=4
        and e.get('intervening_blank_text_paint',[])==list(range(e['caption_paint_seqno']+1,e['outline_paint_seqno']))
        and not e.get('unclassified_ink_indices')
        and not e.get('unclassified_text_inside') and e.get('outline_bbox')==block.get('location',{}).get('bbox')
        and bool(e.get('positive_diagonal_indices'))==bool(e.get('negative_diagonal_indices'))
        and bool(e.get('positive_diagonal_indices') and e.get('negative_diagonal_indices'))==control['selected']
        and bool(control.get('caption_refs')))
