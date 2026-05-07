"""Passive offline detection + light probe — plan §U7.

The orchestrator hits the wire on every capture. If N transient
``errors.network`` failures arrive within a short window, we flip the
"offline" flag, pause the queue, and start a probe loop against a
**user-configurable** URL. On success we flip back to online and ask
the orchestrator to re-dispatch the paused jobs.

Design notes:

* No active polling when online. Probes only run while we believe we're
  offline. Capsule's threat model (CLAUDE.md §1) is to minimise unsolicited
  network traffic, especially for users in censored environments.
* Probe URL defaults to ``https://www.gstatic.com/generate_204`` — small,
  unauthenticated, widely available — but the user can swap it. In some
  censored environments even gstatic is blocked, and the user knows
  better than we do what's reachable.
* The probe target is logged in the audit trail every time we hit it, so
  the user can prove what was contacted from their machine.
* Probes use ``urllib`` from the stdlib so we don't need a new dep just
  for this loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import logging
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger(__name__)

__all__ = [
    "NetworkMonitor",
    "default_probe",
    "DEFAULT_PROBE_URL",
    "DEFAULT_PROBE_INTERVAL_S",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_FAILURE_WINDOW_S",
]


DEFAULT_PROBE_URL = "https://www.gstatic.com/generate_204"
DEFAULT_PROBE_INTERVAL_S = 60.0
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_FAILURE_WINDOW_S = 300.0  # 5 min


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


async def default_probe(url: str, *, timeout_s: float = 5.0) -> bool:
    """Send a HEAD; return True on any 2xx/3xx response.

    Run inside ``asyncio.to_thread`` so the event loop isn't blocked by
    a slow DNS or TCP connect.
    """
    def _go() -> bool:
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return 200 <= resp.status < 400
        except (urllib.error.URLError, OSError, ValueError):
            return False
    try:
        return await asyncio.to_thread(_go)
    except Exception:
        return False


@dataclass
class NetworkState:
    offline: bool
    offline_since: Optional[str]
    probe_url: str
    probe_interval_s: float
    last_probe_at: Optional[str]
    last_probe_ok: Optional[bool]
    last_probe_error: Optional[str]
    failure_count_in_window: int


class NetworkMonitor:
    """Tracks failures, flips offline when the threshold is hit, and probes
    until reachability comes back. Emits no events on its own — the
    orchestrator wires the resume callback.
    """

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_FAILURE_THRESHOLD,
        window_s: float = DEFAULT_FAILURE_WINDOW_S,
        probe_interval_s: float = DEFAULT_PROBE_INTERVAL_S,
        probe_url: str = DEFAULT_PROBE_URL,
        probe: Callable[[str], Awaitable[bool]] = default_probe,
        on_offline: Optional[Callable[[], Awaitable[None]]] = None,
        on_resume: Optional[Callable[[], Awaitable[None]]] = None,
        clock: Callable[[], float] = lambda: asyncio.get_event_loop().time(),
    ):
        self._threshold = threshold
        self._window_s = window_s
        self._probe_interval_s = probe_interval_s
        self._probe_url = probe_url
        self._probe = probe
        self._on_offline = on_offline
        self._on_resume = on_resume
        self._clock = clock
        self._failures: deque[float] = deque()
        self._offline = False
        self._offline_since: Optional[str] = None
        self._last_probe_at: Optional[str] = None
        self._last_probe_ok: Optional[bool] = None
        self._last_probe_error: Optional[str] = None
        self._probe_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # -- introspection --------------------------------------------------

    def is_offline(self) -> bool:
        return self._offline

    def state(self) -> NetworkState:
        self._evict()
        return NetworkState(
            offline=self._offline,
            offline_since=self._offline_since,
            probe_url=self._probe_url,
            probe_interval_s=self._probe_interval_s,
            last_probe_at=self._last_probe_at,
            last_probe_ok=self._last_probe_ok,
            last_probe_error=self._last_probe_error,
            failure_count_in_window=len(self._failures),
        )

    @property
    def probe_url(self) -> str:
        return self._probe_url

    def set_probe_url(self, url: str) -> None:
        self._probe_url = url

    # -- mutation -------------------------------------------------------

    async def record_failure(self) -> bool:
        """Append a failure; return True iff this call flipped us offline."""
        async with self._lock:
            now = self._clock()
            self._failures.append(now)
            self._evict(now)
            if not self._offline and len(self._failures) >= self._threshold:
                self._offline = True
                self._offline_since = _utcnow()
                if self._on_offline is not None:
                    with contextlib.suppress(Exception):
                        await self._on_offline()
                self._start_probe_loop()
                return True
            return False

    async def record_success(self) -> None:
        """A successful capture clears the recent-failure log."""
        async with self._lock:
            self._failures.clear()

    async def force_probe_now(self) -> bool:
        """One-shot probe; flips offline → online if it succeeds."""
        ok = False
        err: Optional[str] = None
        try:
            ok = await self._probe(self._probe_url)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            _log.warning("probe failed for %s: %s", self._probe_url, err)
        async with self._lock:
            self._last_probe_at = _utcnow()
            self._last_probe_ok = ok
            self._last_probe_error = None if ok else err
            if ok and self._offline:
                await self._go_online_locked()
        return ok

    # -- internal -------------------------------------------------------

    def _evict(self, now: Optional[float] = None) -> None:
        cutoff = (now if now is not None else self._clock()) - self._window_s
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    def _start_probe_loop(self) -> None:
        if self._probe_task is not None and not self._probe_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._probe_task = loop.create_task(self._probe_loop())

    async def _probe_loop(self) -> None:
        while self._offline:
            await asyncio.sleep(self._probe_interval_s)
            ok = False
            err: Optional[str] = None
            try:
                ok = await self._probe(self._probe_url)
            except Exception as exc:  # surface diagnostics, never crash the loop
                err = f"{type(exc).__name__}: {exc}"
                _log.warning("probe failed for %s: %s", self._probe_url, err)
            self._last_probe_at = _utcnow()
            self._last_probe_ok = ok
            self._last_probe_error = None if ok else err
            if ok:
                async with self._lock:
                    if self._offline:
                        await self._go_online_locked()
                return

    async def _go_online_locked(self) -> None:
        """Caller must hold ``self._lock``."""
        self._offline = False
        self._offline_since = None
        self._failures.clear()
        if self._on_resume is not None:
            with contextlib.suppress(Exception):
                await self._on_resume()
