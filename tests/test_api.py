"""End-to-end API tests — CLAUDE.md §2.

Drives the FastAPI app through ``httpx.AsyncClient`` over the ASGI
transport — no real network, no real yt-dlp. Job-related tests
monkeypatch ``ytdlp_runner.run`` and ``classify.classify`` so the
orchestrator can be exercised without external services.
"""

from __future__ import annotations

import importlib
import io
import json
from pathlib import Path

import httpx
import pytest


@pytest.fixture
async def client(capsule_dirs):
    # Reload everything that depends on config so ``main`` sees the tmp dirs.
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
        "app.jobs",
        "app.main",
    ):
        if name in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[name])

    from app import jobs as jobs_mod
    from app import main as main_mod
    from app import signing

    signing._reset_cache_for_tests()
    jobs_mod.reset_for_tests()

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        # Trigger startup hook (schema migration + keypair).
        async with main_mod.app.router.lifespan_context(main_mod.app):
            yield c


# --- Cases ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_list_cases(client):
    resp = await client.post(
        "/api/cases", json={"name": "Operation Sunrise"}
    )
    assert resp.status_code == 200
    case = resp.json()
    assert case["slug"] == "operation-sunrise"

    listing = await client.get("/api/cases")
    assert listing.status_code == 200
    items = listing.json()["cases"]
    assert {c["id"] for c in items} == {case["id"]}


