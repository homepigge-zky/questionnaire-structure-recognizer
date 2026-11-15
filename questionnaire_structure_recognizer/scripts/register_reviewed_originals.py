"""Explicit local installation of verified source-reading records only.

No source tasks, answers, test markers or knowledge entries are imported.
All inputs must already have a current real recognition result and unchanged
original bytes. Actual application still replays the records on a new upload.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil

from questionnaire_structure_recognizer.api.app import analysis_is_current
from questionnaire_structure_recognizer.source_review_packages import initialize_package_schema, register_package, _records


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(records_path, results, inventory_path):
    records = json.loads(Path(records_path).read_text('utf8'))
    inventory = json.loads(Path(inventory_path).read_text('utf8'))
    entries = {item['sha256']:item for item in inventory['files']}
    groups = {}
    for review in records:
        groups.setdefault(review['source_hash'],[]).append(review)
    prepared = []
    for source_hash, chain in groups.items():
        _records(source_hash,chain)
        entry = entries.get(source_hash)
        if not entry or sha(Path(entry['path'])) != source_hash:
            raise ValueError('Reviewed original is missing or changed')
        path = Path(results)/source_hash[:12]/'result.json'
        result = json.loads(path.read_text('utf8'))
        if (result.get('source_hash') != source_hash or not analysis_is_current(result)
                or (result.get('model') or {}).get('failed_batches')):
            raise ValueError('Reviewed result must be current, source-bound and have no failed model batch')
        for kind,scope in [('text','reviewed_ocr_transcription_only'),('choice','reviewed_source_selection_only')]:
            expected = [r for r in chain if r['scope']==scope]
            if expected != result.get(f'source_{kind}_review_records',[]):
                raise ValueError(f'{kind} review records differ from the actually processed source')
            if [r['review_digest'] for r in expected] != result.get(f'source_{kind}_review',{}).get('review_digests',[]):
                raise ValueError(f'{kind} review chain was not applied completely')
        prepared.append((source_hash,chain,sha(path)))
    if not prepared:
        raise ValueError('No reviewed originals supplied')
    return prepared


def collect_evidence(prepared, roots):
    wanted={page['image_sha256'] for _,records,_ in prepared for review in records for page in review['evidence']['pages']}
    found={}
    for directory in roots:
        root=Path(directory).resolve()
        if not root.is_dir():raise ValueError('Source evidence directory is missing')
        for path in root.rglob('*.png'):
            if path.is_file() and path.resolve().is_relative_to(root):
                digest=sha(path)
                if digest in wanted:found.setdefault(digest,path)
            if len(found)==len(wanted):return found
    if set(found)!=wanted:
        raise ValueError(f'Missing {len(wanted-set(found))} original source-review evidence images')
    return found


def install(data, prepared, reviewer, evidence_files):
    database = Path(data).resolve()/'qsr.db'
    if not database.is_file():
        raise ValueError('Initialize the local application database before installing source records')
    wanted={page['image_sha256'] for _,records,_ in prepared for review in records for page in review['evidence']['pages']}
    if set(evidence_files)!=wanted or any(sha(Path(path))!=digest for digest,path in evidence_files.items()):
        raise ValueError('Source-image evidence is incomplete or changed')
    # Keep the actual reviewed images with the app backup. Their content IDs
    # are recorded in the signed source-review payload, not in client paths.
    folder=database.parent/'source-review-evidence';folder.mkdir(exist_ok=True)
    for digest,path in evidence_files.items():
        destination=folder/(digest+'.png')
        if destination.exists():
            if sha(destination)!=digest:raise ValueError('Stored source-image evidence changed')
        else:
            shutil.copyfile(path,destination)
            if sha(destination)!=digest:raise ValueError('Evidence copy did not preserve the source bytes')
    with sqlite3.connect(database) as db:
        initialize_package_schema(db)
        db.execute('BEGIN IMMEDIATE')
        installed = [dict(source_hash=h,**register_package(db,h,records,reviewer,digest))
                     for h,records,digest in prepared]
        db.commit()
    return {'packages':installed,'knowledge_entries_imported':0,'tasks_created':0,
            'existing_reviews_or_answers_modified':False,'ocr_enabled':False,
            'source_evidence_images_saved':len(evidence_files)}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data',required=True)
    parser.add_argument('--records',required=True)
    parser.add_argument('--results',required=True)
    parser.add_argument('--inventory',required=True)
    parser.add_argument('--reviewer',required=True)
    parser.add_argument('--evidence-root',action='append',required=True)
    parser.add_argument('--approve-identical-original-reuse',action='store_true')
    args=parser.parse_args()
    if not args.approve_identical_original_reuse:
        parser.error('Explicit --approve-identical-original-reuse is required; this does not approve knowledge reuse')
    prepared=prepare(args.records,args.results,args.inventory)
    evidence_files=collect_evidence(prepared,args.evidence_root)
    print(json.dumps(install(args.data,prepared,args.reviewer,evidence_files),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
