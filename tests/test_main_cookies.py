"""Endpoint tests for the cookies fallback upload — CLAUDE.md §11."""

from __future__ import annotations

import importlib

import httpx
import pytest


SAMPLE = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t9999999999\tSID\tSECRET_VALUE_42\n"
    ".x.com\tTRUE\t/\tTRUE\t9999999999\tauth_token\tTOK_42\n"
)


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
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        async with main_mod.app.router.lifespan_context(main_mod.app):
            yield c


# --- /api/cookies (multipart, the documented fallback per CLAUDE.md §11) ---


@pytest.mark.asyncio
async def test_multipart_accepts_optional_target_url(client):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    files = {"file": ("cookies.txt", SAMPLE.encode())}
    data = {"case_id": str(case["id"]), "target_url": "https://x.com/foo"}
    resp = await client.post("/api/cookies", data=data, files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["target"]["covered"] is True

    # And audit captures the target URL.
    audit = await client.get("/api/audit", params={"case_id": case["id"]})
    assert "x.com/foo" in audit.text


@pytest.mark.asyncio
async def test_multipart_still_works_without_target_url(client):
    """Backward-compat: existing simple-upload UX must keep working unchanged."""
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    files = {"file": ("cookies.txt", SAMPLE.encode())}
    data = {"case_id": str(case["id"])}
    resp = await client.post("/api/cookies", data=data, files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["total_cookies"] == 2
    assert body["target"] is None


@pytest.mark.asyncio
async def test_multipart_rejects_malformed_file(client):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    # A line with too few tab-separated fields cannot be a Netscape record.
    files = {"file": ("cookies.txt", b".x.com\tinvalid\n")}
    data = {"case_id": str(case["id"])}
    resp = await client.post("/api/cookies", data=data, files=files)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_multipart_audit_excludes_cookie_values(client):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    files = {"file": ("cookies.txt", SAMPLE.encode())}
    data = {"case_id": str(case["id"])}
    await client.post("/api/cookies", data=data, files=files)
    audit = (await client.get("/api/audit", params={"case_id": case["id"]})).text
    assert "SECRET_VALUE_42" not in audit and "TOK_42" not in audit
    assert "x.com" in audit and "youtube.com" in audit
