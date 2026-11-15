from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from urllib.parse import urlparse

from ..engine import ENGINE_VERSION, recognize
from ..pipeline_revision import PIPELINE_REVISION
from ..exporter import TemplateFillError, fill_docx_template, fill_workflow_docx_template, knowledge_filled_fields, standard_questionnaire_docx
from ..form_positions import build_fill_target, confirmed_template_ranges, refresh_result_responsibility
from ..knowledge import extract_pending_entries
from ..source_pair_guards import SHARED_TARGET_RESTRICTION, SHARED_TARGET_REASON
from ..semantic_fields import annotation, upsert_annotation
from ..reports import html_report
from ..understanding import health
from ..validation import validate_correction
from ..workflow import (
    apply_answers_to_result, approve_import_items, import_tabular, initialize_schema,
    json_value, match_questionnaire, materialize_import, materialize_questionnaire,
    now, save_snapshot, task_snapshot, knowledge_choice_selection, import_context_by_field,
)


ROOT = Path(__file__).resolve().parents[1]


def analysis_is_current(result: dict) -> bool:
    """PDF-local recovery changes must invalidate whole-result reuse too.

    Native/model caches remain independent and may still be reused safely.
    This check never mutates an existing saved or manually corrected version.
    """
    if result.get('engine_version') != ENGINE_VERSION:return False
    if result.get('pipeline_revision') != PIPELINE_REVISION:return False
    if str(result.get('file_type') or '').lower()!='pdf':
        from ..parser_core.docx_parser import PARSER_VERSION as WORD_PARSER_VERSION
        if result.get('parser_version') != WORD_PARSER_VERSION:return False
        from ..form_positions import (_local_personnel_context_refs,
            _current_participant_source_refs,LOCAL_PERSONNEL_SCOPE_VERSION)
        lookup={b['block_id']:b for b in result.get('source_blocks',[])}
        pending=list(result.get('questions') or [])
        while pending:
            field=pending.pop();pending.extend(field.get('children') or [])
            definition=field.get('form_definition',{})
            if _local_personnel_context_refs(field,lookup) and definition.get('local_personnel_scope_version')!=LOCAL_PERSONNEL_SCOPE_VERSION:return False
            current_refs=_current_participant_source_refs(field,lookup)
            if current_refs and not (definition.get('requires_current_person_confirmation') or definition.get('requires_signatory_confirmation')):return False
            if current_refs and definition.get('local_personnel_scope_version') and definition.get('current_person_evidence_refs')!=current_refs:return False
        return True
    from ..parser_core.pdf_parser import PARSER_VERSION
    from ..validation.scanned_forms import VERSION as SCAN_VERSION
    from ..form_positions import PDF_OWNER_SCOPE_VERSION
    if result.get('parser_version')!=PARSER_VERSION:return False
    pending=list(result.get('questions') or [])
    while pending:
        field=pending.pop();pending.extend(field.get('children') or [])
        if field.get('form_definition',{}).get('pdf_owner_scope_version')!=PDF_OWNER_SCOPE_VERSION:return False
        if field.get('boundary_evidence')=='scan_physical_form' and field.get('boundary_version')!=SCAN_VERSION:return False
        for label in field.get('label_refs') or []:
            for option in field.get('options') or []:
                for ref in option.get('refs') or []:
                    if label.get('block_id')==ref.get('block_id') and max(label.get('start',0),ref.get('start',0))<min(label.get('end',0),ref.get('end',0)):return False
    return True


