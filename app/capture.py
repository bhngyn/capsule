"""Page snapshot capture (CLAUDE.md §2, §6 — Phase 2).

Two producers, one entry point:

* ``Playwright`` (Chromium) writes the MHTML snapshot and the full-page PNG.
  We pass the case cookies into the browser context so the snapshot reflects
  the same authenticated session yt-dlp downloaded the media from.
* ``browsertrix-crawler`` writes a forensic-grade WARC with the same engine,
  scoped to ``page+resources`` (the page itself + every sub-resource it
  loaded — never the whole site). It runs as an external process; if not
  installed, the WARC artifact is omitted and the meta.json reflects that.

The capture function is producer-agnostic on the consumer side: it returns
``CaptureBundle`` paths, which ``postprocess.finalize`` already accepts.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cookies as cookies_mod
from .platforms import is_social

__all__ = [
    "CaptureBundle",
    "capture_page",
    "browsertrix_available",
    "playwright_chromium_version",
    "DEFAULT_PLAYWRIGHT_TIMEOUT_MS",
    "DEFAULT_BROWSERTRIX_TIMEOUT_S",
    "DEFAULT_BROWSERTRIX_ATTEMPTS",
    "DEFAULT_SETTLE_DELAY_MS",
    "DEFAULT_VIEWPORT_WIDTH",
    "DEFAULT_VIEWPORT_HEIGHT",
    "DEFAULT_WARMUP_STEPS",
    "DEFAULT_WARMUP_STEP_MS",
    "DEFAULT_BROWSERTRIX_BEHAVIOR_TIMEOUT_S",
]


# Plan §U3 universal defaults. Profiles may override (Slow extends, Fast keeps
# tighter values).
#
# 60s was the prior Playwright timeout — too tight on slow links. 90s gives
# the page time to render through TLS handshake + first paint over a VPN.
DEFAULT_PLAYWRIGHT_TIMEOUT_MS = 90_000
# A short settle period after DOMContentLoaded so client-side rendering and
# late-loading sub-resources (fonts, hero images) make it into the snapshot.
# 4s is a deliberate compromise: long enough for most SPAs to paint, short
# enough that we don't hold the slot for slowly-streamed embeds.
DEFAULT_SETTLE_DELAY_MS = 4_000
# 180s, up from 90s. The browsertrix call writes the WARC AND every
# sub-resource — on a slow link the prior 90s was tripping for sites with
# heavy hero videos that aren't even part of the evidence.
DEFAULT_BROWSERTRIX_TIMEOUT_S = 180
# Three attempts with exponential backoff. The browsertrix process itself
# does some retrying internally; this is the outer envelope that handles
# "the whole binary fell over" cases (OOM kill, timeout).
DEFAULT_BROWSERTRIX_ATTEMPTS = 3
# Desktop laptop-class viewport. Wider than X/Twitter's ~1265 px breakpoint,
# so responsive sites render in their full desktop layout rather than
# collapsing to a narrow column where embedded media dominates.
DEFAULT_VIEWPORT_WIDTH = 1440
DEFAULT_VIEWPORT_HEIGHT = 900
# Bounded lazy-load warm-up: scroll the page in viewport-sized steps to
# trigger hydration of replies, embedded images, etc. Capped so endless
# threads don't yield 20,000 px screenshots.
DEFAULT_WARMUP_STEPS = 12
DEFAULT_WARMUP_STEP_MS = 250
# browsertrix behavior cap. Stays well under DEFAULT_BROWSERTRIX_TIMEOUT_S
# so behaviors can't starve the rest of the crawl.
DEFAULT_BROWSERTRIX_BEHAVIOR_TIMEOUT_S = 30


@dataclass(frozen=True)
class CaptureBundle:
    mhtml: Path | None
    screenshot: Path | None
    warc: Path | None
    chromium_version: str
    browsertrix_version: str
    page_title: str | None
    response_headers: dict[str, str] | None


def browsertrix_available() -> bool:
    """True iff ``browsertrix-crawler`` is on PATH and runnable."""
    return shutil.which("browsertrix-crawler") is not None


# --- Cookies ----------------------------------------------------------------


def _netscape_to_playwright_cookies(content: str) -> list[dict[str, Any]]:
    """Translate Netscape ``cookies.txt`` lines into Playwright's cookie dicts.

    Values are passed through to the browser context — cookies.py is the
    only module that reads the file off disk, so leakage paths stay
    auditable. Playwright handles the actual session.
    """
    out: list[dict[str, Any]] = []
    for raw in content.splitlines():
        line = raw.rstrip("\r")
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
            http_only = True
        else:
            http_only = False
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expiry, name, value = parts[:7]
        try:
            exp_int = int(expiry)
        except ValueError:
            continue
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain.lstrip("."),
            "path": path or "/",
            "secure": secure.upper() == "TRUE",
            "httpOnly": http_only,
            "sameSite": "Lax",
        }
        if exp_int > 0:
            cookie["expires"] = exp_int
        out.append(cookie)
    return out


def _load_cookies_for(case_slug: str | None) -> list[dict[str, Any]]:
    if not case_slug or not cookies_mod.exists(case_slug):
        return []
    return _netscape_to_playwright_cookies(
        cookies_mod.path_for(case_slug).read_text(encoding="utf-8")
    )


# --- Playwright snapshot ----------------------------------------------------


async def playwright_chromium_version() -> str:
    """Return the bundled Chromium's reported version string."""
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return "0"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                return browser.version or "0"
            finally:
                await browser.close()
    except Exception:
        return "0"


