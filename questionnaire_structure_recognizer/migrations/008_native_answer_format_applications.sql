-- Incremental, idempotent audit storage only. Existing business values and
-- knowledge/source versions are never transformed by this migration.
CREATE TABLE IF NOT EXISTS answer_format_applications(
 id TEXT PRIMARY KEY,
 question_id TEXT NOT NULL,
 created_at TEXT NOT NULL,
 evidence_json TEXT NOT NULL,
 FOREIGN KEY(question_id) REFERENCES questions(id)
);
CREATE INDEX IF NOT EXISTS ix_answer_format_question
 ON answer_format_applications(question_id,created_at);
