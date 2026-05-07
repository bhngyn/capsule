"""Shared fixtures for the Capsule test suite.

Tests must never touch the host's ``/downloads`` or ``/config``. Each test gets
fresh tmp dirs wired into ``app.config`` via env vars before any app module is
imported, and the fixture module re-imports config to flush its module-level
``Path`` constants.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture
def capsule_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point Capsule at tmp dirs for the duration of one test.

    Returns the resolved paths so tests can assert filesystem state directly.
    """
    downloads = tmp_path / "downloads"
    cfg = tmp_path / "config"
    downloads.mkdir()
    cfg.mkdir()

    monkeypatch.setenv("CAPSULE_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setenv("CAPSULE_CONFIG_DIR", str(cfg))

    from app import config as _config

    importlib.reload(_config)

    for mod_name in (
        "app.db",
        "app.signing",
        "app.audit",
        "app.cases",
        "app.cookies",
        "app.classify",
        "app.postprocess",
        "app.jobs",
        "app.paths",
    ):
        if mod_name in os.sys.modules:
            importlib.reload(os.sys.modules[mod_name])

    return {"downloads": downloads, "config": cfg}
