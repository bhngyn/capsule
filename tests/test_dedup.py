"""End-to-end dedup flow (CLAUDE.md §15).

Covers:

* ``POST /api/jobs/preflight`` returns ``status: "duplicate"`` when the
  URL already exists in the case, and emits a ``duplicate.detected``
  audit row.
* ``POST /api/jobs/preflight`` collapses canonical-equivalent paste
  variants into ``within_batch_duplicate`` for everything past the
  first occurrence.
* ``POST /api/jobs/duplicate-outcome`` records ``opened_existing`` /
  ``cancelled`` audit rows.
* ``POST /api/jobs/batch`` with ``items=[{url, force_recapture: True}]``
  bypasses the within-batch dedup and submits forced re-captures.
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
    conn = _db.connect(_db.DB_PATH)
    _db.migrate(conn)
    case = _cases.create(conn, name="Test")
    yield {
        "conn": conn,
        "case": case,
        "downloads": capsule_dirs["downloads"],
        "audit": _audit,
        "pp": _pp,
    }
    _signing._reset_cache_for_tests()
    conn.close()


def _seed_capture(env, *, video_id: str):
    case_dir = env["downloads"] / env["case"].slug
    case_dir.mkdir(parents=True, exist_ok=True)
    media = case_dir / f"{video_id}.mp4"
    media.write_bytes(b"FAKE" * 50)
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


def _stub_classify(monkeypatch):
    """Avoid the network — classify returns the URL untouched."""
    from app import classify as classify_mod
    from app import url_canonical

    async def fake_classify(url, *, case_slug=None, client=None):
        canonical = url_canonical.canonicalize(url)
        import hashlib
        return classify_mod.Classification(
            url_submitted=url,
            url_final=url,
            url_canonical=canonical,
            redirect_chain=[url],
            platform="youtube",
            authenticated_domains=[],
            url_hash=hashlib.sha256(canonical.encode()).hexdigest()[:12],
        )
    monkeypatch.setattr("app.main.classify_mod.classify", fake_classify)


@pytest.mark.asyncio
async def test_preflight_detects_existing_capture(env, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from app import main as main_mod
    importlib.reload(main_mod)
    _stub_classify(monkeypatch)

    _seed_capture(env, video_id="abc")

    transport = ASGITransport(app=main_mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/jobs/preflight",
            json={
                "case_id": env["case"].id,
                "urls": [
                    "https://www.youtube.com/watch?v=abc",
                    "https://www.youtube.com/watch?v=different",
                ],
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    statuses = [r["status"] for r in body["results"]]
    assert statuses[0] == "duplicate"
    assert statuses[1] == "new"
    assert body["results"][0]["existing"]["title"] == "Video abc"
    assert body["summary"] == {
        "new": 1,
        "duplicates_blocked": 1,
        "within_batch_duplicates": 0,
        "classification_failed": 0,
    }
    # Audit anchor.
    audit_actions = [
        r["action"]
        for r in env["conn"].execute("SELECT action FROM audit_log")
    ]
    assert "duplicate.detected" in audit_actions


@pytest.mark.asyncio
async def test_preflight_collapses_canonical_variants_within_batch(env, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from app import main as main_mod
    importlib.reload(main_mod)
    _stub_classify(monkeypatch)

    transport = ASGITransport(app=main_mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/jobs/preflight",
            json={
                "case_id": env["case"].id,
                "urls": [
                    "https://www.youtube.com/watch?v=abc",
                    "https://www.youtube.com/watch?v=abc&utm_source=email",
                    "https://www.youtube.com/watch?v=abc&utm_source=tweet",
                ],
            },
        )
    body = resp.json()
    statuses = [r["status"] for r in body["results"]]
    assert statuses == ["new", "within_batch_duplicate", "within_batch_duplicate"]
    assert body["summary"]["within_batch_duplicates"] == 2


@pytest.mark.asyncio
async def test_duplicate_outcome_audit_route(env):
    from httpx import AsyncClient, ASGITransport
    from app import main as main_mod
    importlib.reload(main_mod)

    seeded = _seed_capture(env, video_id="abc")
    transport = ASGITransport(app=main_mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for outcome in ("opened_existing", "cancelled"):
            resp = await client.post(
                "/api/jobs/duplicate-outcome",
                json={
                    "case_id": env["case"].id,
                    "existing_id": seeded.download_id,
                    "outcome": outcome,
                },
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["ok"] is True

    actions = [
        r["action"]
        for r in env["conn"].execute(
            "SELECT action FROM audit_log WHERE action LIKE 'duplicate.%' ORDER BY id"
        )
    ]
    assert "duplicate.opened_existing" in actions
    assert "duplicate.cancelled" in actions


@pytest.mark.asyncio
async def test_batch_items_force_recapture_bypasses_within_batch_dedup(env, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from app import main as main_mod, jobs as jobs_mod
    importlib.reload(main_mod)
    _stub_classify(monkeypatch)

    # Stub orchestrator.submit so we don't actually run yt-dlp.
    submitted_calls: list[dict] = []

    class _StubJob:
        def to_dict(self):
            return {"id": "stub", "status": "queued"}

    async def fake_submit(self, *, case_id, url, lang=None,
                          force_recapture=False, original_download_id=None,
                          **_kw):
        submitted_calls.append({
            "url": url,
            "force_recapture": force_recapture,
            "original_download_id": original_download_id,
        })
        return _StubJob()

    monkeypatch.setattr(jobs_mod.JobOrchestrator, "submit", fake_submit)

    transport = ASGITransport(app=main_mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/jobs/batch",
            json={
                "case_id": env["case"].id,
                "items": [
                    {"url": "https://www.youtube.com/watch?v=abc"},
                    {
                        "url": "https://www.youtube.com/watch?v=abc",
                        "force_recapture": True,
                        "original_download_id": 7,
                    },
                ],
            },
        )
    assert resp.status_code == 200, resp.text
    # Both items submitted: the second is a forced re-capture so it's
    # NOT collapsed by within-batch dedup.
    assert len(submitted_calls) == 2
    assert submitted_calls[0]["force_recapture"] is False
    assert submitted_calls[1]["force_recapture"] is True
    assert submitted_calls[1]["original_download_id"] == 7
