"""End-to-end tests for the extension API surface (CLAUDE.md §11; plan)."""

from __future__ import annotations

import importlib
import json

import httpx
import pytest


@pytest.fixture
async def client(capsule_dirs, monkeypatch):
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
        "app.extension_tokens",
        "app.main",
    ):
        if name in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[name])

    from app import jobs as jobs_mod
    from app import main as main_mod
    from app import signing

    signing._reset_cache_for_tests()
    jobs_mod.reset_for_tests()

    # Stub the orchestrator's submit so /api/extension/capture doesn't kick
    # off real classify/capture/yt-dlp work.
    class _StubJob:
        def __init__(self, case_id, url):
            import uuid
            self.id = str(uuid.uuid4())
            self.case_id = case_id
            self.url = url
            self.status = "queued"
            self.phase = None
            self.attempts = 0
            self.classification = None
            self.result = None
            self.error = None
            self.last_error_kind = None
            self.last_error_severity = None
            self.next_retry_at = None
            self.created_at = "2026-05-06T00:00:00+00:00"
            self.updated_at = self.created_at

        def to_dict(self):
            return {
                "id": self.id, "case_id": self.case_id, "url": self.url,
                "status": self.status, "phase": self.phase,
                "attempts": self.attempts, "classification": self.classification,
                "result": self.result, "error": self.error,
                "last_error_kind": self.last_error_kind,
                "last_error_severity": self.last_error_severity,
                "next_retry_at": self.next_retry_at,
                "created_at": self.created_at, "updated_at": self.updated_at,
            }

    class _StubOrchestrator:
        async def submit(self, *, case_id, url):
            return _StubJob(case_id, url)

        async def rehydrate(self):
            return []

    monkeypatch.setattr(jobs_mod, "_orchestrator", None, raising=False)
    monkeypatch.setattr(jobs_mod, "orchestrator", lambda: _StubOrchestrator())

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        async with main_mod.app.router.lifespan_context(main_mod.app):
            yield c


