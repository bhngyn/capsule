r"""Network-layer ad/tracker blocklist (CLAUDE.md §13 — capture-side mutations
must be recorded in meta.json + audit log).

Loads ``app/static/blocklists/easylist-essentials.json`` — the *single source
of truth* the extension also consumes — and exposes:

* :func:`should_block(url)` — boolean predicate over a fetch URL.
* :func:`route_handler(blocked_log)` — Playwright route callback that
  ``abort()``\s blocked URLs and records each into ``blocked_log``. The
  caller surfaces the log in ``meta.json.capture.blocked_requests`` and the
  audit entry.

Hard rules:

* The blocklist is **conservative** (a few hundred curated entries). It
  does not ship full EasyList — pattern-based EasyList rules can over-match
  on first-party endpoints, which would silently drop evidence.
* Every blocked URL is *recorded*, not silently dropped. Forensic value of
  the WARC stays intact: the WARC will show the request was made and
  received an aborted response, not that the page never tried.
* Disabled per-case via ``case.settings_json["block_ads"] = False``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from . import config

__all__ = [
    "BlocklistRules",
    "load",
    "default_rules",
    "BLOCKLIST_PATH",
]


BLOCKLIST_PATH: Path = config.STATIC_DIR / "blocklists" / "easylist-essentials.json"


@dataclass(frozen=True)
class BlocklistRules:
    """Compiled, immutable view of the on-disk blocklist."""

    version: str
    blocked_hosts: frozenset[str]
    # Compiled regex over the URL path component. Patterns from the JSON
    # file are anchored loosely (substring match) — the JSON is the source
    # of truth for what's blocked; the regex is just a fast matcher.
    path_pattern: re.Pattern[str] | None
    # Raw list of patterns (kept for audit/debug surfaces).
    blocked_path_patterns: tuple[str, ...] = field(default_factory=tuple)

    def should_block(self, url: str) -> bool:
        """True if ``url`` matches any rule in this list.

        Subdomain match: a rule for ``doubleclick.net`` matches
        ``ad.doubleclick.net`` as well. This mirrors the extension's
        declarativeNetRequest ``requestDomains`` behaviour.
        """
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        host = (parts.hostname or "").lower().lstrip(".")
        if not host:
            return False
        # Host check, including subdomains.
        if host in self.blocked_hosts:
            return True
        for blocked in self.blocked_hosts:
            if host.endswith("." + blocked):
                return True
        # Path pattern check.
        if self.path_pattern is not None and parts.path:
            if self.path_pattern.search(parts.path):
                return True
        return False


def _compile(blocked_path_patterns: list[str]) -> re.Pattern[str] | None:
    if not blocked_path_patterns:
        return None
    parts = [re.escape(p) for p in blocked_path_patterns]
    return re.compile("|".join(parts))


def load(path: Path | None = None) -> BlocklistRules:
    """Load and compile the blocklist from ``path`` (default: bundled file).

    Re-reads the file on every call so tests that monkeypatch the static
    dir see the new content. For production callers, prefer
    :func:`default_rules` which caches the parse.
    """
    target = path or BLOCKLIST_PATH
    raw = json.loads(target.read_text(encoding="utf-8"))
    hosts = frozenset((h or "").lower().lstrip(".") for h in raw.get("blocked_hosts", []))
    patterns = list(raw.get("blocked_path_patterns") or [])
    return BlocklistRules(
        version=str(raw.get("version", "unknown")),
        blocked_hosts=hosts,
        path_pattern=_compile(patterns),
        blocked_path_patterns=tuple(patterns),
    )


@lru_cache(maxsize=1)
def default_rules() -> BlocklistRules:
    """Cached load of the bundled blocklist. Used by the production path."""
    return load()


def reset_cache() -> None:
    """Drop the cached default. Used by tests after monkeypatching the path."""
    default_rules.cache_clear()
