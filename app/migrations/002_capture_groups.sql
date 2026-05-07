-- Capsule library schema, version 2.
-- Adds the parent record for a "capture group" — one user-pasted URL — so
-- the snapshot / archive / media tasks can be persisted, retried, and
-- re-fetched independently while still sharing one logical library item.
-- Plan §U6 / Phase D.

CREATE TABLE IF NOT EXISTS capture_groups (
    id           TEXT PRIMARY KEY,        -- uuid4
    case_id      INTEGER NOT NULL REFERENCES cases(id),
    source_url   TEXT NOT NULL,
    download_id  INTEGER REFERENCES downloads(id),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_capture_groups_case_id
    ON capture_groups(case_id);
CREATE INDEX IF NOT EXISTS idx_capture_groups_download_id
    ON capture_groups(download_id);

-- Forward-only migration: add columns to ``jobs`` so existing tasks keep
-- working ('full' is the legacy single-job behaviour) and new
-- snapshot/archive/media tasks can be enqueued independently.
ALTER TABLE jobs ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'full';
ALTER TABLE jobs ADD COLUMN capture_group_id TEXT REFERENCES capture_groups(id);

CREATE INDEX IF NOT EXISTS idx_jobs_capture_group
    ON jobs(capture_group_id);
CREATE INDEX IF NOT EXISTS idx_jobs_task_kind
    ON jobs(task_kind);
