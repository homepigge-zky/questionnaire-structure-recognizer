-- Additive storage for native Word controls and reviewed fill-position maps.
-- Existing answers, tasks, source files and knowledge records are preserved.
ALTER TABLE document_blocks ADD COLUMN controls_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE document_blocks ADD COLUMN style_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE questions ADD COLUMN form_definition_json TEXT NOT NULL DEFAULT '{}';
