-- Rename ``downloads.sidecar_dir`` to ``downloads.item_dir`` to reflect
-- the new per-item folder layout (CLAUDE.md §5, §6 — Track A).
--
-- The sidecars/ subfolder is gone: every artifact for one capture now
-- lives directly under ``/downloads/{case_slug}/{stem}/``. The DB column
-- still stores a relative path; only the name changes.

ALTER TABLE downloads RENAME COLUMN sidecar_dir TO item_dir;
