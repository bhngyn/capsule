"""Cookie/consent banner CSS hide layer (CLAUDE.md §13 — capture-side
mutations must be recorded in meta.json + audit log).

Loads ``app/static/blocklists/banner-hide.css`` and exposes the CSS body
plus a version string that the capture pipeline records into
``meta.json.capture.banner_hide_version`` and the audit log.

Hard rules:

* CSS only. Never modifies the DOM. The captured MHTML and WARC retain the
  banner element in source — a forensic reviewer can still answer "did the
  page show a consent banner?" by inspecting the archive.
* No "click reject" logic. Auto-clicking is forbidden by Capsule policy
  (CLAUDE.md §13). The site's consent state is whatever the user-agent
  default is at page load.
* Disabled per-case via ``case.settings_json["hide_cookie_banners"] = False``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import config

__all__ = [
    "BannerHideRules",
    "load",
    "default_rules",
    "BANNER_HIDE_PATH",
]


BANNER_HIDE_PATH: Path = config.STATIC_DIR / "blocklists" / "banner-hide.css"

_VERSION_RE = re.compile(r"\bVersion:\s*([0-9A-Za-z._\-]+)")


@dataclass(frozen=True)
class BannerHideRules:
    css: str
    version: str
    sha256: str


def _extract_version(css: str) -> str:
    m = _VERSION_RE.search(css)
    return m.group(1) if m else "unknown"


def load(path: Path | None = None) -> BannerHideRules:
    """Read and tag the bundled CSS file."""
    target = path or BANNER_HIDE_PATH
    css = target.read_text(encoding="utf-8")
    return BannerHideRules(
        css=css,
        version=_extract_version(css),
        sha256=hashlib.sha256(css.encode("utf-8")).hexdigest(),
    )


@lru_cache(maxsize=1)
def default_rules() -> BannerHideRules:
    return load()


def reset_cache() -> None:
    default_rules.cache_clear()
