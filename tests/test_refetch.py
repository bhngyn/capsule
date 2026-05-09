"""Re-fetch archive / media into an existing library item — plan §U6 Phase D."""

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
    from app import postprocess
    from app import signing
    signing._reset_cache_for_tests()
    signing.ensure_keypair()
    jobs_mod.reset_for_tests()
    return db_mod, jobs_mod, postprocess


@pytest.mark.asyncio
async def test_extend_capture_adds_warc_and_resigns(reload_modules, capsule_dirs):
    """A bare 'page_only' item gets a WARC added; meta.json is re-signed
    and the audit chain records ``meta.updated.page_warc``."""
    db_mod, jobs_mod, postprocess = reload_modules
    from app import audit, cases, paths, signing
    from app.postprocess import CaptureInput

    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        case = cases.create(conn, name="P")

        # Manufacture a minimal page_only download via finalize().
        ci = CaptureInput(
            case=case,
            job_uuid="job-x",
            url_submitted="https://example.com/p",
            url_final="https://example.com/p",
            redirect_chain=["https://example.com/p"],
            capture_date=postprocess.utc_now(),
            media_files=[],
            info_json=None,
            extra_sidecars=[],
            page_mhtml=None,
            page_screenshot=None,
            page_warc=None,
            authenticated_domains=[],
            chromium_version="138.0.0.0",
            browsertrix_version="1.5.0",
            ytdlp_version="9999.0.0",
        )
        result = postprocess.finalize(conn, ci)
        download_id = result.download_id

        # Drop a fake WARC into a tmp dir + extend.
        warc = capsule_dirs["downloads"] / "tmpwarc" / "page.warc.gz"
        warc.parent.mkdir(parents=True, exist_ok=True)
        warc.write_bytes(b"WARC/1.1\nfake-warc-bytes\n")

        out = postprocess.extend_capture(
            conn,
            download_id=download_id,
            role="page_warc",
            source=warc,
            actor="user",
        )

        # WARC landed in Captures/ under the canonical name (v0.8 layout).
        item_dir = capsule_dirs["downloads"] / result.relative_item_dir
        warc_target = item_dir / "Captures" / f"{result.stem}.page.warc.gz"
        assert warc_target.is_file()
        assert out["role"] == "page_warc"
        assert out["sha256"]

        # meta.json (in Metadata/) now lists the new role; signature is valid.
        meta_path = item_dir / "Metadata" / f"{result.stem}.meta.json"
        sig_path = item_dir / "Metadata" / f"{result.stem}.meta.json.sig"
        assert meta_path.is_file() and sig_path.is_file()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "page_warc" in meta["artifacts"]
        assert "page_warc" in meta["checksums"]
        assert meta["checksums"]["page_warc"]["sha256"] == out["sha256"]
        assert any(h["role"] == "page_warc" for h in meta["update_history"])
        assert signing.verify(meta_path.read_bytes(), sig_path.read_bytes())

        # checksums.txt reflects both the original artifacts and the new WARC.
        cs = (item_dir / "Metadata" / f"{result.stem}.checksums.txt").read_text(encoding="utf-8")
        assert out["sha256"] in cs

        # Audit row appended.
        actions = [r["action"] for r in audit.iter_entries(conn)]
        assert "meta.updated.page_warc" in actions
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_archive_refetch_via_orchestrator(reload_modules, capsule_dirs, monkeypatch):
    db_mod, jobs_mod, postprocess = reload_modules
    from app import cases
    from app import capture as capture_mod

    # Stash an existing item so the orchestrator has something to refetch.
    conn = db_mod.connect()
    try:
        db_mod.migrate(conn)
        case = cases.create(conn, name="P")
        result = postprocess.finalize(
            conn,
            postprocess.CaptureInput(
                case=case, job_uuid="seed",
                url_submitted="https://example.com/r",
                url_final="https://example.com/r",
                redirect_chain=["https://example.com/r"],
                capture_date=postprocess.utc_now(),
                media_files=[], info_json=None, extra_sidecars=[],
                page_mhtml=None, page_screenshot=None, page_warc=None,
                authenticated_domains=[],
                chromium_version="0", browsertrix_version="0",
                ytdlp_version="9999.0.0",
            ),
        )
    finally:
        conn.close()

    # Stub browsertrix so refetch produces a fake WARC quickly.
    async def fake_warc(*, url, out_dir, case_cookies_path, **_kw):
        target = out_dir / "page.warc.gz"
        target.write_bytes(b"WARC/1.1\nstub\n")
        return target, "1.2.3"

    monkeypatch.setattr(capture_mod, "_browsertrix_warc", fake_warc)

    orch = jobs_mod.orchestrator()
    group_id = jobs_mod.ensure_capture_group(
        db_mod.connect(),
        case_id=case.id,
        url="https://example.com/r",
        download_id=result.download_id,
    )
    job = await orch.submit(
        case_id=case.id,
        url="https://example.com/r",
        task_kind=jobs_mod.TASK_ARCHIVE,
        capture_group_id=group_id,
    )

    for _ in range(60):
        conn = db_mod.connect()
        try:
            row = conn.execute(
                "SELECT status, task_kind, capture_group_id FROM jobs WHERE id = ?",
                (job.id,),
            ).fetchone()
        finally:
            conn.close()
        if row["status"] in ("done", "failed_permanent"):
            break
        await asyncio.sleep(0.1)
    assert row["status"] == "done"
    assert row["task_kind"] == "archive"
    assert row["capture_group_id"] == group_id
