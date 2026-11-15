-- Explicit reviewed question equivalence; retains original source and knowledge version.
CREATE TABLE IF NOT EXISTS knowledge_alias_scopes(
 alias_id TEXT PRIMARY KEY, section TEXT NOT NULL, context TEXT NOT NULL,
 applicable_scope_json TEXT NOT NULL, knowledge_signature TEXT NOT NULL,
 target_evidence_json TEXT NOT NULL, reviewer TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(alias_id) REFERENCES knowledge_aliases(id));
