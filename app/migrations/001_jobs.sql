-- Capsule library schema, version 1.
-- Adds a persistent job queue so captures survive app restarts and
-- intermittent connections (CLAUDE.md plan §U1).

CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,                -- uuid4
    case_id             INTEGER NOT NULL REFERENCES cases(id),
    source_url          TEXT NOT NULL,                   -- url_submitted
    status              TEXT NOT NULL,                   -- 'queued' | 'running' | 'paused' | 'retrying' | 'failed_permanent' | 'done' | 'cancelled'
    phase               TEXT,                            -- last in-flight phase: 'classifying' | 'snapshotting' | 'downloading' | 'finalizing'
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,                            -- short stderr tail or message
    last_error_kind     TEXT,                            -- errors.* i18n key
    last_error_severity TEXT,                            -- 'transient' | 'permanent' | 'internal'
    next_retry_at       TEXT,                            -- ISO 8601 UTC; null = run now
    progress_json       TEXT NOT NULL DEFAULT '{}',      -- last persisted progress snapshot
    classification_json TEXT,                            -- classifier result for retries
    result_json         TEXT,                            -- terminal success payload
    error_json          TEXT,                            -- terminal failure payload
    created_at          TEXT NOT NULL,                   -- ISO 8601 UTC
    updated_at          TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_case_id  ON jobs(case_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status   ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_next_run ON jobs(next_retry_at);
