-- Current applicability is separate from original/knowledge answers.
CREATE TABLE IF NOT EXISTS questionnaire_condition_decisions (
 question_id TEXT PRIMARY KEY REFERENCES questions(id), task_id TEXT NOT NULL,
 source_hash TEXT NOT NULL, header_ref_json TEXT NOT NULL, source_text TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('unknown','applicable','not_applicable')),
 reviewer TEXT NOT NULL, updated_at TEXT NOT NULL
);
