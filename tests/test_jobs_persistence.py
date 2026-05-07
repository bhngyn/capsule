"""Persistent job queue + rehydrate-on-startup — plan §U1.

Validates that:

* every submitted job is durably written to ``jobs``
* a successful run lands as ``status='done'`` with ``result_json`` populated
* a transient-class failure flips the row to ``retrying`` with a future
  ``next_retry_at`` and bumps ``attempts``
* a ``running`` row left over from a crashed run is rehabilitated to
  ``queued`` by ``rehydrate()`` and re-dispatched
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys

import pytest


@pytest.fixture
def reload_modules(capsule_dirs):
    """Re-import the relevant app modules against the test config dirs."""
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
    signing.ensure_keypair()                  # postprocess.finalize signs meta.json
    jobs_mod.reset_for_tests()
    return db_mod, jobs_mod


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Replace the slow capture/classify primitives with deterministic stubs."""
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


def _wait_for(condition, *, timeout_s: float = 3.0, interval_s: float = 0.05):
    async def _inner():
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            if condition():
                return True
            await asyncio.sleep(interval_s)
        return False
    return _inner


@pytest.mark.asyncio
async def test_submit_writes_job_row_immediately(reload_modules, stub_pipeline, monkeypatch):
    """Submitting a job persists a ``queued``/``running`` row before it returns."""
    db_mod, jobs_mod = reload_modules
    classify_mod, capture_mod, ytdlp_runner = stub_pipeline

    async def slow_run(url, *, case_dir, progress_queue=None, **_kw):
        # Hold the run open so we can inspect the row in 'running' state.
        await asyncio.sleep(0.2)
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

    monkeypatch.setattr(ytdlp_runner, "run", slow_run)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="P")
    finally:
        conn.close()

    job = await jobs_mod.orchestrator().submit(case_id=case.id, url="https://example.com/v")

    # Row exists in DB right after submit returns.
    conn = db_mod.connect()
    try:
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job.id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["status"] in ("queued", "running")

    # Wait for terminal.
    for _ in range(60):
        conn = db_mod.connect()
        try:
            row = conn.execute("SELECT status, result_json FROM jobs WHERE id = ?", (job.id,)).fetchone()
        finally:
            conn.close()
        if row["status"] == "done":
            break
        await asyncio.sleep(0.1)
    assert row["status"] == "done"
    assert row["result_json"]
    assert json.loads(row["result_json"])["download_id"]


@pytest.mark.asyncio
async def test_transient_failure_schedules_retry(reload_modules, stub_pipeline, monkeypatch):
    """A network-class failure flips status to retrying with next_retry_at."""
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner = stub_pipeline

    async def failing_run(url, *, case_dir, progress_queue=None, **_kw):
        if progress_queue is not None:
            await progress_queue.put(None)
        return ytdlp_runner.RunResult(
            returncode=1,
            stdout="",
            stderr="ERROR: <urlopen error [Errno -2] getaddrinfo failed>",
            info=None,
            produced_files=[],
        )

    monkeypatch.setattr(ytdlp_runner, "run", failing_run)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="P")
    finally:
        conn.close()

    job = await jobs_mod.orchestrator().submit(case_id=case.id, url="https://example.com/v")

    # Wait for retry scheduling to land.
    for _ in range(60):
        conn = db_mod.connect()
        try:
            row = conn.execute(
                "SELECT status, attempts, next_retry_at, last_error_kind, last_error_severity"
                " FROM jobs WHERE id = ?",
                (job.id,),
            ).fetchone()
        finally:
            conn.close()
        if row["status"] == "retrying":
            break
        await asyncio.sleep(0.1)
    assert row["status"] == "retrying"
    assert row["attempts"] >= 1
    assert row["next_retry_at"]                          # populated
    assert row["last_error_kind"] == "errors.network"
    assert row["last_error_severity"] == "transient"


@pytest.mark.asyncio
async def test_permanent_failure_terminates(reload_modules, stub_pipeline, monkeypatch):
    """A permanent-class failure terminates as failed_permanent (no retry)."""
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner = stub_pipeline

    async def removed_run(url, *, case_dir, progress_queue=None, **_kw):
        if progress_queue is not None:
            await progress_queue.put(None)
        return ytdlp_runner.RunResult(
            returncode=1,
            stdout="",
            stderr="ERROR: Video unavailable",
            info=None,
            produced_files=[],
        )

    monkeypatch.setattr(ytdlp_runner, "run", removed_run)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="P")
    finally:
        conn.close()

    job = await jobs_mod.orchestrator().submit(case_id=case.id, url="https://example.com/v")
    for _ in range(60):
        conn = db_mod.connect()
        try:
            row = conn.execute(
                "SELECT status, last_error_kind, last_error_severity"
                " FROM jobs WHERE id = ?",
                (job.id,),
            ).fetchone()
        finally:
            conn.close()
        if row["status"] in ("failed_permanent", "done"):
            break
        await asyncio.sleep(0.1)
    assert row["status"] == "failed_permanent"
    assert row["last_error_kind"] == "errors.unavailable"
    assert row["last_error_severity"] == "permanent"


