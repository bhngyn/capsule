"""Update management (CLAUDE.md §15 v0.10).

Capsule never installs updates without the user's click. This module owns:

* The component registry — which runtimes are surfaced as updateable, what
  their installed-version probe is, and where to look up the latest
  release. Two tiers today:

    Tier 1 — pip-installed extractors (yt-dlp, gallery-dl). Updates run
    via ``pip install --upgrade`` inside the container; effective until
    the next ``docker rm`` reverts the image-baked baseline.

    Tier 2 — Capsule itself. Updates ship as new Docker images; the UI
    shows a copy-paste ``docker pull`` command.

* The ``latest`` lookup — PyPI for tier 1, GitHub releases for tier 2.
  Plain ``urllib.request`` over a thread; no new runtime dependency
  (CLAUDE.md §13 #3 — prefer the standard library).

* The version cache (``/config/version_cache.json``) — every successful
  check writes this. The UI reads it on every navigation; the network
  call only fires from ``auto_check_on_launch()`` (lifespan hook) or
  ``POST /api/system/updates/check``.

* The auto-check setting — opt-out toggle persisted via the existing
  ``profiles.load_app_default`` / ``save_app_default`` helpers. Default
  ``True``. CLAUDE.md §4.4 + §13 #7 explain the threat-model carve-out.

The API layer in ``main.py`` handles audit-log writes (so this module stays
DB-free and importable from anywhere). Network failures are swallowed and
recorded in the cache as ``error: <key>`` so the UI can show "couldn't
reach pypi.org" without bringing down the page.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, config, profiles

__all__ = [
    "Component",
    "COMPONENTS",
    "auto_check_enabled",
    "set_auto_check",
    "fetch_installed",
    "fetch_latest",
    "perform_check",
    "read_cache",
    "write_cache",
    "compute_components_view",
    "auto_check_on_launch",
    "TIER_PIP",
    "TIER_IMAGE_REBUILD",
    "USER_AGENT",
    "CACHE_FILENAME",
    "DEFAULT_HTTP_TIMEOUT_S",
]


TIER_PIP = 1            # in-container pip update
TIER_IMAGE_REBUILD = 2  # host-side ``docker pull`` + relaunch

CACHE_FILENAME = "version_cache.json"
SETTINGS_KEY = "updates"
SETTINGS_AUTO_CHECK_KEY = "auto_check"
DEFAULT_HTTP_TIMEOUT_S = 5.0
USER_AGENT = f"Capsule/{__version__} (+update-check)"


@dataclass(frozen=True)
class Component:
    """One updatable runtime.

    ``key`` doubles as the audit-log label and the i18n role-key suffix
    (``settings.update.component.<key>.role``).
    """

    key: str
    tier: int
    source: str                       # 'pypi' | 'github'
    pypi_name: str | None = None
    github_repo: str | None = None    # 'owner/repo'


COMPONENTS: tuple[Component, ...] = (
    Component(key="yt-dlp", tier=TIER_PIP, source="pypi", pypi_name="yt-dlp"),
    Component(key="gallery-dl", tier=TIER_PIP, source="pypi", pypi_name="gallery-dl"),
    Component(key="capsule", tier=TIER_IMAGE_REBUILD, source="github"),
)


# --- settings ----------------------------------------------------------------


def auto_check_enabled() -> bool:
    """Read the auto-check toggle from ``/config/settings.json``.

    Default ``True`` per CLAUDE.md §15 v0.10. Investigators on a
    threat-model-conscious deployment flip the Settings toggle off.
    """
    settings = profiles.load_app_default()
    block = settings.get(SETTINGS_KEY) or {}
    return bool(block.get(SETTINGS_AUTO_CHECK_KEY, True))


def set_auto_check(enabled: bool) -> None:
    """Persist the auto-check toggle.

    Read-modify-write: the rest of the settings blob (profile, etc.) is
    left untouched.
    """
    settings = profiles.load_app_default()
    block = dict(settings.get(SETTINGS_KEY) or {})
    block[SETTINGS_AUTO_CHECK_KEY] = bool(enabled)
    settings[SETTINGS_KEY] = block
    profiles.save_app_default(settings)


# --- cache -------------------------------------------------------------------


def _cache_path() -> Path:
    return config.CONFIG_DIR / CACHE_FILENAME


def read_cache() -> dict[str, Any]:
    """Read the version cache. Empty dict if missing or unparseable."""
    path = _cache_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_cache(payload: dict[str, Any]) -> None:
    """Atomic write of the cache file."""
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)


# --- installed-version probes ------------------------------------------------


async def _installed_capsule() -> str:
    return __version__


async def _installed_via_runner(component_key: str) -> str:
    """Lazy import keeps this module importable in tests that don't load the
    full capture pipeline.
    """
    if component_key == "yt-dlp":
        from . import ytdlp_runner

        return await ytdlp_runner.version()
    if component_key == "gallery-dl":
        from . import gallery_dl_runner

        return await gallery_dl_runner.version()
    raise KeyError(component_key)


async def fetch_installed() -> dict[str, str | None]:
    """Probe every registered component's installed version.

    Returns a key→version dict. ``None`` for a component we couldn't
    probe (e.g. a runtime not on PATH). Callers render ``None`` as a
    dashed cell in the UI so a single broken probe doesn't blank the
    whole table.
    """
    results: dict[str, str | None] = {}
    for c in COMPONENTS:
        try:
            if c.key == "capsule":
                v = await _installed_capsule()
            else:
                v = await _installed_via_runner(c.key)
            results[c.key] = (v or "").strip() or None
        except Exception:
            results[c.key] = None
    return results


# --- latest-version lookups --------------------------------------------------


def _http_get_json(url: str, *, timeout: float) -> dict[str, Any]:
    """Synchronous JSON GET. Called from ``asyncio.to_thread``.

    Raises on non-2xx, network error, or non-JSON body.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - vetted scheme
        if resp.status >= 400:
            raise urllib.error.HTTPError(
                url, resp.status, resp.reason, resp.headers, None
            )
        body = resp.read()
    return json.loads(body.decode("utf-8"))