def single_unit_scope(value) -> dict:
    """Drop organization ownership keys in the single-unit deployment."""
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key not in {"company", "factory"} and item not in (None, "")}


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback and release the Windows file handle after each with block."""
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Store:
    def __init__(self, data: Path, key: str, auth_mode: str = "key"):
        if auth_mode not in {"local", "key"}:
            raise RuntimeError("QSR_AUTH_MODE 只能是 local 或 key")
        if auth_mode == "key" and not key:
            raise RuntimeError("必须配置 QSR_ACCESS_KEY")
        self.data, self.key, self.auth_mode = data.resolve(), key, auth_mode
        self.files, self.results, self.cache = self.data / "files", self.data / "results", self.data / "cache"
        for directory in (self.data, self.files, self.results, self.cache):
            directory.mkdir(parents=True, exist_ok=True)
        self.database = self.data / "qsr.db"
        self.pool = ThreadPoolExecutor(max_workers=max(1, min(2, int(os.getenv("QSR_WORKERS", "1")))))
        self.running, self.lock = set(), threading.Lock()
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks(
              id TEXT PRIMARY KEY, filename TEXT NOT NULL, storage TEXT NOT NULL, source_hash TEXT NOT NULL,
              size INTEGER NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL, processed INTEGER DEFAULT 0,
              total INTEGER, error TEXT, current_version INTEGER, latest_analysis_version INTEGER,
              created TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS versions(
              task_id TEXT NOT NULL, version INTEGER NOT NULL, kind TEXT NOT NULL, result_file TEXT NOT NULL,
              base_version INTEGER, created TEXT DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(task_id,version));
            CREATE TABLE IF NOT EXISTS knowledge_entries(
              id TEXT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL,
              source_database TEXT NOT NULL, imported TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS knowledge_imports(
              id TEXT PRIMARY KEY, source_database TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
              imported_count INTEGER NOT NULL, imported TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS ix_qsr_hash ON tasks(source_hash);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
            if "purpose" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN purpose TEXT NOT NULL DEFAULT 'recognition'")
            if "metadata_json" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
            initialize_schema(db)
            from .source_review_routes import initialize_source_review_schema
            initialize_source_review_schema(db)
            from ..source_review_packages import initialize_package_schema
            initialize_package_schema(db)
            db.execute("UPDATE tasks SET status='queued',stage='服务重启后继续处理' WHERE status='processing'")
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.database, timeout=15, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        return db

    def task(self, identifier):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (identifier,)).fetchone()
        return dict(row) if row else None

    def tasks(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM tasks ORDER BY created DESC").fetchall()
        return [dict(x) for x in rows]

    def update(self, identifier, **values):
        with self.connect() as db:
            db.execute(f"UPDATE tasks SET {','.join(f'{k}=?' for k in values)},updated=CURRENT_TIMESTAMP WHERE id=?", (*values.values(), identifier))
            db.commit()

    def versions(self, identifier):
        with self.connect() as db:
            rows = db.execute("SELECT task_id,version,kind,base_version,created FROM versions WHERE task_id=? ORDER BY version DESC", (identifier,)).fetchall()
        return [dict(x) for x in rows]

    def result(self, identifier, version=None):
        task = self.task(identifier)
        if not task:
            return None
        selected = version or task.get("current_version")
        if not selected:
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM versions WHERE task_id=? AND version=?", (identifier, selected)).fetchone()
        if not row:
            return None
        result = json.loads((self.data / row["result_file"]).read_text(encoding="utf-8"))
        return {"version": row["version"], "version_kind": row["kind"], "base_version": row["base_version"], "result": result}

    def save_version(self, identifier, result, kind, base_version=None, make_current=True, *, serialized=None):
        if serialized is not None and (kind != 'model' or not isinstance(serialized,bytes) or json.loads(serialized)!=result):
            raise ValueError('Verified model bytes do not match the result being saved')
        with self.connect() as db:
            version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM versions WHERE task_id=?", (identifier,)).fetchone()[0]
            relative = Path("results") / identifier / f"v{version}.json"
            target = self.data / relative; target.parent.mkdir(parents=True, exist_ok=True)
            if serialized is None:
                target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                target.write_bytes(serialized)
            db.execute("INSERT INTO versions(task_id,version,kind,result_file,base_version) VALUES(?,?,?,?,?)", (identifier, version, kind, str(relative), base_version))
            current = version if make_current else self.task(identifier).get("current_version")
            db.execute("UPDATE tasks SET current_version=?,latest_analysis_version=?,updated=CURRENT_TIMESTAMP WHERE id=?", (current, version if kind == "model" else self.task(identifier).get("latest_analysis_version"), identifier))
            db.commit()
        return version

    def reuse_analysis(self, identifier: str, source_identifier: str) -> bool:
        """Reuse a completed model analysis for an identical uploaded file.

        The uploaded original is still stored independently.  Only the immutable
        structure result is copied, so import/questionnaire workflow state and
        later manual edits remain isolated.
        """
        source_task = self.task(source_identifier)
        task = self.task(identifier)
        if not source_task or not task or task.get('current_version') is not None or source_task.get("source_hash") != task.get("source_hash"):
            return False
        analysis_version = source_task.get("latest_analysis_version") or source_task.get("current_version")
        with self.connect() as db:
            row=db.execute('SELECT kind,result_file FROM versions WHERE task_id=? AND version=?',(source_identifier,analysis_version)).fetchone()
        if not row or row['kind']!='model':return False
        try:
            raw=(self.data/row['result_file']).read_bytes()
            original_result=json.loads(raw)
        except (OSError,ValueError):return False
        if not analysis_is_current(original_result):
            return False
        requested_ocr = json_value(task.get("metadata_json"), {}).get("ocr_enabled", os.getenv("QSR_OCR_ENABLED", "false").lower() in {"true", "1", "yes"})
        if bool(original_result.get("processing_options", {}).get("ocr_enabled", False)) != bool(requested_ocr):
            return False
        if original_result.get("structure_status") == "original_only" or original_result.get("extraction_status") in {"failed", "original_only"}:
            return False
        authorized=None
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            registered=db.execute('SELECT 1 FROM source_review_packages WHERE source_hash=? LIMIT 1',(task['source_hash'],)).fetchone()
            reviewed=any(original_result.get(f'source_{kind}_review',{}).get('reviewed_blocks') or
                         original_result.get(f'source_{kind}_review_records') for kind in ('text','choice'))
            if registered:
                if requested_ocr is not True or not original_result.get('model') or original_result['model'].get('failed_batches'):return False
                try:actual_hash=hashlib.sha256((self.files/task['storage']).read_bytes()).hexdigest()
                except OSError:return False
                if actual_hash!=task['source_hash']:return False
                from ..source_review_packages import validated_analysis_package, bind_package
                authorized=validated_analysis_package(db,actual_hash,hashlib.sha256(raw).hexdigest(),original_result)
                if not authorized:return False
                bound=bind_package(db,identifier,actual_hash,requested_ocr)
                if bound.get('status')!='bound' or bound.get('package_id')!=authorized['package_id']:return False
                metadata=json_value(task.get('metadata_json'),{})
                metadata['source_analysis_reuse']={**authorized,'source_task':source_identifier,'source_model_version':analysis_version,
                    'knowledge_approval':False,'ocr_authorization_inherited':False}
                task['metadata_json']=json.dumps(metadata,ensure_ascii=False)
                db.execute('UPDATE tasks SET metadata_json=? WHERE id=?',(task['metadata_json'],identifier))
            elif reviewed:
                # Unregistered task-local transcription reviews never transfer.
                return False
            db.commit()
        result = refresh_result_responsibility(original_result)
        # Preserve the operator-approved immutable bytes. Current responsibility
        # policy is applied separately to this task's editable materialization.
        version = self.save_version(identifier, original_result if authorized else result, "model", None,
            make_current=True, serialized=raw if authorized else None)
        purpose = task.get("purpose")
        with self.connect() as db:
            if purpose == "import":
                materialize_import(db, task, version, result)
                status, stage = "needs_review", "已复用相同文件的识别结果，请审核问答"
            elif purpose == "questionnaire":
                materialize_questionnaire(db, task, result, json_value(task.get("metadata_json"), {}))
                save_snapshot(db, self.data, identifier, "recognized_and_matched")
                status, stage = "ready", "已复用相同文件的识别结果并完成匹配"
            else:
                status, stage = "needs_review", "已复用相同文件的识别结果"
        total = len(result.get("source_blocks") or [])
        self.update(identifier, status=status, stage=stage, processed=total, total=total, error=None)
        return True

    def submit(self, identifier, password="", reanalysis=False):
        with self.lock:
            if identifier in self.running:
                return
            self.running.add(identifier)
        self.pool.submit(self._run, identifier, password, reanalysis)

    def _run(self, identifier, password, reanalysis):
        task = self.task(identifier)
        try:
            from ..source_review_packages import bind_package
            requested_ocr = json_value(task.get('metadata_json'), {}).get('ocr_enabled',
                os.getenv('QSR_OCR_ENABLED', 'false').lower() in {'true','1','yes'})
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if requested_ocr is True and db.execute('SELECT 1 FROM source_review_packages WHERE source_hash=? LIMIT 1',(task['source_hash'],)).fetchone():
                    bind_package(db, identifier, hashlib.sha256((self.files/task['storage']).read_bytes()).hexdigest(), requested_ocr)
                db.commit()
            from .source_review_routes import saved_source_reviews
            source_reviews = saved_source_reviews(self, identifier)
            choice_reviews = saved_source_reviews(self, identifier, 'choice')
            reanalysis = reanalysis or bool(task.get('current_version'))
            self.update(identifier, status="processing", stage="准备原文提取", processed=0, total=None, error=None)
            result = recognize(
                self.files / task["storage"], self.cache, password=password, use_model=True,
                progress=lambda stage, detail, done, total: self.update(identifier, status="processing", stage=detail, processed=done or 0, total=total),
                ocr_enabled=json_value(task.get("metadata_json"), {}).get("ocr_enabled"),
                **({'source_text_reviews': source_reviews} if source_reviews else {}),
                **({'source_choice_reviews': choice_reviews} if choice_reviews else {}),
            )
            current = self.result(identifier)
            keep_manual = bool(reanalysis and current and current["version_kind"] == "manual")
            if reanalysis:
                with self.connect() as db:
                    edited_answers = db.execute('SELECT 1 FROM answers a JOIN questions q ON q.id=a.question_id WHERE q.task_id=? AND a.manual_override=1 LIMIT 1',(identifier,)).fetchone()
                    edited_import = db.execute("SELECT 1 FROM import_items i JOIN import_batches b ON b.id=i.batch_id WHERE b.task_id=? AND (i.updated_at!=i.created_at OR i.review_status!='pending') LIMIT 1",(identifier,)).fetchone()
                    edited_workflow = db.execute("SELECT 1 FROM task_versions WHERE task_id=? AND (kind IN ('manual_edit','fill_position_corrected','candidate_selected','task_context_updated','condition_decision') OR kind LIKE 'bulk_%') LIMIT 1", (identifier,)).fetchone()
                    batch = db.execute("SELECT id FROM import_batches WHERE task_id=?", (identifier,)).fetchone()
                    # updated_at has second precision. A fast edit can have the
                    # same timestamp as creation; compare persisted values to
                    # the immutable prior analysis rather than losing that edit.
                    if batch and current and not edited_import:
                        prior = current["result"]
                        drafts = extract_pending_entries(task, current["version"], prior)
                        baseline = {str(d.get("source", {}).get("field_id") or ""): d for d in drafts}
                        baseline_contexts = import_context_by_field(prior)
                        rows = list(db.execute("SELECT * FROM import_items WHERE batch_id=?", (batch["id"],)))
                        metadata = json_value(task.get("metadata_json"), {})
                        baseline_scope = {"text": str(metadata["applicable_scope"])} if metadata.get("applicable_scope") else {}
                        edited_import = len(rows) != len(drafts)
                        for row in rows:
                            origin = json_value(row["source_location_json"], {})
                            draft = baseline.get(str(origin.get("field_id") or ""))
                            if not draft:
                                edited_import = True
                                break
                            expected = {
                                "original_question": draft["question"], "canonical_question": draft["question"],
                                "answer": draft["answer"], "section": draft.get("section", ""),
                                "category": str(metadata.get("category") or ""), "language": "", "allow_auto_reuse": 0,
                                "context": baseline_contexts.get(str(draft.get("source", {}).get("field_id") or ""), {}).get("context_text", ""),
                            }
                            if any(row[key] != value for key, value in expected.items()) or json_value(row["applicable_scope_json"], {}) != baseline_scope:
                                edited_import = True
                                break
                keep_manual = keep_manual or bool(edited_answers or edited_import or edited_workflow)
            version = self.save_version(identifier, result, "model", current["version"] if current else None, make_current=not keep_manual)
            if keep_manual:
                # A new parser can change field IDs. Do not delete/recreate
                # edited workflow rows merely because the structure changed.
                self.update(identifier,status='needs_review',stage='新版识别已保存为独立版本；保留人工修改，请查看版本差异',processed=len(result['source_blocks']),total=len(result['source_blocks']),error=None)
                return
            final = "needs_review" if result["extraction_status"] != "complete" or any(q["review_status"] != "model_proposed" for q in result["questions"]) else "complete"
            task = self.task(identifier)
            with self.connect() as db:
                if task.get("purpose") == "import":
                    batch_id = materialize_import(db, task, version, result)
                    metadata = json_value(task.get("metadata_json"), {})
                    if metadata.get("trusted_source"):
                        db.execute("UPDATE import_items SET allow_auto_reuse=1 WHERE batch_id=? AND ambiguous=0 AND empty_answer=0 AND pairing_validation='valid'", (batch_id,))
                        ids = [row[0] for row in db.execute("SELECT id FROM import_items WHERE batch_id=? AND review_status='pending' AND ambiguous=0 AND empty_answer=0 AND pairing_validation='valid'", (batch_id,))]
                        approval = approve_import_items(db, batch_id, ids, "已授权历史来源")
                        remaining = db.execute("SELECT COUNT(*) FROM import_items WHERE batch_id=? AND review_status='pending'", (batch_id,)).fetchone()[0]
                        final = "needs_review" if remaining else "complete"
                    else:
                        approval = None
                        final = "needs_review"
                elif task.get("purpose") == "questionnaire":
                    materialize_questionnaire(db, task, result, json_value(task.get("metadata_json"), {}))
                    save_snapshot(db, self.data, identifier, "recognized_and_matched")
                    final = "ready" if result.get("questions") and result.get("structure_status") != "original_only" else "needs_review"
            stage = "识别、匹配完成，可以填写" if final == "ready" else "识别及已授权问答入库完成" if final == "complete" and task.get("purpose") == "import" else "识别完成，请核对" if final == "complete" else "部分完成，需要核对"
            if not result.get('questions'):
                final = 'needs_review'
                stage = '尚未识别到可填写字段，请查看原文与未读取内容；未完成自动填报'
            self.update(identifier, status=final, stage=stage, processed=len(result["source_blocks"]), total=len(result["source_blocks"]), error=None)
        except Exception as exc:
            self.update(identifier, status="failed", stage="处理失败", error=f"{type(exc).__name__}: {exc}")
        finally:
            password = ""
            with self.lock:
                self.running.discard(identifier)

    def resume(self):
        for task in reversed(self.tasks()):
            if task["status"] == "queued":
                self.submit(task["id"])


def create_app(data_dir: Path | None = None, access_key: str | None = None, auth_mode: str | None = None):
    selected_mode = auth_mode or ("key" if access_key is not None else os.getenv("QSR_AUTH_MODE", "local").lower())
    store = Store(
        data_dir or Path(os.getenv("QSR_DATA_DIR", ROOT / "data")),
        access_key if access_key is not None else os.getenv("QSR_ACCESS_KEY", ""),
        selected_mode,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        store.resume(); yield; store.pool.shutdown(wait=False, cancel_futures=False)

    app = FastAPI(title="问卷结构识别器", lifespan=lifespan)
    app.state.store = store

    def auth(request: Request, x_qsr_key: str = Header(default="")):
        if store.auth_mode == "local":
            client_host = request.client.host if request.client else ""
            host = request.headers.get("host", "").split(":", 1)[0].strip("[]").lower()
            origin_host = (urlparse(request.headers.get("origin", "")).hostname or "").lower()
            def loopback(value: str) -> bool:
                if value in {"localhost", "testclient", "testserver"}: return True
                try: return ipaddress.ip_address(value).is_loopback
                except ValueError: return False
            if not loopback(client_host) or not loopback(host) or (origin_host and not loopback(origin_host)):
                raise HTTPException(403, "本地免登录模式只接受本机来源")
            return
        if not secrets.compare_digest(x_qsr_key, store.key):
            raise HTTPException(401, "访问密钥不正确")

    def refresh_all_questionnaire_matches(db: sqlite3.Connection, reason: str) -> int:
        """Rebuild derived matches after a reviewed knowledge change."""
        task_ids = [row[0] for row in db.execute("SELECT id FROM questionnaire_tasks")]
        for task_id in task_ids:
            match_questionnaire(db, task_id, replace_auto=True)
            save_snapshot(db, store.data, task_id, reason)
        return len(task_ids)

    @app.get("/api/capabilities", dependencies=[Depends(auth)])
    def capabilities():
        from ..parser_core.ocr import available as ocr_available
        return {"formats": [".docx", ".pdf", ".doc"], "max_upload_mb": int(os.getenv("QSR_MAX_UPLOAD_MB", "30")), "ocr": {"enabled": os.getenv("QSR_OCR_ENABLED", "false").lower() in {"true", "1", "yes"}, "ready": ocr_available(), "mode": "PDF pages without usable native text"}, "model": health(), "auth_mode": store.auth_mode, "deployment_mode": "single_unit", "schema_version": "questionnaire-workflow-v5-single-unit"}

    @app.get("/api/tasks", dependencies=[Depends(auth)])
    def tasks():
        return store.tasks()

    async def save_upload(file: UploadFile, allowed: set[str], purpose: str, metadata: dict | None = None, submit: bool = True, password: str = ""):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in allowed:
            raise HTTPException(422, f"当前入口支持：{', '.join(sorted(allowed))}")
        maximum = int(os.getenv("QSR_MAX_UPLOAD_MB", "30")) * 1024 * 1024
        temporary = store.data / ("incoming-" + uuid.uuid4().hex + suffix)
        digest, size = hashlib.sha256(), 0
        try:
            with temporary.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > maximum:
                        raise HTTPException(413, "文件超过配置限制")
                    digest.update(chunk); output.write(chunk)
            if not size:
                raise HTTPException(422, "上传文件为空")
            checksum = digest.hexdigest(); metadata_text = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
            with store.connect() as db:
                duplicate = db.execute("SELECT id FROM tasks WHERE source_hash=? AND purpose=? AND metadata_json=? ORDER BY created DESC LIMIT 1", (checksum, purpose, metadata_text)).fetchone()
                reusable = db.execute(
                    "SELECT id FROM tasks WHERE source_hash=? AND latest_analysis_version IS NOT NULL ORDER BY updated DESC LIMIT 1",
                    (checksum,),
                ).fetchone()
            if duplicate:
                existing = store.task(duplicate["id"])
                envelope = store.result(existing['id'], existing.get('latest_analysis_version'))
                stale = bool(envelope and not analysis_is_current(envelope['result']))
                retry = existing['status'] == 'failed'
                if (stale or retry) and existing['status'] not in {'queued', 'processing'}:
                    store.update(existing['id'], status='queued', stage='同一原文件使用新版继续处理；人工版本保留', error=None)
                    store.submit(existing['id'], password, reanalysis=True)
                return {**store.task(duplicate["id"]), "duplicate": True, 'new_analysis_requested': stale or retry}
            identifier, storage = uuid.uuid4().hex, uuid.uuid4().hex + suffix
            os.replace(temporary, store.files / storage)
            with store.connect() as db:
                db.execute("INSERT INTO tasks(id,filename,storage,source_hash,size,status,stage,purpose,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)", (identifier, Path(file.filename or storage).name, storage, checksum, size, "queued", "等待处理", purpose, metadata_text)); db.commit()
            if submit:
                reused = bool(reusable and store.reuse_analysis(identifier, reusable["id"]))
                if not reused:
                    store.submit(identifier, password)
            return store.task(identifier)
        finally:
            temporary.unlink(missing_ok=True)

    @app.post("/api/tasks", status_code=202, dependencies=[Depends(auth)])
    async def create_task(file: UploadFile = File(...), password: str = Form(default=""), ocr_enabled: bool | None = Form(default=None)):
        try:
            value = await save_upload(file, {".docx", ".pdf", ".doc"}, "recognition", {"ocr_enabled": ocr_enabled} if ocr_enabled is not None else {}, password=password)
            return value
        finally:
            password = ""

    @app.get("/api/tasks/{identifier}", dependencies=[Depends(auth)])
    def task(identifier: str):
        value = store.task(identifier)
        if not value: raise HTTPException(404, "任务不存在")
        value["versions"] = store.versions(identifier)
        return value

    @app.get("/api/tasks/{identifier}/result", dependencies=[Depends(auth)])
    def result(identifier: str, version: int | None = None):
        value = store.result(identifier, version)
        if not value: raise HTTPException(404, "结果尚未生成")
        return value

    @app.post("/api/tasks/{identifier}/corrections", dependencies=[Depends(auth)])
    def correct(identifier: str, payload: dict):
        current = store.result(identifier, payload.get("base_version"))
        if not current: raise HTTPException(404, "基础版本不存在")
        try:
            corrected = validate_correction(current["result"], payload.get("result") or {})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        version = store.save_version(identifier, corrected, "manual", current["version"], make_current=True)
        store.update(identifier, status="needs_review", stage="人工修正已保存为新版本")
        return {"version": version, "kind": "manual"}

    @app.post("/api/tasks/{identifier}/reanalyze", status_code=202, dependencies=[Depends(auth)])
    def reanalyze(identifier: str, payload: dict | None = Body(default=None)):
        task = store.task(identifier)
        if not task: raise HTTPException(404, "任务不存在")
        if task["status"] == "processing": raise HTTPException(409, "任务正在处理")
        if payload and "ocr_enabled" in payload:
            if type(payload["ocr_enabled"]) is not bool: raise HTTPException(422, "ocr_enabled 必须是布尔值")
            metadata = json_value(task.get("metadata_json"), {})
            metadata["ocr_enabled"] = payload["ocr_enabled"]
            store.update(identifier, metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True))
        store.update(identifier, status="queued", stage="等待重新识别", processed=0, total=None, error=None)
        store.submit(identifier, reanalysis=True)
        return store.task(identifier)

    @app.get("/api/tasks/{identifier}/export.json", dependencies=[Depends(auth)])
    def export_json(identifier: str, version: int | None = None):
        value = store.result(identifier, version)
        if not value: raise HTTPException(404, "结果尚未生成")
        path = store.results / identifier / f"export-v{value['version']}.json"
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(value["result"], ensure_ascii=False, indent=2), encoding="utf-8")
        return FileResponse(path, filename=f"questionnaire-structure-v{value['version']}.json")

    @app.get("/api/tasks/{identifier}/report.html", dependencies=[Depends(auth)], response_class=HTMLResponse)
    def report(identifier: str, version: int | None = None):
        value = store.result(identifier, version)
        if not value: raise HTTPException(404, "结果尚未生成")
        return HTMLResponse(html_report(value["result"]))

    @app.get("/api/tasks/{identifier}/original", dependencies=[Depends(auth)])
    def original(identifier: str):
        task = store.task(identifier)
        if not task: raise HTTPException(404, "任务不存在")
        return FileResponse(store.files / task["storage"], filename=task["filename"])

    @app.get("/api/tasks/{identifier}/filled.docx", dependencies=[Depends(auth)])
    def filled_docx(identifier: str, version: int | None = None):
        task = store.task(identifier)
        envelope = store.result(identifier, version)
        if not task or not envelope:
            raise HTTPException(404, "任务或结果不存在")
        if Path(task["storage"]).suffix.lower() != ".docx":
            raise HTTPException(422, "原模板回填当前只支持 DOCX")
        with store.connect() as db:
            for field in knowledge_filled_fields(envelope["result"]):
                source = field.get("knowledge_source") or {}
                row = db.execute("SELECT version,payload FROM knowledge_entries WHERE id=?", (source.get("knowledge_id"),)).fetchone()
                if not row:
                    raise HTTPException(409, "引用的知识条目不存在")
                item = json.loads(row["payload"])
                if row["version"] != source.get("answer_version") or item.get("review") != "approved" or item.get("active") is not True or item.get("answer") != source.get("answer"):
                    raise HTTPException(409, "引用的知识条目版本、状态或答案已变化，请重新检查")
        target = store.results / identifier / f"filled-v{envelope['version']}.docx"
        try:
            fill_docx_template(store.files / task["storage"], envelope["result"], target)
        except TemplateFillError as exc:
            raise HTTPException(422, str(exc)) from exc
        safe_stem = Path(task["filename"]).stem
        return FileResponse(target, filename=f"{safe_stem}-filled-v{envelope['version']}.docx")

    @app.get("/api/knowledge", dependencies=[Depends(auth)])
    def knowledge(review: str | None = None, search: str = "", category: str = ""):
        with store.connect() as db:
            rows = db.execute("SELECT * FROM knowledge_records ORDER BY updated_at DESC").fetchall()
            values = []
            for row in rows:
                item = dict(row)
                from ..scoped_aliases import signature as equivalence_signature
                item["equivalence_review_signature"] = equivalence_signature(item)
                if review and item["review_status"] != review: continue
                if category and item["category"] != category: continue
                if search and search.lower() not in (item["canonical_question"] + " " + item["answer"]).lower(): continue
                item["applicable_scope"] = json_value(item.pop("applicable_scope_json"), {})
                item["source_location"] = json_value(item.pop("source_location_json"), {})
                item["aliases"] = [dict(x) for x in db.execute("SELECT * FROM knowledge_aliases WHERE knowledge_id=?", (item["id"],))]
                item["versions"] = [dict(x) for x in db.execute("SELECT id,version,answer,reviewer,created_at FROM knowledge_versions WHERE knowledge_id=? ORDER BY version DESC", (item["id"],))]
                semantic_row = db.execute("SELECT * FROM semantic_annotations WHERE owner_type='knowledge' AND owner_id=?", (item["id"],)).fetchone(); item["semantic"] = annotation(dict(semantic_row)) if semantic_row else {}
                values.append(item)
        return values

    @app.post("/api/tasks/{identifier}/knowledge-drafts", dependencies=[Depends(auth)])
    def knowledge_drafts(identifier: str, payload: dict | None = None):
        task = store.task(identifier)
        if not task:
            raise HTTPException(404, "任务不存在")
        requested_version = (payload or {}).get("version")
        envelope = store.result(identifier, requested_version)
        if not envelope:
            raise HTTPException(409, "识别结果尚未生成")
        drafts = extract_pending_entries(task, envelope["version"], envelope["result"])
        created = 0
        with store.connect() as db:
            for draft in drafts:
                before = db.total_changes
                db.execute(
                    "INSERT OR IGNORE INTO knowledge_entries(id,version,payload,source_database) VALUES(?,?,?,?)",
                    (draft["id"], draft["version"], json.dumps(draft, ensure_ascii=False), f"document-task:{identifier}"),
                )
                created += db.total_changes - before
            db.commit()
        return {"task_id": identifier, "task_version": envelope["version"], "extracted": len(drafts), "created": created, "existing": len(drafts) - created, "review": "pending", "entries": drafts}

    @app.get("/api/dashboard", dependencies=[Depends(auth)])
    def dashboard():
        with store.connect() as db:
            scalar = lambda query, args=(): db.execute(query, args).fetchone()[0]
            unanswered_rows = db.execute("SELECT a.answer_text,a.selected_options_json,q.form_definition_json FROM questions q JOIN answers a ON a.question_id=q.id").fetchall()
            unanswered = sum(
                not str(row["answer_text"] or "").strip()
                and not json_value(row["selected_options_json"], [])
                and json_value(row["form_definition_json"], {}).get("field_type") != "repeating_record"
                and json_value(row["form_definition_json"], {}).get("record_slot_status") != "reserved"
                for row in unanswered_rows
            )
            return {
                "questionnaires": scalar("SELECT COUNT(*) FROM questionnaire_tasks"),
                "historical_imports": scalar("SELECT COUNT(*) FROM import_batches"),
                "pending_imports": scalar("SELECT COUNT(*) FROM import_items WHERE review_status='pending'"),
                "knowledge": scalar("SELECT COUNT(*) FROM knowledge_records WHERE review_status='approved' AND active=1"),
                "exports": scalar("SELECT COUNT(*) FROM exports"),
                "unanswered": unanswered,
                "recent_questionnaires": [dict(x) for x in db.execute("SELECT * FROM questionnaire_tasks ORDER BY updated_at DESC LIMIT 8")],
                "recent_exports": [dict(x) for x in db.execute("SELECT * FROM exports ORDER BY created_at DESC LIMIT 8")],
            }

    @app.post("/api/imports", status_code=202, dependencies=[Depends(auth)])
    async def create_import(file: UploadFile = File(...), category: str = Form(default=""), applicable_scope: str = Form(default=""), trusted_source: bool = Form(default=False), ocr_enabled: bool | None = Form(default=None)):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix in {".csv", ".xlsx"}:
            payload = await file.read()
            maximum = int(os.getenv("QSR_MAX_UPLOAD_MB", "30")) * 1024 * 1024
            if not payload: raise HTTPException(422, "上传文件为空")
            if len(payload) > maximum: raise HTTPException(413, "文件超过配置限制")
            try:
                with store.connect() as db:
                    batch_id, count = import_tabular(db, Path(file.filename or "data" + suffix).name, payload)
                    if trusted_source:
                        db.execute("UPDATE import_items SET allow_auto_reuse=1 WHERE batch_id=? AND empty_answer=0 AND ambiguous=0 AND pairing_validation='valid'", (batch_id,))
                        ids = [row[0] for row in db.execute("SELECT id FROM import_items WHERE batch_id=? AND review_status='pending' AND empty_answer=0 AND ambiguous=0 AND pairing_validation='valid'", (batch_id,))]
                        approve_import_items(db, batch_id, ids, "已授权历史来源")
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return {"id": batch_id, "status": "review", "items": count}
        metadata = {"category": category, "applicable_scope": applicable_scope, "trusted_source": trusted_source}
        if ocr_enabled is not None: metadata["ocr_enabled"] = ocr_enabled
        task = await save_upload(file, {".docx", ".pdf", ".doc"}, "import", metadata, submit=False)
        stamp = now()
        with store.connect() as db:
            row = db.execute("SELECT id FROM import_batches WHERE task_id=?", (task["id"],)).fetchone()
            batch_id = row["id"] if row else uuid.uuid4().hex
            if not row:
                db.execute("INSERT INTO import_batches VALUES(?,?,?,?,?,?,?)", (batch_id, task["id"], task["filename"], suffix, "processing", stamp, stamp)); db.commit()
        if not task.get("duplicate"):
            with store.connect() as db:
                reusable = db.execute(
                    "SELECT id FROM tasks WHERE source_hash=? AND id<>? AND latest_analysis_version IS NOT NULL ORDER BY updated DESC LIMIT 1",
                    (task["source_hash"], task["id"]),
                ).fetchone()
            if not (reusable and store.reuse_analysis(task["id"], reusable["id"])):
                store.submit(task["id"])
        return {**store.task(task["id"]), "batch_id": batch_id, "duplicate": bool(task.get("duplicate"))}

    @app.post("/api/imports/manual", dependencies=[Depends(auth)])
    def manual_import(payload: dict):
        question, answer = str(payload.get("question") or "").strip(), str(payload.get("answer") or "").strip()
        if not question: raise HTTPException(422, "问题不能为空")
        stamp, batch_id, item_id = now(), uuid.uuid4().hex, uuid.uuid4().hex
        requested_scope = single_unit_scope(payload.get("applicable_scope"))
        with store.connect() as db:
            db.execute("INSERT INTO import_batches VALUES(?,?,?,?,?,?,?)", (batch_id, None, "手工录入", "manual", "review", stamp, stamp))
            db.execute("INSERT INTO import_items(id,batch_id,original_question,canonical_question,answer,section,category,context,applicable_scope_json,source_file,source_location_json,language,ambiguous,empty_answer,allow_auto_reuse,review_status,answer_presence,pairing_validation,validation_reasons_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (item_id, batch_id, question, str(payload.get("canonical_question") or question), answer, str(payload.get("section") or ""), str(payload.get("category") or ""), str(payload.get("context") or ""), json.dumps(requested_scope, ensure_ascii=False), "手工录入", "{}", str(payload.get("language") or ""), 0, 0 if answer else 1, 1 if payload.get("allow_auto_reuse") else 0, "pending", "present" if answer else "blank", "valid", "[]", stamp, stamp))
            upsert_annotation(db, "import_item", item_id, str(payload.get("canonical_question") or question), str(payload.get("section") or ""), str(payload.get("context") or ""), scope=requested_scope)
            db.commit()
        return {"id": batch_id, "item_id": item_id, "status": "review"}

    @app.get("/api/imports", dependencies=[Depends(auth)])
    def imports():
        with store.connect() as db:
            rows = []
            for row in db.execute("SELECT b.*,COUNT(i.id) item_count,SUM(CASE WHEN i.review_status='pending' THEN 1 ELSE 0 END) pending_count FROM import_batches b LEFT JOIN import_items i ON i.batch_id=b.id GROUP BY b.id ORDER BY b.created_at DESC"):
                rows.append(dict(row))
            return rows

    @app.get("/api/imports/{batch_id}", dependencies=[Depends(auth)])
    def import_detail(batch_id: str):
        with store.connect() as db:
            batch = db.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
            if not batch: raise HTTPException(404, "导入批次不存在")
            items = []
            for row in db.execute("SELECT * FROM import_items WHERE batch_id=? ORDER BY created_at,id", (batch_id,)):
                item = dict(row); item["applicable_scope"] = json_value(item.pop("applicable_scope_json"), {}); item["source_location"] = json_value(item.pop("source_location_json"), {}); item["validation_reasons"] = json_value(item.pop("validation_reasons_json"), []); items.append(item)
            if batch["task_id"]:
                block_order = {row["block_id"]: row["order_no"] for row in db.execute("SELECT block_id,order_no FROM document_blocks WHERE task_id=?", (batch["task_id"],))}
                def source_order(item):
                    refs = item["source_location"].get("question_refs") or []
                    return (block_order.get(refs[0].get("block_id"), 10**9) if refs else 10**9, item["id"])
                items.sort(key=source_order)
            task_state = store.task(batch["task_id"]) if batch["task_id"] else None
            version_diff = None
            if task_state:
                versions = store.versions(batch["task_id"]); task_state["versions"] = versions
                if len(versions) >= 2:
                    newer, older = store.result(batch["task_id"], versions[0]["version"]), store.result(batch["task_id"], versions[1]["version"])
                    def indexed(envelope):
                        values = {}
                        def visit(field):
                            refs = field.get("label_refs") or field.get("question_refs") or []
                            key = tuple((r.get("block_id"), r.get("start"), r.get("end")) for r in refs) or ((field.get("field_id"), 0, 0),)
                            values[key] = {"question": field.get("question_text_original", ""), "answer": field.get("answer_text_original", ""), "validation": field.get("pairing_validation", "unvalidated")}
                            for child in field.get("children") or []: visit(child)
                        for field in envelope["result"].get("questions") or []: visit(field)
                        return values
                    old, new = indexed(older), indexed(newer); shared = old.keys() & new.keys()
                    changed = [{"before": old[k], "after": new[k]} for k in shared if old[k] != new[k]]
                    version_diff = {"from": older["version"], "to": newer["version"], "added": len(new.keys()-old.keys()), "removed": len(old.keys()-new.keys()), "changed": len(changed), "examples": changed[:12]}
            stats = {"total": len(items), "valid": sum(x["pairing_validation"] == "valid" and not x["ambiguous"] for x in items), "needs_review": sum(x["pairing_validation"] != "valid" or x["ambiguous"] for x in items), "blank": sum(bool(x["empty_answer"]) for x in items)}
            return {"batch": dict(batch), "task": task_state, "stats": stats, "version_diff": version_diff, "items": items}

    @app.patch("/api/import-items/{item_id}", dependencies=[Depends(auth)])
    def edit_import_item(item_id: str, payload: dict):
        allowed = {"canonical_question", "original_question", "answer", "section", "category", "context", "language", "allow_auto_reuse", "review_status"}
        values = {key: payload[key] for key in allowed if key in payload}
        if "applicable_scope" in payload: values["applicable_scope_json"] = json.dumps(single_unit_scope(payload["applicable_scope"]), ensure_ascii=False)
        if "answer" in values:
            values["empty_answer"] = 0 if str(values["answer"]).strip() else 1
            values["answer_presence"] = "present" if str(values["answer"]).strip() else "blank"
        if {"canonical_question", "answer"} & values.keys():
            values["pairing_validation"] = "needs_review"; values["ambiguous"] = 1; values["allow_auto_reuse"] = 0
            values["validation_reasons_json"] = json.dumps(["审核内容已修改，请重新对照原文确认问题与答案关系。"], ensure_ascii=False)
        if not values: raise HTTPException(422, "没有可更新字段")
        values["updated_at"] = now()
        with store.connect() as db:
            current = db.execute("SELECT pairing_validation FROM import_items WHERE id=?", (item_id,)).fetchone()
            if not current: raise HTTPException(404, "审核条目不存在")
            if values.get("allow_auto_reuse") and current["pairing_validation"] != "valid": raise HTTPException(409, "问题与答案配对未通过校验，不能允许自动复用")
            db.execute(f"UPDATE import_items SET {','.join(f'{key}=?' for key in values)} WHERE id=?", (*values.values(), item_id))
            updated = db.execute("SELECT * FROM import_items WHERE id=?", (item_id,)).fetchone(); upsert_annotation(db, "import_item", item_id, updated["canonical_question"], updated["section"], updated["context"], scope=json_value(updated["applicable_scope_json"], {})); db.commit()
        return {"id": item_id, "saved": True}

    @app.post("/api/import-items/{item_id}/confirm-pairing", dependencies=[Depends(auth)])
    def confirm_import_pairing(item_id: str):
        stamp = now()
        with store.connect() as db:
            row = db.execute("SELECT * FROM import_items WHERE id=?", (item_id,)).fetchone()
            if not row: raise HTTPException(404, "审核条目不存在")
            if SHARED_TARGET_RESTRICTION in json_value(row['source_location_json'], {}).get('reuse_restrictions', []):
                raise HTTPException(409, SHARED_TARGET_REASON)
            if not row["original_question"].strip() or not row["canonical_question"].strip() or not row["answer"].strip(): raise HTTPException(422, "问题、标准问题和答案必须非空，才能确认配对")
            db.execute("UPDATE import_items SET pairing_validation='valid',ambiguous=0,validation_reasons_json=?,updated_at=? WHERE id=?", (json.dumps(["审核人已对照原文确认问题与答案配对。"], ensure_ascii=False), stamp, item_id)); db.commit()
        return {"id": item_id, "pairing_validation": "valid", "saved": True}

    @app.post("/api/imports/{batch_id}/reanalyze", status_code=202, dependencies=[Depends(auth)])
    def reanalyze_import(batch_id: str, payload: dict | None = Body(default=None)):
        if payload and "ocr_enabled" in payload and type(payload["ocr_enabled"]) is not bool:
            raise HTTPException(422, "ocr_enabled 必须是布尔值")
        with store.connect() as db:
            batch = db.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
            if not batch: raise HTTPException(404, "导入批次不存在")
            if not batch["task_id"]: raise HTTPException(422, "该导入没有可重新解析的原文件")
            db.execute("UPDATE import_batches SET status='processing',updated_at=? WHERE id=?", (now(), batch_id)); db.commit()
        if payload and "ocr_enabled" in payload:
            task = store.task(batch["task_id"])
            metadata = json_value(task.get("metadata_json"), {})
            metadata["ocr_enabled"] = payload["ocr_enabled"]
            store.update(task["id"], metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True))
        store.submit(batch["task_id"], reanalysis=True)
        return {"batch_id": batch_id, "task_id": batch["task_id"], "status": "processing"}

    @app.delete("/api/import-items/{item_id}", dependencies=[Depends(auth)])
    def delete_import_item(item_id: str):
        with store.connect() as db:
            row = db.execute("SELECT review_status FROM import_items WHERE id=?", (item_id,)).fetchone()
            if not row: raise HTTPException(404, "审核条目不存在")
            if row["review_status"] == "approved": raise HTTPException(409, "已入库条目请到问答库停用，不能从审核批次删除")
            db.execute("DELETE FROM import_items WHERE id=?", (item_id,)); db.commit()
        return {"deleted": True}

    @app.post("/api/imports/{batch_id}/approve", dependencies=[Depends(auth)])
    def approve_import(batch_id: str, payload: dict):
        with store.connect() as db:
            try:
                result = approve_import_items(db, batch_id, list(payload.get("item_ids") or []), str(payload.get("reviewer") or ""))
                result["rematched_tasks"] = refresh_all_questionnaire_matches(db, "knowledge_import_approved")
                return result
            except sqlite3.IntegrityError as exc: raise HTTPException(409, f"入库冲突：{exc}") from exc

    @app.post("/api/questionnaires", status_code=202, dependencies=[Depends(auth)])
    async def create_questionnaire(
        file: UploadFile = File(...), customer_name: str = Form(default=""), questionnaire_name: str = Form(default=""),
        product: str = Form(default=""),
        business_scope: str = Form(default=""), notes: str = Form(default=""), password: str = Form(default=""),
        ocr_enabled: bool | None = Form(default=None),
    ):
        metadata = {"customer_name": customer_name, "questionnaire_name": questionnaire_name, "company": "", "factory": "", "product": product, "business_scope": business_scope, "notes": notes}
        if ocr_enabled is not None: metadata["ocr_enabled"] = ocr_enabled
        try: return await save_upload(file, {".docx", ".pdf", ".doc"}, "questionnaire", metadata, password=password)
        finally: password = ""

    @app.get("/api/questionnaires", dependencies=[Depends(auth)])
    def questionnaires():
        with store.connect() as db:
            return [dict(x) for x in db.execute("SELECT qt.*,d.original_filename,d.file_type FROM questionnaire_tasks qt JOIN documents d ON d.id=qt.document_id ORDER BY qt.updated_at DESC")]

    @app.get("/api/questionnaires/{identifier}", dependencies=[Depends(auth)])
    def questionnaire(identifier: str):
        with store.connect() as db:
            try: value = task_snapshot(db, identifier)
            except ValueError as exc: raise HTTPException(404, str(exc)) from exc
        source_task = store.task(identifier) or {}
        analysis = store.result(identifier)
        result = analysis["result"] if analysis else {}
        latest_version = source_task.get("latest_analysis_version")
        latest = analysis if analysis and latest_version == analysis["version"] else (store.result(identifier, latest_version) if latest_version else None)
        value["recognition"] = {
            "analysis_current": bool(analysis and analysis_is_current(result)),
            "analysis_version": analysis["version"] if analysis else None,
            "latest_analysis_version": latest_version,
            "latest_analysis_current": bool(latest and analysis_is_current(latest["result"])),
            "status": source_task.get("status", ""),
            "stage": source_task.get("stage", ""),
            "processed": source_task.get("processed", 0),
            "total": source_task.get("total"),
            "updated": source_task.get("updated", ""),
            "source_blocks": len(result.get("source_blocks") or []),
            "structured_questions": len(result.get("questions") or []),
            "warnings": result.get("warnings") or [],
        }
        return value

    @app.patch("/api/questionnaires/{identifier}/context", dependencies=[Depends(auth)])
    def edit_questionnaire_context(identifier: str, payload: dict):
        allowed = {"customer_name", "product", "business_scope", "notes"}
        values = {key: str(payload[key] or "") for key in allowed if key in payload}
        if not values: raise HTTPException(422, "没有可更新的任务对象信息")
        stamp = now(); values["updated_at"] = stamp
        with store.connect() as db:
            if not db.execute("SELECT 1 FROM questionnaire_tasks WHERE id=?", (identifier,)).fetchone(): raise HTTPException(404, "问卷任务不存在")
            db.execute(f"UPDATE questionnaire_tasks SET {','.join(f'{key}=?' for key in values)} WHERE id=?", (*values.values(), identifier))
            task_row = dict(db.execute("SELECT * FROM questionnaire_tasks WHERE id=?", (identifier,)).fetchone())
            for row in db.execute("SELECT id,question_text,section,form_definition_json FROM questions WHERE task_id=?", (identifier,)).fetchall(): upsert_annotation(db, "question", row["id"], row["question_text"], row["section"], json_value(row["form_definition_json"],{}).get("semantic_context",""), task=task_row)
            counts = match_questionnaire(db, identifier, replace_auto=True)
            version, _ = save_snapshot(db, store.data, identifier, "task_context_updated")
        return {"saved": True, "version": version, **counts}

    @app.patch("/api/questionnaires/{identifier}/questions/{question_id}/condition", dependencies=[Depends(auth)])
    def edit_local_condition(identifier: str, question_id: str, payload: dict):
        from ..local_condition_scopes import save_decision
        with store.connect() as db:
            row=db.execute('SELECT * FROM questions WHERE id=? AND task_id=?',(question_id,identifier)).fetchone()
            if not row:raise HTTPException(404,'问题不存在')
            try:save_decision(db,dict(row),payload.get('decision'),str(payload.get('reviewer') or '本机用户'),now())
            except ValueError as exc:raise HTTPException(422,str(exc)) from exc
            match_questionnaire(db,identifier,replace_auto=True)
            version,snapshot=save_snapshot(db,store.data,identifier,'condition_decision')
        return {'saved':True,'version':version,**snapshot}

    @app.patch("/api/questionnaires/{identifier}/questions/{question_id}", dependencies=[Depends(auth)])
    def edit_question(identifier: str, question_id: str, payload: dict):
        stamp = now()
        with store.connect() as db:
            row = db.execute("SELECT q.id,q.section,q.input_type,q.form_definition_json,qt.company,qt.factory,qt.product FROM questions q JOIN questionnaire_tasks qt ON qt.id=q.task_id WHERE q.id=? AND q.task_id=?", (question_id, identifier)).fetchone()
            if not row: raise HTTPException(404, "问题不存在")
            if "question_text" in payload:
                db.execute("UPDATE questions SET question_text=?,review_status='needs_review',updated_at=? WHERE id=?", (str(payload["question_text"]), stamp, question_id))
                upsert_annotation(db, "question", question_id, str(payload["question_text"]), row["section"], json_value(row["form_definition_json"],{}).get("semantic_context",""), task=dict(row))
                db.execute("DELETE FROM matches WHERE question_id=?", (question_id,))
            answer_changed = any(key in payload for key in ("answer_text", "selected_options"))
            if answer_changed or "notes" in payload:
                current = db.execute("SELECT * FROM answers WHERE question_id=?", (question_id,)).fetchone()
                answer = str(payload.get("answer_text", current["answer_text"]))
                selected_values=payload.get("selected_options", json_value(current["selected_options_json"], []))
                definition=json_value(row['form_definition_json'],{})
                if answer_changed:
                    from ..manual_choice_input import reconcile_manual_choice
                    options=[dict(x) for x in db.execute('SELECT text,value FROM answer_options WHERE question_id=? ORDER BY option_order',(question_id,))]
                    try:
                        answer,selected_values=reconcile_manual_choice(payload,options,row['input_type'],current['answer_text'],json_value(current['selected_options_json'],[]))
                    except ValueError as exc:raise HTTPException(422,str(exc)) from exc
                if answer_changed and (answer.strip() or selected_values) and definition.get('conditional_group'):
                    raise HTTPException(422,'原文条件标题没有答复位置；请设置本次适用条件并填写子项，不生成原文没有的答案')
                format_application = None
                if answer_changed:
                    from ..native_text_constraints import prepare_saved_text
                    try: answer, format_application = prepare_saved_text(answer, definition)
                    except ValueError as exc: raise HTTPException(422, str(exc)) from exc
                if answer_changed and (answer.strip() or selected_values) and definition.get('fill_responsibility') in {'customer','reviewer','shared'}:
                    raise HTTPException(422,'此位置属于客户、审核或共同填写区域；请先核对原文并在填写区域预览中明确更正填写责任。可以清空错误内容。')
                selected = json.dumps(selected_values, ensure_ascii=False)
                notes_value = str(payload.get("notes", current["notes"]))
                origin = "manual" if answer_changed else current["origin"]
                override = 1 if answer_changed else current["manual_override"]
                db.execute("UPDATE answers SET answer_text=?,selected_options_json=?,notes=?,origin=?,manual_override=?,confirmed=?,updated_at=? WHERE question_id=?", (answer, selected, notes_value, origin, override, 1 if answer_changed else current["confirmed"], stamp, question_id))
                if answer_changed:
                    from ..native_text_constraints import record_application
                    record_application(db, question_id, format_application, 'manual', stamp)
                if answer_changed and db.execute('SELECT 1 FROM questions WHERE parent_id=? LIMIT 1',(question_id,)).fetchone():
                    match_questionnaire(db,identifier,replace_auto=True)
            db.execute("UPDATE questionnaire_tasks SET workflow_status='editing',save_status='saving',updated_at=? WHERE id=?", (stamp, identifier)); db.commit()
            version, snapshot = save_snapshot(db, store.data, identifier, "manual_edit")
        saved_question = next(q for q in snapshot['questions'] if q['id'] == question_id)
        return {"saved": True, "version": version, "last_saved_at": snapshot["task"]["last_saved_at"],
                'answer_text': saved_question['answer_text'], 'native_format_application': saved_question['native_format_application'],
                'saved_question': saved_question, 'stats': snapshot['stats'],
                "dependent_questions": [q for q in snapshot['questions'] if q.get('parent_id') == question_id]}

    @app.get("/api/questionnaires/{identifier}/fill-regions", dependencies=[Depends(auth)])
    def fill_regions(identifier: str):
        with store.connect() as db:
            task = db.execute("SELECT qt.*,d.source_hash,d.original_filename FROM questionnaire_tasks qt JOIN documents d ON d.id=qt.document_id WHERE qt.id=?", (identifier,)).fetchone()
            if not task: raise HTTPException(404, "问卷任务不存在")
            questions = [dict(x) for x in db.execute("SELECT id,field_id,ordinal,section,question_text,form_definition_json FROM questions WHERE task_id=? ORDER BY ordinal", (identifier,))]
            blocks = [dict(x) for x in db.execute("SELECT block_id,kind,order_no,text,location_json,structure_json,controls_json FROM document_blocks WHERE task_id=? ORDER BY order_no", (identifier,))]
            semantic_rows = {row["owner_id"]: annotation(dict(row)) for row in db.execute("SELECT * FROM semantic_annotations WHERE owner_type='question'")}
        fields = []
        target_map, label_map = {}, {}
        for question in questions:
            definition = json_value(question.pop("form_definition_json"), {})
            question["semantic"] = semantic_rows.get(question["id"], {})
            question["form_definition"] = definition; fields.append(question)
            for ref in definition.get("label_refs") or []: label_map.setdefault(ref.get("block_id"), []).append(question["id"])
            for target in definition.get("fill_targets") or []: target_map.setdefault(target.get("block_id"), []).append(question["id"])
        tables, paragraphs, page_regions = {}, [], {}
        for block in blocks:
            location, structure, controls = json_value(block["location_json"], {}), json_value(block["structure_json"], {}), json_value(block["controls_json"], [])
            table_id = structure.get("table_id")
            labels, fills = label_map.get(block["block_id"], []), target_map.get(block["block_id"], [])
            if structure.get("is_merge_continuation"):
                region_status = "merge_continuation"
            elif fills:
                region_status = "mapped_fill"
            elif labels:
                region_status = "fixed_label"
            elif structure.get("blank_state") == "blank":
                region_status = "unmapped_blank"
            elif controls:
                region_status = "unmapped_control"
            else:
                region_status = "fixed_content"
            cell = {"block_id": block["block_id"], "text": block["text"], "location": location, "structure": structure, "controls": controls, "label_for": labels, "fill_for": fills, "region_status": region_status}
            if location.get("type") == "pdf":
                page_regions.setdefault(location.get("page"), []).append(cell)
            if not table_id:
                if location.get("type") == "docx" and (labels or fills or controls): paragraphs.append(cell)
                continue
            tables.setdefault(table_id, {"table_id": table_id, "table": location.get("table"), "rows": {}})["rows"].setdefault(structure.get("row_start"), []).append(cell)
        output_tables = []
        for table in tables.values():
            table["rows"] = [{"row": row, "cells": sorted(cells, key=lambda x: x["structure"].get("grid_col_start", 0))} for row, cells in sorted(table["rows"].items())]
            output_tables.append(table)
        all_targets = [target for field in fields for target in (field["form_definition"].get("fill_targets") or [])]
        target_ids = [target.get("target_id") for target in all_targets if target.get("target_id")]
        summary = {
            "physical_cells": sum(len(row["cells"]) for table in output_tables for row in table["rows"]),
            "mapped_targets": len(target_ids),
            "unique_targets": len(set(target_ids)),
            "located_fields": sum(bool(field["form_definition"].get("fill_targets")) for field in fields),
            "unlocated_fields": sum(not field["form_definition"].get("fill_targets") for field in fields),
            "unmapped_blank_cells": sum(cell["region_status"] == "unmapped_blank" for table in output_tables for row in table["rows"] for cell in row["cells"]),
        }
        pages = []
        if Path(task["original_filename"]).suffix.lower() == ".pdf":
            import fitz
            stored = store.task(identifier)
            with fitz.open(store.files / stored["storage"]) as pdf:
                if not pdf.needs_pass:
                    for number in sorted(n for n in page_regions if isinstance(n, int) and 1 <= n <= len(pdf)):
                        page = pdf[number-1]
                        pages.append({"page": number, "width": page.rect.width, "height": page.rect.height,
                            "image_url": f"/api/questionnaires/{identifier}/source-pages/{number}.png",
                            "regions": page_regions[number]})
        return {"task": {"id": task["id"], "filename": task["original_filename"], "source_hash": task["source_hash"]}, "fields": fields, "tables": output_tables, "paragraphs": paragraphs, "pages": pages, "summary": summary}

    @app.get("/api/questionnaires/{identifier}/source-pages/{number}.png", dependencies=[Depends(auth)])
    def source_page(identifier: str, number: int):
        import fitz
        stored = store.task(identifier)
        if Path(stored["filename"]).suffix.lower() != ".pdf": raise HTTPException(422, "此文件没有 PDF 原页预览")
        with fitz.open(store.files / stored["storage"]) as pdf:
            if pdf.needs_pass: raise HTTPException(422, "加密原件需重新提供密码；密码不会长期保存")
            if not 1 <= number <= len(pdf): raise HTTPException(404, "原文页码不存在")
            destination = store.cache / 'page-preview' / stored['source_hash'] / f'{number}.png'
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                page = pdf[number-1]
                scale = min(2, 1800/max(page.rect.width,page.rect.height))
                image = page.get_pixmap(matrix=fitz.Matrix(scale,scale), alpha=False)
                image.save(destination)
        return FileResponse(destination, media_type="image/png", headers={"Cache-Control":"private, max-age=3600"})

    @app.patch("/api/questionnaires/{identifier}/questions/{question_id}/fill-position", dependencies=[Depends(auth)])
    def correct_fill_position(identifier: str, question_id: str, payload: dict):
        responsibility_values = {"our_side", "customer", "reviewer", "shared", "unknown"}
        required_values = {"required", "optional", "unknown"}
        with store.connect() as db:
            row = db.execute("SELECT q.*,d.source_hash FROM questions q JOIN questionnaire_tasks qt ON qt.id=q.task_id JOIN documents d ON d.id=qt.document_id WHERE q.id=? AND q.task_id=?", (question_id, identifier)).fetchone()
            if not row: raise HTTPException(404, "问题不存在")
            definition = json_value(row["form_definition_json"], {})
            action = str(payload.get("action") or "")
            if "fill_responsibility" in payload:
                value = str(payload["fill_responsibility"])
                if value not in responsibility_values: raise HTTPException(422, "填写责任不合法")
                definition["fill_responsibility"] = value
            if "required_status" in payload:
                value = str(payload["required_status"])
                if value not in required_values: raise HTTPException(422, "必填状态不合法")
                definition["required_status"] = value
            if action == "not_fill_region":
                definition["fill_targets"] = []; definition["allowed_write_methods"] = []; definition["fillable_status"] = "not_fill_region"; definition["auto_fill_eligible"] = False
            elif action == "needs_split":
                definition["validation_status"] = "needs_review"; definition["auto_fill_eligible"] = False
                definition.setdefault("validation_reasons", []).append("用户标记：该位置包含多个子字段，需要拆分")
            elif action == "confirm_mapping":
                if not definition.get("fill_targets"): raise HTTPException(422, "没有可确认的填写位置")
                definition["validation_status"] = "valid"
                definition["auto_fill_eligible"] = definition.get("fill_responsibility") == "our_side" and all(x.get("writable") for x in definition.get("fill_targets") or [])
            elif action == 'confirm_template_placeholder':
                candidates = definition.get('template_placeholder_candidates') or []
                if not candidates: raise HTTPException(422, '该字段没有经过来源定位的模板占位候选')
                if definition.get('fill_responsibility') in {'customer','reviewer','shared'}:
                    raise HTTPException(422, '不能替换客户、审核人或共同签署区域的模板文字')
                lookup = {}
                for block_row in db.execute('SELECT * FROM document_blocks WHERE task_id=?',(identifier,)):
                    block = dict(block_row)
                    for key in ('location','structure','controls'): block[key] = json_value(block.get(key+'_json'), [] if key=='controls' else {})
                    lookup[block['block_id']] = block
                definition['confirmed_template_placeholders'] = candidates
                definition['mapping_review_status'] = 'human_corrected'
                refs = confirmed_template_ranges(definition,lookup,row['source_hash'])
                if len(refs) != len(candidates): raise HTTPException(409, '模板占位文字或原文件版本已变化，须重新识别')
                for other in db.execute('SELECT id,source_refs_json,answer_regions_json FROM questions WHERE task_id=? AND id!=?',(identifier,question_id)):
                    for ref in refs:
                        for area in json_value(other['source_refs_json'],[])+json_value(other['answer_regions_json'],[]):
                            if area.get('block_id') == ref['block_id'] and max(area.get('start',0),ref['start']) < min(area.get('end',0),ref['end']):
                                raise HTTPException(422, '该模板文字同时属于另一字段，需先修正结构，不能覆盖')
                targets = [build_fill_target(ref,lookup,row['source_hash'],str(definition.get('parser_version','')),definition) for ref in refs]
                if any(not target or not target.get('writable') for target in targets):
                    raise HTTPException(422, '模板占位位置未通过安全写入校验')
                definition.update(fill_targets=targets,allowed_write_methods=['replace_text_range'],fillable_status='fillable',
                    validation_status='valid',validation_reasons=[],location_evidence='human_confirmed_template_placeholder')
            elif action == "assign_target":
                block_id = str(payload.get("block_id") or "")
                block_row = db.execute("SELECT block_id,kind,text,location_json,structure_json,controls_json FROM document_blocks WHERE task_id=? AND block_id=?", (identifier, block_id)).fetchone()
                if not block_row: raise HTTPException(422, "所选 Word 位置不存在或不属于当前文件")
                block = dict(block_row); block["location"] = json_value(block.pop("location_json"), {}); block["structure"] = json_value(block.pop("structure_json"), {}); block["controls"] = json_value(block.pop("controls_json"), [])
                if block["structure"].get("is_merge_continuation") or block["structure"].get("blank_state") == "merge_continuation":
                    raise HTTPException(422, "合并单元格的延续位置不能作为独立填写区域")
                for other in db.execute("SELECT id,question_text,source_refs_json FROM questions WHERE task_id=? AND id!=?", (identifier, question_id)):
                    if any(ref.get("block_id") == block_id for ref in json_value(other["source_refs_json"], [])):
                        raise HTTPException(422, f"该位置是另一字段的固定标签：{other['question_text']}")
                text_value = str(block.get("text") or "")
                ref = {"block_id": block_id, "start": 0, "end": len(text_value)}
                target = build_fill_target(ref, {block_id: block}, row["source_hash"], str(definition.get("parser_version") or "human-mapping"))
                if not target or not target.get("writable"):
                    raise HTTPException(422, (target or {}).get("validation_reason") or "该位置没有受支持的安全写入方式")
                definition["fill_targets"] = [target]
                definition["allowed_write_methods"] = [target["write_method"]]
                definition["fillable_status"] = "fillable"
                definition["location_evidence"] = "human_selected_physical_region"
                definition["validation_status"] = "valid"
                definition["validation_reasons"] = []
                definition["auto_fill_eligible"] = definition.get("fill_responsibility") == "our_side"
            elif action and action not in {"not_fill_region", "needs_split", "confirm_mapping", "assign_target", "confirm_template_placeholder"}:
                raise HTTPException(422, "未知的填写位置纠正动作")
            # Changing a physical mapping or owner does not approve a signature,
            # resolve an if-condition, or license writing into a customer area.
            mapping_eligible = bool(
                definition.get('validation_status') == 'valid' and definition.get('fill_targets') and
                all(t.get('writable') for t in definition['fill_targets']) and
                definition.get('fill_responsibility') == 'our_side' and
                definition.get('field_type') not in {'manual_signature','repeating_record','composite'} and
                not definition.get('response_format_conflict') and
                not definition.get('requires_current_person_confirmation') and
                definition.get('record_identity_status') != 'ambiguous' and
                not definition.get('requires_signatory_confirmation') and definition.get('record_slot_status') != 'reserved')
            definition['auto_fill_eligible'] = mapping_eligible and not definition.get('applicability_condition')
            definition['auto_fill_eligible_if_condition_met'] = mapping_eligible and bool(definition.get('applicability_condition'))
            definition["mapping_review_status"] = "human_corrected"
            definition["source_hash"] = row["source_hash"]
            refs = [x.get("ref") for x in definition.get("fill_targets") or []]
            stamp = now()
            db.execute("UPDATE questions SET form_definition_json=?,answer_regions_json=?,review_status=?,updated_at=? WHERE id=?", (json.dumps(definition, ensure_ascii=False), json.dumps(refs, ensure_ascii=False), "ready" if definition.get("validation_status") == "valid" else "needs_review", stamp, question_id))
            version, snapshot = save_snapshot(db, store.data, identifier, "fill_position_corrected")
        return {"saved": True, "version": version, "form_definition": definition, "last_saved_at": snapshot["task"]["last_saved_at"]}

    @app.post("/api/questionnaires/{identifier}/rematch", dependencies=[Depends(auth)])
    def rematch(identifier: str):
        with store.connect() as db:
            try:
                counts = match_questionnaire(db, identifier, replace_auto=True)
                version, _ = save_snapshot(db, store.data, identifier, "rematch")
            except ValueError as exc: raise HTTPException(404, str(exc)) from exc
        return {"version": version, **counts}

    @app.post("/api/questionnaires/{identifier}/bulk", dependencies=[Depends(auth)])
    def questionnaire_bulk(identifier: str, payload: dict):
        question_ids = list(dict.fromkeys(payload.get("question_ids") or [])); action = payload.get("action")
        confirm_all_answered = action == "confirm" and bool(payload.get("all_answered"))
        if not question_ids and not confirm_all_answered: raise HTTPException(422, "请先选择问题")
        stamp = now()
        with store.connect() as db:
            if confirm_all_answered:
                valid = [x[0] for x in db.execute("SELECT q.id FROM questions q JOIN answers a ON a.question_id=q.id WHERE q.task_id=? AND TRIM(a.answer_text)!=''", (identifier,))]
            else:
                placeholders = ",".join("?" for _ in question_ids)
                valid = [x[0] for x in db.execute(f"SELECT id FROM questions WHERE task_id=? AND id IN ({placeholders})", (identifier, *question_ids))]
            if action == "clear":
                db.executemany("UPDATE answers SET answer_text='',selected_options_json='[]',origin='manual',manual_override=1,confirmed=1,updated_at=? WHERE question_id=?", [(stamp, qid) for qid in valid])
            elif action == "confirm":
                db.executemany("UPDATE answers SET confirmed=1,updated_at=? WHERE question_id=?", [(stamp, qid) for qid in valid])
            else: raise HTTPException(422, "批量动作仅支持 clear 或 confirm")
            db.commit(); version, _ = save_snapshot(db, store.data, identifier, f"bulk_{action}")
        return {"updated": len(valid), "version": version}

    @app.post("/api/questionnaires/{identifier}/questions/{question_id}/use-match/{match_id}", dependencies=[Depends(auth)])
    def use_match(identifier: str, question_id: str, match_id: str):
        with store.connect() as db:
            row = db.execute("SELECT m.*,k.answer,k.answer_version,k.original_question,k.source_location_json,k.section AS historical_section,k.context AS historical_context FROM matches m JOIN knowledge_records k ON k.id=m.knowledge_id WHERE m.id=? AND m.task_id=? AND m.question_id=? AND k.review_status='approved' AND k.active=1", (match_id, identifier, question_id)).fetchone()
            if not row: raise HTTPException(404, "候选答案不存在或已失效")
            question=db.execute('SELECT * FROM questions WHERE id=? AND task_id=?',(question_id,identifier)).fetchone()
            definition=json_value(question['form_definition_json'],{}) if question else {}
            from ..workflow import whole_record_coverage_reason
            incomplete_record = whole_record_coverage_reason(definition,dict(row))
            if incomplete_record:
                raise HTTPException(422,incomplete_record)
            from ..workflow import resolved_applicability
            from ..form_part_routes import incompatible_historical_route, historical_route
            historical=historical_route(db,json_value(row['source_location_json'],{}),row['historical_section'],row['historical_context'])
            route=definition.get('source_part_route')
            if resolved_applicability(db,dict(question),definition)!='applicable' or incompatible_historical_route(route,historical):
                raise HTTPException(422,'当前分部不适用、条件未确认或历史来源属于不同分部。')
            if definition.get('fill_responsibility') in {'customer','reviewer','shared'} or definition.get('field_type')=='manual_signature' or definition.get('requires_signatory_confirmation') or definition.get('requires_current_person_confirmation'):
                raise HTTPException(422,'此位置属于受保护填写区或本次签署/参与人信息，不能直接套用历史答案；请核对来源和填写责任。')
            try: selected = knowledge_choice_selection(db, question_id, row['answer'])
            except ValueError as exc: raise HTTPException(422, str(exc)) from exc
            from ..native_text_constraints import prepare_saved_text, record_application
            try: saved_answer, format_application = prepare_saved_text(row['answer'], definition)
            except ValueError as exc: raise HTTPException(422, str(exc)) from exc
            stamp = now(); db.execute("UPDATE answers SET answer_text=?,selected_options_json=?,origin='reviewed_knowledge',manual_override=0,match_id=?,confirmed=1,updated_at=? WHERE question_id=?", (saved_answer, json.dumps(selected, ensure_ascii=False), match_id, stamp, question_id))
            record_application(db, question_id, format_application, 'reviewed_knowledge', stamp, row['knowledge_id'], row['knowledge_version'])
            db.commit()
            version, _ = save_snapshot(db, store.data, identifier, "candidate_selected")
        return {"saved": True, "version": version}

    @app.post("/api/questionnaires/{identifier}/questions/{question_id}/confirm-equivalence", dependencies=[Depends(auth)])
    def confirm_question_equivalence(identifier: str, question_id: str, payload: dict):
        from ..scoped_aliases import confirm_equivalence
        with store.connect() as db:
            try: result = confirm_equivalence(db,identifier,question_id,payload,now())
            except RuntimeError as exc:raise HTTPException(409,str(exc)) from exc
            except ValueError as exc:raise HTTPException(422,str(exc)) from exc
            db.commit()
            matched = match_questionnaire(db,identifier,replace_auto=True)
            version,_ = save_snapshot(db,store.data,identifier,'scoped_equivalence_confirmed')
        return {**result,'saved':True,'version':version,'matching':matched}

    @app.post("/api/questionnaires/{identifier}/knowledge-drafts", dependencies=[Depends(auth)])
    def questionnaire_knowledge_drafts(identifier: str, payload: dict):
        ids = set(payload.get("question_ids") or [])
        stamp, batch_id = now(), uuid.uuid4().hex
        with store.connect() as db:
            db.execute("INSERT INTO import_batches VALUES(?,?,?,?,?,?,?)", (batch_id, None, f"问卷任务 {identifier} 人工答案", "task_answers", "review", stamp, stamp))
            count = 0
            for row in db.execute("SELECT q.*,a.answer_text,a.selected_options_json,a.origin,a.manual_override FROM questions q JOIN answers a ON a.question_id=q.id WHERE q.task_id=?", (identifier,)):
                selected=json_value(row['selected_options_json'], [])
                if selected:
                    from ..choice_identity import selected_labels
                    options=[dict(x) for x in db.execute('SELECT text,value FROM answer_options WHERE question_id=? ORDER BY option_order',(row['id'],))]
                    try:answer='\n'.join(selected_labels(options,selected))
                    except ValueError as exc:raise HTTPException(422,str(exc))
                else:answer=row['answer_text'].strip()
                if row["id"] not in ids or not row["manual_override"] or not answer: continue
                item_id = uuid.uuid4().hex
                definition = json_value(row['form_definition_json'],{})
                source_record = {'question_id':row['id']}
                if definition.get('whole_record_requirement'):
                    source_record['whole_record_requirement'] = definition['whole_record_requirement']
                db.execute("INSERT INTO import_items(id,batch_id,original_question,canonical_question,answer,section,category,context,applicable_scope_json,source_file,source_location_json,language,ambiguous,empty_answer,allow_auto_reuse,review_status,answer_presence,pairing_validation,validation_reasons_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (item_id, batch_id, row["question_text"], row["question_text"], row["answer_text"], row["section"], "", "", "{}", f"任务 {identifier}", json.dumps({"question_id": row["id"]}, ensure_ascii=False), "", 0, 0, 0, "pending", "present", "valid", "[]", stamp, stamp))
                upsert_annotation(db, "import_item", item_id, row["question_text"], row["section"]); count += 1
                context = definition.get('semantic_context', '')
                db.execute('UPDATE import_items SET answer=?,context=?,source_location_json=? WHERE id=?',
                           (answer, context, json.dumps(source_record,ensure_ascii=False), item_id))
                upsert_annotation(db, 'import_item', item_id, row['question_text'], row['section'], context)
            db.commit()
        return {"batch_id": batch_id, "created": count, "review_status": "pending"}

    @app.get("/api/questionnaires/{identifier}/export-summary", dependencies=[Depends(auth)])
    def export_summary(identifier: str):
        with store.connect() as db:
            try: return task_snapshot(db, identifier)["stats"]
            except ValueError as exc: raise HTTPException(404, str(exc)) from exc

    @app.post("/api/questionnaires/{identifier}/exports", dependencies=[Depends(auth)])
    def create_export(identifier: str, payload: dict):
        mode = str(payload.get("mode") or "standard")
        if mode not in {"standard", "template"}: raise HTTPException(422, "导出模式必须是 standard 或 template")
        with store.connect() as db:
            from ..manual_choice_input import validate_saved_choice
            try:preview=task_snapshot(db,identifier)
            except ValueError as exc:raise HTTPException(404,str(exc)) from exc
            try:
                for question in preview['questions']:validate_saved_choice(question)
            except ValueError as exc:raise HTTPException(422,str(exc)) from exc
            try: version, snapshot = save_snapshot(db, store.data, identifier, "export_snapshot")
            except ValueError as exc: raise HTTPException(404, str(exc)) from exc
        task = store.task(identifier); envelope = store.result(identifier)
        if not task or not envelope: raise HTTPException(409, "原始识别结果不存在")
        export_id, stamp = uuid.uuid4().hex, now()
        safe_stem = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", Path(task["filename"]).stem)[:80]
        filename = f"{safe_stem}_{mode}_v{version}.docx"
        relative = Path("results") / identifier / "exports" / filename
        target = store.data / relative
        warnings: list[str] = []
        writeback_plan: list[dict] = []
        layout_adjustments: list[dict] = []
        try:
            if mode == "standard":
                standard_questionnaire_docx(snapshot, target)
            else:
                suffix = Path(task["filename"]).suffix.lower()
                if suffix not in {".docx", ".doc"}: raise TemplateFillError("PDF 输入请使用标准 Word 导出")
                filled_result = apply_answers_to_result(envelope["result"], snapshot)
                template = store.files / task["storage"]
                if suffix == ".doc":
                    from ..parser_core.router import converted_doc_template
                    if not (filled_result.get("extraction_metadata") or {}).get("converted_template_hash"):
                        raise TemplateFillError("此旧版 DOC 尚未保存转换模板指纹，请使用新版重新识别或标准 Word 导出")
                    template = converted_doc_template(template, store.cache / "converted")
                writeback_plan, warnings = fill_workflow_docx_template(template, filled_result, target)
                metadata_path = target.with_suffix('.metadata.json')
                if metadata_path.exists():
                    export_metadata = json.loads(metadata_path.read_text(encoding='utf8'))
                    if export_metadata.get('export_sha256') != hashlib.sha256(target.read_bytes()).hexdigest():
                        raise TemplateFillError('导出版面核验元数据与文件指纹不一致')
                    layout_adjustments = export_metadata.get('layout_adjustments', [])
        except TemplateFillError as exc:
            raise HTTPException(422, str(exc)) from exc
        with store.connect() as db:
            db.execute("INSERT INTO exports VALUES(?,?,?,?,?,?,?,?)", (export_id, identifier, version, mode, str(relative), filename, json.dumps(warnings, ensure_ascii=False), stamp))
            db.execute("UPDATE questionnaire_tasks SET workflow_status='exported',updated_at=? WHERE id=?", (stamp, identifier)); db.commit()
        return {"id": export_id, "version": version, "mode": mode, "filename": filename, "warnings": warnings, "layout_adjustments": layout_adjustments, "writeback_plan": writeback_plan, "stats": snapshot["stats"], "download_url": f"/api/exports/{export_id}/file"}

    @app.get("/api/exports/{export_id}/file", dependencies=[Depends(auth)])
    def export_file(export_id: str):
        with store.connect() as db:
            row = db.execute("SELECT * FROM exports WHERE id=?", (export_id,)).fetchone()
        if not row: raise HTTPException(404, "导出记录不存在")
        path = store.data / row["file_path"]
        if not path.is_file(): raise HTTPException(410, "导出文件已丢失")
        return FileResponse(path, filename=row["filename"], media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    @app.post("/api/knowledge/manual", dependencies=[Depends(auth)])
    def add_knowledge(payload: dict):
        question, answer = str(payload.get("canonical_question") or "").strip(), str(payload.get("answer") or "").strip()
        if not question or not answer: raise HTTPException(422, "标准问题和答案不能为空")
        stamp, identifier = now(), uuid.uuid4().hex
        scope_value = single_unit_scope(payload.get("applicable_scope"))
        scope = json.dumps(scope_value, ensure_ascii=False)
        with store.connect() as db:
            db.execute("INSERT INTO knowledge_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (identifier, question, str(payload.get("original_question") or question), answer, str(payload.get("category") or ""), str(payload.get("section") or ""), str(payload.get("context") or ""), scope, "手工录入", "{}", str(payload.get("language") or ""), "approved" if payload.get("approved") else "pending", 1 if payload.get("approved") else 0, 1 if payload.get("approved") else 0, 1, stamp, stamp))
            upsert_annotation(db, "knowledge", identifier, question, str(payload.get("section") or ""), str(payload.get("context") or ""), scope=scope_value)
            db.execute("INSERT INTO knowledge_versions VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, identifier, 1, answer, json.dumps(payload, ensure_ascii=False), str(payload.get("reviewer") or ""), stamp))
            for alias in payload.get("aliases") or []:
                if str(alias).strip(): db.execute("INSERT INTO knowledge_aliases VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, identifier, str(alias).strip(), str(payload.get("context") or ""), scope, 1))
            db.commit()
            rematched = refresh_all_questionnaire_matches(db, "knowledge_manual_changed")
        return {"id": identifier, "review_status": "approved" if payload.get("approved") else "pending", "rematched_tasks": rematched}

    @app.patch("/api/knowledge/{identifier}", dependencies=[Depends(auth)])
    def edit_knowledge(identifier: str, payload: dict):
        with store.connect() as db:
            row = db.execute("SELECT * FROM knowledge_records WHERE id=?", (identifier,)).fetchone()
            if not row: raise HTTPException(404, "知识条目不存在")
            current, stamp = dict(row), now()
            answer = str(payload.get("answer", current["answer"]))
            version = current["answer_version"] + (1 if answer != current["answer"] else 0)
            allowed = {"canonical_question", "original_question", "answer", "category", "section", "context", "language", "review_status", "active", "allow_auto_reuse"}
            values = {key: payload[key] for key in allowed if key in payload}
            if "applicable_scope" in payload: values["applicable_scope_json"] = json.dumps(single_unit_scope(payload["applicable_scope"]), ensure_ascii=False)
            values.update({"answer_version": version, "updated_at": stamp})
            db.execute(f"UPDATE knowledge_records SET {','.join(f'{key}=?' for key in values)} WHERE id=?", (*values.values(), identifier))
            refreshed = db.execute("SELECT * FROM knowledge_records WHERE id=?", (identifier,)).fetchone(); upsert_annotation(db, "knowledge", identifier, refreshed["canonical_question"], refreshed["section"], refreshed["context"], scope=json_value(refreshed["applicable_scope_json"], {}))
            if version != current["answer_version"]:
                updated = dict(current); updated.update(values)
                db.execute("INSERT INTO knowledge_versions VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, identifier, version, answer, json.dumps(updated, ensure_ascii=False), str(payload.get("reviewer") or ""), stamp))
            for alias in payload.get("aliases_add") or []:
                if str(alias).strip(): db.execute("INSERT INTO knowledge_aliases VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, identifier, str(alias).strip(), str(payload.get("context", current["context"])), values.get("applicable_scope_json", current["applicable_scope_json"]), 1))
            db.commit()
            rematched = refresh_all_questionnaire_matches(db, "knowledge_record_changed")
        return {"id": identifier, "answer_version": version, "saved": True, "rematched_tasks": rematched}

    from .source_review_routes import install_source_review_routes
    install_source_review_routes(app, store, auth)
    app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
    return app


def app_from_env():
    return create_app()