@pytest.mark.asyncio
async def test_get_missing_case_returns_404(client):
    resp = await client.get("/api/cases/9999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rename_case(client):
    create = (await client.post("/api/cases", json={"name": "Old"})).json()
    resp = await client.patch(
        f"/api/cases/{create['id']}", json={"name": "New"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New"


@pytest.mark.asyncio
async def test_status_transitions(client):
    create = (await client.post("/api/cases", json={"name": "X"})).json()
    resp = await client.post(
        f"/api/cases/{create['id']}/status", json={"status": "archived"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"


@pytest.mark.asyncio
async def test_invalid_status_rejected(client):
    create = (await client.post("/api/cases", json={"name": "X"})).json()
    resp = await client.post(
        f"/api/cases/{create['id']}/status", json={"status": "deleted"}
    )
    assert resp.status_code == 400


# --- Cookies ----------------------------------------------------------------


SAMPLE_COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t9999999999\tSID\tSECRET_VALUE_42\n"
)


@pytest.mark.asyncio
async def test_upload_cookies_and_retrieve_summary(client):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    files = {"file": ("cookies.txt", SAMPLE_COOKIES.encode())}
    data = {"case_id": str(case["id"])}
    resp = await client.post("/api/cookies", data=data, files=files)
    assert resp.status_code == 200
    summary = resp.json()["summary"]
    assert summary["total_cookies"] == 1
    assert summary["domains"][0]["domain"] == "youtube.com"

    # Sanity: no cookie value leaks in the JSON response.
    assert "SECRET_VALUE_42" not in resp.text

    fetch = await client.get("/api/cookies", params={"case_id": case["id"]})
    assert fetch.status_code == 200
    assert fetch.json()["summary"]["total_cookies"] == 1


@pytest.mark.asyncio
async def test_audit_log_records_cookie_upload_without_values(client, capsule_dirs):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    files = {"file": ("cookies.txt", SAMPLE_COOKIES.encode())}
    data = {"case_id": str(case["id"])}
    await client.post("/api/cookies", data=data, files=files)

    audit_resp = await client.get("/api/audit", params={"case_id": case["id"]})
    actions = [e["action"] for e in audit_resp.json()["entries"]]
    assert "cookies.uploaded" in actions
    # The raw audit JSON must contain the *domain* but not the value.
    raw = audit_resp.text
    assert "youtube.com" in raw
    assert "SECRET_VALUE_42" not in raw


# --- System ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_version(client):
    resp = await client.get("/api/system/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["app"]
    assert "signing_key_fingerprint" in body
    assert len(body["signing_key_fingerprint"]) == 32


@pytest.mark.asyncio
async def test_i18n_endpoint_still_works(client):
    resp = await client.get("/api/i18n/ar")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dir"] == "rtl"
    assert "errors.unknown" in body["messages"]


# --- Jobs (mocked pipeline) -------------------------------------------------


@pytest.mark.asyncio
async def test_jobs_happy_path_with_mocked_pipeline(client, monkeypatch, capsule_dirs):
    """Submit a job; mock classify + ytdlp_runner so no network/processes
    are touched. Asserts the job reaches ``done`` and produces a DB row.
    """
    import asyncio

    from app import capture as capture_mod
    from app import classify as classify_mod
    from app import jobs as jobs_mod
    from app import ytdlp_runner

    case = (await client.post("/api/cases", json={"name": "Ops"})).json()

    async def fake_classify(url, *, case_slug=None, client=None):
        return classify_mod.Classification(
            url_submitted=url,
            url_final=url,
            redirect_chain=[url],
            platform="youtube",
            authenticated_domains=[],
            url_hash="0123456789ab",
        )

    async def fake_run(url, *, case_dir, cookies_file=None, format_spec=None,
                      progress_queue=None, extra_args=None, executable=None,
                      env=None, **_extra):
        case_dir.mkdir(parents=True, exist_ok=True)
        media = case_dir / "abc.mp4"
        media.write_bytes(b"FAKE")
        info_path = case_dir / "abc.info.json"
        info = {
            "id": "abc",
            "title": "Hello",
            "ext": "mp4",
            "extractor_key": "Youtube",
            "uploader": "veritasium",
            "upload_date": "20240812",
        }
        info_path.write_text(json.dumps(info))
        if progress_queue is not None:
            await progress_queue.put(None)
        return ytdlp_runner.RunResult(
            returncode=0, stdout="", stderr="",
            info=info,
            produced_files=[media, info_path],
        )

    async def fake_version() -> str:
        return "9999.0.0"

    async def fake_capture(*, url, case_slug, work_dir=None, timeout_ms=60000, **_extra):
        # Skip the real Playwright launch — return an empty bundle so
        # postprocess records the absent page artifacts in meta.json.
        return capture_mod.CaptureBundle(
            mhtml=None, screenshot=None, warc=None,
            chromium_version="0", browsertrix_version="0",
            page_title=None, response_headers=None,
        )

    monkeypatch.setattr(classify_mod, "classify", fake_classify)
    monkeypatch.setattr(ytdlp_runner, "run", fake_run)
    monkeypatch.setattr(ytdlp_runner, "version", fake_version)
    monkeypatch.setattr(capture_mod, "capture_page", fake_capture)

    resp = await client.post(
        "/api/jobs", json={"case_id": case["id"], "url": "https://www.youtube.com/watch?v=abc"}
    )
    assert resp.status_code == 200
    job = resp.json()

    # Wait for the orchestrator to finish.
    for _ in range(50):
        cur = (await client.get(f"/api/jobs/{job['id']}")).json()
        if cur["status"] in ("done", "failed_permanent", "cancelled"):
            break
        await asyncio.sleep(0.05)
    assert cur["status"] == "done", cur

    # Library now has one row.
    library = (await client.get("/api/library")).json()
    assert len(library["items"]) == 1
    assert library["items"][0]["platform"] == "youtube"

    # And the audit chain is intact.
    audit = (await client.get("/api/audit")).json()
    assert audit["chain_ok"] is True
    actions = [e["action"] for e in audit["entries"]]
    assert "download.created" in actions


@pytest.mark.asyncio
async def test_library_verify_after_capture(client, monkeypatch, capsule_dirs):
    """Run a mocked capture, then call /api/library/verify. Should be all-green."""
    import asyncio

    from app import capture as capture_mod
    from app import classify as classify_mod
    from app import ytdlp_runner

    case = (await client.post("/api/cases", json={"name": "Ops"})).json()

    async def fake_classify(url, *, case_slug=None, client=None):
        return classify_mod.Classification(
            url_submitted=url, url_final=url, redirect_chain=[url],
            platform="youtube", authenticated_domains=[],
            url_hash="0123456789ab",
        )

    async def fake_run(url, *, case_dir, cookies_file=None, format_spec=None,
                      progress_queue=None, extra_args=None, executable=None,
                      env=None, **_extra):
        case_dir.mkdir(parents=True, exist_ok=True)
        media = case_dir / "abc.mp4"
        media.write_bytes(b"FAKEDATA")
        info_path = case_dir / "abc.info.json"
        info_path.write_text(json.dumps({
            "id": "abc", "title": "Hello", "ext": "mp4",
            "extractor_key": "Youtube", "upload_date": "20240812",
        }))
        if progress_queue is not None:
            await progress_queue.put(None)
        return ytdlp_runner.RunResult(
            returncode=0, stdout="", stderr="",
            info=json.loads(info_path.read_text()),
            produced_files=[media, info_path],
        )

    async def fake_version() -> str:
        return "9999.0.0"

    async def fake_capture(*, url, case_slug, work_dir=None, timeout_ms=60000, **_extra):
        return capture_mod.CaptureBundle(
            mhtml=None, screenshot=None, warc=None,
            chromium_version="0", browsertrix_version="0",
            page_title=None, response_headers=None,
        )

    monkeypatch.setattr(classify_mod, "classify", fake_classify)
    monkeypatch.setattr(ytdlp_runner, "run", fake_run)
    monkeypatch.setattr(ytdlp_runner, "version", fake_version)
    monkeypatch.setattr(capture_mod, "capture_page", fake_capture)

    resp = await client.post(
        "/api/jobs", json={"case_id": case["id"], "url": "https://www.youtube.com/watch?v=abc"}
    )
    job = resp.json()
    for _ in range(50):
        cur = (await client.get(f"/api/jobs/{job['id']}")).json()
        if cur["status"] in ("done", "failed_permanent", "cancelled"):
            break
        await asyncio.sleep(0.05)
    assert cur["status"] == "done"

    verify = (await client.post("/api/library/verify")).json()
    assert all(r["ok"] for r in verify["results"]), verify
