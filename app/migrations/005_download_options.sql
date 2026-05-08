-- Capsule library schema, version 5.
-- CLAUDE.md §15 (v0.7): per-job download-modification + reliability counters.
-- Stored as JSON because the field set is small and the orchestrator only
-- ever reads the whole blob; column-per-knob would just bloat the schema
-- without a corresponding query benefit.

ALTER TABLE jobs ADD COLUMN download_options_json TEXT NOT NULL DEFAULT '{}';
