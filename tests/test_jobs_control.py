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
            url_submitted=url, url_final=url, url_canonical=url,
            redirect_chain=[url],
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


# --- CLAUDE.md §15 v0.7: DownloadOptions + restart() -----------------------


def test_download_options_round_trip_via_json():
    from app.jobs import DownloadOptions
    a = DownloadOptions(
        audio_only=True, quality_cap="720",
        subtitle_langs=["en", "ar"], restart_count=2, stalled_count=1,
    )
    b = DownloadOptions.from_json(a.to_json())
    assert b.audio_only is True
    assert b.quality_cap == "720"
    assert b.subtitle_langs == ["en", "ar"]
    assert b.restart_count == 2
    assert b.stalled_count == 1


def test_download_options_from_json_handles_garbage():
    from app.jobs import DownloadOptions
    # Empty / corrupt blobs collapse to defaults — important for jobs
    # rows pre-005 migration that have download_options_json = '{}'.
    assert DownloadOptions.from_json(None).is_default()
    assert DownloadOptions.from_json("").is_default()
    assert DownloadOptions.from_json("{}").is_default()
    assert DownloadOptions.from_json("not-json").is_default()


def test_download_options_is_default_only_for_blank_state():
    from app.jobs import DownloadOptions
    assert DownloadOptions().is_default() is True
    assert DownloadOptions(audio_only=True).is_default() is False
    assert DownloadOptions(quality_cap="720").is_default() is False
    assert DownloadOptions(subtitle_langs=["en"]).is_default() is False
    # v0.9: container picks count as investigator-facing knobs too — even
    # without any other option, they should fire the audit row.
    assert DownloadOptions(video_container="mp4").is_default() is False
    assert DownloadOptions(audio_container="m4a").is_default() is False
    # Counters alone are not "investigator-facing" knobs — they don't
    # trigger the audit row, hence is_default still returns True.
    assert DownloadOptions(restart_count=3).is_default() is True


# --- CLAUDE.md §15 v0.9: container picker ----------------------------------


def test_download_options_round_trip_with_containers():
    from app.jobs import DownloadOptions
    # Both fields can be set simultaneously (the dataclass doesn't enforce
    # the audio_only/video_container split — that's the runner's job).
    a = DownloadOptions(
        audio_only=False, quality_cap="720",
        video_container="mp4", audio_container="m4a",
    )
    b = DownloadOptions.from_json(a.to_json())
    assert b.video_container == "mp4"
    assert b.audio_container == "m4a"
    # Round-trip preserves the v0.7 fields too.
    assert b.audio_only is False
    assert b.quality_cap == "720"


def test_download_options_from_dict_rejects_invalid_container_enum():
    # Defensive coerce: an unknown string in a stored row (e.g. someone
    # hand-edited download_options_json) silently degrades to None instead
    # of riding through to yt-dlp's argv.
    from app.jobs import DownloadOptions
    raw = {
        "video_container": "mov",   # not in {mp4, webm, mkv}
        "audio_container": "aiff",  # not in {mp3, m4a, opus, wav, flac}
    }
    opts = DownloadOptions.from_dict(raw)
    assert opts.video_container is None
    assert opts.audio_container is None


def test_download_options_to_dict_emits_container_fields_always():
    # to_dict() emits both fields unconditionally (even when None) so a
    # downstream consumer (PDF report, audit details, meta.json) can
    # distinguish "not set" from "missing" without a presence check.
    from app.jobs import DownloadOptions
    out = DownloadOptions().to_dict()
    assert "video_container" in out and out["video_container"] is None
    assert "audio_container" in out and out["audio_container"] is None


def test_download_options_module_constants_exposed():
    # Single source of truth — drift between the dataclass and the API
    # validators / runner / frontend would let an unknown string slip in.
    from app import jobs as jobs_mod
    assert "mp4" in jobs_mod.VIDEO_CONTAINERS
    assert "webm" in jobs_mod.VIDEO_CONTAINERS
    assert "mkv" in jobs_mod.VIDEO_CONTAINERS
    assert "mp3" in jobs_mod.AUDIO_CONTAINERS
    assert "m4a" in jobs_mod.AUDIO_CONTAINERS
    assert "opus" in jobs_mod.AUDIO_CONTAINERS
    assert "wav" in jobs_mod.AUDIO_CONTAINERS
    assert "flac" in jobs_mod.AUDIO_CONTAINERS


