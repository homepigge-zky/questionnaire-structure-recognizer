-- Incremental semantic field annotations shared by imported knowledge and tasks.
-- This migration does not modify review, active, reuse authorization, or answers.
CREATE TABLE IF NOT EXISTS semantic_annotations(
  owner_type TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  normalized_text TEXT NOT NULL DEFAULT '',
  canonical_field_key TEXT NOT NULL DEFAULT '',
  canonical_label TEXT NOT NULL DEFAULT '',
  entity_type TEXT NOT NULL DEFAULT '',
  entity_id TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT '',
  qualifiers_json TEXT NOT NULL DEFAULT '{}',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  normalization_version TEXT NOT NULL,
  PRIMARY KEY(owner_type, owner_id)
);

CREATE INDEX IF NOT EXISTS ix_semantic_field
ON semantic_annotations(owner_type, canonical_field_key, entity_type, entity_id);