async def _latest_pypi(pkg: str, *, timeout: float) -> str:
    payload = await asyncio.to_thread(
        _http_get_json,
        f"https://pypi.org/pypi/{pkg}/json",
        timeout=timeout,
    )
    info = payload.get("info") or {}
    version = info.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"pypi: no version field for {pkg}")
    return version


async def _latest_github(repo: str, *, timeout: float) -> str:
    payload = await asyncio.to_thread(
        _http_get_json,
        f"https://api.github.com/repos/{repo}/releases/latest",
        timeout=timeout,
    )
    tag = payload.get("tag_name") or payload.get("name")
    if not isinstance(tag, str) or not tag:
        raise ValueError(f"github: no tag_name for {repo}")
    return tag.lstrip("v")


async def _fetch_latest_one(
    component: Component, *, timeout: float
) -> tuple[str | None, str | None]:
    """Return ``(latest, error_key)``.

    ``error_key`` is one of ``None`` / ``"network"`` / ``"not_configured"`` /
    ``"unsupported"``. The cache stores both so the UI can render
    "Latest: —" with a tooltip explaining why.
    """
    try:
        if component.source == "pypi":
            if not component.pypi_name:
                return None, "not_configured"
            return await _latest_pypi(component.pypi_name, timeout=timeout), None
        if component.source == "github":
            repo = component.github_repo or config.CAPSULE_GITHUB_REPO
            if not repo:
                # Self-update lookup is opt-in: dev builds don't have an
                # upstream release stream yet. The Tier 2 card hides
                # entirely when the repo isn't configured (see
                # ``compute_components_view``).
                return None, "not_configured"
            return await _latest_github(repo, timeout=timeout), None
        return None, "unsupported"
    except (urllib.error.URLError, OSError, ValueError, TimeoutError, asyncio.TimeoutError):
        return None, "network"


async def fetch_latest(
    *, timeout: float = DEFAULT_HTTP_TIMEOUT_S
) -> dict[str, dict[str, str | None]]:
    """Fetch the latest version for every registered component in parallel.

    Returns a dict keyed by component key; each value is
    ``{"latest": str|None, "error": str|None, "source": str}``.
    """
    results = await asyncio.gather(
        *[_fetch_latest_one(c, timeout=timeout) for c in COMPONENTS],
        return_exceptions=False,
    )
    out: dict[str, dict[str, str | None]] = {}
    for component, (latest, err) in zip(COMPONENTS, results):
        out[component.key] = {
            "latest": latest,
            "error": err,
            "source": component.source,
        }
    return out


