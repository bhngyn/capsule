"""Bounded retry helper for transient SQLite lock contention.

CLAUDE.md §16 (v0.11 backlog, bucket 2 #8): single-investigator usage
masks the occasional ``sqlite3.OperationalError: database is locked``
that fires when an external tool (Windows Defender, Time Machine, a
Spotlight indexer) briefly holds the DB file. Without a retry, that
contention drops audit rows and stalls counters on the floor — the
forensic chain develops gaps the recipient can't see.

The helper is intentionally narrow:

* Only ``sqlite3.OperationalError`` whose message contains ``"locked"``
  or ``"busy"`` is retried. Schema errors (``no such table``) and
  integrity errors must propagate so a regression isn't masked.
* Bounded attempts (default 4) with exponential backoff + jitter so
  even a worst case completes in well under a second.
* Sync only. The audit and orchestrator writes that consume this all
  sit inside ``with conn:`` blocks; there is no async DB path today.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from typing import Callable, TypeVar

__all__ = ["db_retry", "DEFAULT_ATTEMPTS", "DEFAULT_BASE_DELAY_S"]

DEFAULT_ATTEMPTS = 4
DEFAULT_BASE_DELAY_S = 0.05

_log = logging.getLogger("capsule.db_retry")

T = TypeVar("T")


def _is_transient(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def db_retry(
    fn: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    label: str = "db_op",
) -> T:
    """Run ``fn`` with bounded retry on transient SQLite lock errors.

    On a transient failure: ``base_delay_s * (2 ** i) + jitter`` where
    jitter is ``base_delay_s * rand()``. The final attempt re-raises if
    it still fails so the caller sees the original error. Non-transient
    ``OperationalError`` (e.g. ``no such table``) and every other
    exception propagate immediately.

    ``sleep`` and ``rand`` are injectable so the test suite can drive
    the retry without real-time delays.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last_exc: sqlite3.OperationalError | None = None
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not _is_transient(exc):
                raise
            last_exc = exc
            if i == attempts - 1:
                break
            delay = base_delay_s * (2 ** i) + base_delay_s * rand()
            _log.debug(
                "db_retry: transient lock on %s attempt %d/%d: %s (sleeping %.3fs)",
                label, i + 1, attempts, exc, delay,
            )
            sleep(delay)
    assert last_exc is not None  # loop exits via return or break-with-exc
    raise last_exc
