-- Append-only, task-local transcription reviews. No knowledge approval is implied.
CREATE TABLE IF NOT EXISTS source_text_reviews(
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  base_version INTEGER NOT NULL,
  source_hash TEXT NOT NULL,
  review_digest TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(task_id, sequence), UNIQUE(task_id, review_digest)
);
CREATE INDEX IF NOT EXISTS ix_source_reviews_task ON source_text_reviews(task_id, sequence);
