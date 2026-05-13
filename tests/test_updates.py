"""Tests for ``app.updates`` (CLAUDE.md §15 v0.10).

Covers the module-level surface that ``main.py`` calls into:

* The component registry (yt-dlp + gallery-dl + capsule).
* The settings round-trip (``auto_check_enabled`` / ``set_auto_check``).
* The cache atomic write/read.
* ``fetch_latest`` against mocked PyPI / GitHub endpoints.
* ``compute_components_view`` (combines installed + latest).
* ``perform_check`` end-to-end with the network mocked.
* ``auto_check_on_launch`` honours the toggle, swallows network errors.

Network is stubbed at the ``urllib.request.urlopen`` boundary — the only
seam the production code uses — so we exercise the real json-decode +
header path without ever opening a socket.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
from typing import Any

import pytest


@pytest.fixture
def updates_mod(capsule_dirs):
    # Reload config + profiles so the module-level paths point at tmp dirs.
    for name in ("app.config", "app.profiles", "app.updates"):
        if name in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[name])
    from app import updates as updates_mod  # noqa: PLC0415

    return updates_mod


# --- registry ---------------------------------------------------------------


def test_registry_has_yt_dlp_gallery_dl_and_capsule(updates_mod):
    keys = [c.key for c in updates_mod.COMPONENTS]
    assert "yt-dlp" in keys
    assert "gallery-dl" in keys
    assert "capsule" in keys


def test_registry_tier_assignment(updates_mod):
    by_key = {c.key: c for c in updates_mod.COMPONENTS}
    assert by_key["yt-dlp"].tier == updates_mod.TIER_PIP
    assert by_key["gallery-dl"].tier == updates_mod.TIER_PIP
    assert by_key["capsule"].tier == updates_mod.TIER_IMAGE_REBUILD


# --- settings round-trip ---------------------------------------------------


def test_auto_check_default_true(updates_mod):
    # Empty /config/settings.json — default behaviour is opt-out.
    assert updates_mod.auto_check_enabled() is True


def test_auto_check_persists_across_reads(updates_mod):
    updates_mod.set_auto_check(False)
    assert updates_mod.auto_check_enabled() is False
    updates_mod.set_auto_check(True)
    assert updates_mod.auto_check_enabled() is True


def test_set_auto_check_does_not_clobber_other_settings(updates_mod, monkeypatch):
    from app import profiles

    profiles.save_app_default({"profile": "fast", "other": {"keep": True}})
    updates_mod.set_auto_check(False)
    settings = profiles.load_app_default()
    assert settings["profile"] == "fast"
    assert settings["other"] == {"keep": True}
    assert settings["updates"]["auto_check"] is False


# --- cache I/O --------------------------------------------------------------


def test_read_cache_missing_returns_empty(updates_mod):
    assert updates_mod.read_cache() == {}


def test_write_then_read_roundtrip(updates_mod):
    payload = {
        "triggered_by": "manual",
        "last_checked_at": "2026-05-08T12:00:00+00:00",
        "components": [{"key": "yt-dlp", "installed": "2025.1.1", "latest": "2026.4.7"}],
        "updates_available": 1,
        "auto_check": True,
    }
    updates_mod.write_cache(payload)
    out = updates_mod.read_cache()
    assert out == payload


def test_write_cache_is_atomic(updates_mod, tmp_path, monkeypatch):
    # Inject a write failure mid-flight; the previous cache must survive.
    updates_mod.write_cache({"good": True})
    real_replace = type(tmp_path).replace

    def boom(self, target):
        raise OSError("simulated disk full")

    monkeypatch.setattr("pathlib.Path.replace", boom)
    with pytest.raises(OSError):
        updates_mod.write_cache({"bad": True})
    monkeypatch.setattr("pathlib.Path.replace", real_replace)
    assert updates_mod.read_cache() == {"good": True}


# --- fetch_latest mocking ---------------------------------------------------


class _FakeResponse:
    """Mimics ``urllib.request.urlopen`` context manager."""

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
    """Patch ``urllib.request.urlopen`` to dispatch by URL.

    Values can be:
      * ``bytes`` — returned as a 200 body.
      * ``Exception`` instance — raised.
    """

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for prefix, response in mapping.items():
            if url.startswith(prefix):
                if isinstance(response, BaseException):
                    raise response
                return _FakeResponse(response)
        raise AssertionError(f"unmocked URL in test: {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def test_fetch_latest_pypi_happy_path(updates_mod, monkeypatch):
    _install_fake_urlopen(
        monkeypatch,
        {
            "https://pypi.org/pypi/yt-dlp/json": json.dumps(
                {"info": {"version": "2026.4.7"}}
            ).encode(),
            "https://pypi.org/pypi/gallery-dl/json": json.dumps(
                {"info": {"version": "1.30.0"}}
            ).encode(),
            "https://api.github.com/": json.dumps({"tag_name": "v1.1.0"}).encode(),
        },
    )
    result = asyncio.run(updates_mod.fetch_latest(timeout=1.0))
    assert result["yt-dlp"]["latest"] == "2026.4.7"
    assert result["yt-dlp"]["error"] is None
    assert result["gallery-dl"]["latest"] == "1.30.0"


def test_fetch_latest_github_strips_v_prefix(updates_mod, monkeypatch, tmp_path):
    monkeypatch.setenv("CAPSULE_GITHUB_REPO", "owner/capsule")
    # Reload config + updates so the env var takes effect.
    from app import config

    importlib.reload(config)
    importlib.reload(updates_mod)

    _install_fake_urlopen(
        monkeypatch,
        {
            "https://pypi.org/pypi/yt-dlp/json": json.dumps(
                {"info": {"version": "2026.4.7"}}
            ).encode(),
            "https://pypi.org/pypi/gallery-dl/json": json.dumps(
                {"info": {"version": "1.30.0"}}
            ).encode(),
            "https://api.github.com/repos/owner/capsule/releases/latest": json.dumps(
                {"tag_name": "v1.2.0"}
            ).encode(),
        },
    )
    result = asyncio.run(updates_mod.fetch_latest(timeout=1.0))
    assert result["capsule"]["latest"] == "1.2.0"
    assert result["capsule"]["error"] is None


def test_fetch_latest_network_error_recorded(updates_mod, monkeypatch):
    import urllib.error

    _install_fake_urlopen(
        monkeypatch,
        {
            "https://pypi.org/": urllib.error.URLError("DNS"),
            "https://api.github.com/": urllib.error.URLError("DNS"),
        },
    )
    result = asyncio.run(updates_mod.fetch_latest(timeout=1.0))
    assert result["yt-dlp"]["latest"] is None
    assert result["yt-dlp"]["error"] == "network"
    assert result["gallery-dl"]["latest"] is None
    assert result["gallery-dl"]["error"] == "network"


def test_fetch_latest_capsule_skipped_when_repo_unset(updates_mod, monkeypatch):
    # Default fixture has no env var; ensure capsule comes back as not_configured.
    monkeypatch.delenv("CAPSULE_GITHUB_REPO", raising=False)
    from app import config

    importlib.reload(config)
    importlib.reload(updates_mod)

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
    result = asyncio.run(updates_mod.fetch_latest(timeout=1.0))
    assert result["capsule"]["latest"] is None
    assert result["capsule"]["error"] == "not_configured"


# --- compute_components_view ------------------------------------------------


def test_normalize_version_collapses_leading_zeros(updates_mod):
    # yt-dlp's `--version` emits ``2026.03.17`` while PyPI normalizes to
    # ``2026.3.17``. Both should compare equal.
    a = updates_mod._normalize_version("2026.03.17")
    b = updates_mod._normalize_version("2026.3.17")
    assert a == b


def test_versions_differ_handles_padded_dates(updates_mod):
    assert updates_mod._versions_differ("2026.03.17", "2026.3.17") is False
    assert updates_mod._versions_differ("2026.03.17", "2026.4.7") is True
    assert updates_mod._versions_differ(None, "1.0.0") is False
    assert updates_mod._versions_differ("1.0.0", None) is False


def test_compute_view_marks_available_when_versions_differ(updates_mod):
    installed = {"yt-dlp": "2025.1.1", "gallery-dl": "1.30.0", "capsule": "1.0.0"}
    latest = {
        "yt-dlp": {"latest": "2026.4.7", "error": None, "source": "pypi"},
        "gallery-dl": {"latest": "1.30.0", "error": None, "source": "pypi"},
        "capsule": {"latest": "1.1.0", "error": None, "source": "github"},
    }
    # Force the GitHub repo so the capsule row is included.
    import os

    os.environ["CAPSULE_GITHUB_REPO"] = "owner/capsule"
    from app import config

    importlib.reload(config)
    importlib.reload(updates_mod)
    view = updates_mod.compute_components_view(installed=installed, latest=latest)
    by_key = {row["key"]: row for row in view}
    assert by_key["yt-dlp"]["available"] is True
    assert by_key["gallery-dl"]["available"] is False
    assert by_key["capsule"]["available"] is True
    del os.environ["CAPSULE_GITHUB_REPO"]


def test_compute_view_hides_capsule_when_repo_unset(updates_mod):
    installed = {"yt-dlp": "2025.1.1", "gallery-dl": "1.30.0", "capsule": "1.0.0"}
    latest = {
        "yt-dlp": {"latest": "2025.1.1", "error": None, "source": "pypi"},
        "gallery-dl": {"latest": "1.30.0", "error": None, "source": "pypi"},
        "capsule": {"latest": None, "error": "not_configured", "source": "github"},
    }
    view = updates_mod.compute_components_view(installed=installed, latest=latest)
    keys = [row["key"] for row in view]
    assert "capsule" not in keys
    assert "yt-dlp" in keys


# --- perform_check + auto_check_on_launch ----------------------------------


def test_perform_check_writes_cache(updates_mod, monkeypatch):
    async def fake_installed():
        return {"yt-dlp": "2025.1.1", "gallery-dl": "1.30.0", "capsule": "1.0.0"}

    async def fake_latest(timeout=5.0):
        return {
            "yt-dlp": {"latest": "2026.4.7", "error": None, "source": "pypi"},
            "gallery-dl": {"latest": "1.30.0", "error": None, "source": "pypi"},
            "capsule": {"latest": None, "error": "not_configured", "source": "github"},
        }

    monkeypatch.setattr(updates_mod, "fetch_installed", fake_installed)
    monkeypatch.setattr(updates_mod, "fetch_latest", fake_latest)

    snapshot = asyncio.run(updates_mod.perform_check(triggered_by="manual"))
    assert snapshot.triggered_by == "manual"
    assert snapshot.updates_available == 1  # yt-dlp differs
    assert any(c["key"] == "yt-dlp" and c["available"] for c in snapshot.components)
    cache = updates_mod.read_cache()
    assert cache["last_checked_at"] == snapshot.last_checked_at
    assert cache["updates_available"] == 1


def test_auto_check_on_launch_skipped_when_disabled(updates_mod, monkeypatch):
    updates_mod.set_auto_check(False)
    called = []

    async def boom(*a, **k):  # pragma: no cover - shouldn't be reached
        called.append("hit network")
        return {}

    monkeypatch.setattr(updates_mod, "fetch_latest", boom)
    asyncio.run(updates_mod.auto_check_on_launch())
    assert called == []


def test_auto_check_on_launch_swallows_errors(updates_mod, monkeypatch):
    updates_mod.set_auto_check(True)

    async def fake_installed():
        return {"yt-dlp": None, "gallery-dl": None, "capsule": "1.0.0"}

    async def boom(timeout=5.0):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(updates_mod, "fetch_installed", fake_installed)
    monkeypatch.setattr(updates_mod, "fetch_latest", boom)
    # Must not raise. Cache stays empty.
    asyncio.run(updates_mod.auto_check_on_launch())


def test_auto_check_on_launch_invokes_audit_callback(updates_mod, monkeypatch):
    updates_mod.set_auto_check(True)

    async def fake_installed():
        return {"yt-dlp": "2025.1.1", "gallery-dl": "1.30.0", "capsule": "1.0.0"}

    async def fake_latest(timeout=5.0):
        return {
            "yt-dlp": {"latest": "2026.4.7", "error": None, "source": "pypi"},
            "gallery-dl": {"latest": "1.30.0", "error": None, "source": "pypi"},
            "capsule": {"latest": None, "error": "not_configured", "source": "github"},
        }

    monkeypatch.setattr(updates_mod, "fetch_installed", fake_installed)
    monkeypatch.setattr(updates_mod, "fetch_latest", fake_latest)

    received: list[Any] = []

    def cb(snapshot):
        received.append(snapshot)

    asyncio.run(updates_mod.auto_check_on_launch(audit_callback=cb))
    assert len(received) == 1
    assert received[0].triggered_by == "launch"
