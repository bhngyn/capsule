"""Migration smoke tests, focused on 004 (canonicalize url_hash)."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def env(capsule_dirs):
    from app import db as _db
    importlib.reload(_db)
    return {"db": _db, "capsule_dirs": capsule_dirs}


def _seed_pre_migration(conn, *, case_id, capture_kind, final_url, url_hash, capture_date):
    """Insert a downloads row as if migration 004 had not yet run."""
    conn.execute(
        """
        INSERT INTO downloads(
            case_id, job_uuid, capture_kind, source_url, final_url, platform,
            video_id, url_hash, uploader, title, title_original, upload_date,
            capture_date, relative_path, item_dir, file_size_bytes, md5, sha256,
            duration_seconds, ytdlp_version, chromium_version, browsertrix_version,
            app_version, signing_key_fp, meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case_id, f"job-{url_hash}", capture_kind, final_url, final_url,
            "youtube", "abc", url_hash, "u", "t", "t", "20240101",
            capture_date, None, f"slug/{url_hash}", None, None, None,
            None, "1", "1", "1", "1", "fp",
            json.dumps({"schema_version": 4, "url_hash": url_hash}),
        ),
    )


def test_004_collapses_utm_variants(env):
    """Two pre-migration rows with utm-variants of the same URL get
    consolidated: the older one keeps the unsuffixed canonical hash, the
    younger gets ``__c2``.
    """
    db = env["db"]

    # Apply migrations 000-003 only by reading the migrations dir,
    # filtering out 004+. We achieve this by running migrate() on a
    # filtered copy of the migrations directory.
    import tempfile
    import shutil

    real_migrations = Path(__file__).parent.parent / "app" / "migrations"
    with tempfile.TemporaryDirectory() as tmp:
        early = Path(tmp) / "early"
        early.mkdir()
        for f in real_migrations.iterdir():
            if f.name[:3].isdigit() and int(f.name[:3]) < 4:
                shutil.copy(f, early / f.name)

        conn = db.connect(":memory:")
        try:
            db.migrate(conn, directory=early)
            # Seed two rows that are pre-canonical hash collisions.
            from app import cases as cases_mod
            case = cases_mod.create(conn, name="Test")
            url_a = "https://www.youtube.com/watch?v=abc&utm_source=email"
            url_b = "https://www.youtube.com/watch?v=abc&utm_source=tweet"
            import hashlib
            ha = hashlib.sha256(url_a.encode()).hexdigest()[:12]
            hb = hashlib.sha256(url_b.encode()).hexdigest()[:12]
            assert ha != hb  # pre-migration: distinct hashes
            _seed_pre_migration(
                conn, case_id=case.id, capture_kind="media",
                final_url=url_a, url_hash=ha, capture_date="2026-01-01T00:00:00",
            )
            _seed_pre_migration(
                conn, case_id=case.id, capture_kind="media",
                final_url=url_b, url_hash=hb, capture_date="2026-02-01T00:00:00",
            )

            # Now apply migration 004 alone via the full migrate() against
            # the real migrations dir (it's idempotent for already-applied
            # versions).
            applied = db.migrate(conn)
            assert 4 in applied

            # Post-migration: same canonical base, distinct suffixes.
            rows = list(conn.execute(
                "SELECT url_hash, capture_date FROM downloads "
                "WHERE case_id = ? ORDER BY capture_date ASC",
                (case.id,),
            ))
            assert len(rows) == 2
            base = rows[0]["url_hash"]
            assert "__c" not in base
            assert rows[1]["url_hash"] == base + "__c2"

            # Audit entry recorded the migration.
            audit_rows = list(conn.execute(
                "SELECT action FROM audit_log WHERE action = ?",
                ("duplicate.url_canonicalized",),
            ))
            assert len(audit_rows) == 1

            # Chain still verifies.
            from app import audit as audit_mod
            ok, broken = audit_mod.verify_chain(conn)
            assert ok, f"audit chain broken at row {broken}"
        finally:
            conn.close()


def test_004_non_colliding_rows_unchanged_apart_from_canonical_hash(env):
    """A row whose canonical form is unique still has its url_hash
    rewritten to the canonical-derived value (so future captures of
    paste-variants of the same URL collide with it correctly).
    """
    db = env["db"]
    conn = db.connect(":memory:")
    try:
        db.migrate(conn)
        from app import cases as cases_mod
        case = cases_mod.create(conn, name="Test")
        url = "https://www.youtube.com/watch?v=xyz"
        import hashlib
        from app import url_canonical
        canon = url_canonical.canonicalize(url)
        canonical_hash = hashlib.sha256(canon.encode()).hexdigest()[:12]
        # Seed BEFORE migration would have already run — we have to
        # reverse-engineer this by deleting from schema_migrations and
        # rolling back the 004 effect. Easier: simulate by inserting a
        # row with a non-canonical hash, then running migrate again.
        conn.execute("DELETE FROM schema_migrations WHERE version = 4")
        # Insert the row with a DIFFERENT pre-migration hash.
        bogus = "00" * 6  # 12 hex chars
        _seed_pre_migration(
            conn, case_id=case.id, capture_kind="media",
            final_url=url, url_hash=bogus,
            capture_date="2026-01-01T00:00:00",
        )
        applied = db.migrate(conn)
        assert 4 in applied
        row = conn.execute(
            "SELECT url_hash FROM downloads WHERE case_id = ?", (case.id,),
        ).fetchone()
        assert row["url_hash"] == canonical_hash
    finally:
        conn.close()