async def _warm_lazy_load(
    page,
    *,
    max_steps: int = DEFAULT_WARMUP_STEPS,
    step_ms: int = DEFAULT_WARMUP_STEP_MS,
) -> None:
    """Scroll the page to surface lazy-loaded content, then return to top.

    Stops as soon as ``document.scrollHeight`` stops growing, or after
    ``max_steps`` viewport scrolls — whichever comes first. The cap exists
    because endless threads (e.g. an X tweet with thousands of replies)
    would otherwise produce a screenshot tens of thousands of pixels tall.
    Runs before the MHTML snapshot so MHTML and PNG see the same hydrated
    DOM.
    """
    last_height = 0
    for _ in range(max_steps):
        height = await page.evaluate("document.documentElement.scrollHeight")
        if height <= last_height:
            break
        last_height = height
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.9)")
        await page.wait_for_timeout(step_ms)
    await page.evaluate("window.scrollTo(0, 0)")
    # Brief pause so any image fade-in animations settle before the PNG.
    await page.wait_for_timeout(step_ms)


async def _playwright_snapshot(
    *,
    url: str,
    out_dir: Path,
    cookies: list[dict[str, Any]],
    timeout_ms: int = DEFAULT_PLAYWRIGHT_TIMEOUT_MS,
    settle_ms: int = DEFAULT_SETTLE_DELAY_MS,
    proxy_url: str | None = None,
) -> tuple[Path, Path, str, dict[str, str], str]:
    """Open ``url`` once and produce ``(mhtml, screenshot, title, headers, version)``.

    The CDP ``Page.captureSnapshot`` call yields a single-file MHTML — no
    second navigation, so the snapshot and the screenshot are guaranteed to
    be of the same DOM state.

    Wait policy (CLAUDE.md plan §U3): ``domcontentloaded`` + a short settle
    delay, **not** ``networkidle``. ``networkidle`` is unreliable on slow
    links and on sites that hold long-poll connections open — it can hang
    until the timeout for pages that have already rendered everything we
    need. ``domcontentloaded`` fires deterministically; the settle delay
    catches late-loading sub-resources without a hard upper bound.
    """
    from playwright.async_api import async_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    mhtml_path = out_dir / "page.mhtml"
    png_path = out_dir / "page.png"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx_kwargs: dict[str, Any] = dict(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 "
                    "Capsule/0.1"
                ),
                viewport={
                    "width": DEFAULT_VIEWPORT_WIDTH,
                    "height": DEFAULT_VIEWPORT_HEIGHT,
                },
            )
            if proxy_url:
                # Plan §U8: route Playwright traffic through the same tunnel
                # as yt-dlp + browsertrix so the page snapshot, the WARC, and
                # the media all originate from one identity.
                ctx_kwargs["proxy"] = {"server": proxy_url}
            ctx = await browser.new_context(**ctx_kwargs)
            if cookies:
                await ctx.add_cookies(cookies)
            page = await ctx.new_page()
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=timeout_ms,
            )
            if settle_ms > 0:
                await page.wait_for_timeout(settle_ms)
            await _warm_lazy_load(page)
            title = await page.title()
            headers = dict(response.headers) if response else {}

            # CDP captureSnapshot — MHTML.
            cdp = await ctx.new_cdp_session(page)
            snap = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
            mhtml_path.write_text(snap["data"], encoding="utf-8")

            await page.screenshot(path=str(png_path), full_page=True)
            version = browser.version or "0"
            return mhtml_path, png_path, title, headers, version
        finally:
            await browser.close()


# --- browsertrix-crawler WARC -----------------------------------------------


