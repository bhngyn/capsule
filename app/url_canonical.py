"""Canonical URL form for de-duplication (CLAUDE.md §15).

The forensic record always preserves ``url_submitted`` (what the user
pasted) and ``url_final`` (what the redirect chain landed on) untouched.
Canonicalization is **only** used to derive ``url_hash`` so two paste-
variants of the same URL — different tracking params, different scheme
case, trailing slash — collapse to the same dedup key.

Stripping rules:

* Lowercase scheme and host. Path and query values stay as-is — those
  are server-defined and can be case-sensitive.
* Drop the fragment (never sent to the server).
* Drop a curated list of tracking / share / analytics parameters
  (utm_*, fbclid, gclid, igshid, mc_eid, mc_cid, _ga, _gl, yclid,
  msclkid, ref, ref_src, ref_url, share_id, si, feature, mkt_tok,
  hsCtaTracking, _hsenc, _hsmi). Match is case-insensitive on the
  parameter name only.
* Sort remaining query keys alphabetically (preserve duplicate-key
  ordering — same key repeated keeps its original relative order).
* Strip a trailing slash from the path unless the path *is* ``/``.

This module is pure: no I/O, no network, no DB. Easy to test exhaustively.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

__all__ = [
    "canonicalize",
    "TRACKING_PARAM_PREFIXES",
    "TRACKING_PARAM_EXACT",
]

# Exact (case-insensitive) parameter names dropped during canonicalization.
TRACKING_PARAM_EXACT: frozenset[str] = frozenset({
    "fbclid",
    "gclid",
    "igshid",
    "mc_eid",
    "mc_cid",
    "_ga",
    "_gl",
    "yclid",
    "msclkid",
    "ref",
    "ref_src",
    "ref_url",
    "share_id",
    "si",          # YouTube share tag
    "feature",     # YouTube feature=share
    "mkt_tok",
    "hsctatracking",
    "_hsenc",
    "_hsmi",
    "spm",         # AliExpress / Taobao tracking
    "scm",
})

# Lowercased prefixes — any param whose lowered name starts with one of
# these is dropped. Covers ``utm_source``, ``utm_medium``, ``utm_campaign``,
# etc., and any future ``utm_*`` flavour.
TRACKING_PARAM_PREFIXES: tuple[str, ...] = (
    "utm_",
)


def _is_tracking_param(name: str) -> bool:
    n = name.lower()
    if n in TRACKING_PARAM_EXACT:
        return True
    return any(n.startswith(p) for p in TRACKING_PARAM_PREFIXES)


def canonicalize(url: str) -> str:
    """Return the canonical form of ``url``.

    The function is total: even malformed input returns a string. If the
    input has no scheme/host (e.g. ``"not a url"``) the result is the
    input unchanged — the caller (``url_hash``) still produces a stable
    digest, and the dedup behaviour for malformed URLs falls back to raw
    string equality.
    """
    if not url:
        return url
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        # Don't try to be clever for inputs that aren't URLs; preserves
        # the original behaviour for non-URL strings.
        return url

    scheme = parts.scheme.lower()
    # ``netloc`` may carry user:pass@host:port. Lowercase only the host
    # portion; user/password and port stay verbatim.
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, hostport = netloc.rsplit("@", 1)
        netloc = userinfo + "@" + _lower_hostport(hostport)
    else:
        netloc = _lower_hostport(netloc)

    path = parts.path or ""
    # Empty path is equivalent to "/" — collapse so
    # https://example.com and https://example.com/ canonicalize the same.
    if path == "":
        path = "/"
    elif len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
        if not path:
            path = "/"

    # Filter and sort the query. ``parse_qsl(keep_blank_values=True)``
    # preserves repeated keys and empty values verbatim.
    if parts.query:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        kept = [(k, v) for (k, v) in pairs if not _is_tracking_param(k)]
        # Stable sort by lowered key; ties keep original order.
        kept.sort(key=lambda kv: kv[0].lower())
        # ``safe='/'`` keeps slashes readable in the canonical form when
        # they appear inside a query value (rare, but happens with some
        # CDN tokens). Doesn't affect dedup since the encoding is
        # consistent across all callers.
        query = "&".join(
            f"{quote(unquote(k), safe='/')}={quote(unquote(v), safe='/')}"
            if v != "" else f"{quote(unquote(k), safe='/')}"
            for (k, v) in kept
        )
    else:
        query = ""

    # Always drop fragment.
    return urlunsplit((scheme, netloc, path, query, ""))


def _lower_hostport(hostport: str) -> str:
    """Lowercase only the host. Port (after ``:``) stays as-is."""
    if ":" in hostport:
        host, _, port = hostport.rpartition(":")
        return host.lower() + ":" + port
    return hostport.lower()
