-- Capsule library schema, version 0.
-- Source of truth: CLAUDE.md §8 (audit_log) and §9 (cases, downloads).
-- Forward-only migrations; never edit a migration after it has shipped.

CREATE TABLE IF NOT EXISTS cases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    settings_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS downloads (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id             INTEGER NOT NULL REFERENCES cases(id),
    job_uuid            TEXT NOT NULL UNIQUE,
    capture_kind        TEXT NOT NULL,                  -- 'media' | 'page_only'
    source_url          TEXT NOT NULL,                  -- url_submitted
    final_url           TEXT,                           -- url_final
    platform            TEXT NOT NULL,
    video_id            TEXT,                           -- nullable for page_only
    url_hash            TEXT NOT NULL,                  -- sha256(final_url)[:12]
    uploader            TEXT,
    title               TEXT NOT NULL,                  -- sanitised
    title_original      TEXT NOT NULL,                  -- raw, untruncated
    upload_date         TEXT,                           -- ISO 8601 date
    capture_date        TEXT NOT NULL,                  -- ISO 8601 datetime UTC
    relative_path       TEXT,                           -- relative to /downloads (null for page_only)
    sidecar_dir         TEXT NOT NULL,                  -- relative to /downloads
    file_size_bytes     INTEGER,
    md5                 TEXT,
    sha256              TEXT,
    duration_seconds    INTEGER,
    ytdlp_version       TEXT NOT NULL,
    chromium_version    TEXT NOT NULL,
    browsertrix_version TEXT NOT NULL,
    app_version         TEXT NOT NULL,
    signing_key_fp      TEXT NOT NULL,
    meta_json           TEXT NOT NULL,
    UNIQUE(case_id, capture_kind, url_hash)
);

CREATE INDEX IF NOT EXISTS idx_downloads_case_id     ON downloads(case_id);
CREATE INDEX IF NOT EXISTS idx_downloads_platform    ON downloads(platform);
CREATE INDEX IF NOT EXISTS idx_downloads_capture_date ON downloads(capture_date);
CREATE INDEX IF NOT EXISTS idx_downloads_video_id    ON downloads(video_id);
CREATE INDEX IF NOT EXISTS idx_downloads_url_hash    ON downloads(url_hash);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,                        -- ISO 8601 UTC
    action        TEXT NOT NULL,                        -- e.g. 'capture.started'
    case_id       INTEGER,
    download_id   INTEGER,
    actor         TEXT NOT NULL,                        -- 'system' | 'user'
    details_json  TEXT NOT NULL,                        -- structured details
    prev_hash     TEXT NOT NULL,                        -- 64-zero string for row 1
    row_hash      TEXT NOT NULL                         -- sha256 of canonical encoding
);

CREATE INDEX IF NOT EXISTS idx_audit_log_case_id ON audit_log(case_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_action  ON audit_log(action);