# --- view builder ------------------------------------------------------------


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _normalize_version(v: str) -> tuple[Any, ...]:
    """Coerce a version string into a comparable tuple.

    Each dot-separated segment is converted to ``int`` when possible (so
    ``2026.03.17`` and ``2026.3.17`` collapse to the same tuple — yt-dlp's
    ``--version`` output keeps the leading zeros while PyPI normalizes
    them away, and we don't want to flag that as an update). Any
    non-numeric tail (e.g. ``1.0.0a1``) keeps its trailing string segment
    intact so distinct pre-releases still compare unequal.
    """
    parts: list[Any] = []
    for raw in v.strip().split("."):
        if not raw:
            continue
        try:
            parts.append(int(raw))
        except ValueError:
            # Non-numeric segment — keep as string. Tuples mix int/str
            # comparisons fine for equality, which is all we need here.
            parts.append(raw)
    return tuple(parts)


def _versions_differ(installed: str | None, latest: str | None) -> bool:
    """Equality compare with leading-zero tolerance.

    yt-dlp / gallery-dl ship date-CalVer tags (``YYYY.MM.DD``); the runner's
    ``--version`` keeps zero-padded months/days while PyPI normalizes them.
    We compare normalized tuples so the two forms collapse. Capsule tags
    are normalized to drop a leading ``v`` in :func:`_latest_github` so
    ``v1.0.0`` and ``1.0.0`` compare equal.
    """
    if installed is None or latest is None:
        return False
    return _normalize_version(installed) != _normalize_version(latest)


def compute_components_view(
    *,
    installed: dict[str, str | None],
    latest: dict[str, dict[str, str | None]],
) -> list[dict[str, Any]]:
    """Combine installed + latest into the per-component records the API
    returns. Hides the ``capsule`` entry when no GitHub repo is
    configured AND we couldn't fetch a latest tag — there's no useful UI
    in that case.
    """
    components: list[dict[str, Any]] = []
    for c in COMPONENTS:
        latest_block = latest.get(c.key) or {}
        installed_v = installed.get(c.key)
        latest_v = latest_block.get("latest")
        error = latest_block.get("error")

        if (
            c.key == "capsule"
            and not config.CAPSULE_GITHUB_REPO
            and not c.github_repo
        ):
            # Hide rather than show a permanently-dashed self-update row.
            continue

        components.append(
            {
                "key": c.key,
                "tier": c.tier,
                "source": c.source,
                "installed": installed_v,
                "latest": latest_v,
                "error": error,
                "available": _versions_differ(installed_v, latest_v),
            }
        )
    return components


@dataclass
class CheckResult:
    """Snapshot of a single check pass.

    Stored in the cache and returned by ``perform_check``. ``triggered_by``
    is one of ``"launch"`` | ``"manual"`` so the audit log can tell the two
    apart.
    """

    triggered_by: str
    last_checked_at: str
    components: list[dict[str, Any]]
    updates_available: int
    auto_check: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggered_by": self.triggered_by,
            "last_checked_at": self.last_checked_at,
            "components": list(self.components),
            "updates_available": self.updates_available,
            "auto_check": self.auto_check,
        }


async def perform_check(
    *,
    triggered_by: str,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
) -> CheckResult:
    """Run installed + latest probes, write the cache, return the snapshot.

    ``triggered_by`` is recorded in the cache (and the audit log row, by
    the caller in ``main.py``). The cache is the single source of truth
    for the GET endpoint — we always overwrite it on a fresh run rather
    than merging, so a component that vanishes from ``COMPONENTS`` doesn't
    leave a stale row.
    """
    installed = await fetch_installed()
    latest = await fetch_latest(timeout=timeout)
    view = compute_components_view(installed=installed, latest=latest)
    snapshot = CheckResult(
        triggered_by=triggered_by,
        last_checked_at=_utcnow_iso(),
        components=view,
        updates_available=sum(1 for c in view if c["available"]),
        auto_check=auto_check_enabled(),
    )
    write_cache(snapshot.to_dict())
    return snapshot


# --- launch hook -------------------------------------------------------------


async def auto_check_on_launch(
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
    audit_callback: Any = None,
) -> None:
    """Fire-and-forget launch check. Never raises.

    The lifespan hook in ``main.py`` schedules this with
    ``asyncio.create_task`` so startup is never blocked. The
    ``audit_callback`` is a function ``(snapshot: CheckResult) -> None``
    that the lifespan wires up to write the audit row — keeps this module
    DB-free.
    """
    if not auto_check_enabled():
        return
    try:
        snapshot = await perform_check(
            triggered_by="launch", timeout=timeout
        )
    except Exception:
        # Swallow — a network blip on launch must never crash uvicorn.
        return
    if audit_callback is None:
        return
    try:
        result = audit_callback(snapshot)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        # Audit-write failure also non-fatal — the cache is still on disk.
        return
