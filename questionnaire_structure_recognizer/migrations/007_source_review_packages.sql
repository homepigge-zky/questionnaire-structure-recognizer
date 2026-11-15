-- Explicit local approval to reuse transcription ONLY for identical bytes.
CREATE TABLE IF NOT EXISTS source_review_packages(
 id TEXT PRIMARY KEY,
 source_hash TEXT NOT NULL,
 package_digest TEXT NOT NULL UNIQUE,
 payload_json TEXT NOT NULL,
 approved_by TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_source_review_packages_source ON source_review_packages(source_hash);
CREATE TABLE IF NOT EXISTS source_review_package_bindings(
 task_id TEXT PRIMARY KEY,
 package_id TEXT NOT NULL REFERENCES source_review_packages(id),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
