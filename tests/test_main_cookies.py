"""Endpoint tests for the cookies wizard — CLAUDE.md §11."""

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


# --- /api/cookies/preview ---------------------------------------------------


@pytest.mark.asyncio
async def test_preview_returns_summary_no_save(client, capsule_dirs):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    resp = await client.post(
        "/api/cookies/preview",
        json={"content": SAMPLE, "target_url": "https://x.com/foo"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["total_cookies"] == 2
    assert body["target"]["covered"] is True
    assert body["errors"] == []
    # No file was saved on the case.
    from app import cookies as cookies_mod
    assert not cookies_mod.exists(case["slug"])
    # Sanity: cookie values never appear in the preview response.
    assert "SECRET_VALUE_42" not in resp.text
    assert "TOK_42" not in resp.text


@pytest.mark.asyncio
async def test_preview_malformed_returns_errors_200(client):
    resp = await client.post(
        "/api/cookies/preview",
        json={"content": "not a cookies file\n"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] is None
    assert body["target"] is None
    assert body["errors"]


@pytest.mark.asyncio
async def test_preview_with_unrelated_target_reports_coverage(client):
    resp = await client.post(
        "/api/cookies/preview",
        json={"content": SAMPLE, "target_url": "https://example.org/x"},
    )
    body = resp.json()
    assert body["target"]["target_domain"] == "example.org"
    assert body["target"]["covered"] is False
    assert body["target"]["matched_domains"] == []


@pytest.mark.asyncio
async def test_preview_without_target_returns_neutral(client):
    resp = await client.post(
        "/api/cookies/preview",
        json={"content": SAMPLE},
    )
    body = resp.json()
    assert body["summary"]["total_cookies"] == 2
    assert body["target"] is None


# --- /api/cookies/text ------------------------------------------------------


@pytest.mark.asyncio
async def test_text_endpoint_saves_and_audits(client, capsule_dirs):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    resp = await client.post(
        "/api/cookies/text",
        json={
            "case_id": case["id"],
            "content": SAMPLE,
            "target_url": "https://x.com/some/post",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["total_cookies"] == 2
    assert body["target"]["covered"] is True

    # File saved on disk.
    from app import cookies as cookies_mod
    assert cookies_mod.exists(case["slug"])

    # Re-fetch via the read endpoint matches.
    fetched = (await client.get("/api/cookies", params={"case_id": case["id"]})).json()
    assert fetched["summary"]["total_cookies"] == 2


@pytest.mark.asyncio
async def test_text_endpoint_audit_excludes_values(client, capsule_dirs):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    await client.post(
        "/api/cookies/text",
        json={
            "case_id": case["id"],
            "content": SAMPLE,
            "target_url": "https://x.com/some/post",
        },
    )
    audit = await client.get("/api/audit", params={"case_id": case["id"]})
    raw = audit.text
    assert "cookies.uploaded" in raw
    assert "youtube.com" in raw
    # Target URL is not sensitive — investigators want to know what they
    # were trying to cover. Values, however, must never leak.
    assert "x.com/some/post" in raw
    assert "SECRET_VALUE_42" not in raw
    assert "TOK_42" not in raw


@pytest.mark.asyncio
async def test_text_endpoint_rejects_malformed(client):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    resp = await client.post(
        "/api/cookies/text",
        json={"case_id": case["id"], "content": "garbage\n"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_text_endpoint_404_on_unknown_case(client):
    resp = await client.post(
        "/api/cookies/text",
        json={"case_id": 99999, "content": SAMPLE},
    )
    assert resp.status_code == 404


# --- /api/cookies (multipart) — backward compat + new target_url field -----


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


# --- Merge mode -------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_endpoint_merge_mode_appends(client):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    # First save (replace) — establishes the baseline.
    await client.post(
        "/api/cookies/text",
        json={"case_id": case["id"], "content": SAMPLE},
    )
    # Now merge in cookies for a new domain.
    extra = (
        "# Netscape HTTP Cookie File\n"
        ".reddit.com\tTRUE\t/\tTRUE\t9999999999\treddit_session\tNEW\n"
    )
    resp = await client.post(
        "/api/cookies/text",
        json={"case_id": case["id"], "content": extra, "mode": "merge"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["merge_stats"] == {"added": 1, "replaced": 0, "kept": 2}
    domains = {d["domain"] for d in body["summary"]["domains"]}
    assert domains == {"youtube.com", "x.com", "reddit.com"}


@pytest.mark.asyncio
async def test_text_endpoint_merge_mode_replaces_overlapping(client):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    initial = (
        "# Netscape HTTP Cookie File\n"
        ".x.com\tTRUE\t/\tTRUE\t1\tauth_token\tEXPIRED\n"
    )
    await client.post(
        "/api/cookies/text",
        json={"case_id": case["id"], "content": initial},
    )
    refreshed = (
        "# Netscape HTTP Cookie File\n"
        ".x.com\tTRUE\t/\tTRUE\t9999999999\tauth_token\tFRESH\n"
    )
    resp = await client.post(
        "/api/cookies/text",
        json={"case_id": case["id"], "content": refreshed, "mode": "merge"},
    )
    body = resp.json()
    assert body["merge_stats"] == {"added": 0, "replaced": 1, "kept": 0}
    # After merge, x.com is no longer expired.
    by_domain = {d["domain"]: d for d in body["summary"]["domains"]}
    assert by_domain["x.com"]["has_expired"] is False
    # And no value leakage in the response.
    assert "EXPIRED" not in resp.text and "FRESH" not in resp.text


@pytest.mark.asyncio
async def test_text_endpoint_merge_with_no_existing_file_acts_like_replace(client):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    resp = await client.post(
        "/api/cookies/text",
        json={"case_id": case["id"], "content": SAMPLE, "mode": "merge"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # 2 cookies added, nothing existed to replace or keep.
    assert body["merge_stats"] == {"added": 2, "replaced": 0, "kept": 0}


@pytest.mark.asyncio
async def test_text_endpoint_invalid_mode_400(client):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    resp = await client.post(
        "/api/cookies/text",
        json={"case_id": case["id"], "content": SAMPLE, "mode": "wipe"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_text_endpoint_merge_audits_mode_and_counts(client):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    await client.post(
        "/api/cookies/text",
        json={"case_id": case["id"], "content": SAMPLE},
    )
    extra = (
        "# Netscape HTTP Cookie File\n"
        ".reddit.com\tTRUE\t/\tTRUE\t9999999999\treddit_session\tR\n"
    )
    await client.post(
        "/api/cookies/text",
        json={"case_id": case["id"], "content": extra, "mode": "merge"},
    )
    audit = (await client.get("/api/audit", params={"case_id": case["id"]})).json()
    cookie_entries = [e for e in audit["entries"] if e["action"] == "cookies.uploaded"]
    assert len(cookie_entries) == 2
    # First was replace, second was merge.
    modes = [e["details"]["mode"] for e in cookie_entries]
    assert "replace" in modes and "merge" in modes
    merge_entry = next(e for e in cookie_entries if e["details"]["mode"] == "merge")
    assert merge_entry["details"]["added"] == 1
    assert merge_entry["details"]["replaced"] == 0
    assert merge_entry["details"]["kept"] == 2


@pytest.mark.asyncio
async def test_preview_returns_merge_block_when_case_id_and_mode_given(client):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    await client.post(
        "/api/cookies/text",
        json={"case_id": case["id"], "content": SAMPLE},
    )
    extra = (
        "# Netscape HTTP Cookie File\n"
        ".reddit.com\tTRUE\t/\tTRUE\t9999999999\treddit_session\tR\n"
    )
    resp = await client.post(
        "/api/cookies/preview",
        json={
            "content": extra,
            "case_id": case["id"],
            "mode": "merge",
        },
    )
    body = resp.json()
    assert body["merge_preview"] is not None
    mp = body["merge_preview"]
    assert mp["added"] == 1
    assert mp["replaced"] == 0
    assert mp["kept"] == 2
    assert mp["resulting_summary"]["total_cookies"] == 3
    domains = {d["domain"] for d in mp["resulting_summary"]["domains"]}
    assert domains == {"youtube.com", "x.com", "reddit.com"}


@pytest.mark.asyncio
async def test_preview_omits_merge_block_when_mode_replace(client):
    case = (await client.post("/api/cases", json={"name": "Ops"})).json()
    await client.post(
        "/api/cookies/text",
        json={"case_id": case["id"], "content": SAMPLE},
    )
    resp = await client.post(
        "/api/cookies/preview",
        json={
            "content": SAMPLE,
            "case_id": case["id"],
            "mode": "replace",
        },
    )
    body = resp.json()
    assert body["merge_preview"] is None
