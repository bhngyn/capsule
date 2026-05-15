"""Capture-pipeline forensic failure markers — CLAUDE.md §16 v0.11 bucket 2 #1/#2/#3/#4.

Each test sets up a CaptureBundle whose CaptureReport carries the new
forensic error markers and asserts the orchestrator emits the matching
audit row. The bundle is injected through ``capture_page`` so no real
Playwright session runs.
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


def _bundle_with_report(capture_mod, *, warc=None, report=None):
    return capture_mod.CaptureBundle(
        mhtml=None, screenshot=None, warc=warc,
        chromium_version="0", browsertrix_version="0",
        page_title=None, response_headers=None,
        report=report or capture_mod.CaptureReport(),
    )


def _yt_empty_run_factory():
    async def fake_run(url, *, case_dir, progress_queue=None, **_kw):
        case_dir.mkdir(parents=True, exist_ok=True)
        if progress_queue is not None:
            await progress_queue.put(None)
        from app import ytdlp_runner
        return ytdlp_runner.RunResult(
            returncode=0, stdout="", stderr="",
            info=None, produced_files=[],
        )
    return fake_run


async def _wait_for_terminal(db_mod, job_id: str, max_iter: int = 80):
    for _ in range(max_iter):
        conn = db_mod.connect()
        try:
            row = conn.execute(
                "SELECT status FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        finally:
            conn.close()
        if row and row["status"] in ("done", "failed_permanent", "cancelled"):
            return row
        await asyncio.sleep(0.05)
    raise TimeoutError(f"job {job_id} never reached terminal")


def _audit_actions(db_mod, case_id: int) -> list[str]:
    conn = db_mod.connect()
    try:
        return [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_log WHERE case_id = ? ORDER BY id",
                (case_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# CaptureReport unit checks (no orchestrator)
# ----------------------------------------------------------------------


def test_capture_report_to_dict_carries_new_error_markers(reload_modules):
    from app import capture as capture_mod
    report = capture_mod.CaptureReport(
        warc_in_session_error="RuntimeError",
        banner_hide_error="TimeoutError",
        console_sidecar_error="OSError",
    )
    out = report.to_dict()
    assert out["warc"]["in_session_error"] == "RuntimeError"
    assert out["banner_hide_error"] == "TimeoutError"
    assert out["console_sidecar_error"] == "OSError"


def test_capture_report_to_dict_defaults_are_none(reload_modules):
    from app import capture as capture_mod
    out = capture_mod.CaptureReport().to_dict()
    assert out["warc"]["in_session_error"] is None
    assert out["banner_hide_error"] is None
    assert out["console_sidecar_error"] is None


# ----------------------------------------------------------------------
# Orchestrator emits the new audit rows when the markers are set
# ----------------------------------------------------------------------


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
            url_hash="ab" * 6,
        )

    async def fake_version() -> str:
        return "9999.0.0"

    monkeypatch.setattr(classify_mod, "classify", fake_classify)
    monkeypatch.setattr(ytdlp_runner, "version", fake_version)
    monkeypatch.setattr(ytdlp_runner, "run", _yt_empty_run_factory())
    return capture_mod, classify_mod, ytdlp_runner


def _make_case(db_mod, name="Forensic markers"):
    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        return cases.create(conn, name=name)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_warc_session_failed_audit_row(reload_modules, stub_pipeline, monkeypatch):
    db_mod, jobs_mod = reload_modules
    capture_mod, _, _ = stub_pipeline

    # bundle.warc IS set (fallback succeeded) but report records the
    # in-session error.
    fallback_warc = capsule_warc_path = None  # type: ignore

    async def fake_capture(*, url, case_slug, work_dir=None, **_kw):
        report = capture_mod.CaptureReport(
            warc_in_session_error="RuntimeError",
            warc_captured_in_session=False,
        )
        # Pretend browsertrix wrote a fallback WARC by passing a sentinel
        # Path that doesn't need to exist for the audit emission to fire.
        from pathlib import Path
        return _bundle_with_report(
            capture_mod, warc=Path("/dev/null/fake.warc.gz"), report=report,
        )

    monkeypatch.setattr(capture_mod, "capture_page", fake_capture)

    case = _make_case(db_mod)
    job = await jobs_mod.orchestrator().submit(
        case_id=case.id, url="https://example.com/x",
    )
    await _wait_for_terminal(db_mod, job.id)

    actions = _audit_actions(db_mod, case.id)
    assert "capture.warc_session_failed" in actions
    assert "capture.warc_missing" not in actions  # fallback succeeded


@pytest.mark.asyncio
async def test_warc_missing_audit_row(reload_modules, stub_pipeline, monkeypatch):
    db_mod, jobs_mod = reload_modules
    capture_mod, _, _ = stub_pipeline

    async def fake_capture(*, url, case_slug, work_dir=None, **_kw):
        report = capture_mod.CaptureReport(
            warc_in_session_error="TimeoutError",
            warc_captured_in_session=False,
        )
        # bundle.warc is None ⇒ both in-session AND browsertrix failed.
        return _bundle_with_report(capture_mod, warc=None, report=report)

    monkeypatch.setattr(capture_mod, "capture_page", fake_capture)

    case = _make_case(db_mod)
    job = await jobs_mod.orchestrator().submit(
        case_id=case.id, url="https://example.com/x",
    )
    await _wait_for_terminal(db_mod, job.id)

    actions = _audit_actions(db_mod, case.id)
    assert "capture.warc_session_failed" in actions
    assert "capture.warc_missing" in actions


@pytest.mark.asyncio
async def test_banner_hide_failed_audit_row(reload_modules, stub_pipeline, monkeypatch):
    db_mod, jobs_mod = reload_modules
    capture_mod, _, _ = stub_pipeline

    async def fake_capture(*, url, case_slug, work_dir=None, **_kw):
        report = capture_mod.CaptureReport(
            banner_hide_applied=False,
            banner_hide_error="TimeoutError",
            banner_hide_version="2026-01-01",
        )
        return _bundle_with_report(capture_mod, report=report)

    monkeypatch.setattr(capture_mod, "capture_page", fake_capture)

    case = _make_case(db_mod)
    job = await jobs_mod.orchestrator().submit(
        case_id=case.id, url="https://example.com/x",
    )
    await _wait_for_terminal(db_mod, job.id)

    actions = _audit_actions(db_mod, case.id)
    assert "capture.banners_hide_failed" in actions
    # The success row must NOT fire alongside the failure row.
    assert "capture.banners_hidden" not in actions


@pytest.mark.asyncio
async def test_console_sidecar_failed_audit_row_and_counts_zero(
    reload_modules, stub_pipeline, monkeypatch,
):
    db_mod, jobs_mod = reload_modules
    capture_mod, _, _ = stub_pipeline

    async def fake_capture(*, url, case_slug, work_dir=None, **_kw):
        # Counts are 0 here to mirror the capture-side enforcement in
        # _playwright_snapshot — but even if a buggy upstream sent counts
        # >0, the orchestrator path would still fire the audit row.
        report = capture_mod.CaptureReport(
            console_message_count=0,
            console_error_count=0,
            console_sidecar_error="OSError",
        )
        return _bundle_with_report(capture_mod, report=report)

    monkeypatch.setattr(capture_mod, "capture_page", fake_capture)

    case = _make_case(db_mod)
    job = await jobs_mod.orchestrator().submit(
        case_id=case.id, url="https://example.com/x",
    )
    await _wait_for_terminal(db_mod, job.id)

    actions = _audit_actions(db_mod, case.id)
    assert "capture.console_sidecar_failed" in actions


# ----------------------------------------------------------------------
# Phase 3c — HAR redaction failure detection (postprocess-side)
# ----------------------------------------------------------------------


def test_har_without_redaction_marker_is_deleted_and_audited(capsule_dirs):
    """Drive postprocess.finalize directly with a HAR sidecar that lacks
    the ``_capsule_redacted_header_count`` marker. The post-processor
    must (a) delete the HAR, (b) drop ``page_har`` from artifacts, and
    (c) emit the ``capture.har_redaction_failed`` audit row.
    """
    import importlib as _il
    from app import (
        audit as _audit, cases as _cases, db as _db, paths as _paths,
        postprocess as _pp, signing as _signing,
    )
    _il.reload(_paths)
    _il.reload(_signing)
    _signing._reset_cache_for_tests()
    _il.reload(_cases)
    _il.reload(_pp)

    conn = _db.connect(":memory:")
    _db.migrate(conn)
    case = _cases.create(conn, name="HAR redaction failure")
    _signing.ensure_keypair()

    # Stage a page-only capture with a HAR that has no redaction marker —
    # simulating the silent-swallow path in capture._redact_har_in_place.
    case_dir = capsule_dirs["downloads"] / case.slug
    case_dir.mkdir(parents=True, exist_ok=True)
    har_src = case_dir / "page.har"
    # NOTE: no ``_capsule_redacted_header_count`` key under log → marker
    # absent → post-processor must delete the file.
    har_src.write_text(
        json.dumps({"log": {"version": "1.2", "entries": []}}),
        encoding="utf-8",
    )

    capture_input = _pp.CaptureInput(
        case=case,
        job_uuid=_pp.new_job_uuid(),
        url_submitted="https://example.com/x",
        url_final="https://example.com/x",
        redirect_chain=["https://example.com/x"],
        capture_date=_pp.utc_now(),
        media_files=[],
        info_json=None,
        page_har=har_src,
    )
    result = _pp.finalize(conn, capture_input)

    # HAR sidecar removed; the role doesn't appear in artifacts.
    item_dir = case_dir / result.stem
    assert not (item_dir / "Captures" / f"{result.stem}.page.har").exists()

    import json as _json
    meta = _json.loads((item_dir / "Metadata" / f"{result.stem}.meta.json").read_bytes())
    assert "page_har" not in meta["artifacts"]

    actions = [r["action"] for r in _audit.iter_entries(conn)]
    assert "capture.har_redaction_failed" in actions

    conn.close()


def test_har_with_marker_is_retained(capsule_dirs):
    """Baseline: a HAR carrying the marker rides through unchanged."""
    import importlib as _il
    from app import (
        audit as _audit, cases as _cases, db as _db, paths as _paths,
        postprocess as _pp, signing as _signing,
    )
    _il.reload(_paths)
    _il.reload(_signing)
    _signing._reset_cache_for_tests()
    _il.reload(_cases)
    _il.reload(_pp)

    conn = _db.connect(":memory:")
    _db.migrate(conn)
    case = _cases.create(conn, name="HAR redaction OK")
    _signing.ensure_keypair()

    case_dir = capsule_dirs["downloads"] / case.slug
    case_dir.mkdir(parents=True, exist_ok=True)
    har_src = case_dir / "page.har"
    har_src.write_text(
        json.dumps({"log": {
            "version": "1.2",
            "entries": [],
            "_capsule_redacted_header_count": 0,
        }}),
        encoding="utf-8",
    )

    capture_input = _pp.CaptureInput(
        case=case,
        job_uuid=_pp.new_job_uuid(),
        url_submitted="https://example.com/y",
        url_final="https://example.com/y",
        redirect_chain=["https://example.com/y"],
        capture_date=_pp.utc_now(),
        media_files=[],
        info_json=None,
        page_har=har_src,
    )
    result = _pp.finalize(conn, capture_input)

    item_dir = capsule_dirs["downloads"] / case.slug / result.stem
    assert (item_dir / "Captures" / f"{result.stem}.page.har").exists()

    actions = [r["action"] for r in _audit.iter_entries(conn)]
    assert "capture.har_redaction_failed" not in actions

    conn.close()


@pytest.mark.asyncio
async def test_clean_capture_emits_no_failure_rows(reload_modules, stub_pipeline, monkeypatch):
    """Baseline: a healthy capture must not emit any of the new failure rows."""
    db_mod, jobs_mod = reload_modules
    capture_mod, _, _ = stub_pipeline

    from pathlib import Path

    async def fake_capture(*, url, case_slug, work_dir=None, **_kw):
        report = capture_mod.CaptureReport(
            banner_hide_applied=True,
            banner_hide_version="2026-01-01",
            warc_captured_in_session=True,
            warc_record_count=42,
        )
        return _bundle_with_report(
            capture_mod, warc=Path("/dev/null/fake.warc.gz"), report=report,
        )

    monkeypatch.setattr(capture_mod, "capture_page", fake_capture)

    case = _make_case(db_mod)
    job = await jobs_mod.orchestrator().submit(
        case_id=case.id, url="https://example.com/x",
    )
    await _wait_for_terminal(db_mod, job.id)

    actions = _audit_actions(db_mod, case.id)
    for failure_action in (
        "capture.warc_session_failed",
        "capture.warc_missing",
        "capture.banners_hide_failed",
        "capture.console_sidecar_failed",
    ):
        assert failure_action not in actions
