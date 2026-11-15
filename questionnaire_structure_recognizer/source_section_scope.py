"""Normalize only the SECTION/Roman-number syntax of verified source headers."""
from copy import deepcopy
import re
from .semantic_fields import normalized_text

VERSION = 'source-section-scope-v1-verified-roman-caption-spacing'
PREFIX = re.compile(r'^(\s*)SECTION\s*([IVXLCDM]+)(\s*[:：])', re.I)


def _read(refs, lookup):
    pieces = []
    for rr in refs:
        block = lookup.get(rr.get('block_id'), {}); a, z = rr.get('start'), rr.get('end')
        if not isinstance(a, int) or not isinstance(z, int) or not 0 <= a < z <= len(block.get('text', '')):
            return None
        pieces.append(block['text'][a:z])
    return '\n'.join(pieces)


def source_section_scope(field, lookup, source_hash):
    refs = field.get('section_refs') or []
    raw = _read(refs, lookup)
    if not raw or not PREFIX.match(raw) or ' '.join(raw.split()) != ' '.join(field.get('section_text_original', '').split()):
        return None
    blocks = [lookup[r['block_id']] for r in refs]
    first = blocks[0]; s = first.get('structure', {}); loc = first.get('location', {})
    native = (len(blocks) == 1 and first.get('kind') == 'table_cell' and loc.get('extraction') == 'native'
              and s.get('layout_evidence') == 'visible_vector_borders' and s.get('row_start') == s.get('row_end') == 1
              and s.get('grid_col_start') == 1 and s.get('grid_col_end') == s.get('table_column_count')
              and s.get('native_bold_fraction', 0) >= .5 and s.get('native_text_spans') and not first.get('controls')
              and refs[0]['start'] == 0 and refs[0]['end'] == len(first['text']))
    if native:
        labels = [lookup.get(r['block_id'], {}) for r in field.get('label_refs', [])]
        native = bool(labels) and all(b.get('location', {}).get('page') == loc.get('page')
                     and b.get('structure', {}).get('table_id') == s.get('table_id')
                     and b.get('structure', {}).get('row_start', 0) > 1 for b in labels)
    from .validation.scanned_table_sections import VERSION as SCAN_VERSION
    from .validation.scan_section_boundaries import _partition_cell
    proof = s.get('scanned_table_section_evidence') or {}
    scanned = (loc.get('extraction') == 'ocr' and proof.get('version') == SCAN_VERSION
               and proof.get('heading_refs') == refs and proof.get('first_row_refs')
               and all(b.get('structure', {}).get('scanned_table_section_evidence') == proof for b in blocks)
               and all(_partition_cell(lookup.get(r.get('block_id'), {})) for r in proof['first_row_refs']))
    if not native and not scanned:
        return None
    canonical = PREFIX.sub(lambda m: m[1] + 'SECTION ' + m[2].upper() + m[3], raw, count=1)
    context_refs=field.get('context_refs') or []
    same_context=(context_refs==refs and _read(context_refs,lookup)==raw)
    return {'version': VERSION, 'source_hash': source_hash, 'refs': deepcopy(refs), 'text_original': raw,
            'context_is_same_source_section':same_context,
            'key': normalized_text(canonical), 'evidence': 'native_table_header' if native else 'scanned_table_header'}


def current_section_key(section, proof):
    if (not isinstance(proof, dict) or proof.get('version') != VERSION or not proof.get('refs')
            or not re.fullmatch(r'[0-9a-f]{64}', proof.get('source_hash', ''))
            or ' '.join(section.split()) != ' '.join(proof.get('text_original', '').split())
            or not PREFIX.match(proof.get('text_original', ''))):
        return None
    expected = normalized_text(PREFIX.sub(lambda m: m[1] + 'SECTION ' + m[2].upper() + m[3], proof['text_original'], count=1))
    return expected if proof.get('key') == expected else None


def duplicate_section_context(context, proof):
    """Only remove the duplicated tuple component; section scope is retained."""
    return (isinstance(proof,dict) and proof.get('context_is_same_source_section') is True
            and current_section_key(context,proof) is not None)
