"""Orchestrator gallery-dl fallback — CLAUDE.md §15 Gallery pass v0.5.

When yt-dlp returns no media, gallery-dl runs against the same URL with
the same cookies. If it yields ≥1 image, the capture finalizes as
``gallery``. If gallery-dl also yields nothing, the capture is
``page_only`` exactly as before.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

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
def stub_pipeline(monkeypatch):
    """Stub classify/capture so the orchestrator never makes real network calls."""
    from app import capture as capture_mod
    from app import classify as classify_mod
    from app import gallery_dl_runner
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

    async def fake_yt_version() -> str:
        return "9999.0.0"

    async def fake_g_version() -> str:
        return "1.30.fake"

    monkeypatch.setattr(classify_mod, "classify", fake_classify)
    monkeypatch.setattr(capture_mod, "capture_page", fake_capture)
    monkeypatch.setattr(ytdlp_runner, "version", fake_yt_version)
    monkeypatch.setattr(gallery_dl_runner, "version", fake_g_version)
    return classify_mod, capture_mod, ytdlp_runner, gallery_dl_runner


def _empty_yt_run_factory(produced_files: list | None = None):
    """yt-dlp returned nothing media-y — only sidecars or empty."""
    async def fake_run(url, *, case_dir, progress_queue=None, **_kw):
        case_dir.mkdir(parents=True, exist_ok=True)
        if progress_queue is not None:
            await progress_queue.put(None)
        from app import ytdlp_runner
        return ytdlp_runner.RunResult(
            returncode=0, stdout="", stderr="",
            info=None, produced_files=list(produced_files or []),
        )
    return fake_run


def _gallery_run_factory(image_extensions: list[str], extractor: str = "twitter"):
    """gallery-dl returns N images named <NN>.<ext> with metadata sidecars."""
    async def fake_run(url, *, work_dir, progress_queue=None, **_kw):
        work_dir.mkdir(parents=True, exist_ok=True)
        sub = work_dir / extractor / "user"
        sub.mkdir(parents=True, exist_ok=True)
        images: list[Path] = []
        meta_files: list[Path] = []
        for i, ext in enumerate(image_extensions, start=1):
            p = sub / f"{i:02d}.{ext}"
            p.write_bytes(f"IMG{i}".encode())
            images.append(p)
            m = sub / f"{p.name}.json"
            m.write_text(json.dumps({"category": extractor, "filename": p.name}))
            meta_files.append(m)
        info = sub / "info.json"
        info.write_text(json.dumps({"category": extractor, "url": url}))
        meta_files.append(info)
        if progress_queue is not None:
            await progress_queue.put(None)
        from app import gallery_dl_runner
        return gallery_dl_runner.RunResult(
            returncode=0, stdout="", stderr="",
            info=json.loads(info.read_text()),
            produced_files=images + meta_files,
            image_files=images,
            metadata_files=meta_files,
            extractor=extractor,
        )
    return fake_run


def _gallery_fail_factory(returncode: int = 1, stderr: str = ""):
    async def fake_run(url, *, work_dir, progress_queue=None, **_kw):
        work_dir.mkdir(parents=True, exist_ok=True)
        if progress_queue is not None:
            await progress_queue.put(None)
        from app import gallery_dl_runner
        return gallery_dl_runner.RunResult(
            returncode=returncode, stdout="", stderr=stderr,
            info=None, produced_files=[],
            image_files=[], metadata_files=[], extractor=None,
        )
    return fake_run


async def _wait_for_terminal(db_mod, job_id: str, max_iter: int = 80):
    for _ in range(max_iter):
        conn = db_mod.connect()
        try:
            row = conn.execute(
                "SELECT status, result_json FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        finally:
            conn.close()
        if row["status"] in ("done", "failed_permanent", "cancelled"):
            return row
        await asyncio.sleep(0.1)
    raise TimeoutError(f"job {job_id} never reached terminal: {row['status']}")


@pytest.mark.asyncio
async def test_gallery_runs_when_yt_dlp_finds_no_media(
    reload_modules, stub_pipeline, monkeypatch,
):
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner, gallery_dl_runner = stub_pipeline

    monkeypatch.setattr(ytdlp_runner, "run", _empty_yt_run_factory())
    monkeypatch.setattr(
        gallery_dl_runner, "run", _gallery_run_factory(["jpg", "png", "webp"]),
    )

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="Image investigation")
    finally:
        conn.close()

    job = await jobs_mod.orchestrator().submit(
        case_id=case.id, url="https://x.com/u/status/1",
    )
    row = await _wait_for_terminal(db_mod, job.id)
    assert row["status"] == "done"
    result = json.loads(row["result_json"])
    assert result["capture_kind"] == "gallery"

    # Verify the audit log got the right gallery actions.
    conn = db_mod.connect()
    try:
        actions = [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_log WHERE case_id = ? ORDER BY id",
                (case.id,),
            ).fetchall()
        ]
    finally:
        conn.close()
    assert "gallery.started" in actions
    assert "gallery.captured" in actions
    assert "download.created" in actions


@pytest.mark.asyncio
async def test_gallery_fallback_skipped_when_yt_dlp_succeeds(
    reload_modules, stub_pipeline, monkeypatch,
):
    """Video URLs (yt-dlp produces media) skip gallery-dl entirely.

    The gallery branch must NOT add latency or audit events when yt-dlp
    has already supplied a media file.
    """
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner, gallery_dl_runner = stub_pipeline

    async def yt_succeeds(url, *, case_dir, progress_queue=None, **_kw):
        case_dir.mkdir(parents=True, exist_ok=True)
        media = case_dir / "abc.mp4"
        media.write_bytes(b"FAKEMP4DATA")
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

    gallery_calls = {"n": 0}

    async def gallery_panic(*_a, **_kw):
        gallery_calls["n"] += 1
        raise AssertionError("gallery-dl must not be invoked when yt-dlp succeeded")

    monkeypatch.setattr(ytdlp_runner, "run", yt_succeeds)
    monkeypatch.setattr(gallery_dl_runner, "run", gallery_panic)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="Video investigation")
    finally:
        conn.close()

    job = await jobs_mod.orchestrator().submit(
        case_id=case.id, url="https://www.youtube.com/watch?v=abc",
    )
    row = await _wait_for_terminal(db_mod, job.id)
    assert row["status"] == "done"
    assert json.loads(row["result_json"])["capture_kind"] == "media"
    assert gallery_calls["n"] == 0  # gallery-dl never ran


@pytest.mark.asyncio
async def test_gallery_empty_outcome_finalizes_as_page_only(
    reload_modules, stub_pipeline, monkeypatch,
):
    """When neither yt-dlp nor gallery-dl find anything, capture is page_only."""
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner, gallery_dl_runner = stub_pipeline

    monkeypatch.setattr(ytdlp_runner, "run", _empty_yt_run_factory())
    monkeypatch.setattr(gallery_dl_runner, "run", _gallery_fail_factory(returncode=0))

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="Page only")
    finally:
        conn.close()

    job = await jobs_mod.orchestrator().submit(
        case_id=case.id, url="https://example.com/static",
    )
    row = await _wait_for_terminal(db_mod, job.id)
    assert row["status"] == "done"
    assert json.loads(row["result_json"])["capture_kind"] == "page_only"

    conn = db_mod.connect()
    try:
        actions = [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_log WHERE case_id = ? ORDER BY id",
                (case.id,),
            ).fetchall()
        ]
    finally:
        conn.close()
    assert "gallery.started" in actions
    assert "gallery.empty" in actions


@pytest.mark.asyncio
async def test_gallery_disabled_per_case_skips_gallery_dl(
    reload_modules, stub_pipeline, monkeypatch,
):
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner, gallery_dl_runner = stub_pipeline

    monkeypatch.setattr(ytdlp_runner, "run", _empty_yt_run_factory())

    gallery_calls = {"n": 0}

    async def gallery_panic(*_a, **_kw):
        gallery_calls["n"] += 1
        raise AssertionError("gallery-dl must not run when case opts out")

    monkeypatch.setattr(gallery_dl_runner, "run", gallery_panic)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(
            conn,
            name="No gallery",
            settings={"gallery_enabled": False},
        )
    finally:
        conn.close()

    job = await jobs_mod.orchestrator().submit(
        case_id=case.id, url="https://example.com/x",
    )
    row = await _wait_for_terminal(db_mod, job.id)
    assert row["status"] == "done"
    assert json.loads(row["result_json"])["capture_kind"] == "page_only"
    assert gallery_calls["n"] == 0


@pytest.mark.asyncio
async def test_force_gallery_run_attaches_images_to_media_capture(
    reload_modules, stub_pipeline, monkeypatch,
):
    """When the user opts in via DownloadOptions.force_gallery_run, gallery-dl
    runs alongside yt-dlp even though yt-dlp produced media. The capture's
    primary kind stays ``media`` (yt-dlp wins) and the gallery images attach
    as additional ``gallery_NNN`` artifacts on the same item.
    """
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner, gallery_dl_runner = stub_pipeline

    async def yt_succeeds(url, *, case_dir, progress_queue=None, **_kw):
        case_dir.mkdir(parents=True, exist_ok=True)
        media = case_dir / "abc.mp4"
        media.write_bytes(b"FAKEMP4DATA")
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

    monkeypatch.setattr(ytdlp_runner, "run", yt_succeeds)
    monkeypatch.setattr(
        gallery_dl_runner, "run", _gallery_run_factory(["jpg", "png"], extractor="generic"),
    )

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="Blog post with embedded video and photo set")
    finally:
        conn.close()

    job = await jobs_mod.orchestrator().submit(
        case_id=case.id,
        url="https://kal-akal.com/?p=436",
        download_options=jobs_mod.DownloadOptions(force_gallery_run=True),
    )
    row = await _wait_for_terminal(db_mod, job.id)
    assert row["status"] == "done"
    result = json.loads(row["result_json"])
    # yt-dlp wins primary; gallery rides as extra artifacts.
    assert result["capture_kind"] == "media"

    # The audit log records both the gallery start and the gallery capture
    # event, plus download.created with gallery_count and gallery_extractor.
    conn = db_mod.connect()
    try:
        actions = [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_log WHERE case_id = ? ORDER BY id",
                (case.id,),
            ).fetchall()
        ]
        # download.created details should carry the gallery counters even
        # though capture_kind is media.
        created_row = conn.execute(
            "SELECT details_json FROM audit_log "
            "WHERE case_id = ? AND action = 'download.created' "
            "ORDER BY id DESC LIMIT 1",
            (case.id,),
        ).fetchone()
    finally:
        conn.close()
    assert "gallery.started" in actions
    assert "gallery.captured" in actions
    details = json.loads(created_row["details_json"])
    assert details["capture_kind"] == "media"
    assert details["gallery_count"] == 2
    assert details["gallery_extractor"] == "generic"


@pytest.mark.asyncio
async def test_force_gallery_run_no_op_when_case_disables_gallery(
    reload_modules, stub_pipeline, monkeypatch,
):
    """Per-case gallery_enabled=False overrides force_gallery_run.

    The case-level toggle is the operator's escape hatch for an
    investigation that must not call gallery-dl at all (e.g. a sensitive
    site where any extra request is too noisy). It MUST win over the
    per-job opt-in.
    """
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner, gallery_dl_runner = stub_pipeline

    monkeypatch.setattr(ytdlp_runner, "run", _empty_yt_run_factory())

    gallery_calls = {"n": 0}

    async def gallery_panic(*_a, **_kw):
        gallery_calls["n"] += 1
        raise AssertionError("gallery-dl must not run when case opts out")

    monkeypatch.setattr(gallery_dl_runner, "run", gallery_panic)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(
            conn,
            name="No gallery, even with force_gallery_run",
            settings={"gallery_enabled": False},
        )
    finally:
        conn.close()

    job = await jobs_mod.orchestrator().submit(
        case_id=case.id,
        url="https://example.com/x",
        download_options=jobs_mod.DownloadOptions(force_gallery_run=True),
    )
    row = await _wait_for_terminal(db_mod, job.id)
    assert row["status"] == "done"
    assert json.loads(row["result_json"])["capture_kind"] == "page_only"
    assert gallery_calls["n"] == 0


# ---------------------------------------------------------------------------
# capture_mode tests (v0.10)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capture_mode_gallery_skips_ytdlp_forces_gallery_dl(
    reload_modules, stub_pipeline, monkeypatch,
):
    """capture_mode='gallery' → yt-dlp is skipped entirely; gallery-dl runs
    unconditionally and its images become the primary capture artifact."""
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner, gallery_dl_runner = stub_pipeline

    ytdlp_calls = {"n": 0}

    async def ytdlp_panic(*_a, **_kw):
        ytdlp_calls["n"] += 1
        raise AssertionError("yt-dlp must not run in gallery mode")

    monkeypatch.setattr(ytdlp_runner, "run", ytdlp_panic)
    monkeypatch.setattr(
        gallery_dl_runner, "run", _gallery_run_factory(["jpg", "png"]),
    )

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="Gallery mode test")
    finally:
        conn.close()

    job = await jobs_mod.orchestrator().submit(
        case_id=case.id,
        url="https://www.instagram.com/p/xyz/",
        download_options=jobs_mod.DownloadOptions(capture_mode="gallery"),
    )
    row = await _wait_for_terminal(db_mod, job.id)
    assert row["status"] == "done"
    result = json.loads(row["result_json"])
    assert result["capture_kind"] == "gallery"
    assert ytdlp_calls["n"] == 0  # yt-dlp never ran


@pytest.mark.asyncio
async def test_capture_mode_webpage_always_runs_gallery_dl_even_with_media(
    reload_modules, stub_pipeline, monkeypatch,
):
    """capture_mode='webpage' → gallery-dl runs even when yt-dlp produced media.

    Emphasis is on the page context; the video is incidental. Gallery images
    attach as additional artifacts on the item; capture_kind stays 'media'
    (yt-dlp wins as primary output).
    """
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner, gallery_dl_runner = stub_pipeline

    async def yt_succeeds(url, *, case_dir, progress_queue=None, **_kw):
        case_dir.mkdir(parents=True, exist_ok=True)
        media = case_dir / "abc.mp4"
        media.write_bytes(b"FAKEMP4DATA")
        info = case_dir / "abc.info.json"
        info.write_text(json.dumps({
            "id": "abc", "title": "Webpage video", "ext": "mp4",
            "extractor_key": "Generic",
        }))
        if progress_queue is not None:
            await progress_queue.put(None)
        return ytdlp_runner.RunResult(
            returncode=0, stdout="", stderr="",
            info=json.loads(info.read_text()),
            produced_files=[media, info],
        )

    gallery_calls = {"n": 0}

    async def gallery_runs(*_a, **_kw):
        gallery_calls["n"] += 1
        return await _gallery_run_factory(["jpg"])(*_a, **_kw)

    monkeypatch.setattr(ytdlp_runner, "run", yt_succeeds)
    monkeypatch.setattr(gallery_dl_runner, "run", gallery_runs)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="Webpage mode test")
    finally:
        conn.close()

    job = await jobs_mod.orchestrator().submit(
        case_id=case.id,
        url="https://example.com/article-with-embed",
        download_options=jobs_mod.DownloadOptions(capture_mode="webpage"),
    )
    row = await _wait_for_terminal(db_mod, job.id)
    assert row["status"] == "done"
    # yt-dlp found media → kind stays media; gallery images attach alongside
    assert json.loads(row["result_json"])["capture_kind"] == "media"
    assert gallery_calls["n"] == 1  # gallery-dl ran regardless of yt-dlp


@pytest.mark.asyncio
async def test_capture_mode_media_skips_gallery_when_ytdlp_succeeds(
    reload_modules, stub_pipeline, monkeypatch,
):
    """capture_mode='media' → gallery-dl is skipped when yt-dlp finds media
    (same as the current default fallback behaviour)."""
    db_mod, jobs_mod = reload_modules
    _, _, ytdlp_runner, gallery_dl_runner = stub_pipeline

    async def yt_succeeds(url, *, case_dir, progress_queue=None, **_kw):
        case_dir.mkdir(parents=True, exist_ok=True)
        media = case_dir / "vid.mp4"
        media.write_bytes(b"FAKEVIDEO")
        info = case_dir / "vid.info.json"
        info.write_text(json.dumps({
            "id": "vid", "title": "Video", "ext": "mp4",
            "extractor_key": "Youtube",
        }))
        if progress_queue is not None:
            await progress_queue.put(None)
        return ytdlp_runner.RunResult(
            returncode=0, stdout="", stderr="",
            info=json.loads(info.read_text()),
            produced_files=[media, info],
        )

    gallery_calls = {"n": 0}

    async def gallery_panic(*_a, **_kw):
        gallery_calls["n"] += 1
        raise AssertionError("gallery-dl must not run when yt-dlp succeeded in media mode")

    monkeypatch.setattr(ytdlp_runner, "run", yt_succeeds)
    monkeypatch.setattr(gallery_dl_runner, "run", gallery_panic)

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        from app import cases
        case = cases.create(conn, name="Media mode test")
    finally:
        conn.close()

    job = await jobs_mod.orchestrator().submit(
        case_id=case.id,
        url="https://www.youtube.com/watch?v=vid",
        download_options=jobs_mod.DownloadOptions(capture_mode="media"),
    )
    row = await _wait_for_terminal(db_mod, job.id)
    assert row["status"] == "done"
    assert json.loads(row["result_json"])["capture_kind"] == "media"
    assert gallery_calls["n"] == 0  # gallery-dl never ran
