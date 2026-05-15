"""Cancel-cleanup hardening — CLAUDE.md §16 v0.11 bucket 2 #6.

When the user cancels a running yt-dlp job, the orchestrator must wipe
every ``*.part`` / ``*.ytdl`` in the case dir (not just the files the
runner happened to surface in ``produced_files``) and emit a distinct
``job.cancelled_cleanup`` audit row recording what was wiped. This
prevents a future re-capture from accidentally resuming a corrupted
blob and keeps the chain-of-custody visible to a reviewer.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys

import pytest


@pytest.fixture
def reload_modules(capsule_dirs):
    for name in (
        "app.config",
        "app.paths",
        "app.signing",
        "app.db",
        "app.audit",
        "app.cases",
        "app.cookies",
        "app.classify",
        "app.postprocess",
        "app.errors",
        "app.ytdlp_runner",
        "app.gallery_dl_runner",
        "app.capture",
        "app.jobs",
    ):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    from app import db as db_mod
    from app import jobs as jobs_mod
    from app import signing
    signing._reset_cache_for_tests()
    signing.ensure_keypair()
    jobs_mod.reset_for_tests()
    return db_mod, jobs_mod


@pytest.fixture
def stub_pipeline(monkeypatch, reload_modules):
    from app import capture as capture_mod
    from app import classify as classify_mod
    from app import ytdlp_runner

    async def fake_classify(url, *, case_slug=None, client=None):
        return classify_mod.Classification(
            url_submitted=url, url_final=url, url_canonical=url,
            redirect_chain=[url],
            platform="generic", authenticated_domains=[],
            url_hash="ef" * 6,
        )

    async def fake_capture(*, url, case_slug, work_dir=None, **_kw):
        return capture_mod.CaptureBundle(
            mhtml=None, screenshot=None, warc=None,
            chromium_version="0", browsertrix_version="0",
            page_title=None, response_headers=None,
        )

    async def fake_version() -> str:
        return "9999.0.0"

    monkeypatch.setattr(classify_mod, "classify", fake_classify)
    monkeypatch.setattr(capture_mod, "capture_page", fake_capture)
    monkeypatch.setattr(ytdlp_runner, "version", fake_version)
    return capture_mod, classify_mod, ytdlp_runner


def test_wipe_partial_files_removes_part_and_ytdl(tmp_path):
    """Direct unit-level coverage of the existing helper consumed by the
    cancel path. Confirms the loop sweeps both extensions and ignores
    untouched files.
    """
    from app import ytdlp_runner

    (tmp_path / "video.mp4.part").write_bytes(b"x")
    (tmp_path / "video.mp4.ytdl").write_bytes(b"x")
    (tmp_path / "keepme.mp4").write_bytes(b"x")
    n = ytdlp_runner._wipe_partial_files(tmp_path)
    assert n == 2
    assert not (tmp_path / "video.mp4.part").exists()
    assert not (tmp_path / "video.mp4.ytdl").exists()
    assert (tmp_path / "keepme.mp4").exists()


@pytest.mark.asyncio
async def test_cancel_wipes_partial_files_and_emits_cleanup_audit(
    reload_modules, stub_pipeline, monkeypatch,
):
    """Cancel a job whose runner produced a .part/.ytdl pair. Confirm
    they're wiped and the ``job.cancelled_cleanup`` audit row landed."""
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner = stub_pipeline

    cancel_event = asyncio.Event()
    paths_staged: dict[str, "object"] = {}

    async def slow_run(url, *, case_dir, progress_queue=None, proc_holder=None, **_kw):
        case_dir.mkdir(parents=True, exist_ok=True)
        part = case_dir / "video.mp4.part"
        ytdl = case_dir / "video.mp4.ytdl"
        part.write_bytes(b"\x00" * 16)
        ytdl.write_bytes(b"\x00" * 8)
        paths_staged["part"] = part
        paths_staged["ytdl"] = ytdl
        if progress_queue is not None:
            await progress_queue.put(None)
        cancel_event.set()
        # Pretend SIGTERM'd: wait until the cancel arrives and surface a
        # returncode != 0 with no produced files (the cancel path runs
        # regardless of run_result.produced_files).
        await asyncio.sleep(0.6)
        return ytdlp_runner.RunResult(
            returncode=-15, stdout="", stderr="terminated",
            info=None, produced_files=[part, ytdl],
        )

    monkeypatch.setattr(ytdlp_runner, "run", slow_run)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="Cancel cleanup")
    finally:
        conn.close()

    orch = jobs_mod.orchestrator()
    job = await orch.submit(case_id=case.id, url="https://example.com/x")

    # Wait until the runner has staged the partial files, then cancel.
    await asyncio.wait_for(cancel_event.wait(), timeout=2.0)
    await orch.cancel(job.id)

    # Wait for the cancel to land.
    for _ in range(60):
        conn = db_mod.connect()
        try:
            row = conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job.id,),
            ).fetchone()
        finally:
            conn.close()
        if row and row["status"] == "cancelled":
            break
        await asyncio.sleep(0.05)
    assert row["status"] == "cancelled"

    # Partial files wiped.
    assert not paths_staged["part"].exists()
    assert not paths_staged["ytdl"].exists()

    # Audit log carries job.cancelled AND job.cancelled_cleanup with the
    # wiped_count populated.
    conn = db_mod.connect()
    try:
        rows = conn.execute(
            "SELECT action, details_json FROM audit_log WHERE case_id = ? ORDER BY id",
            (case.id,),
        ).fetchall()
    finally:
        conn.close()
    actions = [r["action"] for r in rows]
    assert "job.cancelled" in actions
    assert "job.cancelled_cleanup" in actions
    for r in rows:
        if r["action"] == "job.cancelled_cleanup":
            details = json.loads(r["details_json"])
            assert details["job_id"] == job.id
            assert details["wiped_count"] == 2
            break
