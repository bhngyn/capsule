"""Tests for the ``/api/system/updates*`` HTTP surface (CLAUDE.md §15 v0.10).

Drives the FastAPI app with mocked network so the routes exercise the
real audit-log writes, real cache I/O, real registry — only the outbound
HTTP is stubbed.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import httpx
import pytest


@pytest.fixture
async def client(capsule_dirs):
    for name in (
        "app.config",
        "app.profiles",
        "app.updates",
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


class _FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200):
        self._body = body
        self.status = status
        self.reason = "OK"
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _install_fake_urlopen(monkeypatch, mapping: dict[str, Any]):
    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for prefix, response in mapping.items():
            if url.startswith(prefix):
                if isinstance(response, BaseException):
                    raise response
                return _FakeResponse(response)
        raise AssertionError(f"unmocked URL in test: {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def _stub_runner_versions(monkeypatch):
    """Make the installed-version probes fast and deterministic."""
    from app import ytdlp_runner, gallery_dl_runner

    async def fake_yt():
        return "2025.1.1"

    async def fake_gd():
        return "1.30.0"

    monkeypatch.setattr(ytdlp_runner, "version", fake_yt)
    monkeypatch.setattr(gallery_dl_runner, "version", fake_gd)


@pytest.mark.asyncio
async def test_get_updates_empty_cache_returns_defaults(client):
    resp = await client.get("/api/system/updates")
    assert resp.status_code == 200
    body = resp.json()
    assert body["auto_check"] is True
    assert body["last_checked_at"] is None
    assert body["components"] == []
    assert body["updates_available"] == 0


@pytest.mark.asyncio
async def test_check_now_populates_cache_and_audits(client, monkeypatch):
    _stub_runner_versions(monkeypatch)
    _install_fake_urlopen(
        monkeypatch,
        {
            "https://pypi.org/pypi/yt-dlp/json": json.dumps(
                {"info": {"version": "2026.4.7"}}
            ).encode(),
            "https://pypi.org/pypi/gallery-dl/json": json.dumps(
                {"info": {"version": "1.30.0"}}
            ).encode(),
        },
    )
    resp = await client.post("/api/system/updates/check")
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_checked_at"] is not None
    assert body["updates_available"] == 1  # yt-dlp behind
    by_key = {c["key"]: c for c in body["components"]}
    assert by_key["yt-dlp"]["available"] is True
    assert by_key["gallery-dl"]["available"] is False

    # Audit row landed.
    audit = await client.get("/api/audit")
    assert audit.status_code == 200
    rows = audit.json()["entries"]
    actions = {r["action"] for r in rows}
    assert "system.update_check" in actions


@pytest.mark.asyncio
async def test_auto_check_toggle_persists_and_audits(client, monkeypatch):
    # Default ON; flip OFF.
    resp = await client.put(
        "/api/system/updates/auto_check", json={"enabled": False}
    )
    assert resp.status_code == 200
    assert resp.json()["auto_check"] is False

    # Flipping again to the same value should NOT add another audit row.
    resp = await client.put(
        "/api/system/updates/auto_check", json={"enabled": False}
    )
    assert resp.status_code == 200
    assert resp.json()["auto_check"] is False

    # Flip back ON.
    resp = await client.put(
        "/api/system/updates/auto_check", json={"enabled": True}
    )
    assert resp.json()["auto_check"] is True

    audit = await client.get("/api/audit")
    rows = audit.json()["entries"]
    changes = [r for r in rows if r["action"] == "system.auto_check_changed"]
    # First-toggle (T→F) and re-enable (F→T) audit, but the duplicate F→F
    # toggle does not.
    assert len(changes) == 2


@pytest.mark.asyncio
async def test_update_unknown_component_returns_400_with_i18n_key(client):
    resp = await client.post(
        "/api/system/update", params={"component": "nonexistent"}
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["i18n_key"] == "errors.update.unknown_component"
    assert detail["component"] == "nonexistent"


@pytest.mark.asyncio
async def test_update_capsule_returns_image_rebuild_400(client):
    resp = await client.post(
        "/api/system/update", params={"component": "capsule"}
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["i18n_key"] == "errors.update.requires_image_rebuild"
    assert detail["component"] == "capsule"


@pytest.mark.asyncio
async def test_dismiss_banner_audits(client):
    resp = await client.post(
        "/api/system/updates/dismiss_banner",
        json={"components": ["yt-dlp", "capsule"]},
    )
    assert resp.status_code == 200
    audit = await client.get("/api/audit")
    rows = audit.json()["entries"]
    dismissals = [r for r in rows if r["action"] == "system.update_dismissed"]
    assert len(dismissals) == 1
    assert dismissals[0]["details"]["components"] == ["yt-dlp", "capsule"]
