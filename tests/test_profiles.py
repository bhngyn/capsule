"""Connection profiles — plan §C."""

from __future__ import annotations

import json

import pytest

from app import profiles


def test_slow_default_caps_quality_and_concurrency():
    p = profiles.slow_default()
    assert p.name == "slow"
    assert p.concurrency == 1
    assert p.limit_rate_kbps == 500
    assert p.socket_timeout_s == 60
    assert p.tasks_visible is True
    assert p.heavy_task_default == "session_confirm"
    # Slow defaults to 480p — full quality is opt-in via Advanced.
    assert "height<=480" in p.default_format
    # Portrait videos (width is the short side) must also be covered, or the
    # spec would reject every available variant and yt-dlp would abort.
    assert "width<=480" in p.default_format
    # Final unconditional `/best` fallback so a capture never fails purely
    # because no ≤480 variant exists. Anchored at end-of-string so an
    # earlier `/best[...]` clause does not satisfy the assertion.
    assert p.default_format.endswith("/best")


def test_fast_default_unconstrained():
    p = profiles.fast_default()
    assert p.name == "fast"
    assert p.concurrency == 4
    assert p.limit_rate_kbps is None
    assert p.tasks_visible is False
    assert p.heavy_task_default == "auto"
    assert p.default_format == "best"


def test_by_name_rejects_unknown():
    with pytest.raises(ValueError):
        profiles.by_name("turbo")


def test_effective_default_to_slow():
    """No app or case settings → default profile (slow)."""
    res = profiles.effective_for_case(None, app_settings={})
    assert res.base_name == "slow"
    assert res.settings.concurrency == 1


def test_per_case_overrides_app_choice():
    res = profiles.effective_for_case(
        case_settings={"profile": "fast"},
        app_settings={"profile": "slow"},
    )
    assert res.base_name == "fast"
    assert res.settings.concurrency == 4


def test_overrides_patch_individual_values():
    res = profiles.effective_for_case(
        case_settings={
            "profile": "slow",
            "profile_overrides": {"limit_rate_kbps": 200, "concurrency": 1},
        },
        app_settings={},
    )
    assert res.settings.limit_rate_kbps == 200
    assert res.settings.concurrency == 1
    assert res.settings.name == "slow"


def test_overrides_ignore_unknown_keys():
    """Forward-compat: a per-case JSON written by a future version should
    still load on an older one."""
    res = profiles.effective_for_case(
        case_settings={
            "profile": "fast",
            "profile_overrides": {"unknown_future_field": "x"},
        },
        app_settings={},
    )
    assert res.base_name == "fast"


def test_app_overrides_then_case_overrides_layer():
    """App overrides apply first, case overrides go on top."""
    res = profiles.effective_for_case(
        case_settings={"profile_overrides": {"limit_rate_kbps": 100}},
        app_settings={
            "profile": "slow",
            "profile_overrides": {"limit_rate_kbps": 1000, "concurrency": 1},
        },
    )
    # Case override wins for limit_rate_kbps; app override sticks for concurrency.
    assert res.settings.limit_rate_kbps == 100
    assert res.settings.concurrency == 1


def test_invalid_profile_falls_back_to_default(capsule_dirs):
    res = profiles.effective_for_case(
        case_settings={"profile": "ferrari"},
        app_settings={},
    )
    assert res.base_name == profiles.DEFAULT_PROFILE


def test_save_and_load_app_default(capsule_dirs):
    profiles.save_app_default({"profile": "fast"})
    loaded = profiles.load_app_default()
    assert loaded["profile"] == "fast"
    assert "updated_at" in loaded
