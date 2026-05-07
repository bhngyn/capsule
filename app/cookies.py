"""Per-case Netscape cookies.txt handling (CLAUDE.md §11).

Cookies are a primary investigator workflow — the same authenticated session
flows to yt-dlp and (Phase 2+) Playwright + browsertrix-crawler so that the
media file and the page snapshot come from the same logged-in view of the
site.

**Hard rules:** values are never returned by any function in this module,
never written to a log, never echoed in audit-log details. Only the list of
domains and the count/expiry per domain are surfaced. The on-disk file is
created with mode 0600.

The Netscape format is the lowest-common-denominator that yt-dlp,
browsertrix, and the major browser-extension exporters all speak. Lines:

    # Netscape HTTP Cookie File
    domain  flag  path  secure  expiration  name  value

Tab-separated. Lines starting with ``#`` (other than the magic header) are
comments. Blank lines are allowed.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import config

__all__ = [
    "DomainSummary",
    "CookiesSummary",
    "MergeStats",
    "FreshnessReport",
    "path_for",
    "save",
    "save_merged",
    "summary",
    "parse",
    "merge",
    "merge_preview",
    "target_coverage",
    "domains_for",
    "exists",
    "write_json",
    "to_netscape",
    "validate_freshness",
    "snapshot_hash",
    "ephemeral_path",
    "write_ephemeral",
    "discard_ephemeral",
]


# Default expiry for session cookies when serialised to Netscape format.
# yt-dlp / browsertrix treat ``0`` as a session cookie; we use that to keep
# parity with the parser in :func:`_parse_lines`.
_SESSION_EXPIRY = 0


@dataclass(frozen=True)
class DomainSummary:
    domain: str
    count: int
    earliest_expiry: int | None  # epoch seconds; None means session cookie
    has_expired: bool


@dataclass(frozen=True)
class CookiesSummary:
    total_cookies: int
    domains: list[DomainSummary]


def path_for(case_slug: str) -> Path:
    return config.CONFIG_DIR / "cases" / case_slug / "cookies.txt"


def exists(case_slug: str) -> bool:
    return path_for(case_slug).is_file()


def _parse_lines(content: str) -> Iterable[tuple[str, int | None]]:
    """Yield ``(domain, expiry_epoch_or_none)`` for every cookie line.

    Values are never yielded — callers cannot accidentally surface them.
    """
    for raw in content.splitlines():
        line = raw.rstrip("\r")
        if not line:
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        # Strip leading "#HttpOnly_" if present (yt-dlp/curl convention).
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        parts = line.split("\t")
        if len(parts) < 7:
            raise ValueError(f"malformed cookies line: {len(parts)} fields, expected 7")
        domain = parts[0].lstrip(".").lower()
        try:
            exp = int(parts[4])
        except ValueError as e:
            raise ValueError(f"non-integer expiry in cookies line for {domain!r}") from e
        yield domain, (exp if exp > 0 else None)


def _summarise(content: str) -> CookiesSummary:
    by_domain: dict[str, list[int | None]] = {}
    total = 0
    for domain, exp in _parse_lines(content):
        by_domain.setdefault(domain, []).append(exp)
        total += 1

    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    domains: list[DomainSummary] = []
    for domain, expirations in sorted(by_domain.items()):
        non_session = [e for e in expirations if e is not None]
        earliest = min(non_session) if non_session else None
        has_expired = any(e is not None and e < now for e in expirations)
        domains.append(
            DomainSummary(
                domain=domain,
                count=len(expirations),
                earliest_expiry=earliest,
                has_expired=has_expired,
            )
        )
    return CookiesSummary(total_cookies=total, domains=domains)


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write ``data`` to ``target`` via a sibling temp file + atomic rename.

    Guarantees the target either reflects the previous content or the new
    content; never a truncated partial write. The temp file inherits mode
    0600 on Unix before the rename so the final file is never world-readable
    even momentarily.
    """
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    tmp.replace(target)


