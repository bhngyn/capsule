"""SQLite schema + migration runner — CLAUDE.md §8, §9."""

from __future__ import annotations

import sqlite3

import pytest

from app import db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def test_pragmas_set(conn):
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1


def test_migrate_creates_all_tables(conn):
    applied = db.migrate(conn)
    # Migrations apply in order; assert at least the base versions are present.
    assert 0 in applied
    assert 1 in applied
    rows = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "cases", "downloads", "audit_log",
        "jobs", "capture_groups", "schema_migrations",
    } <= rows


def test_migrate_is_idempotent(conn):
    first = db.migrate(conn)
    second = db.migrate(conn)
    assert second == []
    versions = db.applied_versions(conn)
    assert versions == set(first)
    assert {0, 1} <= versions


def test_unique_capture_constraint(conn):
    db.migrate(conn)
    conn.execute(
        "INSERT INTO cases(slug, name, created_at, updated_at) VALUES('c', 'C', 't', 't')"
    )
    case_id = conn.execute("SELECT id FROM cases").fetchone()[0]

    base = (
        case_id, "j1", "media", "https://x.com/1", "https://x.com/1",
        "twitter", "1", "abcdef012345", "u", "t", "T", None,
        "2026-05-06T00:00:00+00:00", None, "downloads/c/sidecars/x",
        None, None, None, None, "1.0", "0", "0", "0.1.0", "fp", "{}",
    )
    cols = (
        "case_id, job_uuid, capture_kind, source_url, final_url, platform, "
        "video_id, url_hash, uploader, title, title_original, upload_date, "
        "capture_date, relative_path, sidecar_dir, file_size_bytes, md5, "
        "sha256, duration_seconds, ytdlp_version, chromium_version, "
        "browsertrix_version, app_version, signing_key_fp, meta_json"
    )
    placeholders = ", ".join(["?"] * len(base))
    conn.execute(f"INSERT INTO downloads({cols}) VALUES ({placeholders})", base)

    # Same (case_id, capture_kind, url_hash) — must fail.
    base2 = list(base)
    base2[1] = "j2"  # different job uuid still hits the UNIQUE
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO downloads({cols}) VALUES ({placeholders})", base2
        )


def test_indexes_exist(conn):
    db.migrate(conn)
    rows = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    expected = {
        "idx_downloads_case_id",
        "idx_downloads_platform",
        "idx_downloads_capture_date",
        "idx_downloads_video_id",
        "idx_downloads_url_hash",
        "idx_audit_log_case_id",
        "idx_audit_log_action",
        "idx_jobs_case_id",
        "idx_jobs_status",
        "idx_jobs_next_run",
        "idx_jobs_capture_group",
        "idx_jobs_task_kind",
        "idx_capture_groups_case_id",
        "idx_capture_groups_download_id",
    }
    assert expected <= rows


def test_connect_creates_parent_directory(tmp_path):
    target = tmp_path / "deep" / "library.db"
    c = db.connect(target)
    db.migrate(c)
    assert target.exists()
    c.close()
