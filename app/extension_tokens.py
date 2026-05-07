"""Per-extension Bearer tokens (CLAUDE.md §11; plan: backend changes).

A pair-and-trust model for the browser extension. The Capsule UI generates
a token, shows it to the investigator exactly once, and stores its SHA-256
on disk. The extension keeps the raw token in ``chrome.storage.local`` and
sends it as ``Authorization: Bearer <token>`` on every request.

Hard rules:

* The raw token is **never** persisted on the host. Only the SHA-256 digest
  is stored in ``$CAPSULE_CONFIG_DIR/extension_tokens.json`` (mode 0600).
* Verification uses ``secrets.compare_digest`` over the digest so a timing
  attack can't enumerate tokens.
* Tokens grant write-only access (submit captures, upload cookies, list
  cases). They never grant read access to existing captures or audit log;
  see ``main.py`` for the routes they unlock.
* When a token is paired with an ``extension_id``, every authenticated
  request must present that same id (Chrome guarantees the id can only be
  forged with developer access to the build process). Tokens paired
  without an id (legacy) are grandfathered.
* :func:`rotate` issues a fresh token and revokes the prior one in a single
  call so a token leak can be remediated without re-pairing the extension
  from scratch.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from . import config

__all__ = [
    "Token",
    "ExtensionIdMismatch",
    "tokens_path",
    "issue",
    "list_tokens",
    "verify",
    "touch",
    "revoke",
    "rotate",
]


class ExtensionIdMismatch(ValueError):
    """Raised by :func:`verify` when the presented extension_id does not
    match the id the token was bound to. The API layer surfaces this as a
    403 (the right answer for "you sent valid creds for the wrong device").
    """


def tokens_path() -> Path:
    """Live lookup so tests that swap ``CONFIG_DIR`` see the new path."""
    return config.CONFIG_DIR / "extension_tokens.json"


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Token:
    """Public-facing record. ``hash`` is the on-disk identifier; the raw
    token is only returned by :func:`issue` and never round-trips after."""

    id: str
    label: str
    extension_id: str | None
    created_at: str
    last_used_at: str | None


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _short_id(digest: str) -> str:
    """First 12 hex chars of the digest. Stable, collision-safe for the
    handful of paired extensions an investigator will ever have."""

    return digest[:12]


def _load() -> list[dict]:
    path = tokens_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return data


def _save(rows: list[dict]) -> None:
    path = tokens_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(rows, indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(payload, encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)


def _row_to_token(row: dict) -> Token:
    return Token(
        id=str(row.get("id", "")),
        label=str(row.get("label", "")),
        extension_id=row.get("extension_id"),
        created_at=str(row.get("created_at", "")),
        last_used_at=row.get("last_used_at"),
    )


def issue(label: str, *, extension_id: str | None = None) -> tuple[Token, str]:
    """Generate a new token. Returns ``(record, raw_token)``.

    The raw token is the only chance the caller has to observe it — the
    UI must surface it to the investigator immediately and discard it.
    """
    label = (label or "").strip()
    if not label:
        raise ValueError("token label must not be empty")
    raw = secrets.token_urlsafe(32)
    digest = _hash(raw)
    record = {
        "id": _short_id(digest),
        "hash": digest,
        "label": label,
        "extension_id": extension_id or None,
        "created_at": _utcnow(),
        "last_used_at": None,
    }
    rows = _load()
    rows.append(record)
    _save(rows)
    return _row_to_token(record), raw


def list_tokens() -> list[Token]:
    return [_row_to_token(r) for r in _load()]


def verify(raw: str, *, extension_id: str | None = None) -> Token | None:
    """Constant-time check against every stored hash.

    Returns the matching :class:`Token` or ``None``. Callers should follow
    a successful verify with :func:`touch` to keep ``last_used_at`` fresh.

    When the matched token was paired with an ``extension_id``, the caller
    must present the same value or this function raises
    :class:`ExtensionIdMismatch`. Tokens paired without an id (legacy) are
    accepted regardless. The extension passes its ``chrome.runtime.id`` in
    the ``X-Extension-Id`` header on every authenticated request.
    """
    if not raw:
        return None
    presented = _hash(raw)
    presented_b = presented.encode("ascii")
    match: dict | None = None
    for row in _load():
        stored = str(row.get("hash", "")).encode("ascii")
        # Always compare to keep the loop's timing data-independent.
        if secrets.compare_digest(presented_b, stored) and match is None:
            match = row
    if match is None:
        return None
    bound = match.get("extension_id")
    if bound:
        if not extension_id:
            raise ExtensionIdMismatch(
                f"token bound to extension_id {bound!r} but request supplied none"
            )
        if str(extension_id) != str(bound):
            raise ExtensionIdMismatch(
                f"token bound to extension_id {bound!r}, got {extension_id!r}"
            )
    return _row_to_token(match)


def touch(token_id: str) -> None:
    """Update ``last_used_at`` for the token with ``id``. Best-effort —
    failure to update is non-fatal so request handling continues."""
    rows = _load()
    changed = False
    for row in rows:
        if row.get("id") == token_id:
            row["last_used_at"] = _utcnow()
            changed = True
            break
    if changed:
        try:
            _save(rows)
        except OSError:
            pass


def revoke(token_id: str) -> Token | None:
    """Remove a paired extension. Returns the removed record, or ``None``
    if no such token existed."""
    rows = _load()
    kept: list[dict] = []
    removed: dict | None = None
    for row in rows:
        if row.get("id") == token_id and removed is None:
            removed = row
        else:
            kept.append(row)
    if removed is None:
        return None
    _save(kept)
    return _row_to_token(removed)


def rotate(token_id: str) -> tuple[Token, str] | None:
    """Issue a replacement token for an existing pairing.

    Atomic: the prior row is removed and the new row is written in a
    single :func:`_save` call so a crash mid-rotation can't leave both
    tokens active.

    Returns ``(record, raw_token)`` on success, or ``None`` if no token
    with ``token_id`` existed. The label and extension_id binding carry
    over from the prior pairing.
    """
    rows = _load()
    prior_idx: int | None = None
    for i, row in enumerate(rows):
        if row.get("id") == token_id:
            prior_idx = i
            break
    if prior_idx is None:
        return None
    prior = rows[prior_idx]
    raw = secrets.token_urlsafe(32)
    digest = _hash(raw)
    new_record = {
        "id": _short_id(digest),
        "hash": digest,
        "label": prior.get("label", ""),
        "extension_id": prior.get("extension_id"),
        "created_at": _utcnow(),
        "last_used_at": None,
    }
    rows.pop(prior_idx)
    rows.append(new_record)
    _save(rows)
    return _row_to_token(new_record), raw
