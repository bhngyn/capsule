"""Connection profiles — plan §C.

A profile is a named bundle of values that override universal defaults.
Stored at three levels with this resolution order:

* per-job override (Advanced) — not yet wired in v1
* per-case override (``cases.settings_json.profile``)
* app-wide default (``/config/settings.json``)

Two profiles ship: ``slow`` (default — defensive choice; a slow-profile
user on a fast pipe just downloads less, while a fast-profile user on
a slow pipe wastes their quota) and ``fast``.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Optional

from . import config

__all__ = [
    "ProfileSettings",
    "PROFILE_NAMES",
    "DEFAULT_PROFILE",
    "slow_default",
    "fast_default",
    "by_name",
    "load_app_default",
    "save_app_default",
    "effective_for_case",
    "ProfileResolution",
]


PROFILE_NAMES = ("slow", "fast")
DEFAULT_PROFILE = "slow"  # safer default — caps bandwidth, never wastes quota

HeavyTaskDefault = Literal["auto", "session_confirm", "always_confirm"]


@dataclass(frozen=True)
class ProfileSettings:
    name: str

    # Wire-level
    default_format: str
    socket_timeout_s: int
    limit_rate_kbps: Optional[int]   # None = no rate limit

    # Concurrency
    concurrency: int

    # Retry policy
    retry_backoff_cap_s: int

    # Network monitor
    probe_interval_s: int
    probe_url: Optional[str]         # None = use NetworkMonitor default

    # Sidecar choices (yt-dlp)
    write_thumbnail: bool
    write_subs_default: bool

    # Decoupled-task visibility (plan §U6 / Phase D)
    tasks_visible: bool              # True (Slow): show snapshot/archive/media
    heavy_task_default: HeavyTaskDefault
    intra_capture_parallel: bool     # False (Slow): sequential

    # UX
    save_data_default: bool
    schedule_in_main_ui: bool
    proxy_in_main_ui: bool
    ui_auto_dismiss_s: Optional[int]  # None = never

    def merged_with(self, overrides: dict[str, Any]) -> "ProfileSettings":
        """Return a copy with ``overrides`` applied. Unknown keys are ignored
        — settings are append-only across releases, so a per-case JSON written
        by a future version of Capsule still loads on an older one."""
        valid: dict[str, Any] = {}
        allowed = {f.name for f in self.__dataclass_fields__.values()}
        for k, v in overrides.items():
            if k in allowed and k != "name":
                valid[k] = v
        return replace(self, **valid)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def slow_default() -> ProfileSettings:
    """Slow / Weak / VPN / Intermittent — plan profile L."""
    return ProfileSettings(
        name="slow",
        default_format=(
            # Prefer ≤480 on the long side for landscape...
            "bestvideo[height<=480][ext=mp4]+bestaudio/"
            "best[height<=480]/"
            # ...and on the short side for portrait video, where the height
            # filter would otherwise reject every available variant.
            "bestvideo[width<=480][ext=mp4]+bestaudio/"
            "best[width<=480]/"
            # Final fallback: never abort the capture just because no small
            # variant exists; the audit log records what was actually fetched.
            "best"
        ),
        socket_timeout_s=60,
        limit_rate_kbps=500,                  # 500 KB/s
        concurrency=1,                        # sequential — don't fight the pipe
        retry_backoff_cap_s=24 * 60 * 60,     # 24h
        probe_interval_s=120,
        probe_url=None,                       # user-configurable; default unset
        write_thumbnail=False,                # extra bytes, low evidentiary value
        write_subs_default=False,
        tasks_visible=True,
        heavy_task_default="session_confirm",
        intra_capture_parallel=False,
        save_data_default=False,              # opt-in (always_confirm) toggle
        schedule_in_main_ui=True,
        proxy_in_main_ui=True,
        ui_auto_dismiss_s=None,               # never — user may step away
    )


def fast_default() -> ProfileSettings:
    """Fast / Stable — plan profile F."""
    return ProfileSettings(
        name="fast",
        default_format="best",
        socket_timeout_s=20,
        limit_rate_kbps=None,
        concurrency=4,
        retry_backoff_cap_s=60 * 60,          # 1h
        probe_interval_s=30,
        probe_url=None,
        write_thumbnail=True,
        write_subs_default=False,             # explicit opt-in still
        tasks_visible=False,
        heavy_task_default="auto",
        intra_capture_parallel=True,
        save_data_default=False,
        schedule_in_main_ui=False,
        proxy_in_main_ui=False,
        ui_auto_dismiss_s=5,                  # 5 s, up from 1.2 s
    )


def by_name(name: str) -> ProfileSettings:
    if name == "slow":
        return slow_default()
    if name == "fast":
        return fast_default()
    raise ValueError(f"unknown profile {name!r}; expected one of {PROFILE_NAMES}")


# --- App-wide settings file --------------------------------------------------


def _settings_path() -> Path:
    return config.CONFIG_DIR / "settings.json"


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def load_app_default() -> dict[str, Any]:
    """Read ``/config/settings.json``. Returns an empty dict if missing."""
    path = _settings_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_app_default(settings: dict[str, Any]) -> None:
    """Write ``/config/settings.json`` atomically.

    Adds an ``updated_at`` timestamp so a future audit-trail review can
    confirm when the choice was made.
    """
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**settings, "updated_at": _utcnow()}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)


# --- Resolution --------------------------------------------------------------


@dataclass(frozen=True)
class ProfileResolution:
    """The effective profile plus the per-case overrides that produced it.

    ``settings`` is what the orchestrator/runner/capture should read.
    ``case_overrides`` is the raw dict from ``cases.settings_json`` (minus
    the ``profile`` key) so the API can echo it back unchanged.
    """
    settings: ProfileSettings
    base_name: str
    case_overrides: dict[str, Any] = field(default_factory=dict)


def effective_for_case(
    case_settings: Optional[dict[str, Any]] = None,
    *,
    app_settings: Optional[dict[str, Any]] = None,
) -> ProfileResolution:
    """Merge app-wide → per-case → return the effective profile.

    A ``profile`` key at either level picks the base. A ``profile_overrides``
    dict at either level patches individual values on top.
    """
    case_settings = case_settings or {}
    app_settings = app_settings if app_settings is not None else load_app_default()

    base_name = (
        case_settings.get("profile")
        or app_settings.get("profile")
        or DEFAULT_PROFILE
    )
    if base_name not in PROFILE_NAMES:
        base_name = DEFAULT_PROFILE
    profile = by_name(base_name)

    # App-wide overrides apply first, then per-case override on top.
    app_overrides = app_settings.get("profile_overrides") or {}
    case_overrides = case_settings.get("profile_overrides") or {}
    if app_overrides:
        profile = profile.merged_with(app_overrides)
    if case_overrides:
        profile = profile.merged_with(case_overrides)

    return ProfileResolution(
        settings=profile,
        base_name=base_name,
        case_overrides=case_overrides,
    )