async def _browsertrix_attempt(
    *,
    binary: str,
    url: str,
    out_dir: Path,
    case_cookies_path: Path | None,
    timeout_s: int,
    attempt_idx: int,
    proxy_url: str | None = None,
) -> Path | None:
    """One browsertrix invocation. Returns the merged WARC path or ``None``.

    Each attempt uses a fresh working directory so a previous timeout's
    half-written archive can't contaminate this run.
    """
    crawl_dir = out_dir / f"btx-attempt-{attempt_idx}"
    crawl_dir.mkdir(parents=True, exist_ok=True)

    argv = [
        binary,
        "--url", url,
        "--scopeType", "page",
        "--limit", "1",
        "--cwd", str(crawl_dir),
        "--collection", "capsule",
        "--text", "false",
        "--screenshot", "false",  # we use Playwright for that
        "--timeout", str(timeout_s),
        # autoscroll surfaces lazy-loaded sub-resources (replies, embedded
        # images, related media) so the WARC reflects the same hydrated
        # DOM that the Playwright MHTML/PNG captures. autoplay is left
        # off — yt-dlp is the canonical media producer; we don't want
        # browsertrix re-downloading video bytes.
        "--behaviors", "autoscroll",
        "--behaviorTimeout", str(DEFAULT_BROWSERTRIX_BEHAVIOR_TIMEOUT_S),
    ]
    if case_cookies_path and case_cookies_path.is_file():
        argv += ["--cookieFile", str(case_cookies_path)]
    if proxy_url:
        # Plan §U8: same-identity routing across all three producers.
        argv += ["--proxyServer", proxy_url]

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        return None
    if proc.returncode not in (0, None):
        return None

    warcs = list(crawl_dir.rglob("*.warc.gz"))
    if not warcs:
        return None
    target = out_dir / "page.warc.gz"
    if len(warcs) == 1:
        shutil.move(str(warcs[0]), str(target))
    else:
        # Single-page captures usually produce one file; defensive merge for
        # the rare case browsertrix splits at a size boundary.
        with target.open("wb") as out:
            for w in sorted(warcs):
                out.write(w.read_bytes())
    return target


async def _browsertrix_warc(
    *,
    url: str,
    out_dir: Path,
    case_cookies_path: Path | None,
    timeout_s: int = DEFAULT_BROWSERTRIX_TIMEOUT_S,
    attempts: int = DEFAULT_BROWSERTRIX_ATTEMPTS,
    proxy_url: str | None = None,
) -> tuple[Path | None, str]:
    """Run ``browsertrix-crawler`` for one URL, retrying on hard failure.

    Returns ``(warc_path_or_None, version)``. When browsertrix isn't on PATH
    the function is a no-op — callers see ``None`` and the meta.json
    reflects the absent artifact.

    Plan §U3: outer retry envelope with exponential backoff (1s, 2s, 4s...).
    The browsertrix process does some retrying internally; this catches
    "the whole binary fell over" cases (OOM kill, hard timeout, transient
    DNS).
    """
    binary = shutil.which("browsertrix-crawler")
    if binary is None:
        return None, "0"

    target: Path | None = None
    for i in range(attempts):
        target = await _browsertrix_attempt(
            binary=binary,
            url=url,
            out_dir=out_dir,
            case_cookies_path=case_cookies_path,
            timeout_s=timeout_s,
            attempt_idx=i + 1,
            proxy_url=proxy_url,
        )
        if target is not None:
            break
        if i < attempts - 1:
            await asyncio.sleep(2 ** i)  # 1s, 2s, 4s, ...

    version = "unknown"
    try:
        v_proc = await asyncio.create_subprocess_exec(
            binary, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        v_out, _ = await v_proc.communicate()
        version = v_out.decode().strip() or "unknown"
    except Exception:
        pass

    return target, version


# --- Public entry point -----------------------------------------------------


async def capture_page(
    *,
    url: str,
    case_slug: str | None,
    work_dir: Path | None = None,
    timeout_ms: int = DEFAULT_PLAYWRIGHT_TIMEOUT_MS,
    settle_ms: int = DEFAULT_SETTLE_DELAY_MS,
    browsertrix_timeout_s: int = DEFAULT_BROWSERTRIX_TIMEOUT_S,
    browsertrix_attempts: int = DEFAULT_BROWSERTRIX_ATTEMPTS,
    proxy_url: str | None = None,
) -> CaptureBundle:
    """Drive both producers and return a ``CaptureBundle``.

    ``work_dir`` is where artifacts are staged before postprocess moves them
    into the canonical sidecar dir; pass one in tests, otherwise a
    private tmp dir is created and the caller is responsible for moving
    the files out before it goes out of scope.
    """
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="capsule-capture-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    cookies = _load_cookies_for(case_slug)
    cookies_path = (
        cookies_mod.path_for(case_slug)
        if case_slug and cookies_mod.exists(case_slug) and is_social(url)
        else None
    )

    mhtml, png, title, headers, chromium_v = await _playwright_snapshot(
        url=url,
        out_dir=work_dir,
        cookies=cookies,
        timeout_ms=timeout_ms,
        settle_ms=settle_ms,
        proxy_url=proxy_url,
    )
    warc, btx_v = await _browsertrix_warc(
        url=url,
        out_dir=work_dir,
        case_cookies_path=cookies_path,
        timeout_s=browsertrix_timeout_s,
        attempts=browsertrix_attempts,
        proxy_url=proxy_url,
    )
    return CaptureBundle(
        mhtml=mhtml,
        screenshot=png,
        warc=warc,
        chromium_version=chromium_v,
        browsertrix_version=btx_v,
        page_title=title,
        response_headers=headers,
    )