def save(case_slug: str, content: bytes) -> CookiesSummary:
    """Validate + persist a cookies.txt for ``case_slug``.

    Raises ``ValueError`` on a malformed file (no partial write). The file
    is written with mode 0600 to guard against accidental snooping by other
    users on a shared host.
    """
    text = content.decode("utf-8", errors="strict")
    summary_obj = _summarise(text)  # validates first
    target = path_for(case_slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(target, content)
    return summary_obj


@dataclass(frozen=True)
class MergeStats:
    """Outcome of merging an incoming cookies file into an existing one.

    Counted at the cookie granularity, where a "cookie" is identified by
    ``(domain, path, name)`` — the same identity browsers use to decide
    whether a Set-Cookie supersedes an earlier one.
    """

    added: int      # cookies in incoming, not present in existing
    replaced: int   # cookies present in both — incoming wins
    kept: int       # cookies in existing, not touched by incoming


def _iter_cookie_lines(text: str) -> Iterable[tuple[tuple[str, str, str], str]]:
    """Yield ``((domain, path, name), full_line)`` for every cookie line.

    The full line is preserved verbatim (including any ``#HttpOnly_``
    prefix) so a merge round-trip writes back exactly what was supplied.
    Comment lines and blanks are skipped. Malformed lines raise
    ``ValueError`` — the same signal ``_parse_lines`` raises.
    """
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line:
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        keying = line[len("#HttpOnly_") :] if line.startswith("#HttpOnly_") else line
        parts = keying.split("\t")
        if len(parts) < 7:
            raise ValueError(f"malformed cookies line: {len(parts)} fields, expected 7")
        domain = parts[0].lstrip(".").lower()
        path = parts[2]
        name = parts[5]
        yield (domain, path, name), line


def merge(existing_text: str, incoming_text: str) -> tuple[str, MergeStats]:
    """Merge ``incoming_text`` into ``existing_text``.

    Cookies are keyed by ``(domain, path, name)``. Where both sides have
    the same key, the incoming cookie wins (newer expiry, refreshed value).
    Cookies present only in existing are kept. Cookies present only in
    incoming are appended.

    Returns ``(merged_text, stats)``. ``merged_text`` ends with a single
    trailing newline so a write-and-reread round-trip is stable.
    """
    existing_records: dict[tuple[str, str, str], str] = {}
    for key, line in _iter_cookie_lines(existing_text):
        existing_records[key] = line

    incoming_records: dict[tuple[str, str, str], str] = {}
    for key, line in _iter_cookie_lines(incoming_text):
        incoming_records[key] = line

    added = replaced = kept = 0
    out: list[str] = ["# Netscape HTTP Cookie File"]
    seen: set[tuple[str, str, str]] = set()
    for key, line in existing_records.items():
        if key in incoming_records:
            out.append(incoming_records[key])
            replaced += 1
        else:
            out.append(line)
            kept += 1
        seen.add(key)
    for key, line in incoming_records.items():
        if key not in seen:
            out.append(line)
            added += 1
    return "\n".join(out) + "\n", MergeStats(added=added, replaced=replaced, kept=kept)


def merge_preview(case_slug: str, incoming_text: str) -> tuple[CookiesSummary, MergeStats]:
    """What ``save_merged`` would produce, without writing.

    Used by the wizard's review step so the investigator sees the
    post-merge total and counts before committing. If no existing
    cookies file exists for ``case_slug``, behaves as if merging into
    an empty set: ``added`` equals the incoming count, ``replaced`` and
    ``kept`` are zero.
    """
    target = path_for(case_slug)
    existing_text = target.read_text(encoding="utf-8") if target.is_file() else ""
    merged_text, stats = merge(existing_text, incoming_text)
    return _summarise(merged_text), stats


def save_merged(case_slug: str, content: bytes) -> tuple[CookiesSummary, MergeStats]:
    """Merge ``content`` into the case's existing cookies file.

    If no cookies file exists yet, this writes ``content`` as-is and
    returns ``MergeStats(added=N, replaced=0, kept=0)`` where ``N`` is
    the count of valid cookies in the input. The on-disk file is mode
    ``0600`` on Unix, the same as :func:`save`.
    """
    text = content.decode("utf-8", errors="strict")
    target = path_for(case_slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        existing_text = target.read_text(encoding="utf-8")
        merged_text, stats = merge(existing_text, text)
        summary_obj = _summarise(merged_text)
        _atomic_write_bytes(target, merged_text.encode("utf-8"))
    else:
        summary_obj = _summarise(text)
        # Use the input bytes verbatim on first save so a "merge" against
        # an empty case is byte-identical to a plain ``save``.
        _atomic_write_bytes(target, content)
        stats = MergeStats(added=summary_obj.total_cookies, replaced=0, kept=0)
    return summary_obj, stats


def summary(case_slug: str) -> CookiesSummary | None:
    target = path_for(case_slug)
    if not target.is_file():
        return None
    return _summarise(target.read_text(encoding="utf-8"))


def parse(content: bytes | str) -> CookiesSummary:
    """Validate + summarise without writing anything to disk.

    The wizard calls this through ``/api/cookies/preview`` so the user sees
    parse errors and a domain summary before committing the file. ``save``
    uses the same underlying summariser, so the wizard's preview and the
    eventual save agree on the parse result.
    """
    text = content.decode("utf-8", errors="strict") if isinstance(content, bytes) else content
    return _summarise(text)


def _target_host(target_url: str) -> str | None:
    """Extract the lowercased hostname from ``target_url``.

    Accepts bare hosts (``twitter.com``) as well as full URLs. Returns
    ``None`` if no usable host can be parsed.
    """
    if not target_url:
        return None
    candidate = target_url.strip()
    if "//" not in candidate:
        # urlparse only finds a netloc when a scheme is present.
        candidate = "//" + candidate
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower().strip(".")
    return host or None


def target_coverage(
    summary: CookiesSummary, target_url: str | None
) -> dict | None:
    """Report whether the parsed cookies cover ``target_url``.

    Returns ``None`` if ``target_url`` is empty/unparseable so the wizard
    can render a neutral state. Otherwise:

    .. code-block:: python

        {"target_domain": "x.com",
         "covered": True,
         "matched_domains": ["x.com"]}

    A cookie domain ``d`` covers a target host ``h`` when ``h == d`` or
    ``h.endswith("." + d)`` — i.e. cookies set on a parent domain apply to
    its subdomains, but not the other way around. This matches the host-
    matching used by the browsers that exported the file.
    """
    host = _target_host(target_url) if target_url else None
    if host is None:
        return None
    matched = [
        d.domain
        for d in summary.domains
        if d.domain == host or host.endswith("." + d.domain)
    ]
    return {
        "target_domain": host,
        "covered": bool(matched),
        "matched_domains": matched,
    }


def _to_netscape_line(cookie: dict) -> str:
    """Render one browser-extension cookie object as a Netscape-format line.

    Input shape matches Chrome's ``chrome.cookies.getAll`` and Firefox's
    equivalent: ``{name, value, domain, path, expirationDate?, secure,
    httpOnly, hostOnly, sameSite?}``. ``expirationDate`` is a float in
    epoch seconds (Chrome) — we cast to int with ``0`` as the session-
    cookie sentinel, matching :func:`_parse_lines`.

    HttpOnly cookies emit the ``#HttpOnly_`` prefix line so the parser
    round-trips them faithfully (the same convention curl/yt-dlp use).
    """
    name = str(cookie.get("name") or "").strip()
    if not name:
        raise ValueError("cookie missing 'name'")
    if any(ch in name for ch in "\t\r\n"):
        raise ValueError(f"cookie name contains illegal whitespace: {name!r}")
    value = cookie.get("value")
    if value is None:
        raise ValueError(f"cookie {name!r} missing 'value'")
    value = str(value)
    if any(ch in value for ch in "\t\r\n"):
        raise ValueError(f"cookie {name!r} value contains illegal whitespace")

    domain = str(cookie.get("domain") or "").strip().lower().rstrip(".")
    if not domain:
        raise ValueError(f"cookie {name!r} missing 'domain'")

    # Netscape "include subdomains" flag. Browser extensions expose
    # ``hostOnly``: True means "do NOT include subdomains". We invert.
    host_only = bool(cookie.get("hostOnly", False))
    if host_only:
        domain_field = domain
        include_subdomains = "FALSE"
    else:
        domain_field = "." + domain
        include_subdomains = "TRUE"

    path = str(cookie.get("path") or "/")
    if not path.startswith("/"):
        path = "/" + path

    secure = "TRUE" if cookie.get("secure") else "FALSE"

    raw_exp = cookie.get("expirationDate")
    if raw_exp is None or raw_exp is False:
        expiry = _SESSION_EXPIRY
    else:
        try:
            expiry = int(float(raw_exp))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"cookie {name!r} has non-numeric expirationDate: {raw_exp!r}"
            ) from exc
        if expiry < 0:
            expiry = _SESSION_EXPIRY

    line = "\t".join(
        [domain_field, include_subdomains, path, secure, str(expiry), name, value]
    )
    if cookie.get("httpOnly"):
        line = "#HttpOnly_" + line
    return line


def to_netscape(cookies: list[dict]) -> str:
    """Render a list of browser-extension cookie objects as Netscape text.

    Output starts with the canonical magic header so downstream parsers
    (yt-dlp, browsertrix, curl) recognise the format.
    """
    if not isinstance(cookies, list):
        raise ValueError("cookies must be a list")
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        if not isinstance(c, dict):
            raise ValueError(f"cookie entry must be an object, got {type(c).__name__}")
        lines.append(_to_netscape_line(c))
    return "\n".join(lines) + "\n"


def write_json(
    case_slug: str,
    cookies: list[dict],
    target_url: str | None = None,
) -> CookiesSummary:
    """Persist browser-extension JSON cookies as the case's Netscape file.

    Reuses the existing 0600 path so the same downstream consumers (yt-dlp,
    Playwright, browsertrix) see no difference between a manually-uploaded
    cookies.txt and one supplied by the extension. ``target_url`` is
    threaded through purely so the caller can compute coverage; this
    function does not validate it.
    """
    text = to_netscape(cookies)
    return save(case_slug, text.encode("utf-8"))


def domains_for(case_slug: str) -> set[str]:
    """Return the set of cookie domains for ``case_slug``.

    Empty set if no cookies file exists. Used by ``classify`` to decide
    whether to mark a URL as authenticated for the active case.
    """
    s = summary(case_slug)
    if s is None:
        return set()
    return {d.domain for d in s.domains}


# --- Freshness, snapshotting, and ephemeral storage -------------------------
#
# The hardening pass introduces three forensic affordances:
#
#   1. ``validate_freshness`` — record (don't second-guess) when cookies have
#      already expired at capture time. Investigators want to know the
#      capture used credentials the site would have rejected.
#   2. ``snapshot_hash`` — sha256 of the on-disk cookies.txt at the moment
#      a job starts. Two jobs run minutes apart can be proven to have used
#      the same cookie set (or not) without ever logging values.
#   3. ``ephemeral_path`` / ``write_ephemeral`` / ``discard_ephemeral`` —
#      a per-job tmpdir cookie file that's wiped after the job ends, for
#      one-shot captures the investigator does not want persisted to the
#      case directory.


@dataclass(frozen=True)
class FreshnessReport:
    """Outcome of a freshness check at job-start.

    ``expired`` and ``expiring_soon`` list domains, never values. Session
    cookies (no expiry) never appear in either list — a session cookie is
    valid as long as the browser session it was exported from is alive.
    """

    expired: list[DomainSummary]
    expiring_soon: list[DomainSummary]
    # Cookies-file SHA-256 at the moment of the check. Used to bind the
    # report to the exact cookie set the job will use.
    snapshot_sha256: str | None
    checked_at: str  # ISO 8601 UTC


def validate_freshness(
    case_slug: str,
    *,
    soon_window_s: int = 24 * 60 * 60,
    now: int | None = None,
) -> FreshnessReport | None:
    """Check the case cookies file for expired / soon-to-expire cookies.

    Returns ``None`` if no cookies file exists for ``case_slug`` (no work
    to do). Otherwise returns a :class:`FreshnessReport` whose lists never
    contain cookie values.

    ``soon_window_s`` defaults to one day — a cookie that will expire while
    a long capture is running is worth flagging.
    """
    target = path_for(case_slug)
    if not target.is_file():
        return None
    text = target.read_text(encoding="utf-8")
    summary_obj = _summarise(text)
    now_ts = now if now is not None else int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    expired: list[DomainSummary] = []
    soon: list[DomainSummary] = []
    for d in summary_obj.domains:
        if d.has_expired:
            expired.append(d)
            continue
        if d.earliest_expiry is not None and d.earliest_expiry - now_ts < soon_window_s:
            soon.append(d)
    return FreshnessReport(
        expired=expired,
        expiring_soon=soon,
        snapshot_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        checked_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    )


def snapshot_hash(case_slug: str) -> str | None:
    """SHA-256 of the on-disk cookies file for ``case_slug``, or ``None``
    if no file exists. Used by the orchestrator to bind a job to the exact
    cookie set it consumed.

    Hashing is over the raw file bytes — values are never decoded out of
    this function. The hash is opaque (a 64-hex string) so writing it into
    audit details cannot leak credentials.
    """
    target = path_for(case_slug)
    if not target.is_file():
        return None
    return hashlib.sha256(target.read_bytes()).hexdigest()


def snapshot_hash_path(path: Path) -> str | None:
    """SHA-256 of ``path`` if it exists. Used by the ephemeral path that
    doesn't live under the case slug.
    """
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ephemeral_root() -> Path:
    """Per-instance tmpdir for one-shot cookie files. Never world-readable.

    Lives under ``CONFIG_DIR/cookies_ephemeral/`` so it shares the same
    storage privileges as the persistent case cookie files. Each call
    creates a fresh sub-directory; cleanup is the caller's responsibility.
    """
    root = config.CONFIG_DIR / "cookies_ephemeral"
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
    return root


def ephemeral_path(job_id: str) -> Path:
    """Return a fresh tmpdir cookie path for ``job_id``.

    The directory is created with mode 0700 and the file path is returned
    without writing. Use :func:`write_ephemeral` to materialise the cookies.
    """
    root = _ephemeral_root()
    tmpdir = Path(tempfile.mkdtemp(prefix=f"job-{job_id[:8]}-", dir=str(root)))
    if os.name != "nt":
        try:
            os.chmod(tmpdir, 0o700)
        except OSError:
            pass
    return tmpdir / "cookies.txt"


def write_ephemeral(job_id: str, cookies: list[dict]) -> tuple[Path, CookiesSummary]:
    """Render JSON cookies to a per-job ephemeral Netscape file.

    Returns ``(path, summary)``. The file lives in a fresh tmpdir and is
    not written to the case directory. Caller MUST call
    :func:`discard_ephemeral` when the job ends, regardless of outcome.
    """
    text = to_netscape(cookies)
    summary_obj = _summarise(text)
    target = ephemeral_path(job_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(target, text.encode("utf-8"))
    return target, summary_obj


def discard_ephemeral(path: Path) -> None:
    """Remove an ephemeral cookies file and its parent tmpdir, best-effort.

    Safe to call on a path that has already been removed (no-op).
    """
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass
    parent = path.parent
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
