"""Unit tests for DownloadOptions.capture_mode (v0.10).

These tests import only the dataclass mechanics and do not require the
``cryptography`` module, so they run on the host dev machine as well as
inside Docker.

The trick: we stub the heavyweight import chain
(postprocess → signing → cryptography) in sys.modules before importing
``app.jobs``, then clean up afterwards so other tests aren't affected.
"""

from __future__ import annotations

import importlib
import sys
import types
from unittest.mock import MagicMock


def _load_jobs_without_cryptography():
    """Return the ``app.jobs`` module with all cryptography deps stubbed out."""
    stubs: dict[str, types.ModuleType] = {}

    def _stub(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        stubs[name] = m
        sys.modules[name] = m
        return m

    # Stub every module in the postprocess → signing → cryptography chain
    # so the import completes on a host without the cryptography wheel.
    _stub("cryptography")
    _stub("cryptography.exceptions").InvalidSignature = Exception
    _stub("cryptography.hazmat")
    _stub("cryptography.hazmat.primitives")
    _stub("cryptography.hazmat.primitives.asymmetric")
    _stub("cryptography.hazmat.primitives.asymmetric.ed25519")
    _stub("cryptography.hazmat.primitives.serialization")
    _stub("cryptography.hazmat.backends")

    # Stub app.signing so it doesn't actually execute its module body.
    signing_stub = _stub("app.signing")
    signing_stub.ensure_keypair = MagicMock()
    signing_stub.sign = MagicMock(return_value=b"\x00" * 64)
    signing_stub.verify = MagicMock(return_value=True)

    # Stub weasyprint (used by pdf_report) — another optional dep.
    _stub("weasyprint")

    # Now re-import fresh copies of the chain.
    for mod_name in (
        "app.config",
        "app.paths",
        "app.sanitize",
        "app.audit",
        "app.cases",
        "app.url_canonical",
        "app.platforms",
        "app.postprocess",
        "app.jobs",
    ):
        sys.modules.pop(mod_name, None)

    import app.jobs as jobs_mod  # noqa: PLC0415

    return jobs_mod, stubs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDownloadOptionsCaptureMode:
    """Round-trip and validation tests for the capture_mode field."""

    def setup_method(self):
        self._orig_modules = dict(sys.modules)
        self.jobs_mod, self._stubs = _load_jobs_without_cryptography()

    def teardown_method(self):
        # Restore sys.modules to avoid polluting other tests.
        for name in list(sys.modules):
            if name not in self._orig_modules:
                del sys.modules[name]
        sys.modules.update(self._orig_modules)

    def _opts(self, **kwargs):
        return self.jobs_mod.DownloadOptions(**kwargs)

    # --- Field presence --------------------------------------------------- #

    def test_default_capture_mode_is_none(self):
        assert self._opts().capture_mode is None

    def test_valid_modes_accepted(self):
        for mode in ("webpage", "media", "gallery"):
            opts = self._opts(capture_mode=mode)
            assert opts.capture_mode == mode

    # --- to_dict round-trip ----------------------------------------------- #

    def test_to_dict_includes_capture_mode(self):
        d = self._opts(capture_mode="gallery").to_dict()
        assert d["capture_mode"] == "gallery"

    def test_to_dict_capture_mode_null_when_none(self):
        d = self._opts().to_dict()
        assert d["capture_mode"] is None

    # --- from_dict round-trip --------------------------------------------- #

    def test_from_dict_webpage(self):
        opts = self.jobs_mod.DownloadOptions.from_dict({"capture_mode": "webpage"})
        assert opts.capture_mode == "webpage"

    def test_from_dict_media(self):
        opts = self.jobs_mod.DownloadOptions.from_dict({"capture_mode": "media"})
        assert opts.capture_mode == "media"

    def test_from_dict_gallery(self):
        opts = self.jobs_mod.DownloadOptions.from_dict({"capture_mode": "gallery"})
        assert opts.capture_mode == "gallery"

    def test_from_dict_unknown_value_coerces_to_none(self):
        opts = self.jobs_mod.DownloadOptions.from_dict({"capture_mode": "bogus"})
        assert opts.capture_mode is None

    def test_from_dict_null_stays_none(self):
        opts = self.jobs_mod.DownloadOptions.from_dict({"capture_mode": None})
        assert opts.capture_mode is None

    def test_from_dict_missing_key_stays_none(self):
        opts = self.jobs_mod.DownloadOptions.from_dict({})
        assert opts.capture_mode is None

    def test_roundtrip_gallery(self):
        original = self._opts(capture_mode="gallery", audio_only=True)
        restored = self.jobs_mod.DownloadOptions.from_dict(original.to_dict())
        assert restored.capture_mode == "gallery"
        assert restored.audio_only is True

    # --- is_default ------------------------------------------------------- #

    def test_is_default_when_capture_mode_none(self):
        assert self._opts().is_default() is True

    def test_is_not_default_when_capture_mode_set(self):
        assert self._opts(capture_mode="webpage").is_default() is False

    # --- CAPTURE_MODE_VALUES constant ------------------------------------- #

    def test_capture_mode_values_frozenset(self):
        v = self.jobs_mod._CAPTURE_MODE_VALUES
        assert "webpage" in v
        assert "media" in v
        assert "gallery" in v
        assert "bogus" not in v
