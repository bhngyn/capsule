"""Path-safety helpers (CLAUDE.md §3).

All paths persisted to the DB, sidecars, audit-log details, or evidence
exports must be relative to ``$CAPSULE_DOWNLOADS_DIR``. ``relative_to_downloads``
is the only sanctioned way to build those strings — it raises if the input
is outside the downloads root, catching bugs at write time instead of
leaking host details into evidence.
"""

from __future__ import annotations

from pathlib import Path

from . import config

__all__ = ["relative_to_downloads", "is_under_downloads"]


def is_under_downloads(p: Path) -> bool:
    try:
        p.resolve().relative_to(config.DOWNLOADS_DIR.resolve())
        return True
    except ValueError:
        return False


def relative_to_downloads(p: Path) -> str:
    """Return ``p`` as a forward-slash POSIX path relative to the downloads
    root. Raises ``ValueError`` if ``p`` is outside the root.
    """
    rel = p.resolve().relative_to(config.DOWNLOADS_DIR.resolve())
    return rel.as_posix()
