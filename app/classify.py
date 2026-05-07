"""URL classification (CLAUDE.md §5, §11).

Given a pasted URL and the active case, decide everything the capture
pipeline needs *before* yt-dlp / Playwright are invoked:

* the redirect chain (so the audit trail records what we actually fetched)
* the friendly platform slug (for filename + icon)
* whether to mark the URL as authenticated for this case (intersection of
  the redirect-chain hosts with the case's cookie domains, restricted to
  recognised social-media platforms)
* the stable ``url_hash`` used as the page-only anchor and the
  duplicate-detection key

The redirect walk uses ``httpx`` with ``follow_redirects=False`` and a
HEAD-then-GET fallback so we never download a body just to learn where the
URL points. The walk is capped at ``MAX_HOPS``. **No** body fetch happens.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from . import cookies, platforms, url_canonical

__all__ = [
    "Classification",
    "classify",
    "MAX_HOPS",
    "USER_AGENT",
]

MAX_HOPS = 10
USER_AGENT = "Capsule/0.1 (+https://github.com/anthropics/capsule)"
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
# Overall ceiling on the redirect-chain walk so a long chain of slow hops
# can't stall the paste-preview UI. With MAX_HOPS=10 and per-call timeouts
# of ~10s, the worst case without this ceiling is ~100s.
_WALK_BUDGET_S = 15.0


@dataclass(frozen=True)
class Classification:
    url_submitted: str
    url_final: str
    url_canonical: str
    redirect_chain: list[str]
    platform: str
    authenticated_domains: list[str]
    url_hash: str
    error: str | None = field(default=None)

    def to_dict(self) -> dict:
        d = {
            "url_submitted": self.url_submitted,
            "url_final": self.url_final,
            "url_canonical": self.url_canonical,
            "redirect_chain": list(self.redirect_chain),
            "platform": self.platform,
            "authenticated_domains": list(self.authenticated_domains),
            "url_hash": self.url_hash,
        }
        if self.error:
            d["error"] = self.error
        return d


def _url_hash(url: str) -> str:
    """Stable 12-char digest over the **canonical** form of ``url``.

    Two paste-variants of the same URL (different tracking params, scheme
    case, trailing slash) collapse to the same digest, so duplicate
    detection is robust against the noise users actually paste.
    """
    canonical = url_canonical.canonicalize(url)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().lstrip(".")


async def _walk_redirects(
    url: str, *, client: httpx.AsyncClient
) -> tuple[list[str], str | None]:
    """Return ``(chain, error_key_or_None)``.

    ``chain`` always starts with the submitted URL and never contains
    duplicates. If the walk fails (network error, redirect loop, max hops),
    the chain is whatever we managed to collect and ``error`` is set.
    """
    chain: list[str] = [url]
    seen: set[str] = {url}
    current = url
    for _ in range(MAX_HOPS):
        try:
            resp = await client.head(current, follow_redirects=False)
        except httpx.HTTPError as e:
            return chain, f"head_failed:{type(e).__name__}"
        status = resp.status_code
        location: str | None = resp.headers.get("location")
        # Some origins (and many CDNs) refuse HEAD with 405 / 403 / 501.
        # Fall back to a streamed GET so we still see the Location header
        # without downloading the response body.
        if status in (403, 405, 501):
            try:
                async with client.stream(
                    "GET", current, follow_redirects=False
                ) as streamed:
                    status = streamed.status_code
                    location = streamed.headers.get("location")
                    # Don't read the body — we only need the headers.
            except httpx.HTTPError as e:
                return chain, f"get_failed:{type(e).__name__}"
        if status in (301, 302, 303, 307, 308):
            if not location:
                return chain, "missing_location"
            nxt = str(httpx.URL(current).join(location))
            if nxt in seen:
                return chain, "redirect_loop"
            chain.append(nxt)
            seen.add(nxt)
            current = nxt
            continue
        return chain, None
    return chain, "too_many_hops"


def _authenticated_domains(
    chain: list[str], cookie_domains: set[str]
) -> list[str]:
    """Intersection of (chain hosts) and (cookie domains), filtered to social.

    A cookie domain matches a chain host when the host is the cookie domain
    or a subdomain of it. Only social-media domains are considered — we
    don't want to mark a generic page as "authenticated" just because the
    investigator happened to upload cookies for it.
    """
    hosts = {_hostname(u) for u in chain}
    matched: list[str] = []
    for cd in sorted(cookie_domains):
        if not platforms.is_social(cd):
            continue
        if any(host == cd or host.endswith("." + cd) for host in hosts):
            matched.append(cd)
    return matched


async def classify(
    url: str,
    *,
    case_slug: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> Classification:
    """Build a Classification from a submitted URL.

    ``client`` is optional — pass one in tests to short-circuit the network
    via ``httpx.MockTransport``. When ``None``, a transient client is
    created with the Capsule User-Agent and a 10-second read budget.
    """
    submitted = url.strip()
    if not submitted:
        raise ValueError("url is empty")

    cookie_domains = cookies.domains_for(case_slug) if case_slug else set()

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=_TIMEOUT,
            follow_redirects=False,
        )
    try:
        try:
            chain, error = await asyncio.wait_for(
                _walk_redirects(submitted, client=client),
                timeout=_WALK_BUDGET_S,
            )
        except asyncio.TimeoutError:
            chain, error = [submitted], "walk_timeout"
    finally:
        if own_client:
            await client.aclose()

    final_url = chain[-1]
    return Classification(
        url_submitted=submitted,
        url_final=final_url,
        url_canonical=url_canonical.canonicalize(final_url),
        redirect_chain=chain,
        platform=platforms.platform_for_url(final_url),
        authenticated_domains=_authenticated_domains(chain, cookie_domains),
        url_hash=_url_hash(final_url),
        error=error,
    )
