"""Identify the code that assembles complete recognition results.

Native extraction and model batches have their own keys. Changing validation
must invalidate assembled results, without throwing away unchanged OCR/model
checkpoints. The revision is captured at worker startup, never rewritten onto
old results after a source-code update.
"""
import hashlib
from pathlib import Path


def fingerprint(root: Path | None = None) -> str:
    root = root or Path(__file__).resolve().parent
    files = [root / name for name in ('engine.py', 'form_positions.py', 'source_ownership.py',
                                     'semantic_fields.py', 'source_section_scope.py', 'local_condition_scopes.py', 'pipeline_revision.py',
                                     'source_text_review.py', 'source_choice_review.py', 'source_matrix_choices.py', 'knowledge.py')]
    files += list((root / 'validation').glob('*.py'))
    files += list((root / 'understanding').glob('*.py'))
    files += [root / name for name in ('parser_core/filled_word_layout.py', 'parser_core/native_layout.py',
        'parser_core/pdf_geometry_cache.py', 'parser_core/ocr.py', 'parser_core/pdf_vector_forms.py',
        'parser_core/pdf_caption_fragments.py', 'parser_core/scan_tick_shape.py',
        'parser_core/pdf_matrix_marks.py', 'parser_core/pdf_matrix_dividers.py', 'parser_core/pdf_inline_regions.py',
        'parser_core/legacy_fields.py')]
    digest = hashlib.sha256()
    for path in sorted(p for p in files if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


PIPELINE_REVISION = fingerprint()