# --- Pairing --------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_returns_token_and_fingerprint(client):
    resp = await client.post("/api/extension/pair", json={"label": "My laptop"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["token_id"]
    assert body["server_fingerprint"]
    assert len(body["server_fingerprint"]) == 32


@pytest.mark.asyncio
async def test_pair_rejects_empty_label(client):
    resp = await client.post("/api/extension/pair", json={"label": ""})
    assert resp.status_code == 422  # Pydantic validation


@pytest.mark.asyncio
async def test_pair_logs_audit(client):
    await client.post("/api/extension/pair", json={"label": "A"})
    audit = (await client.get("/api/audit")).json()
    actions = [e["action"] for e in audit["entries"]]
    assert "extension.paired" in actions


@pytest.mark.asyncio
async def test_token_list_round_trip(client):
    await client.post("/api/extension/pair", json={"label": "A"})
    await client.post("/api/extension/pair", json={"label": "B"})
    body = (await client.get("/api/extension/tokens")).json()
    labels = sorted(t["label"] for t in body["tokens"])
    assert labels == ["A", "B"]


@pytest.mark.asyncio
async def test_revoke_removes_token(client):
    pair = (await client.post("/api/extension/pair", json={"label": "A"})).json()
    tokens_before = (await client.get("/api/extension/tokens")).json()["tokens"]
    assert len(tokens_before) == 1

    revoke = await client.delete(f"/api/extension/pair/{pair['token_id']}")
    assert revoke.status_code == 200
    tokens_after = (await client.get("/api/extension/tokens")).json()["tokens"]
    assert tokens_after == []


@pytest.mark.asyncio
async def test_revoke_unknown_token_404s(client):
    resp = await client.delete("/api/extension/pair/does-not-exist")
    assert resp.status_code == 404


# --- Bearer-token auth ----------------------------------------------------


@pytest.mark.asyncio
async def test_extension_cases_requires_token(client):
    resp = await client.get("/api/extension/cases")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_extension_cases_rejects_bogus_token(client):
    resp = await client.get(
        "/api/extension/cases",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_extension_cases_returns_open_cases_with_valid_token(client):
    pair = (await client.post("/api/extension/pair", json={"label": "A"})).json()
    await client.post("/api/cases", json={"name": "Op One"})
    await client.post("/api/cases", json={"name": "Op Two"})
    resp = await client.get(
        "/api/extension/cases",
        headers={"Authorization": f"Bearer {pair['token']}"},
    )
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()["cases"]]
    assert {"Op One", "Op Two"} <= set(names)


# --- /api/cookies/json ----------------------------------------------------


@pytest.mark.asyncio
async def test_cookies_json_persists_via_extension_path(client, capsule_dirs):
    pair = (await client.post("/api/extension/pair", json={"label": "A"})).json()
    case = (await client.post("/api/cases", json={"name": "Op"})).json()
    body = {
        "case_id": case["id"],
        "target_url": "https://youtube.com/",
        "cookies": [
            {
                "name": "SID",
                "value": "secret",
                "domain": "youtube.com",
                "path": "/",
                "expirationDate": 9999999999,
                "secure": True,
                "httpOnly": False,
                "hostOnly": False,
            },
            {
                "name": "HSID",
                "value": "httponly-secret",
                "domain": "youtube.com",
                "path": "/",
                "expirationDate": 9999999999,
                "secure": True,
                "httpOnly": True,
                "hostOnly": False,
            },
        ],
    }
    resp = await client.post(
        "/api/cookies/json",
        json=body,
        headers={"Authorization": f"Bearer {pair['token']}"},
    )
    assert resp.status_code == 200
    summary = resp.json()["summary"]
    assert summary["total_cookies"] == 2
    # Disk file is the same one yt-dlp/browsertrix expect.
    cookies_file = capsule_dirs["config"] / "cases" / case["slug"] / "cookies.txt"
    text = cookies_file.read_text()
    assert text.startswith("# Netscape HTTP Cookie File")
    assert "#HttpOnly_" in text
    # Audit details record the domain but never the cookie values.
    audit = (await client.get("/api/audit")).json()
    cookie_actions = [e for e in audit["entries"] if e["action"] == "cookies.uploaded"]
    assert cookie_actions
    last = cookie_actions[-1]
    assert "secret" not in json.dumps(last["details"])
    assert "youtube.com" in last["details"]["domains"]


@pytest.mark.asyncio
async def test_cookies_json_requires_token(client):
    resp = await client.post(
        "/api/cookies/json",
        json={"case_id": 1, "cookies": []},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_cookies_json_rejects_malformed_cookie(client):
    pair = (await client.post("/api/extension/pair", json={"label": "A"})).json()
    case = (await client.post("/api/cases", json={"name": "Op"})).json()
    # Pydantic enforces float | None on expirationDate, so a typed string
    # fails the schema (422) before reaching cookies.write_json. Send a
    # malformed *value* instead — embedded tab — to trigger the converter's
    # own ValueError → HTTP 400 path.
    resp = await client.post(
        "/api/cookies/json",
        json={
            "case_id": case["id"],
            "cookies": [{
                "name": "x",
                "value": "v\tinjected",
                "domain": "example.com",
            }],
        },
        headers={"Authorization": f"Bearer {pair['token']}"},
    )
    assert resp.status_code == 400


# --- /api/extension/capture -----------------------------------------------


@pytest.mark.asyncio
async def test_extension_capture_submits_jobs_and_writes_cookies(client, capsule_dirs):
    pair = (await client.post("/api/extension/pair", json={"label": "A"})).json()
    case = (await client.post("/api/cases", json={"name": "Op"})).json()
    resp = await client.post(
        "/api/extension/capture",
        json={
            "case_id": case["id"],
            "urls": ["https://example.com/a", "https://example.com/b"],
            "cookies": [
                {"name": "SID", "value": "s",
                 "domain": "example.com", "path": "/",
                 "expirationDate": 9999999999, "secure": True,
                 "httpOnly": False, "hostOnly": False},
            ],
        },
        headers={"Authorization": f"Bearer {pair['token']}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["jobs"]) == 2
    assert all(j["url"].startswith("https://example.com/") for j in body["jobs"])
    assert len(body["event_urls"]) == 2

    # Cookies written to disk under the right case slug.
    assert (capsule_dirs["config"] / "cases" / case["slug"] / "cookies.txt").is_file()


@pytest.mark.asyncio
async def test_extension_capture_caps_at_25_urls(client):
    pair = (await client.post("/api/extension/pair", json={"label": "A"})).json()
    case = (await client.post("/api/cases", json={"name": "Op"})).json()
    urls = [f"https://example.com/{i}" for i in range(26)]
    resp = await client.post(
        "/api/extension/capture",
        json={"case_id": case["id"], "urls": urls},
        headers={"Authorization": f"Bearer {pair['token']}"},
    )
    assert resp.status_code == 422  # max_length=25 on the schema


@pytest.mark.asyncio
async def test_extension_capture_requires_token(client):
    resp = await client.post(
        "/api/extension/capture",
        json={"case_id": 1, "urls": ["https://example.com/"]},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_extension_capture_rejects_unknown_case(client):
    pair = (await client.post("/api/extension/pair", json={"label": "A"})).json()
    resp = await client.post(
        "/api/extension/capture",
        json={"case_id": 9999, "urls": ["https://example.com/"]},
        headers={"Authorization": f"Bearer {pair['token']}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_extension_capture_dedupes_urls(client):
    pair = (await client.post("/api/extension/pair", json={"label": "A"})).json()
    case = (await client.post("/api/cases", json={"name": "Op"})).json()
    resp = await client.post(
        "/api/extension/capture",
        json={
            "case_id": case["id"],
            "urls": [
                "https://example.com/a",
                "https://example.com/a",  # duplicate
                "https://example.com/b",
            ],
        },
        headers={"Authorization": f"Bearer {pair['token']}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["jobs"]) == 2


@pytest.mark.asyncio
async def test_extension_capture_with_live_capture_stashes_bundle(client, capsule_dirs, monkeypatch):
    """The live-capture payload should be materialised onto a tmpdir under
    config/extension_inbox/ and a UserBrowserBundle should be attached to
    the matching job."""
    from app import jobs as jobs_mod

    pair = (await client.post("/api/extension/pair", json={"label": "ext"})).json()
    case = (await client.post("/api/cases", json={"name": "Op"})).json()

    captured_attach = []
    real_attach = jobs_mod.attach_user_browser_bundle

    def spy(job_id, bundle):
        captured_attach.append((job_id, bundle))
        return real_attach(job_id, bundle)

    monkeypatch.setattr(jobs_mod, "attach_user_browser_bundle", spy)

    import base64
    sample_mhtml = base64.b64encode(b"<html><!-- live --></html>").decode("ascii")
    sample_png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")
    resp = await client.post(
        "/api/extension/capture",
        json={
            "case_id": case["id"],
            "urls": ["https://example.com/a"],
            "live_captures": [
                {
                    "url": "https://example.com/a",
                    "mhtml_b64": sample_mhtml,
                    "screenshot_b64": sample_png,
                    "har": {"page": {"url": "https://example.com/a"}, "entries": []},
                    "environment": {"userAgent": "TestUA/1", "language": "en-US"},
                }
            ],
        },
        headers={"Authorization": f"Bearer {pair['token']}"},
    )
    assert resp.status_code == 200
    assert len(captured_attach) == 1
    job_id, bundle = captured_attach[0]
    assert bundle.label == "ext"
    assert bundle.mhtml and bundle.mhtml.read_bytes().startswith(b"<html>")
    assert bundle.screenshot and bundle.screenshot.read_bytes().startswith(b"\x89PNG")
    assert bundle.environment and "TestUA/1" in bundle.environment.read_text()


@pytest.mark.asyncio
async def test_extension_capture_audit_records_no_cookie_values(client):
    pair = (await client.post("/api/extension/pair", json={"label": "ext"})).json()
    case = (await client.post("/api/cases", json={"name": "Op"})).json()
    await client.post(
        "/api/extension/capture",
        json={
            "case_id": case["id"],
            "urls": ["https://example.com/"],
            "cookies": [
                {"name": "SID", "value": "topsecret-must-not-leak",
                 "domain": "example.com", "path": "/",
                 "expirationDate": 9999999999, "secure": True,
                 "httpOnly": False, "hostOnly": False},
            ],
        },
        headers={"Authorization": f"Bearer {pair['token']}"},
    )
    audit = (await client.get("/api/audit")).json()
    blob = json.dumps(audit)
    assert "topsecret-must-not-leak" not in blob
    actions = [e["action"] for e in audit["entries"]]
    assert "extension.capture_submitted" in actions


# --- Hardening pass: extension_id binding, rotate, ephemeral cookies, ----
# --- tab_context envelope, full session-state payload --------------------


@pytest.mark.asyncio
async def test_pair_with_extension_id_binds_token(client):
    """When the pair body specifies ``extension_id``, every authenticated
    request must present the same value or it gets a 403."""
    pair = (await client.post(
        "/api/extension/pair",
        json={"label": "bound", "extension_id": "abcdef"},
    )).json()
    # Right id passes.
    ok = await client.get(
        "/api/extension/cases",
        headers={
            "Authorization": f"Bearer {pair['token']}",
            "X-Extension-Id": "abcdef",
        },
    )
    assert ok.status_code == 200
    # Wrong id is rejected with 403, not 401 — the credentials are valid,
    # they're just not for this device.
    bad = await client.get(
        "/api/extension/cases",
        headers={
            "Authorization": f"Bearer {pair['token']}",
            "X-Extension-Id": "different-id",
        },
    )
    assert bad.status_code == 403
    # Missing id with a bound token is also 403.
    missing = await client.get(
        "/api/extension/cases",
        headers={"Authorization": f"Bearer {pair['token']}"},
    )
    assert missing.status_code == 403


@pytest.mark.asyncio
async def test_extension_id_mismatch_audits(client):
    pair = (await client.post(
        "/api/extension/pair",
        json={"label": "bound", "extension_id": "real-id"},
    )).json()
    await client.get(
        "/api/extension/cases",
        headers={
            "Authorization": f"Bearer {pair['token']}",
            "X-Extension-Id": "wrong-id",
        },
    )
    audit = (await client.get("/api/audit")).json()
    actions = [e["action"] for e in audit["entries"]]
    assert "extension.id_mismatch" in actions


@pytest.mark.asyncio
async def test_legacy_unbound_token_still_works_without_id_header(client):
    """Tokens minted without an ``extension_id`` are grandfathered."""
    pair = (await client.post("/api/extension/pair", json={"label": "legacy"})).json()
    resp = await client.get(
        "/api/extension/cases",
        headers={"Authorization": f"Bearer {pair['token']}"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_rotate_token_endpoint(client):
    pair = (await client.post(
        "/api/extension/pair",
        json={"label": "rot", "extension_id": "ext-1"},
    )).json()
    rot = await client.post(f"/api/extension/pair/{pair['token_id']}/rotate")
    assert rot.status_code == 200
    body = rot.json()
    assert body["token"] != pair["token"]
    assert body["token_id"] != pair["token_id"]
    assert body["label"] == "rot"
    # Old token no longer works.
    old = await client.get(
        "/api/extension/cases",
        headers={
            "Authorization": f"Bearer {pair['token']}",
            "X-Extension-Id": "ext-1",
        },
    )
    assert old.status_code == 401
    # New token does.
    new = await client.get(
        "/api/extension/cases",
        headers={
            "Authorization": f"Bearer {body['token']}",
            "X-Extension-Id": "ext-1",
        },
    )
    assert new.status_code == 200
    audit = (await client.get("/api/audit")).json()
    assert "extension.token_rotated" in [e["action"] for e in audit["entries"]]


@pytest.mark.asyncio
async def test_rotate_unknown_token_404(client):
    resp = await client.post("/api/extension/pair/does-not-exist/rotate")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_extension_capture_with_tab_context_persists_envelope(client, capsule_dirs):
    """The tab_context envelope ships through the live-capture stash and
    materialises on the per-job tmpdir for the orchestrator to pick up."""
    from app import jobs as jobs_mod

    pair = (await client.post("/api/extension/pair", json={"label": "ext"})).json()
    case = (await client.post("/api/cases", json={"name": "Op"})).json()

    resp = await client.post(
        "/api/extension/capture",
        json={
            "case_id": case["id"],
            "urls": ["https://example.com/article"],
            "live_captures": [{
                "url": "https://example.com/article",
                "tab_context": {
                    "user_agent": "Mozilla/5.0 (test) Chrome/138",
                    "viewport": {"width": 414, "height": 896, "device_scale_factor": 3},
                    "scroll": {"x": 0, "y": 1240},
                    "timezone": "America/Los_Angeles",
                    "language": "en-US",
                    "color_scheme": "dark",
                    "referrer": "https://news.ycombinator.com/",
                },
                "session_state": [{
                    "origin": "https://example.com",
                    "local_storage": {"jwt": "redacted"},
                    "session_storage": {},
                    "captured_at": "2026-05-06T00:00:00Z",
                }],
                "dom_snapshot_meta": {"counts": {"nodes": 42}},
            }],
        },
        headers={"Authorization": f"Bearer {pair['token']}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["jobs"]) == 1
    job_id = body["jobs"][0]["id"]
    bundle = jobs_mod._user_browser_bundles.get(job_id)
    assert bundle is not None
    assert bundle.tab_context is not None and bundle.tab_context.is_file()
    assert bundle.session_state is not None and bundle.session_state.is_file()
    assert bundle.dom_snapshot_meta is not None and bundle.dom_snapshot_meta.is_file()
    # Tab context content is what we sent.
    raw = bundle.tab_context.read_text(encoding="utf-8")
    assert "Mozilla/5.0 (test) Chrome/138" in raw
    assert "America/Los_Angeles" in raw


@pytest.mark.asyncio
async def test_extension_capture_ephemeral_cookies_not_persisted(client, capsule_dirs):
    """When ``cookie_persistence='ephemeral'``, cookies must NOT be written
    to the case directory; they live in a per-job tmpdir keyed off the job
    id, which is wiped after the job ends."""
    from app import jobs as jobs_mod

    pair = (await client.post("/api/extension/pair", json={"label": "ext"})).json()
    case = (await client.post("/api/cases", json={"name": "Op"})).json()

    resp = await client.post(
        "/api/extension/capture",
        json={
            "case_id": case["id"],
            "urls": ["https://example.com/a"],
            "cookies": [{
                "name": "SID", "value": "EPHEMERAL_SECRET",
                "domain": "example.com", "path": "/",
                "expirationDate": 9999999999, "secure": True,
                "httpOnly": False, "hostOnly": False,
            }],
            "cookie_persistence": "ephemeral",
        },
        headers={"Authorization": f"Bearer {pair['token']}"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["id"]

    # Persistent case file must NOT exist.
    case_cookies = capsule_dirs["config"] / "cases" / case["slug"] / "cookies.txt"
    assert not case_cookies.exists()

    # Bundle attached with an ephemeral cookies path that DOES exist.
    bundle = jobs_mod._user_browser_bundles.get(job_id)
    assert bundle is not None
    assert bundle.ephemeral_cookies is not None
    assert bundle.ephemeral_cookies.is_file()
    text = bundle.ephemeral_cookies.read_text(encoding="utf-8")
    assert "EPHEMERAL_SECRET" in text  # cookie file does have the value
    # But the audit log doesn't.
    audit = (await client.get("/api/audit")).json()
    assert "EPHEMERAL_SECRET" not in json.dumps(audit)


@pytest.mark.asyncio
async def test_extension_capture_persistence_records_in_audit(client, capsule_dirs):
    pair = (await client.post("/api/extension/pair", json={"label": "ext"})).json()
    case = (await client.post("/api/cases", json={"name": "Op"})).json()
    await client.post(
        "/api/extension/capture",
        json={
            "case_id": case["id"],
            "urls": ["https://example.com/x"],
            "cookies": [{
                "name": "k", "value": "v", "domain": "example.com",
                "path": "/", "expirationDate": 9999999999, "secure": True,
                "httpOnly": False, "hostOnly": False,
            }],
            "cookie_persistence": "ephemeral",
        },
        headers={"Authorization": f"Bearer {pair['token']}"},
    )
    audit = (await client.get("/api/audit")).json()
    submitted = [
        e for e in audit["entries"]
        if e["action"] == "extension.capture_submitted"
    ]
    assert submitted
    assert submitted[-1]["details"]["cookie_persistence"] == "ephemeral"
