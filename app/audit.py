"""Hash-chained, append-only audit log (CLAUDE.md §8).

Every state-changing operation calls ``append(conn, action, …)`` which:

1. Reads the previous row's ``row_hash`` (or 64 zero hex digits for row 1).
2. Builds the new row dict.
3. Computes ``row_hash = sha256(canonical_encode(row_minus_row_hash))`` —
   the canonical encoding includes ``prev_hash``, so flipping any byte
   anywhere in the chain breaks every subsequent hash.
4. Inserts the row.

``verify_chain(conn)`` re-derives every hash and reports the first broken
row id, if any. The Audit Log view in the UI calls this on page load.

**Cookie values are forbidden in ``details``.** ``append`` rejects keys named
``cookie``, ``cookies`` or ``set_cookie`` so a regression in a caller can't
leak credentials into evidence.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from typing import Any

__all__ = [
    "ZERO_HASH",
    "FORBIDDEN_DETAIL_KEYS",
    "canonical_encode",
    "row_hash_for",
    "append",
    "verify_chain",
    "iter_entries",
    "DetailLeakError",
]

ZERO_HASH = "0" * 64
FORBIDDEN_DETAIL_KEYS = frozenset({"cookie", "cookies", "set_cookie"})


class DetailLeakError(ValueError):
    """Raised when a caller passes a forbidden key in ``details``.

    Triggers in tests and in production — the audit log refuses to record
    cookie values under any circumstance.
    """


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def canonical_encode(row: dict[str, Any]) -> bytes:
    """Stable JSON encoding used for hashing.

    ``row_hash`` and ``id`` are excluded — the former because it's the
    output of this very function, the latter because it's autoincremented
    after the row is built.
    """
    payload = {k: v for k, v in row.items() if k not in {"row_hash", "id"}}
    # ``ensure_ascii=True`` guarantees byte-stable encoding across Python
    # versions and platforms — the recipient's verifier hashes this same
    # canonical form, so any drift in non-ASCII escaping would break the
    # chain. Non-ASCII characters are escaped as ``\uXXXX``.
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def row_hash_for(row: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_encode(row)).hexdigest()


def _check_details(details: dict[str, Any]) -> None:
    """Reject forbidden keys at any depth (cookies hide in nested dicts too)."""
    stack: list[Any] = [details]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            for k, vv in v.items():
                if k.lower() in FORBIDDEN_DETAIL_KEYS:
                    raise DetailLeakError(
                        f"forbidden audit detail key: {k!r}"
                    )
                stack.append(vv)
        elif isinstance(v, list):
            stack.extend(v)


def _last_row_hash(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["row_hash"] if row else ZERO_HASH


def append(
    conn: sqlite3.Connection,
    action: str,
    *,
    case_id: int | None = None,
    download_id: int | None = None,
    actor: str = "system",
    details: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> int:
    """Append one row. Returns its primary-key id.

    Atomic against the connection — uses ``with conn:`` so a duplicate
    row_hash from a clock-skew clash never half-commits.
    """
    details = details or {}
    _check_details(details)
    ts = timestamp or _utcnow()
    prev_hash = _last_row_hash(conn)
    details_json = json.dumps(details, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    row = {
        "timestamp": ts,
        "action": action,
        "case_id": case_id,
        "download_id": download_id,
        "actor": actor,
        "details_json": details_json,
        "prev_hash": prev_hash,
    }
    rh = row_hash_for(row)
    with conn:
        cur = conn.execute(
            """
            INSERT INTO audit_log
                (timestamp, action, case_id, download_id, actor,
                 details_json, prev_hash, row_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["timestamp"],
                row["action"],
                row["case_id"],
                row["download_id"],
                row["actor"],
                row["details_json"],
                row["prev_hash"],
                rh,
            ),
        )
    return int(cur.lastrowid or 0)


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, int | None]:
    """Re-derive every row hash. Returns ``(ok, first_broken_id)``.

    First broken id is None on success or on an empty table.
    """
    cursor = conn.execute(
        """
        SELECT id, timestamp, action, case_id, download_id, actor,
               details_json, prev_hash, row_hash
          FROM audit_log
         ORDER BY id ASC
        """
    )
    expected_prev = ZERO_HASH
    for row in cursor:
        if row["prev_hash"] != expected_prev:
            return False, int(row["id"])
        rebuilt = {
            "timestamp": row["timestamp"],
            "action": row["action"],
            "case_id": row["case_id"],
            "download_id": row["download_id"],
            "actor": row["actor"],
            "details_json": row["details_json"],
            "prev_hash": row["prev_hash"],
        }
        if row_hash_for(rebuilt) != row["row_hash"]:
            return False, int(row["id"])
        expected_prev = row["row_hash"]
    return True, None


def iter_entries(
    conn: sqlite3.Connection,
    *,
    case_id: int | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream rows for the API. Filters: case, since-timestamp, limit."""
    sql = (
        "SELECT id, timestamp, action, case_id, download_id, actor, "
        "details_json, prev_hash, row_hash FROM audit_log WHERE 1=1"
    )
    params: list[Any] = []
    if case_id is not None:
        sql += " AND case_id = ?"
        params.append(case_id)
    if since:
        sql += " AND timestamp >= ?"
        params.append(since)
    sql += " ORDER BY id ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    for row in conn.execute(sql, params):
        d = dict(row)
        d["details"] = json.loads(d.pop("details_json"))
        yield d
