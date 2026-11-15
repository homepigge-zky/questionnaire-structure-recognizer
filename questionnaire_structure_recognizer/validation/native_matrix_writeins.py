"""Terminal text slots inside independently proven native choice matrices."""
from copy import deepcopy
import re

from ..parser_core.pdf_vector_forms import verified_vector_control
from .field_boundaries import ref

VERSION='native-matrix-terminal-writein-v1'


def _valid_ref(r,lookup):
    b=lookup.get(r.get('block_id'));a,z=r.get('start'),r.get('end')
    return bool(b and type(a) is int and type(z) is int and 0<=a<z<=len(b.get('text','')))


def verified_matrix_writein(block,slot,lookup,source_hash):
    from .native_vector_forms import _number_marker,_number_ref,_choice_set
    if slot.get('kind')!='native_matrix_blank_writein' or slot.get('version')!=VERSION:return False
    probe=block.get('structure',{}).get('native_tail_blank_probe',{})
    nr=slot.get('number_ref',{});stems=slot.get('stem_refs',[]);bullets=slot.get('bullet_refs',[])
    rows=slot.get('peer_option_rows',[]);region=slot.get('closed_region',{})
    if (not source_hash or probe.get('source_hash')!=source_hash or
        probe.get('version')!='native-closed-tail-blank-probe-v1' or
        probe.get('method')!='local_native_pdf_render_no_ocr' or probe.get('blank') is not True or
        probe.get('nonwhite_pixels')!=0 or not re.fullmatch('[0-9a-f]{64}',probe.get('pixel_sha256','')) or
        probe.get('width',0)<140 or probe.get('height',0)<16 or
        probe.get('label_text')!=block.get('text') or probe.get('label_bbox')!=block.get('location',{}).get('bbox') or
        probe.get('bbox')!=slot.get('bbox') or probe.get('closed_region_id')!=region.get('region_id') or
        probe.get('closed_bbox')!=region.get('bbox') or region.get('source')!='native_vector_closed_edges' or
        not stems or len(bullets)<3 or len(rows)!=len(bullets)-1 or
        not all(_valid_ref(r,lookup) for r in [nr]+stems+bullets)):return False
    page=block.get('location',{}).get('page')
    if page!=region.get('page') or page!=probe.get('page'):return False
    n=lookup[nr['block_id']];number=_number_marker(n)
    if not number or _number_ref(number)!=nr:return False
    known=n.get('structure',{}).get('native_closed_form_regions',[])
    if region not in known or region not in block.get('structure',{}).get('native_closed_form_regions',[]):return False
    sources=[lookup[r['block_id']] for r in stems+bullets]
    if any(b.get('location',{}).get('extraction')!='native' or b['location'].get('page')!=page for b in sources):return False
    text=lambda r:lookup[r['block_id']]['text'][r['start']:r['end']]
    if not text(stems[-1]).rstrip().endswith((':','：')):return False
    if not all(re.match(r'^\s*[-•]\s*\S',text(r)) for r in bullets):return False
    if bullets[-1]!=ref(block) or not text(bullets[-1]).rstrip().endswith((':','：')):return False
    boxes=[lookup[r['block_id']]['location']['bbox'] for r in bullets]
    if any(boxes[i][1]>=boxes[i+1][1] for i in range(len(boxes)-1)):return False
    if max(b[0] for b in boxes)-min(b[0] for b in boxes)>12:return False
    all_controls=[]
    for bullet,ids in zip(bullets,rows):
        controls=[lookup.get(bid,{}) for bid in ids]
        if len(controls) not in {2,3} or not all(verified_vector_control(c) for c in controls) or not _choice_set(controls,lookup):return False
        bb=lookup[bullet['block_id']]['location']['bbox'];cy=(bb[1]+bb[3])/2
        for c in controls:
            if c['location'].get('page')!=page:return False
            cb=c['controls'][0]['mark_evidence']['caption_bbox']
            if abs((cb[1]+cb[3])/2-cy)>=3:return False
        all_controls.extend(ids)
    if len(set(all_controls))!=len(all_controls):return False
    box=slot['bbox'];lb=block['location']['bbox'];rb=region['bbox']
    if box!=[lb[2]+6,lb[3]+2,rb[2]-3,rb[3]-3]:return False
    # Retain every source block. An overlapping native text/control/image or
    # unknown scan region invalidates the proposal even if a copied pixel
    # observation incorrectly claimed blankness.
    for b in lookup.values():
        loc=b.get('location',{});bb=loc.get('bbox',[])
        if loc.get('page')!=page or len(bb)!=4 or b is block:continue
        occupied=bool(b.get('text','').strip() or b.get('controls') or b.get('kind')=='image'
                      or b.get('structure',{}).get('scan_candidate'))
        if occupied and max(box[0],bb[0])<min(box[2],bb[2]) and max(box[1],bb[1])<min(box[3],bb[3]):return False
    end=len(block.get('text',''))
    return slot.get('start')==slot.get('end')==end


def terminal_matrix_writein(number,stem,bullets,rows,region,lookup):
    """Return a region ref only after source, matrix and empty-area checks."""
    from .native_vector_forms import _number_ref,_label_ref
    if len(bullets)<3 or len(rows)!=len(bullets) or rows[-1] or any(not row for row in rows[:-1]):return None
    block=bullets[-1];probe=block.get('structure',{}).get('native_tail_blank_probe',{})
    slot_id='native-matrix-writein:'+block['block_id']
    slot={'slot_id':slot_id,'kind':'native_matrix_blank_writein','version':VERSION,
          'start':len(block['text']),'end':len(block['text']),'bbox':deepcopy(probe.get('bbox')),
          'number_ref':_number_ref(number),'stem_refs':[_label_ref(b,number) for b in stem],
          'bullet_refs':[ref(b) for b in bullets],'peer_option_rows':[[b['block_id'] for b in row] for row in rows[:-1]],
          'closed_region':deepcopy(region),'label_refs':[ref(block)],'expected_source_text':block['text']}
    if not verified_matrix_writein(block,slot,lookup,probe.get('source_hash')):return None
    slots=block.setdefault('structure',{}).setdefault('logical_slots',[])
    slots[:]=[s for s in slots if s.get('slot_id')!=slot_id];slots.append(slot)
    return ref(block,len(block['text']),len(block['text']),slot_id)
