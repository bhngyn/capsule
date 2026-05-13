"""Lifespan auto-check behaviour (CLAUDE.md §15 v0.10).

Confirms the launch-time check:
* Fires when the toggle is ON (default).
* Skips entirely when the toggle is OFF.
* Never blocks startup on network failure.
* Audits exactly one ``system.update_check`` row with
  ``triggered_by: "launch"``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any

import httpx
import pytest


def _stub_runner_versions(monkeypatch):
    from app import ytdlp_runner, gallery_dl_runner

    async def fake_yt():
        return "2025.1.1"

    async def fake_gd():
        return "1.30.0"

    monkeypatch.setattr(ytdlp_runner, "version", fake_yt)
    monkeypatch.setattr(gallery_dl_runner, "version", fake_gd)


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


def _reload_app():
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


@pytest.mark.asyncio
async def test_lifespan_runs_check_when_enabled(capsule_dirs, monkeypatch):
    _reload_app()
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
            # Wait for the launch-task to settle. It's fire-and-forget so we
            # explicitly await the tracked handle here.
            task = main_mod.app.state.auto_check_task
            assert task is not None
            await task

            audit = await c.get("/api/audit")
            rows = audit.json()["entries"]
            launch_rows = [
                r
                for r in rows
                if r["action"] == "system.update_check"
                and r["details"].get("triggered_by") == "launch"
            ]
            assert len(launch_rows) == 1


@pytest.mark.asyncio
async def test_lifespan_skips_check_when_disabled(capsule_dirs, monkeypatch):
    _reload_app()
    _stub_runner_versions(monkeypatch)

    from app import profiles

    profiles.save_app_default({"updates": {"auto_check": False}})

    called: list[str] = []

    async def boom(*a, **k):  # pragma: no cover
        called.append("hit")
        return {}

    from app import updates as updates_mod

    monkeypatch.setattr(updates_mod, "fetch_latest", boom)

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
            assert main_mod.app.state.auto_check_task is None
            assert called == []
            audit = await c.get("/api/audit")
            rows = audit.json()["entries"]
            launch_rows = [
                r
                for r in rows
                if r["action"] == "system.update_check"
            ]
            assert launch_rows == []


@pytest.mark.asyncio
async def test_lifespan_does_not_block_on_network_failure(capsule_dirs, monkeypatch):
    """A 5s URLError should not delay startup more than a moment."""
    import urllib.error

    _reload_app()
    _stub_runner_versions(monkeypatch)
    _install_fake_urlopen(
        monkeypatch,
        {
            "https://pypi.org/": urllib.error.URLError("DNS"),
            "https://api.github.com/": urllib.error.URLError("DNS"),
        },
    )

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
            # First request must succeed even before the launch-check completes.
            resp = await c.get("/api/system/version")
            assert resp.status_code == 200

            task = main_mod.app.state.auto_check_task
            if task is not None:
                # Should resolve fast since network is mocked to error.
                await asyncio.wait_for(task, timeout=2.0)