@pytest.mark.asyncio
async def test_submit_persists_download_options(reload_modules, stub_pipeline):
    db_mod, jobs_mod = reload_modules
    orch = jobs_mod.JobOrchestrator(max_concurrent=1)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.ensure_default_case(conn)
    finally:
        conn.close()

    opts = jobs_mod.DownloadOptions(
        audio_only=True, quality_cap=None, subtitle_langs=["en"],
    )
    job = await orch.submit(
        case_id=case.id, url="https://example.com/x",
        download_options=opts,
    )
    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        live = orch.get(job.id)
        if live and live.status in jobs_mod.TERMINAL_STATUSES:
            break
        await asyncio.sleep(0.05)

    conn = db_mod.connect()
    try:
        row = conn.execute(
            "SELECT download_options_json FROM jobs WHERE id = ?", (job.id,),
        ).fetchone()
    finally:
        conn.close()
    persisted = jobs_mod.DownloadOptions.from_json(row["download_options_json"])
    assert persisted.audio_only is True
    assert persisted.subtitle_langs == ["en"]


@pytest.mark.asyncio
async def test_submit_persists_video_and_audio_container(
    reload_modules, stub_pipeline,
):
    """v0.9: container picks must round-trip through ``submit`` → DB → reload.

    Without this, an investigator's mp4-or-m4a choice would be silently
    dropped between the API layer and the runner — meta.json would still
    record the on-disk extension but lose the *intent* (which is what the
    PDF report and audit row surface to the recipient).
    """
    db_mod, jobs_mod = reload_modules
    orch = jobs_mod.JobOrchestrator(max_concurrent=1)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.ensure_default_case(conn)
    finally:
        conn.close()

    opts = jobs_mod.DownloadOptions(
        audio_only=False,
        quality_cap="720",
        video_container="mp4",
        audio_container="m4a",
    )
    job = await orch.submit(
        case_id=case.id, url="https://example.com/x",
        download_options=opts,
    )
    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        live = orch.get(job.id)
        if live and live.status in jobs_mod.TERMINAL_STATUSES:
            break
        await asyncio.sleep(0.05)

    conn = db_mod.connect()
    try:
        row = conn.execute(
            "SELECT download_options_json FROM jobs WHERE id = ?", (job.id,),
        ).fetchone()
    finally:
        conn.close()
    persisted = jobs_mod.DownloadOptions.from_json(row["download_options_json"])
    assert persisted.video_container == "mp4"
    assert persisted.audio_container == "m4a"
    assert persisted.quality_cap == "720"


@pytest.mark.asyncio
async def test_restart_done_job_returns_false(reload_modules, stub_pipeline):
    db_mod, jobs_mod = reload_modules

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.ensure_default_case(conn)
    finally:
        conn.close()

    orch = jobs_mod.JobOrchestrator(max_concurrent=1)
    import uuid
    job_id = str(uuid.uuid4())
    job = jobs_mod.Job(
        id=job_id, case_id=case.id, url="https://example.com/x",
        status=jobs_mod.STATUS_DONE,
    )
    orch._jobs[job_id] = job
    conn = db_mod.connect()
    try:
        jobs_mod._insert_job(conn, job)
        with conn:
            conn.execute(
                "UPDATE jobs SET status = ? WHERE id = ?",
                (jobs_mod.STATUS_DONE, job_id),
            )
    finally:
        conn.close()

    ok = await orch.restart(job_id)
    assert ok is False


@pytest.mark.asyncio
async def test_restart_failed_job_increments_count_and_audits(
    reload_modules, stub_pipeline, monkeypatch,
):
    db_mod, jobs_mod = reload_modules

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.ensure_default_case(conn)
    finally:
        conn.close()

    orch = jobs_mod.JobOrchestrator(max_concurrent=1)

    import uuid
    job_id = str(uuid.uuid4())
    job = jobs_mod.Job(
        id=job_id, case_id=case.id, url="https://example.com/x",
        status=jobs_mod.STATUS_FAILED_PERMANENT,
    )
    orch._jobs[job_id] = job
    orch._channels[job_id] = asyncio.Queue()
    conn = db_mod.connect()
    try:
        jobs_mod._insert_job(conn, job)
        with conn:
            conn.execute(
                "UPDATE jobs SET status = ? WHERE id = ?",
                (jobs_mod.STATUS_FAILED_PERMANENT, job_id),
            )
    finally:
        conn.close()

    async def noop_run(j):
        return

    monkeypatch.setattr(orch, "_run", noop_run)

    ok = await orch.restart(job_id)
    assert ok is True

    live = orch.get(job_id)
    assert live.download_options.restart_count == 1
    assert live.attempts == 0
    assert live.error is None
    assert live.restart_pending is True or live.restart_pending is False

    conn = db_mod.connect()
    try:
        row = conn.execute(
            "SELECT details_json FROM audit_log "
            "WHERE action = 'job.restarted' ORDER BY id DESC LIMIT 1",
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    details = json.loads(row["details_json"])
    assert details.get("job_id") == job_id
    assert details.get("restart_count") == 1
    assert details.get("from") == jobs_mod.STATUS_FAILED_PERMANENT
