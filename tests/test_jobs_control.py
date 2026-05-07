"""Pause / resume / cancel — plan §U4.

Validates that the orchestrator's user-control surface persists, drives
the right state transitions, and cleans up properly.
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
def stub_pipeline(monkeypatch):
    from app import capture as capture_mod
    from app import classify as classify_mod
    from app import ytdlp_runner

    async def fake_classify(url, *, case_slug=None, client=None):
        return classify_mod.Classification(
            url_submitted=url, url_final=url, redirect_chain=[url],
            platform="generic", authenticated_domains=[],
            url_hash="ab" * 6,
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
    return classify_mod, capture_mod, ytdlp_runner


def _wait(pred, timeout=3.0):
    async def _inner():
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if pred():
                return True
            await asyncio.sleep(0.05)
        return False
    return _inner


@pytest.mark.asyncio
async def test_cancel_queued_job_transitions_to_cancelled(reload_modules, stub_pipeline, monkeypatch):
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner = stub_pipeline

    # Block the semaphore by running a slow job first, so subsequent submits
    # stay queued long enough for us to cancel them.
    async def slow_run(url, *, case_dir, progress_queue=None, **_kw):
        await asyncio.sleep(2.0)
        if progress_queue is not None:
            await progress_queue.put(None)
        return ytdlp_runner.RunResult(
            returncode=0, stdout="", stderr="", info=None, produced_files=[],
        )

    monkeypatch.setattr(ytdlp_runner, "run", slow_run)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="P")
    finally:
        conn.close()

    orch = jobs_mod.orchestrator()
    # Saturate concurrency=2 with two slow jobs.
    j1 = await orch.submit(case_id=case.id, url="https://example.com/1")
    j2 = await orch.submit(case_id=case.id, url="https://example.com/2")
    # j3 stays queued behind the semaphore.
    j3 = await orch.submit(case_id=case.id, url="https://example.com/3")
    await asyncio.sleep(0.1)

    ok = await orch.cancel(j3.id)
    assert ok is True

    # j3 should be cancelled in DB.
    conn = db_mod.connect()
    try:
        row = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (j3.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_pause_running_job_terminates_subprocess(reload_modules, stub_pipeline, monkeypatch):
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner = stub_pipeline

    proc_started = asyncio.Event()
    terminated = {"flag": False}

    class FakeProc:
        returncode: int | None = None

        def terminate(self):
            terminated["flag"] = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    async def slow_run(url, *, case_dir, progress_queue=None, proc_holder=None, **_kw):
        # Surface a fake proc to the orchestrator immediately.
        proc = FakeProc()
        if proc_holder is not None:
            proc_holder.append(proc)
        proc_started.set()
        # Sleep until either pause arrives (terminated flips) or natural end.
        for _ in range(200):
            if terminated["flag"]:
                break
            await asyncio.sleep(0.05)
        if progress_queue is not None:
            await progress_queue.put(None)
        return ytdlp_runner.RunResult(
            returncode=-15 if terminated["flag"] else 0,
            stdout="", stderr="",
            info=None, produced_files=[],
        )

    monkeypatch.setattr(ytdlp_runner, "run", slow_run)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="P")
    finally:
        conn.close()

    orch = jobs_mod.orchestrator()
    job = await orch.submit(case_id=case.id, url="https://example.com/pause-me")

    # Wait until the proc actually starts so we have something to pause.
    await asyncio.wait_for(proc_started.wait(), timeout=3.0)
    await asyncio.sleep(0.1)
    assert await orch.pause(job.id) is True

    # Subprocess should have been signalled.
    assert terminated["flag"] is True

    # Wait for the run to wind down and DB to reflect paused.
    for _ in range(60):
        conn = db_mod.connect()
        try:
            row = conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job.id,),
            ).fetchone()
        finally:
            conn.close()
        if row["status"] in ("paused", "cancelled", "done", "failed_permanent"):
            break
        await asyncio.sleep(0.1)
    assert row["status"] == "paused"


@pytest.mark.asyncio
async def test_resume_paused_job_re_enqueues(reload_modules, stub_pipeline, monkeypatch):
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner = stub_pipeline

    # Pre-seed a paused row.
    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="P")
        with conn:
            conn.execute(
                """
                INSERT INTO jobs(id, case_id, source_url, status, attempts,
                                 progress_json, created_at, updated_at)
                VALUES (?, ?, ?, 'paused', 1, '{}',
                        '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00')
                """,
                ("paused-1", case.id, "https://example.com/r"),
            )
    finally:
        conn.close()

    async def quick_run(url, *, case_dir, progress_queue=None, **_kw):
        case_dir.mkdir(parents=True, exist_ok=True)
        media = case_dir / "abc.mp4"
        media.write_bytes(b"FAKEDATA")
        info = case_dir / "abc.info.json"
        info.write_text(json.dumps({
            "id": "abc", "title": "Hi", "ext": "mp4",
            "extractor_key": "Generic",
        }))
        if progress_queue is not None:
            await progress_queue.put(None)
        return ytdlp_runner.RunResult(
            returncode=0, stdout="", stderr="",
            info=json.loads(info.read_text()),
            produced_files=[media, info],
        )

    monkeypatch.setattr(ytdlp_runner, "run", quick_run)

    orch = jobs_mod.orchestrator()
    ok = await orch.resume("paused-1")
    assert ok is True

    # Eventually completes.
    for _ in range(60):
        conn = db_mod.connect()
        try:
            row = conn.execute(
                "SELECT status FROM jobs WHERE id = ?", ("paused-1",),
            ).fetchone()
        finally:
            conn.close()
        if row["status"] in ("done", "failed_permanent"):
            break
        await asyncio.sleep(0.1)
    assert row["status"] == "done"


@pytest.mark.asyncio
async def test_cancel_terminal_job_returns_false(reload_modules, stub_pipeline):
    db_mod, jobs_mod = reload_modules

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="P")
        with conn:
            conn.execute(
                """
                INSERT INTO jobs(id, case_id, source_url, status, attempts,
                                 progress_json, created_at, updated_at)
                VALUES (?, ?, ?, 'done', 1, '{}',
                        '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00')
                """,
                ("done-1", case.id, "https://example.com/d"),
            )
    finally:
        conn.close()

    orch = jobs_mod.orchestrator()
    assert await orch.cancel("done-1") is False
    assert await orch.pause("done-1") is False


@pytest.mark.asyncio
async def test_pause_in_retrying_state_cancels_retry_task(reload_modules, stub_pipeline, monkeypatch):
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner = stub_pipeline

    async def failing_run(url, *, case_dir, progress_queue=None, **_kw):
        if progress_queue is not None:
            await progress_queue.put(None)
        return ytdlp_runner.RunResult(
            returncode=1, stdout="",
            stderr="ERROR: Could not resolve host: example.com",
            info=None, produced_files=[],
        )

    monkeypatch.setattr(ytdlp_runner, "run", failing_run)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="P")
    finally:
        conn.close()

    orch = jobs_mod.orchestrator()
    job = await orch.submit(case_id=case.id, url="https://example.com/r")

    # Wait for retry scheduling.
    for _ in range(60):
        conn = db_mod.connect()
        try:
            row = conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job.id,),
            ).fetchone()
        finally:
            conn.close()
        if row["status"] == "retrying":
            break
        await asyncio.sleep(0.1)
    assert row["status"] == "retrying"
    assert job.id in orch._retry_tasks

    ok = await orch.pause(job.id)
    assert ok is True
    # Retry task removed; status flips to paused.
    assert job.id not in orch._retry_tasks
    conn = db_mod.connect()
    try:
        row = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "paused"
