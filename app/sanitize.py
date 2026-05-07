"""Filename, stem, and case-slug sanitization (CLAUDE.md §3, §5).

Every byte that ends up on disk passes through this module. The rules are
designed to satisfy the strictest target filesystem (NTFS/exFAT) so that a
case folder copied from a Mac to a Windows machine — or zipped for an
evidence handoff — opens without surprises.

Outside callers should use:

* ``sanitize_component(s, max_len)`` — single path component
* ``canonical_filename(...)`` — media kind: stem + extension
* ``canonical_page_only_stem(...)`` — page-only kind (no media file)
* ``slugify_case(name)`` — case-folder slug
* ``next_collision_suffix(existing, stem)`` — append __c2/__c3/...
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

__all__ = [
    "sanitize_component",
    "canonical_filename",
    "canonical_page_only_stem",
    "slugify_case",
    "next_collision_suffix",
    "url_hash",
]

# NTFS/exFAT-illegal chars + ASCII control range, but NOT \t \n \r — those go
# through the whitespace collapse below instead of being turned into "-".
_ILLEGAL_RE = re.compile(
    r"[<>:\"/\\|?*\x00-\x08\x0b\x0c\x0e-\x1f]"
)
_WHITESPACE_RE = re.compile(r"\s+")

_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)

# Defaults aligned with CLAUDE.md §5: title 80, uploader 40.
TITLE_MAX = 80
UPLOADER_MAX = 40
SEPARATOR = "__"


def sanitize_component(s: str, max_len: int = TITLE_MAX) -> str:
    """Return ``s`` made safe for use as a single path component.

    Steps, in order: NFKC-normalise, replace illegal chars with ``-``,
    collapse whitespace to single spaces, strip leading/trailing whitespace
    and dots, codepoint-truncate to ``max_len``, and finally guard against
    Windows reserved names (``CON``, ``PRN``, ``AUX``, ``NUL``,
    ``COM1``-``COM9``, ``LPT1``-``LPT9``) by appending ``_``.

    Empty input — or input that becomes empty after normalisation — returns
    ``"untitled"`` so that the caller never sees a zero-length stem.
    """
    if not s:
        return "untitled"

    s = unicodedata.normalize("NFKC", s)
    s = _ILLEGAL_RE.sub("-", s)
    s = _WHITESPACE_RE.sub(" ", s)
    s = s.strip().strip(".").strip()

    if not s:
        return "untitled"

    if len(s) > max_len:
        s = s[:max_len].rstrip().rstrip(".").rstrip()
        if not s:
            return "untitled"

    if s.upper() in _RESERVED_NAMES or s.upper().split(".", 1)[0] in _RESERVED_NAMES:
        s = s + "_"

    return s


def url_hash(url: str) -> str:
    """First 12 hex chars of ``sha256(url)`` — the page-only anchor (§5)."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def canonical_filename(
    *,
    platform: str,
    uploader: str,
    title: str,
    upload_date: str,
    video_id: str,
    ext: str,
) -> str:
    """Build the media-kind filename per CLAUDE.md §5.

    Pattern: ``{platform}__{uploader}__{title}__{upload_date}__{video_id}.{ext}``.
    ``video_id`` is the unique anchor and is sanitised but never truncated;
    ``upload_date`` is left unsanitised (callers pass ``YYYY-MM-DD`` already).
    """
    parts = [
        sanitize_component(platform, max_len=32),
        sanitize_component(uploader, max_len=UPLOADER_MAX),
        sanitize_component(title, max_len=TITLE_MAX),
        sanitize_component(upload_date, max_len=16),
        sanitize_component(video_id, max_len=128),
    ]
    stem = SEPARATOR.join(parts)
    ext_clean = ext.lstrip(".")
    if not ext_clean:
        return stem
    return f"{stem}.{ext_clean}"


def canonical_page_only_stem(
    *,
    platform: str,
    page_title: str,
    capture_date: str,
    url_final: str,
) -> str:
    """Build the page-only stem per CLAUDE.md §5.

    Pattern: ``{platform}__{page_title}__{capture_date}__{url_hash}`` where
    ``url_hash`` is the first 12 hex chars of ``sha256(url_final)``.
    """
    return SEPARATOR.join(
        [
            sanitize_component(platform, max_len=32),
            sanitize_component(page_title, max_len=TITLE_MAX),
            sanitize_component(capture_date, max_len=16),
            url_hash(url_final),
        ]
    )


def slugify_case(name: str, *, fallback_index: int = 1) -> str:
    """Turn a human case name into a filesystem-safe slug.

    Lowercased ASCII, hyphen-separated. Non-ASCII characters are decomposed
    via NFKD and stripped of combining marks so that "Café 2026" becomes
    "cafe-2026". Empty input returns ``case-{fallback_index}``.
    """
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if not s:
        return f"case-{fallback_index}"
    if len(s) > 64:
        s = s[:64].rstrip("-")
    return s


def next_collision_suffix(existing: Iterable[str], stem: str) -> str:
    """Return an unused stem from ``stem``, ``stem__c2``, ``stem__c3``, ...

    ``existing`` is consumed once; pass a list/set when calling with
    repeated probes. The function is purely string logic — disk presence
    is the caller's job.
    """
    taken = set(existing)
    if stem not in taken:
        return stem
    n = 2
    while True:
        candidate = f"{stem}{SEPARATOR}c{n}"
        if candidate not in taken:
            return candidate
        n += 1
