"""Batch job submission and folder-reveal endpoints — Simple-mode plumbing.

The orchestrator path itself is exercised in ``test_api.py`` already; here we
focus on the request shape, dedupe, validation, and the auto-resolution of the
quick-captures case.
"""

from __future__ import annotations

import importlib

import httpx
import pytest


@pytest.fixture
async def client(capsule_dirs):
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
        async with main_mod.app.router.lifespan_context(main_mod.app):
            yield c


@pytest.mark.asyncio
async def test_batch_without_case_creates_quick_case(client):
    resp = await client.post(
        "/api/jobs/batch",
        json={"urls": ["https://example.com/one", "https://example.com/two"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["jobs"]) == 2
    assert body["jobs"][0]["case_id"] == body["case_id"]

    cases = (await client.get("/api/cases")).json()["cases"]
    quick = [c for c in cases if c["slug"] == "quick-captures"]
    assert len(quick) == 1
    assert quick[0]["id"] == body["case_id"]


@pytest.mark.asyncio
async def test_batch_dedupes_and_strips_blanks(client):
    resp = await client.post(
        "/api/jobs/batch",
        json={
            "urls": [
                "https://example.com/a",
                "  https://example.com/a  ",  # whitespace duplicate
                "",                              # empty
                "https://example.com/b",
            ]
        },
    )
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    assert len(jobs) == 2
    assert {j["url"] for j in jobs} == {
        "https://example.com/a",
        "https://example.com/b",
    }


@pytest.mark.asyncio
async def test_batch_routes_into_explicit_case(client):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    resp = await client.post(
        "/api/jobs/batch",
        json={"case_id": case["id"], "urls": ["https://example.com/x"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["case_id"] == case["id"]
    assert body["jobs"][0]["case_id"] == case["id"]


@pytest.mark.asyncio
async def test_batch_rejects_unknown_case(client):
    resp = await client.post(
        "/api/jobs/batch",
        json={"case_id": 9999, "urls": ["https://example.com/x"]},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_batch_rejects_empty_list(client):
    resp = await client.post("/api/jobs/batch", json={"urls": []})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_batch_rejects_oversized_list(client):
    urls = [f"https://example.com/{i}" for i in range(26)]
    resp = await client.post("/api/jobs/batch", json={"urls": urls})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_batch_rejects_all_blank_after_dedupe(client):
    resp = await client.post(
        "/api/jobs/batch",
        json={"urls": ["   ", "", "\t"]},
    )
    assert resp.status_code == 400


# --- /api/system/version paths block ----------------------------------------


@pytest.mark.asyncio
async def test_system_version_includes_paths(client):
    body = (await client.get("/api/system/version")).json()
    assert "paths" in body
    paths = body["paths"]
    assert paths["downloads_dir"]
    assert paths["quick_captures_dir"].endswith("quick-captures")
    assert isinstance(paths["can_reveal"], bool)


# --- /api/system/reveal -----------------------------------------------------


@pytest.mark.asyncio
async def test_reveal_rejects_path_traversal(client, monkeypatch):
    # Force can_reveal=True so the path-validation path is the one we exercise.
    from app import main as main_mod

    monkeypatch.setattr(main_mod, "_can_reveal", lambda: True)
    resp = await client.post(
        "/api/system/reveal", json={"relative_path": "../../etc/passwd"}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reveal_404_for_missing_path(client, monkeypatch):
    from app import main as main_mod

    monkeypatch.setattr(main_mod, "_can_reveal", lambda: True)
    resp = await client.post(
        "/api/system/reveal", json={"relative_path": "does-not-exist"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reveal_returns_no_desktop_when_unsupported(client, monkeypatch, capsule_dirs):
    from app import main as main_mod

    # Pre-create the quick case so the target dir exists.
    await client.post("/api/jobs/batch", json={"urls": ["https://example.com/x"]})
    monkeypatch.setattr(main_mod, "_can_reveal", lambda: False)
    resp = await client.post(
        "/api/system/reveal", json={"relative_path": "quick-captures"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"] == "no_desktop"


@pytest.mark.asyncio
async def test_reveal_succeeds_with_stubbed_opener(client, monkeypatch, capsule_dirs):
    """When the platform is supported, reveal Popens the OS opener."""
    import subprocess

    from app import main as main_mod

    await client.post("/api/jobs/batch", json={"urls": ["https://example.com/x"]})
    monkeypatch.setattr(main_mod, "_can_reveal", lambda: True)

    spawned: list[list[str]] = []

    class _StubPopen:
        def __init__(self, cmd, *args, **kwargs):
            spawned.append(list(cmd))

    monkeypatch.setattr(subprocess, "Popen", _StubPopen)

    resp = await client.post(
        "/api/system/reveal", json={"relative_path": "quick-captures"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert spawned and spawned[0][0] in {"open", "explorer", "xdg-open"}
    assert spawned[0][-1].endswith("quick-captures")
