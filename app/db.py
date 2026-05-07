"""SQLite library database (CLAUDE.md §8, §9).

Plain ``sqlite3`` — no ORM. The schema is defined as forward-only SQL files
in ``app/migrations/``; each migration's filename starts with a 3-digit
version (``000_init.sql``, ``001_*.sql`` …). Applied versions are tracked in
``schema_migrations``. Re-running ``migrate()`` is idempotent.

Connections are opened with WAL journaling so that a long-running write
(post-processing a capture) does not block API reads.
"""

from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from . import config

__all__ = ["DB_PATH", "connect", "migrate", "applied_versions"]

DB_PATH: Path = config.CONFIG_DIR / "library.db"
MIGRATIONS_DIR: Path = config.APP_DIR / "migrations"

_VERSION_RE = re.compile(r"^(\d{3})_")


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with PRAGMAs the rest of the app expects.

    ``path`` defaults to ``config.CONFIG_DIR / 'library.db'``. Pass
    ``":memory:"`` for tests. Parent directory is created on demand so that
    callers don't need to know about ``/config``.
    """
    target = Path(path) if path else DB_PATH
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(target) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            filename   TEXT NOT NULL
        )
        """
    )


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Return the set of migration versions already applied to ``conn``."""
    _ensure_migrations_table(conn)
    cur = conn.execute("SELECT version FROM schema_migrations")
    return {row[0] for row in cur.fetchall()}


def _migration_files(directory: Path = MIGRATIONS_DIR) -> Iterable[tuple[int, Path]]:
    """Yield ``(version, path)`` for every ``NNN_*.sql`` in directory order."""
    for path in sorted(directory.glob("*.sql")):
        m = _VERSION_RE.match(path.name)
        if not m:
            continue
        yield int(m.group(1)), path


def migrate(conn: sqlite3.Connection, *, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply any pending migrations from ``directory``.

    Returns the list of versions applied during this call (empty if the
    schema is already up to date). Each migration runs in its own
    transaction; partial failure leaves the DB in the previous version.
    """
    _ensure_migrations_table(conn)
    done = applied_versions(conn)
    applied: list[int] = []
    for version, path in _migration_files(directory):
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        # ``executescript`` ends any pending transaction and runs the script
        # in autocommit mode. We then bracket the bookkeeping insert in a
        # plain transaction so the version row only appears if we got here.
        conn.executescript(sql)
        with conn:
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at, filename)"
                " VALUES (?, ?, ?)",
                (version, _utcnow(), path.name),
            )
        applied.append(version)
    return applied
