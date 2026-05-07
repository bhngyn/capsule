"""Case CRUD + filesystem layout (CLAUDE.md §9, §11).

A case is the investigator's primary unit of work: every capture lives
inside a case, every evidence export is per-case, every cookies.txt is
per-case. We keep three things in sync:

* a row in ``cases`` (DB)
* a folder ``$CAPSULE_DOWNLOADS_DIR/{slug}/`` (with ``sidecars/`` inside)
* a folder ``$CAPSULE_CONFIG_DIR/cases/{slug}/`` (cookies live here)

Soft delete (``status = 'archived'``) is the only flavour exposed in v1 —
folders are never removed automatically. An investigator who genuinely
wants to purge a case does it manually on the host, outside Capsule.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import audit, config, sanitize

__all__ = [
    "Case",
    "CASE_STATUSES",
    "QUICK_CASE_SLUG",
    "QUICK_CASE_NAME",
    "create",
    "ensure_quick",
    "get",
    "get_by_slug",
    "list_all",
    "list_open",
    "rename",
    "update_settings",
    "update_status",
    "downloads_dir_for",
    "sidecars_dir_for",
    "case_config_dir_for",
]


CASE_STATUSES = frozenset({"open", "closed", "archived"})

# Slug for the auto-managed case backing the Simple-mode downloader.
# Pinned (not derived via slugify_case) so it stays unambiguously identifiable
# even if a user happens to name their own case "Quick captures".
QUICK_CASE_SLUG = "quick-captures"
QUICK_CASE_NAME = "Quick captures"


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Case:
    id: int
    slug: str
    name: str
    description: str
    status: str
    created_at: str
    updated_at: str
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Case":
        return cls(
            id=int(row["id"]),
            slug=row["slug"],
            name=row["name"],
            description=row["description"] or "",
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            settings=json.loads(row["settings_json"] or "{}"),
        )


# --- Filesystem helpers ------------------------------------------------------


def downloads_dir_for(slug: str) -> Path:
    return config.DOWNLOADS_DIR / slug


def sidecars_dir_for(slug: str) -> Path:
    return downloads_dir_for(slug) / "sidecars"


def case_config_dir_for(slug: str) -> Path:
    return config.CONFIG_DIR / "cases" / slug


def _provision_dirs(slug: str) -> None:
    downloads_dir_for(slug).mkdir(parents=True, exist_ok=True)
    sidecars_dir_for(slug).mkdir(parents=True, exist_ok=True)
    case_config_dir_for(slug).mkdir(parents=True, exist_ok=True)


def _existing_slugs(conn: sqlite3.Connection) -> set[str]:
    return {row["slug"] for row in conn.execute("SELECT slug FROM cases")}


def _next_unique_slug(base: str, taken: Iterable[str]) -> str:
    taken_set = set(taken)
    if base not in taken_set:
        return base
    n = 2
    while True:
        candidate = f"{base}-{n}"
        if candidate not in taken_set:
            return candidate
        n += 1


# --- CRUD --------------------------------------------------------------------


def create(
    conn: sqlite3.Connection,
    *,
    name: str,
    description: str = "",
    settings: dict[str, Any] | None = None,
) -> Case:
    """Create a case row, provision its folders, and audit the event."""
    name = name.strip()
    if not name:
        raise ValueError("case name must not be empty")

    existing = _existing_slugs(conn)
    base_slug = sanitize.slugify_case(name, fallback_index=len(existing) + 1)
    slug = _next_unique_slug(base_slug, existing)

    now = _utcnow()
    settings_json = json.dumps(settings or {}, sort_keys=True, separators=(",", ":"))

    with conn:
        cur = conn.execute(
            """
            INSERT INTO cases(slug, name, description, status,
                              created_at, updated_at, settings_json)
            VALUES (?, ?, ?, 'open', ?, ?, ?)
            """,
            (slug, name, description, now, now, settings_json),
        )
        case_id = int(cur.lastrowid or 0)

    _provision_dirs(slug)
    audit.append(
        conn,
        "case.created",
        case_id=case_id,
        actor="user",
        details={"slug": slug, "name": name},
    )
    return _require(conn, case_id)


def ensure_quick(conn: sqlite3.Connection) -> Case:
    """Resolve (or lazily create) the auto-managed quick-captures case.

    Backs the Simple-mode downloader: every paste-a-link capture lands in
    this single case so the rest of the forensic pipeline (audit, hashing,
    signing) keeps running unchanged. The slug is pinned, so a user who
    later happens to name their own case "Quick captures" gets a different
    slug.
    """
    existing = get_by_slug(conn, QUICK_CASE_SLUG)
    if existing is not None:
        return existing

    now = _utcnow()
    settings_json = json.dumps(
        {"auto_managed": True}, sort_keys=True, separators=(",", ":")
    )
    with conn:
        cur = conn.execute(
            """
            INSERT INTO cases(slug, name, description, status,
                              created_at, updated_at, settings_json)
            VALUES (?, ?, ?, 'open', ?, ?, ?)
            """,
            (
                QUICK_CASE_SLUG,
                QUICK_CASE_NAME,
                "Auto-managed case for the simple downloader.",
                now,
                now,
                settings_json,
            ),
        )
        case_id = int(cur.lastrowid or 0)

    _provision_dirs(QUICK_CASE_SLUG)
    audit.append(
        conn,
        "case.created",
        case_id=case_id,
        actor="system",
        details={"slug": QUICK_CASE_SLUG, "name": QUICK_CASE_NAME, "kind": "quick"},
    )
    return _require(conn, case_id)


def _require(conn: sqlite3.Connection, case_id: int) -> Case:
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if row is None:
        raise LookupError(f"case {case_id} not found")
    return Case.from_row(row)


def get(conn: sqlite3.Connection, case_id: int) -> Case | None:
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    return Case.from_row(row) if row else None


def get_by_slug(conn: sqlite3.Connection, slug: str) -> Case | None:
    row = conn.execute("SELECT * FROM cases WHERE slug = ?", (slug,)).fetchone()
    return Case.from_row(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[Case]:
    rows = conn.execute("SELECT * FROM cases ORDER BY updated_at DESC")
    return [Case.from_row(r) for r in rows]


def list_open(conn: sqlite3.Connection) -> list[Case]:
    rows = conn.execute(
        "SELECT * FROM cases WHERE status = 'open' ORDER BY updated_at DESC"
    )
    return [Case.from_row(r) for r in rows]


def rename(conn: sqlite3.Connection, case_id: int, new_name: str) -> Case:
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("case name must not be empty")
    case = _require(conn, case_id)
    now = _utcnow()
    with conn:
        conn.execute(
            "UPDATE cases SET name = ?, updated_at = ? WHERE id = ?",
            (new_name, now, case_id),
        )
    audit.append(
        conn,
        "case.renamed",
        case_id=case_id,
        actor="user",
        details={"from": case.name, "to": new_name},
    )
    return _require(conn, case_id)


def update_settings(
    conn: sqlite3.Connection, case_id: int, settings: dict[str, Any],
) -> Case:
    """Replace the case's ``settings_json`` blob.

    Plan §C: per-case profile overrides live here. Merging with prior
    settings is the caller's responsibility — pass the full dict you want
    persisted.
    """
    _require(conn, case_id)
    now = _utcnow()
    settings_json = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    with conn:
        conn.execute(
            "UPDATE cases SET settings_json = ?, updated_at = ? WHERE id = ?",
            (settings_json, now, case_id),
        )
    return _require(conn, case_id)


def update_status(conn: sqlite3.Connection, case_id: int, status: str) -> Case:
    if status not in CASE_STATUSES:
        raise ValueError(f"invalid status {status!r}")
    case = _require(conn, case_id)
    if case.status == status:
        return case
    now = _utcnow()
    with conn:
        conn.execute(
            "UPDATE cases SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, case_id),
        )
    audit.append(
        conn,
        "case.status_changed",
        case_id=case_id,
        actor="user",
        details={"from": case.status, "to": status},
    )
    return _require(conn, case_id)
