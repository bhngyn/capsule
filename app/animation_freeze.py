"""Animation/transition freeze layer for the still-PNG screenshot.

The CSS in ``app/static/blocklists/animation-freeze.css`` is injected via
``page.add_style_tag`` immediately before ``page.screenshot(full_page=True)``
and removed immediately after, so MHTML and WARC (already captured by then)
are unaffected.

Mirrors :mod:`app.banner_hide` so the version/hash plumbing into
``meta.json.capture.animations_frozen_version`` and the audit log uses the
same shape — every capture-side mutation is fingerprinted by the file it
came from.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import config

__all__ = [
    "AnimationFreezeRules",
    "load",
    "default_rules",
    "ANIMATION_FREEZE_PATH",
]


ANIMATION_FREEZE_PATH: Path = config.STATIC_DIR / "blocklists" / "animation-freeze.css"

_VERSION_RE = re.compile(r"\bVersion:\s*([0-9A-Za-z._\-]+)")


@dataclass(frozen=True)
class AnimationFreezeRules:
    css: str
    version: str
    sha256: str


def _extract_version(css: str) -> str:
    m = _VERSION_RE.search(css)
    return m.group(1) if m else "unknown"


def load(path: Path | None = None) -> AnimationFreezeRules:
    target = path or ANIMATION_FREEZE_PATH
    css = target.read_text(encoding="utf-8")
    return AnimationFreezeRules(
        css=css,
        version=_extract_version(css),
        sha256=hashlib.sha256(css.encode("utf-8")).hexdigest(),
    )


@lru_cache(maxsize=1)
def default_rules() -> AnimationFreezeRules:
    return load()


def reset_cache() -> None:
    default_rules.cache_clear()
