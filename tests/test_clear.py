"""POST /api/cases/{id}/clear — destructive bulk delete with audit anchor.

Verifies:

* per-item folders are removed from disk (every artifact)
* ``downloads`` rows for the case are deleted
* the case row, cookies file, and signing key all stay
* a single ``library.cleared`` audit row is appended with a snapshot
  of every deleted item's hashes
* the audit-log hash chain stays unbroken after the clear
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def env(capsule_dirs):
    from app import (
        audit as _audit,
        cases as _cases,
        cookies as _cookies,
        db as _db,
        paths as _paths,
        postprocess as _pp,
        signing as _signing,
    )
    importlib.reload(_paths)
    importlib.reload(_signing)
    _signing._reset_cache_for_tests()
    importlib.reload(_cases)
    importlib.reload(_cookies)
    importlib.reload(_pp)

    # Use the on-disk DB so the FastAPI route can open its own
    # connections — each route call's ``conn.close()`` would otherwise
    # invalidate our shared in-memory connection.
    conn = _db.connect(_db.DB_PATH)
    _db.migrate(conn)
    case = _cases.create(conn, name="Operation Sunrise")
    yield {
        "conn": conn,
        "case": case,
        "db": _db,
        "downloads": capsule_dirs["downloads"],
        "config": capsule_dirs["config"],
        "audit": _audit,
        "pp": _pp,
    }
    _signing._reset_cache_for_tests()
    conn.close()


def _stage_and_finalize(env, *, video_id: str):
    """Run a full finalize() so we have on-disk artifacts + DB row."""
    case_dir = env["downloads"] / env["case"].slug
    case_dir.mkdir(parents=True, exist_ok=True)
    media = case_dir / f"{video_id}.mp4"
    media.write_bytes(b"FAKE" * 100)
    info_path = case_dir / f"{video_id}.info.json"
    info = {
        "id": video_id,
        "title": f"Video {video_id}",
        "ext": "mp4",
        "extractor_key": "Youtube",
        "uploader": "u",
        "upload_date": "20240101",
        "duration": 60,
        "description": "desc",
    }
    info_path.write_text(json.dumps(info))
    desc = case_dir / f"{video_id}.description"
    desc.write_text("desc")
    pp = env["pp"]
    return pp.finalize(env["conn"], pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted=f"https://www.youtube.com/watch?v={video_id}",
        url_final=f"https://www.youtube.com/watch?v={video_id}",
        redirect_chain=[f"https://www.youtube.com/watch?v={video_id}"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=info,
        extra_sidecars=[info_path, desc],
        ytdlp_version="2026.01.01",
    ))


@pytest.mark.asyncio
async def test_clear_case_deletes_artifacts_and_rows_but_keeps_audit(env):
    from httpx import AsyncClient, ASGITransport
    from app import main as main_mod
    importlib.reload(main_mod)

    # Seed two captures.
    a = _stage_and_finalize(env, video_id="aaa")
    b = _stage_and_finalize(env, video_id="bbb")
    case_id = env["case"].id

    item_dir_a = env["downloads"] / a.relative_item_dir
    item_dir_b = env["downloads"] / b.relative_item_dir
    assert item_dir_a.exists() and item_dir_b.exists()

    # Drop a fake cookies file under the case to confirm it survives.
    cookies_path = env["config"] / "cases" / env["case"].slug / "cookies.txt"
    cookies_path.parent.mkdir(parents=True, exist_ok=True)
    cookies_path.write_text("# Netscape HTTP Cookie File\n")

    # The signing key — already created by test_clear_case_* above via
    # finalize(); confirm its presence as a precondition.
    keys_dir = env["config"] / "keys"
    assert keys_dir.exists() and any(keys_dir.iterdir())

    transport = ASGITransport(app=main_mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/cases/{case_id}/clear")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted_count"] == 2
    assert body["freed_bytes"] > 0
    assert body["audit_id"] is not None

    # On-disk artifacts gone.
    assert not item_dir_a.exists()
    assert not item_dir_b.exists()
    # downloads table is empty for this case.
    assert env["conn"].execute(
        "SELECT COUNT(*) AS n FROM downloads WHERE case_id = ?", (case_id,),
    ).fetchone()["n"] == 0
    # Case row still exists.
    from app import cases as cases_mod
    assert cases_mod.get(env["conn"], case_id) is not None
    # Cookies file untouched.
    assert cookies_path.exists()
    # Signing key untouched.
    assert keys_dir.exists() and any(keys_dir.iterdir())
    # Audit log: every prior row still there + one library.cleared.
    actions = [
        r["action"]
        for r in env["conn"].execute("SELECT action FROM audit_log ORDER BY id")
    ]
    assert "library.cleared" in actions
    cleared = env["conn"].execute(
        "SELECT details_json FROM audit_log WHERE action = 'library.cleared'"
    ).fetchone()
    details = json.loads(cleared["details_json"])
    assert details["count"] == 2
    assert len(details["items"]) == 2
    for item in details["items"]:
        assert "url_hash" in item and "meta_json_sha256" in item
    # Chain still verifies.
    ok, broken = env["audit"].verify_chain(env["conn"])
    assert ok, f"audit chain broken at {broken}"


@pytest.mark.asyncio
async def test_clear_empty_case_is_noop(env):
    from httpx import AsyncClient, ASGITransport
    from app import main as main_mod
    importlib.reload(main_mod)

    transport = ASGITransport(app=main_mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/cases/{env['case'].id}/clear")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_count"] == 0
    assert body["freed_bytes"] == 0
