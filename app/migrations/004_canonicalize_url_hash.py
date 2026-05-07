"""Migration 004: recompute ``downloads.url_hash`` from canonical URL form.

Before this migration, ``url_hash = sha256(url_final)[:12]`` — so two
paste-variants of the same URL (different ``utm_*`` params, different
scheme case, trailing slash) had different hashes and the duplicate-
detection modal (CLAUDE.md §15) could not collapse them. After this
migration, the hash is taken over the *canonical* form of ``url_final``
(see ``app/url_canonical.py``).

Invariants preserved:

* The ``UNIQUE(case_id, capture_kind, url_hash)`` constraint stays. If
  two existing rows in the same ``(case_id, capture_kind)`` would
  collide post-canonicalization, the older row in ``capture_date`` order
  keeps the unsuffixed hash; younger rows get ``__c2`` / ``__c3`` / …
  appended (the same suffix mechanism §15 forced re-captures use, so
  the dedup key shape is consistent forever after).
* ``meta.json`` files on disk are signed and frozen at capture time.
  This migration **does not touch them.** The DB column is a queryable
  index over the same data; the meta.json's stored hash stays valid as
  the historical record. Verifiers that re-derive the hash from the
  canonical form will agree with the new DB column; verifiers that read
  it out of the meta.json will get the original (pre-canonical) value
  for legacy rows. Both are documented in CLAUDE.md §5.

A summary audit-log entry (``duplicate.url_canonicalized``) records the
migrated count and any collision-resolved rows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

# Allow this migration to import ``app.url_canonical`` whether it's run
# inside the regular app (where ``app`` is a package) or via the test
# harness (where the parent dir is on ``sys.path``).
_THIS_DIR = Path(__file__).resolve().parent
_APP_DIR = _THIS_DIR.parent
_REPO_DIR = _APP_DIR.parent
if str(_REPO_DIR) not in sys.path:
    sys.path.insert(0, str(_REPO_DIR))
from app import url_canonical  # noqa: E402


def upgrade(conn: sqlite3.Connection) -> None:
    rows = list(conn.execute(
        "SELECT id, case_id, capture_kind, url_hash, final_url, source_url, "
        "capture_date FROM downloads"
    ).fetchall())
    if not rows:
        return

    # Step 1: compute the new base hash for each row.
    # We canonicalize the stored ``final_url`` (preferred) or fall back
    # to ``source_url`` if final_url is null on a partially-finalized row.
    proposed: list[tuple[int, int, str, str, str, str]] = []
    # (row_id, case_id, capture_kind, old_hash, new_base_hash, capture_date)
    for r in rows:
        url = r["final_url"] or r["source_url"] or ""
        canonical = url_canonical.canonicalize(url) if url else ""
        new_base = (
            hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
            if canonical else r["url_hash"]
        )
        proposed.append((
            int(r["id"]),
            int(r["case_id"]),
            str(r["capture_kind"]),
            str(r["url_hash"]),
            new_base,
            str(r["capture_date"] or ""),
        ))

    # Step 2: resolve collisions. For each (case_id, capture_kind,
    # new_base_hash) group, the oldest capture_date wins the unsuffixed
    # slot; subsequent rows in capture_date order get __c2, __c3, ….
    # If the row had a __cN suffix originally (forced re-capture), we
    # preserve the same N to keep audit-log details consistent.
    groups: dict[tuple[int, str, str], list[tuple[int, str, str, str]]] = {}
    # group key -> list of (row_id, capture_date, old_hash, original_suffix)
    for row_id, case_id, kind, old_hash, new_base, capture_date in proposed:
        # Extract any pre-existing __cN suffix from the old hash so we
        # can preserve the index (forced re-captures stay forced).
        suffix = ""
        bare = old_hash
        if "__c" in old_hash:
            bare, _, tail = old_hash.partition("__c")
            try:
                int(tail)
                suffix = "__c" + tail
            except ValueError:
                # Malformed — treat as no suffix.
                suffix = ""
                bare = old_hash
        key = (case_id, kind, new_base)
        groups.setdefault(key, []).append((row_id, capture_date, old_hash, suffix))

    updates: list[tuple[str, int]] = []  # (new_url_hash, row_id)
    collisions: list[dict[str, object]] = []
    for (case_id, kind, new_base), members in groups.items():
        # Keep rows that were originally forced re-captures (__cN suffix
        # set) at their original index. Among rows without a suffix, the
        # oldest capture_date wins index 1.
        suffixed = [m for m in members if m[3]]
        bare = [m for m in members if not m[3]]
        bare.sort(key=lambda m: (m[1], m[0]))
        used_indices: set[int] = set()
        # Re-apply the original suffix to forced re-captures verbatim.
        for row_id, _capture_date, _old, suffix in suffixed:
            try:
                used_indices.add(int(suffix.removeprefix("__c")))
            except ValueError:
                continue
            updates.append((new_base + suffix, row_id))
        # Bare rows: oldest gets the unsuffixed slot, then count up.
        next_index = 2
        for i, (row_id, capture_date, _old, _suffix) in enumerate(bare):
            if i == 0 and 1 not in used_indices:
                updates.append((new_base, row_id))
                used_indices.add(1)
            else:
                while next_index in used_indices:
                    next_index += 1
                used_indices.add(next_index)
                new_hash = f"{new_base}__c{next_index}"
                updates.append((new_hash, row_id))
                collisions.append({
                    "row_id": row_id,
                    "case_id": case_id,
                    "capture_kind": kind,
                    "capture_date": capture_date,
                    "old_url_hash": _old,
                    "new_url_hash": new_hash,
                })

    # Step 3: apply updates. Each row's hash is rewritten; rows whose
    # hash didn't change still get touched (no-op UPDATE) — keeps the
    # logic simple and the audit count accurate.
    changed = 0
    with conn:
        for new_hash, row_id in updates:
            cur = conn.execute(
                "UPDATE downloads SET url_hash = ? WHERE id = ? AND url_hash != ?",
                (new_hash, row_id, new_hash),
            )
            if cur.rowcount:
                changed += 1

    # Step 4: a single rolled-up audit-log entry. Append directly via
    # ``audit.append`` so the chain stays intact.
    if changed or collisions:
        from app import audit as audit_mod
        audit_mod.append(
            conn,
            "duplicate.url_canonicalized",
            actor="system",
            details={
                "migrated_rows": changed,
                "collisions_resolved": len(collisions),
                "collision_summary": collisions[:50],  # cap log payload
            },
        )
