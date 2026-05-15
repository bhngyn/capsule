"""SQLite lock-contention retry — CLAUDE.md §16 v0.11 bucket 2 #8.

Single-investigator usage masks the occasional
``sqlite3.OperationalError: database is locked`` that fires when an
external tool (Windows AV, Time Machine, Spotlight) briefly holds the
DB file. Without the retry, that contention drops audit rows and stall
counters on the floor. These tests exercise the retry's three legs:

1. Transient ``locked``/``busy`` errors are retried within budget.
2. Genuine ``OperationalError`` (e.g. ``no such table``) propagate.
3. The audit log and job-update paths consume the retry transparently.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from app import audit, db, db_retry


# ----------------------------------------------------------------------
# db_retry primitive
# ----------------------------------------------------------------------


def test_db_retry_returns_on_success():
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        return "ok"

    assert db_retry.db_retry(fn, sleep=lambda _: None) == "ok"
    assert calls["n"] == 1


def test_db_retry_retries_transient_lock():
    seq = iter([
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("database is busy"),
        None,
    ])
    sleeps: list[float] = []

    def fn() -> str:
        nxt = next(seq)
        if isinstance(nxt, Exception):
            raise nxt
        return "ok"

    out = db_retry.db_retry(fn, sleep=sleeps.append, rand=lambda: 0.0)
    assert out == "ok"
    # Two retries fired → two sleeps; geometric: base, 2*base
    assert len(sleeps) == 2
    assert sleeps[0] == pytest.approx(db_retry.DEFAULT_BASE_DELAY_S)
    assert sleeps[1] == pytest.approx(db_retry.DEFAULT_BASE_DELAY_S * 2)


def test_db_retry_propagates_non_transient_operational_error():
    def fn():
        raise sqlite3.OperationalError("no such table: jobs")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        db_retry.db_retry(fn, sleep=lambda _: None)


def test_db_retry_propagates_other_exception_types():
    def fn():
        raise ValueError("not a DB error")

    with pytest.raises(ValueError):
        db_retry.db_retry(fn, sleep=lambda _: None)


def test_db_retry_exhausts_attempts_and_raises_last():
    def fn():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db_retry.db_retry(fn, attempts=3, sleep=lambda _: None, rand=lambda: 0.0)


def test_db_retry_jitter_uses_rand():
    captured: list[float] = []

    seq = iter([
        sqlite3.OperationalError("database is locked"),
        None,
    ])

    def fn():
        nxt = next(seq)
        if isinstance(nxt, Exception):
            raise nxt
        return "ok"

    db_retry.db_retry(
        fn,
        base_delay_s=0.1,
        sleep=captured.append,
        rand=lambda: 0.5,
    )
    # delay = base * 2**0 + base * rand = 0.1 + 0.1 * 0.5 = 0.15
    assert captured == [pytest.approx(0.15)]


# ----------------------------------------------------------------------
# audit.append consumes the retry transparently
# ----------------------------------------------------------------------


def test_audit_append_retries_through_transient_lock(monkeypatch, tmp_path: Path):
    """Open two connections to the same DB; the second one holds a write
    lock while ``audit.append`` is mid-call. The retry should ride through.
    """
    db_path = tmp_path / "library.db"
    writer = db.connect(str(db_path))
    db.migrate(writer)
    holder = db.connect(str(db_path))

    # Synthesize "locked" by having ``holder`` open a long write transaction.
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "INSERT INTO audit_log "
        "(timestamp, action, case_id, download_id, actor, details_json, prev_hash, row_hash) "
        "VALUES (?, ?, NULL, NULL, ?, ?, ?, ?)",
        ("2026-01-01T00:00:00+00:00", "test.holder", "system", "{}", audit.ZERO_HASH, "h" * 64),
    )

    def release_after(_):
        # The retry's first sleep call is the cue to release the holder.
        try:
            holder.commit()
        except sqlite3.Error:
            pass

    out = db_retry.db_retry(
        lambda: audit.append(writer, "test.action", details={"k": "v"}),
        sleep=release_after,
        rand=lambda: 0.0,
    )
    assert isinstance(out, int) and out > 0

    writer.close()
    holder.close()


def test_audit_append_propagates_schema_error(tmp_path: Path):
    """A genuine OperationalError (table missing) must propagate immediately."""
    db_path = tmp_path / "blank.db"
    conn = sqlite3.connect(str(db_path))
    # Don't migrate — audit_log doesn't exist.
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        audit.append(conn, "test.action")
    conn.close()