@pytest.mark.asyncio
async def test_rehydrate_resumes_orphaned_running_row(reload_modules, stub_pipeline, monkeypatch):
    """A 'running' row from a crashed previous boot should be re-dispatched."""
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner = stub_pipeline

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

    # Pre-seed a 'running' row that survived a crash.
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
                VALUES (?, ?, ?, 'running', 1, '{}', '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00')
                """,
                ("orphan-1", case.id, "https://example.com/v"),
            )
    finally:
        conn.close()

    rehydrated = await jobs_mod.orchestrator().rehydrate()
    assert any(j.id == "orphan-1" for j in rehydrated)

    # Eventually it completes.
    for _ in range(60):
        conn = db_mod.connect()
        try:
            row = conn.execute(
                "SELECT status FROM jobs WHERE id = ?", ("orphan-1",),
            ).fetchone()
        finally:
            conn.close()
        if row["status"] in ("done", "failed_permanent"):
            break
        await asyncio.sleep(0.1)
    assert row["status"] == "done"


@pytest.mark.asyncio
async def test_rehydrate_skips_terminal_rows(reload_modules, stub_pipeline):
    """``done`` / ``failed_permanent`` / ``cancelled`` rows must not be re-run."""
    db_mod, jobs_mod = reload_modules

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="P")
        with conn:
            for jid, status in [
                ("done-1", "done"),
                ("perm-1", "failed_permanent"),
                ("canc-1", "cancelled"),
            ]:
                conn.execute(
                    """
                    INSERT INTO jobs(id, case_id, source_url, status, attempts,
                                     progress_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, '{}',
                            '2026-01-01T00:00:00+00:00',
                            '2026-01-01T00:00:00+00:00')
                    """,
                    (jid, case.id, "https://example.com/v", status),
                )
    finally:
        conn.close()

    rehydrated = await jobs_mod.orchestrator().rehydrate()
    assert rehydrated == []


@pytest.mark.asyncio
async def test_duplicate_capture_cleans_up_ytdlp_orphans(
    reload_modules, stub_pipeline, monkeypatch, capsule_dirs,
):
    """A duplicate-rejected job must not leave yt-dlp's downloaded files in case_dir.

    Otherwise a follow-up capture for a related URL hits ``--continue``,
    skips the download, and the before/after diff in ``ytdlp_runner.run``
    reports no produced media → the capture is mis-classified as page_only.
    """
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner = stub_pipeline

    async def fake_run(url, *, case_dir, progress_queue=None, **_kw):
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

    monkeypatch.setattr(ytdlp_runner, "run", fake_run)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="P")
    finally:
        conn.close()

    url = "https://example.com/dupe"

    # First capture: success.
    job1 = await jobs_mod.orchestrator().submit(case_id=case.id, url=url)
    for _ in range(60):
        conn = db_mod.connect()
        try:
            row = conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job1.id,),
            ).fetchone()
        finally:
            conn.close()
        if row["status"] in ("done", "failed_permanent"):
            break
        await asyncio.sleep(0.1)
    assert row["status"] == "done"

    case_dir = capsule_dirs["downloads"] / case.slug
    # Sanity: the first capture's files were moved to canonical names — no
    # raw yt-dlp output (abc.*) should be sitting in case_dir.
    assert not (case_dir / "abc.mp4").exists()
    assert not (case_dir / "abc.info.json").exists()

    # Second capture of the same URL → DuplicateCapture.
    job2 = await jobs_mod.orchestrator().submit(case_id=case.id, url=url)
    for _ in range(60):
        conn = db_mod.connect()
        try:
            row = conn.execute(
                "SELECT status, last_error_kind FROM jobs WHERE id = ?", (job2.id,),
            ).fetchone()
        finally:
            conn.close()
        if row["status"] == "failed_permanent":
            break
        await asyncio.sleep(0.1)
    assert row["status"] == "failed_permanent"
    assert row["last_error_kind"] == "errors.duplicate"

    # The orphan check: yt-dlp's downloaded files from the duplicate-rejected
    # run must not be sitting in case_dir.
    assert not (case_dir / "abc.mp4").exists(), (
        "duplicate-rejected yt-dlp output left in case_dir — will trip "
        "--continue on the next capture and mis-classify it as page_only"
    )
    assert not (case_dir / "abc.info.json").exists()


@pytest.mark.asyncio
async def test_get_falls_back_to_db_for_unknown_job(reload_modules, stub_pipeline):
    """``get(id)`` reads the DB when the in-memory cache misses."""
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
                                 progress_json, result_json, created_at, updated_at)
                VALUES (?, ?, ?, 'done', 1, '{}', '{}',
                        '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00')
                """,
                ("frozen-1", case.id, "https://example.com/v"),
            )
    finally:
        conn.close()

    fetched = jobs_mod.orchestrator().get("frozen-1")
    assert fetched is not None
    assert fetched.id == "frozen-1"
    assert fetched.status == "done"
