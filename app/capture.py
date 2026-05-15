"""Page snapshot capture (CLAUDE.md §2, §6 — Phase 2).

Two producers, one entry point:

* ``Playwright`` (Chromium) writes the MHTML snapshot and the full-page PNG.
  We pass the case cookies into the browser context so the snapshot reflects
  the same authenticated session yt-dlp downloaded the media from.
* ``browsertrix-crawler`` writes a forensic-grade WARC with the same engine,
  scoped to ``page+resources`` (the page itself + every sub-resource it
  loaded — never the whole site). It runs as an external process; if not
  installed, the WARC artifact is omitted and the meta.json reflects that.

Hardening pass (CLAUDE.md §13 — capture-side mutations recorded):

* When the extension supplies a ``TabContext`` (UA, viewport, timezone,
  scroll, color scheme, referrer), the canonical Chromium capture mirrors
  it. Mobile pages render as mobile, dark-mode pages render dark, etc.
* Render-wait policy: ``load`` → ``document.fonts.ready`` → visible-image
  completion → video readyState → ``networkidle`` (best-effort, capped).
  Each wait's outcome is recorded so the audit log can show what was
  actually awaited and what timed out.
* Network-layer ad/tracker blocking via ``app.blocklist`` — every blocked
  URL recorded; toggle off per-case.
* Cookie/consent banner CSS hide via ``app.banner_hide`` — DOM untouched;
  toggle off per-case.

The capture function is producer-agnostic on the consumer side: it returns
``CaptureBundle`` paths, which ``postprocess.finalize`` already accepts.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import animation_freeze as animation_freeze_mod
from . import banner_hide as banner_hide_mod
from . import blocklist as blocklist_mod
from . import cookies as cookies_mod
from .platforms import is_social

__all__ = [
    "CaptureBundle",
    "TabContext",
    "RenderWait",
    "CaptureReport",
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
    "DEFAULT_RENDER_WAIT_BUDGET_MS",
    "DEFAULT_LAZY_LOAD_MAX_STEPS",
    "DEFAULT_SCREENSHOT_MAX_HEIGHT_PX",
]


# Plan §U3 universal defaults. Profiles may override (Slow extends, Fast keeps
# tighter values).
#
# 60s was the prior Playwright timeout — too tight on slow links. 90s gives
# the page time to render through TLS handshake + first paint over a VPN.
DEFAULT_PLAYWRIGHT_TIMEOUT_MS = 90_000
# A short settle period after the render-wait orchestrator finishes, before
# the snapshot. The orchestrator already waits for fonts/images/video; this
# cushion is kept for parity with the prior pipeline (callers that disable
# render waits still get a coherent settle).
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
# Hard ceiling on the render-wait orchestrator. The individual waits each
# have their own caps; this is the outer envelope so a misbehaving page
# can't hold the slot indefinitely.
DEFAULT_RENDER_WAIT_BUDGET_MS = 60_000
# Adaptive lazy-load cap. The orchestrator sizes the step count to the page
# but never exceeds this — endless feeds (X home timeline, Reddit r/all)
# would otherwise scroll forever.
DEFAULT_LAZY_LOAD_MAX_STEPS = 50
# Screenshot height ceiling. Above this, the full-page PNG is clipped and
# the truncation is recorded into meta.json.capture.screenshot_truncated_at_px.
# MHTML and WARC are never clipped.
DEFAULT_SCREENSHOT_MAX_HEIGHT_PX = 30_000


# --- Tab context (extension-supplied environment mirror) -------------------


@dataclass(frozen=True)
class TabContext:
    """User-environment fields captured by the Capsule extension.

    All fields optional — when present, the canonical Chromium capture
    mirrors them so the snapshot reflects the user's authenticated view.
    Missing fields fall back to the engine defaults.
    """

    user_agent: str | None = None
    viewport_width: int | None = None
    viewport_height: int | None = None
    device_scale_factor: float | None = None
    timezone: str | None = None
    locale: str | None = None
    color_scheme: str | None = None  # 'light' | 'dark' | 'no-preference'
    reduced_motion: bool | None = None
    referrer: str | None = None
    scroll_x: int | None = None
    scroll_y: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "TabContext | None":
        """Tolerantly construct from the JSON envelope the extension sends."""
        if not raw:
            return None
        viewport = raw.get("viewport") or {}
        scroll = raw.get("scroll") or {}
        languages = raw.get("languages") or []
        locale = raw.get("language") or (languages[0] if languages else None)
        return cls(
            user_agent=_str_or_none(raw.get("user_agent")),
            viewport_width=_int_or_none(viewport.get("width")),
            viewport_height=_int_or_none(viewport.get("height")),
            device_scale_factor=_float_or_none(viewport.get("device_scale_factor")),
            timezone=_str_or_none(raw.get("timezone")),
            locale=_str_or_none(locale),
            color_scheme=_str_or_none(raw.get("color_scheme")),
            reduced_motion=_bool_or_none(raw.get("reduced_motion")),
            referrer=_str_or_none(raw.get("referrer")),
            scroll_x=_int_or_none(scroll.get("x")),
            scroll_y=_int_or_none(scroll.get("y")),
        )


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bool_or_none(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes")
    return bool(v)


# --- Render-wait reporting --------------------------------------------------


@dataclass(frozen=True)
class RenderWait:
    """One render-wait gate's outcome. Surfaced into ``meta.json.capture``
    and the audit log so a forensic reviewer can answer "what did the
    capture process actually wait for?".
    """

    name: str           # 'load' | 'fonts' | 'images' | 'video' | 'networkidle' | 'lazy_load'
    ok: bool            # True if the gate's condition was satisfied
    elapsed_ms: int     # how long the wait took
    timed_out: bool     # True iff the cap was hit before the condition
    detail: str | None = None  # optional human-readable note (e.g. "8 images, 2 incomplete")


@dataclass(frozen=True)
class CaptureReport:
    """Auditable record of capture-side mutations and wait outcomes."""

    render_waits: list[RenderWait] = field(default_factory=list)
    blocked_requests: list[str] = field(default_factory=list)  # URLs only — no headers
    blocklist_version: str | None = None
    banner_hide_applied: bool = False
    banner_hide_version: str | None = None
    tab_context_used: bool = False
    # Tier A — render fidelity
    lazy_promoted_count: int = 0
    lazy_load_max_height_px: int = 0
    videos_paused: int = 0
    animations_frozen: bool = False
    animations_frozen_version: str | None = None
    shadow_dom_walked: bool = False
    iframes_seen: int = 0
    screenshot_truncated_at_px: int | None = None
    readiness_timed_out: bool = False
    # Tier B — forensic instrumentation
    console_message_count: int = 0
    console_error_count: int = 0
    response: dict[str, Any] | None = None
    # Tier C — media context
    media_context_captured: bool = False
    media_context_selector: str | None = None
    # Tier D — WARC session provenance
    warc_captured_in_session: bool = False
    warc_record_count: int = 0
    warc_encoding_normalized: bool = False
    warc_format_version: str | None = None
    # CLAUDE.md §16 v0.11 bucket 2 #1/#3/#4: forensic failure markers.
    # Each is the exception class name (e.g. "TimeoutError") of the
    # silently-swallowed step, or None when the step succeeded. The
    # orchestrator emits a distinct audit row when any of these is set
    # so a recipient can see WHY a capture-side mutation didn't fire.
    warc_in_session_error: str | None = None
    banner_hide_error: str | None = None
    console_sidecar_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "render_waits": [
                {
                    "name": w.name,
                    "ok": w.ok,
                    "elapsed_ms": w.elapsed_ms,
                    "timed_out": w.timed_out,
                    "detail": w.detail,
                }
                for w in self.render_waits
            ],
            "blocked_request_count": len(self.blocked_requests),
            # Cap the URL list at a reasonable size so meta.json stays readable
            # on pages with thousands of trackers. The full set still appears
            # in the WARC (each is a recorded aborted request), so forensic
            # completeness isn't compromised.
            "blocked_requests_sample": list(self.blocked_requests[:200]),
            "blocklist_version": self.blocklist_version,
            "banner_hide_applied": self.banner_hide_applied,
            "banner_hide_version": self.banner_hide_version,
            "tab_context_used": self.tab_context_used,
            "lazy_promoted_count": self.lazy_promoted_count,
            "lazy_load_max_height_px": self.lazy_load_max_height_px,
            "videos_paused": self.videos_paused,
            "animations_frozen": self.animations_frozen,
            "animations_frozen_version": self.animations_frozen_version,
            "shadow_dom_walked": self.shadow_dom_walked,
            "iframes_seen": self.iframes_seen,
            "screenshot_truncated_at_px": self.screenshot_truncated_at_px,
            "readiness_timed_out": self.readiness_timed_out,
            "console_message_count": self.console_message_count,
            "console_error_count": self.console_error_count,
            "response": self.response,
            "media_context_captured": self.media_context_captured,
            "media_context_selector": self.media_context_selector,
            "warc": {
                "captured_in_session": self.warc_captured_in_session,
                "record_count": self.warc_record_count,
                "encoding_normalized": self.warc_encoding_normalized,
                "format_version": self.warc_format_version,
                "in_session_error": self.warc_in_session_error,
            },
            "banner_hide_error": self.banner_hide_error,
            "console_sidecar_error": self.console_sidecar_error,
        }
        return out


@dataclass(frozen=True)
class CaptureBundle:
    mhtml: Path | None
    screenshot: Path | None
    warc: Path | None
    chromium_version: str
    browsertrix_version: str
    page_title: str | None
    response_headers: dict[str, str] | None
    report: CaptureReport = field(default_factory=CaptureReport)
    # Tier B + C artifacts. None when the corresponding capture step was
    # disabled or didn't apply (e.g. context_screenshot is None when no
    # media-ish element was found on the page).
    har: Path | None = None
    console_log: Path | None = None
    context_screenshot: Path | None = None
    warcio_version: str | None = None


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


def _load_cookies_from_path(cookies_path: Path | None) -> list[dict[str, Any]]:
    if cookies_path is None or not cookies_path.is_file():
        return []
    return _netscape_to_playwright_cookies(cookies_path.read_text(encoding="utf-8"))


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


async def _wait_with_cap(
    coro_factory,
    *,
    name: str,
    cap_ms: int,
) -> RenderWait:
    """Run ``coro_factory()`` (a zero-arg async callable) with a hard cap.

    Returns a :class:`RenderWait` describing the outcome. The async work
    is wrapped in :func:`asyncio.wait_for`; on timeout the gate is recorded
    as ``timed_out=True`` and the capture continues — best-effort waits do
    not abort the capture.
    """
    import time
    started = time.monotonic()
    try:
        detail = await asyncio.wait_for(coro_factory(), timeout=cap_ms / 1000.0)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return RenderWait(name=name, ok=True, elapsed_ms=elapsed_ms, timed_out=False, detail=detail)
    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return RenderWait(name=name, ok=False, elapsed_ms=elapsed_ms, timed_out=True, detail=None)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return RenderWait(
            name=name, ok=False, elapsed_ms=elapsed_ms, timed_out=False,
            detail=f"error:{type(exc).__name__}",
        )


async def _wait_for_fonts(page) -> str | None:
    """Resolve when ``document.fonts.ready`` resolves, or never if no
    document.fonts. Returns a brief detail string with the font count."""
    return await page.evaluate(
        """async () => {
            if (!document.fonts) return 'no-fonts-api';
            await document.fonts.ready;
            return 'fonts:' + document.fonts.size;
        }"""
    )


_VISIBLE_IMAGES_JS = """
async ({ traverseShadow, traverseFrames, timeoutMs }) => {
    const collectImages = (root) => {
        const out = [];
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
        let node = walker.currentNode;
        while (node) {
            if (node.tagName === 'IMG') out.push(node);
            if (traverseShadow && node.shadowRoot) {
                out.push(...collectImages(node.shadowRoot));
            }
            node = walker.nextNode();
        }
        return out;
    };
    const collectFrames = (root) => {
        const out = [];
        if (!traverseFrames) return out;
        const frames = root.querySelectorAll('iframe, frame');
        for (const f of frames) {
            try {
                if (f.contentDocument) out.push(f.contentDocument);
            } catch (_) { /* cross-origin, skip silently */ }
        }
        return out;
    };
    const allImages = () => {
        const set = new Set(collectImages(document));
        for (const fdoc of collectFrames(document)) {
            for (const img of collectImages(fdoc)) set.add(img);
        }
        return Array.from(set);
    };
    const start = performance.now();
    let lastCount = -1;
    let stableSince = 0;
    while (performance.now() - start < timeoutMs) {
        const imgs = allImages();
        if (imgs.length === 0) return 'no-images';
        let pending = 0;
        for (const img of imgs) {
            if (!img.complete || img.naturalHeight === 0) pending++;
        }
        if (pending === 0) return 'images:' + imgs.length + ' complete';
        if (imgs.length === lastCount) {
            // Set has stabilized; stop polling sooner once we've hit a
            // plateau for ~1.5s, so blocked CDNs don't wedge the gate.
            if (stableSince === 0) stableSince = performance.now();
            if (performance.now() - stableSince > 1500) {
                return 'images:plateau:' + imgs.length + '/' + pending + '-pending';
            }
        } else {
            stableSince = 0;
            lastCount = imgs.length;
        }
        await new Promise(r => setTimeout(r, 250));
    }
    return 'images:timeout';
}
"""


async def _wait_for_visible_images(
    page,
    *,
    timeout_ms: int = 8_000,
    traverse_shadow: bool = True,
    traverse_frames: bool = True,
) -> str | None:
    """Resolve when every ``<img>`` in the document (including shadow roots
    and same-origin frames) is ``complete`` with a non-zero ``naturalHeight``.

    Cross-origin iframe contents stay opaque from the page-script side, but
    their resources are still recorded by the in-session WARC writer — the
    forensic record is complete even when this gate can't introspect them.

    Tolerant: stops trying after ``timeout_ms`` or after the image count
    plateaus, so broken/blocked images never wedge the capture.
    """
    return await page.evaluate(
        _VISIBLE_IMAGES_JS,
        {
            "traverseShadow": traverse_shadow,
            "traverseFrames": traverse_frames,
            "timeoutMs": timeout_ms,
        },
    )


async def _wait_for_video_ready(page) -> str | None:
    """Resolve when every ``<video>`` has ``readyState >= 2`` (the first
    frame is decoded), pause autoplay videos, and wait for any explicit
    ``poster`` image to load.

    Reasoning: ``readyState >= 1`` (HAVE_METADATA) only guarantees we know
    the video's dimensions and duration — not that the poster or first
    frame is painted. ``HAVE_CURRENT_DATA`` is the threshold at which the
    still PNG actually shows a frame rather than a blank box. Autoplaying
    videos are paused so the screenshot captures a stable frame.

    The number of paused videos is returned as part of the detail string
    and also stored on the page object so the orchestrator can pick it up.
    """
    return await page.evaluate(
        """async () => {
            const start = performance.now();
            const vids = Array.from(document.querySelectorAll('video'));
            if (vids.length === 0) return 'no-video';
            // Pause autoplay first so subsequent readyState checks don't
            // race against a self-advancing playhead.
            let paused = 0;
            for (const v of vids) {
                try {
                    if (!v.paused) { v.pause(); paused++; }
                } catch (_) { /* permission/CSP — ignore */ }
            }
            // Best-effort poster preload via a hidden Image so the next
            // gate (visible-images) catches it too.
            for (const v of vids) {
                if (v.poster) { try { new Image().src = v.poster; } catch (_) {} }
            }
            window.__capsule_videos_paused = paused;
            while (performance.now() - start < 8000) {
                const pending = vids.filter(v => v.readyState < 2).length;
                if (pending === 0) {
                    return 'video:' + vids.length + ' ready paused:' + paused;
                }
                await new Promise(r => setTimeout(r, 250));
            }
            return 'video:timeout paused:' + paused;
        }"""
    )


async def _read_videos_paused(page) -> int:
    """Read back the autoplay-pause counter the video gate stored on
    ``window.__capsule_videos_paused``. Returns 0 if the gate didn't run
    (e.g. timed out before assignment) or the page is gone."""
    try:
        n = await page.evaluate("window.__capsule_videos_paused || 0")
        return int(n)
    except Exception:
        return 0


async def _promote_lazy_attrs(page) -> int:
    """Flip ``loading="lazy"`` to ``loading="eager"`` on every ``<img>`` /
    ``<iframe>`` so the lazy-load scroll surfaces them on the wire even
    when the IntersectionObserver heuristics don't fire (e.g. images
    several viewports below the fold).

    Recorded as ``capture.lazy_promoted_count`` — pure attribute flip; the
    underlying ``src`` URLs are unchanged, so the WARC and HAR record the
    same network requests they would have under any user's browser that
    eventually scrolled past the image.
    """
    return int(await page.evaluate(
        """() => {
            let n = 0;
            for (const sel of ['img', 'iframe']) {
                for (const el of document.querySelectorAll(sel + '[loading="lazy"]')) {
                    el.setAttribute('loading', 'eager');
                    n++;
                }
            }
            return n;
        }"""
    ))


async def _warm_lazy_load(
    page,
    *,
    max_steps: int = DEFAULT_LAZY_LOAD_MAX_STEPS,
    step_ms: int = DEFAULT_WARMUP_STEP_MS,
) -> tuple[int, int]:
    """Scroll the page in viewport-sized steps to surface lazy-loaded content.

    Sizes the step count to ``min(max_steps, ceil(scrollHeight/innerHeight) + 4)``
    so a tall page gets enough steps to reach the bottom, but an endless
    feed is still capped. Returns ``(steps_taken, final_scroll_height_px)``
    so the caller can record both into the capture report.

    The cap exists because endless threads (e.g. an X tweet with thousands
    of replies) would otherwise produce a screenshot tens of thousands of
    pixels tall. Runs before the MHTML snapshot so MHTML and PNG see the
    same hydrated DOM.
    """
    # Estimate steps from the initial document height. We'll re-measure as
    # the page hydrates, but this gives the loop a sensible upper bound.
    sizing = await page.evaluate(
        """() => ({
            viewport: window.innerHeight || 800,
            scroll: document.documentElement.scrollHeight,
        })"""
    )
    viewport_h = max(int(sizing.get("viewport") or 800), 1)
    initial_h = max(int(sizing.get("scroll") or viewport_h), viewport_h)
    estimated_steps = (initial_h // viewport_h) + 4
    bounded_steps = max(1, min(max_steps, estimated_steps))

    last_height = 0
    steps_taken = 0
    final_height = initial_h
    for _ in range(bounded_steps):
        info = await page.evaluate(
            """() => ({
                height: document.documentElement.scrollHeight,
                viewport: window.innerHeight,
            })"""
        )
        height = int(info.get("height") or 0)
        if height <= last_height:
            break
        last_height = height
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.9)")
        await page.wait_for_timeout(step_ms)
        steps_taken += 1
        final_height = height
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(step_ms)
    return steps_taken, final_height


# --- Response-block, HAR redaction, media-element selection ---------------


_SENSITIVE_HEADER_SUBSTRINGS = ("cookie", "authorization", "proxy-authorization")
_VIDEO_HOST_HINTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "vimeo.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "soundcloud.com",
    "bilibili.com",
    "twitch.tv",
    "dailymotion.com",
)


def _sanitize_response_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Drop any header whose lowered name contains a sensitive substring.

    Mirrors :mod:`app.audit`'s cookie-bearing key guard — the lower-case
    substring match catches ``Cookie``, ``Set-Cookie``, ``set-cookie``,
    ``Cookie2``, ``Authorization``, ``Proxy-Authorization``, etc. We never
    emit real cookie or authorization values into ``meta.json``.
    """
    if not headers:
        return {}
    out: dict[str, str] = {}
    for name, value in headers.items():
        n = (name or "").lower()
        if any(s in n for s in _SENSITIVE_HEADER_SUBSTRINGS):
            continue
        out[name] = value
    return out


async def _capture_response_block(response, *, elapsed_ms: int) -> dict[str, Any]:
    """Build the ``meta.json.capture.response`` block from Playwright's
    final navigation response — final URL, status, sanitized headers, plus
    the redirect chain (each hop's URL/status/Location header).

    Sensitive headers are stripped via :func:`_sanitize_response_headers`
    before this dict goes anywhere near ``meta.json`` or the audit log.
    """
    if response is None:
        return {
            "final_status": None,
            "final_url": None,
            "redirect_chain": [],
            "headers": {},
            "elapsed_ms": elapsed_ms,
        }
    chain: list[dict[str, Any]] = []
    try:
        req = response.request
        # Walk the redirect chain — Playwright exposes redirected_from on the
        # request object. Each hop is its own request → response.
        hop = req.redirected_from
        while hop is not None:
            try:
                hop_resp = await hop.response()
            except Exception:
                hop_resp = None
            if hop_resp is not None:
                hop_headers = _sanitize_response_headers(dict(hop_resp.headers or {}))
                chain.append({
                    "url": hop.url,
                    "status": hop_resp.status,
                    "location": hop_headers.get("location") or hop_headers.get("Location"),
                })
            hop = hop.redirected_from
    except Exception:
        pass
    chain.reverse()
    return {
        "final_status": response.status,
        "final_url": response.url,
        "redirect_chain": chain,
        "headers": _sanitize_response_headers(dict(response.headers or {})),
        "elapsed_ms": elapsed_ms,
    }


def _redact_har_in_place(har_path: Path) -> None:
    """Strip Set-Cookie/Cookie/Authorization headers and ``cookies[]``
    arrays from the HAR file Playwright writes.

    Playwright's HAR includes both per-request cookie arrays AND the
    Cookie/Set-Cookie request/response headers — every channel through
    which a value could leak gets redacted before the file is hashed and
    signed via ``meta.json.sig``.
    """
    if not har_path.is_file():
        return
    try:
        data = json.loads(har_path.read_text(encoding="utf-8"))
    except Exception:
        return
    log = data.get("log") or {}
    entries = log.get("entries") or []
    redacted = 0
    for entry in entries:
        for side in ("request", "response"):
            block = entry.get(side) or {}
            block["cookies"] = []
            headers = block.get("headers") or []
            block["headers"] = [
                h for h in headers
                if not any(s in (h.get("name") or "").lower() for s in _SENSITIVE_HEADER_SUBSTRINGS)
            ]
            redacted += len(headers) - len(block["headers"])
    log["_capsule_redacted_header_count"] = redacted
    data["log"] = log
    har_path.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


async def _find_media_element(page) -> str | None:
    """Pick the most prominent media-ish element on the page.

    Strategy (in order):
        1. The largest visible ``<video>`` with a ``src`` (or matching
           any source child).
        2. The largest visible ``<iframe>`` whose ``src`` host matches a
           known video-host hint (YouTube, Vimeo, Twitter/X, TikTok, etc.).
        3. The largest visible ``<video>`` regardless of ``src``.

    Returns a CSS selector string that uniquely identifies the chosen
    element (used for the second screenshot AND for the audit-log entry,
    so a forensic reviewer can answer "what element was the context
    screenshot framed on?"). Returns ``None`` when nothing qualifies —
    callers skip the context screenshot in that case.
    """
    selector = await page.evaluate(
        """({hosts}) => {
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                if (r.width < 64 || r.height < 64) return false;
                const style = getComputedStyle(el);
                if (style.visibility === 'hidden' || style.display === 'none') return false;
                if (parseFloat(style.opacity || '1') < 0.05) return false;
                return true;
            };
            const area = (el) => {
                const r = el.getBoundingClientRect();
                return Math.max(0, r.width) * Math.max(0, r.height);
            };
            const cssEscape = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/[^a-zA-Z0-9_-]/g, '_');
            const selectorFor = (el) => {
                if (el.id) return el.tagName.toLowerCase() + '#' + cssEscape(el.id);
                // Prefer src for stability and forensic readability.
                const src = el.getAttribute('src') || '';
                if (src) return el.tagName.toLowerCase() + '[src="' + src.replace(/"/g, '\\\\"') + '"]';
                // Fall back to nth-of-type rooted at body.
                let path = el.tagName.toLowerCase();
                let parent = el.parentElement;
                while (parent && parent !== document.body) {
                    const idx = Array.from(parent.children).indexOf(el) + 1;
                    path = parent.tagName.toLowerCase() + ' > ' + path + ':nth-child(' + idx + ')';
                    el = parent;
                    parent = parent.parentElement;
                }
                return 'body ' + path;
            };
            const isVideoIframe = (el) => {
                const src = el.getAttribute('src') || '';
                if (!src) return false;
                try {
                    const u = new URL(src, document.location.href);
                    return hosts.some(h => u.hostname === h || u.hostname.endsWith('.' + h));
                } catch (_) { return false; }
            };

            // Tier 1: <video> with src
            const tier1 = Array.from(document.querySelectorAll('video'))
                .filter(v => visible(v) && (v.currentSrc || v.src || v.querySelector('source[src]')));
            if (tier1.length) {
                tier1.sort((a, b) => area(b) - area(a));
                return selectorFor(tier1[0]);
            }
            // Tier 2: video-host iframes
            const tier2 = Array.from(document.querySelectorAll('iframe'))
                .filter(f => visible(f) && isVideoIframe(f));
            if (tier2.length) {
                tier2.sort((a, b) => area(b) - area(a));
                return selectorFor(tier2[0]);
            }
            // Tier 3: any visible <video>
            const tier3 = Array.from(document.querySelectorAll('video')).filter(visible);
            if (tier3.length) {
                tier3.sort((a, b) => area(b) - area(a));
                return selectorFor(tier3[0]);
            }
            return null;
        }""",
        {"hosts": list(_VIDEO_HOST_HINTS)},
    )
    return selector or None


def _route_handler_factory(rules: blocklist_mod.BlocklistRules, log: list[str]):
    """Build a Playwright route handler that aborts blocked URLs."""

    async def _handler(route, request):
        url = request.url
        if rules.should_block(url):
            log.append(url)
            try:
                await route.abort("blockedbyclient")
            except Exception:
                pass
            return
        try:
            await route.continue_()
        except Exception:
            pass

    return _handler


async def _playwright_snapshot(
    *,
    url: str,
    out_dir: Path,
    cookies: list[dict[str, Any]],
    timeout_ms: int = DEFAULT_PLAYWRIGHT_TIMEOUT_MS,
    settle_ms: int = DEFAULT_SETTLE_DELAY_MS,
    proxy_url: str | None = None,
    tab_context: TabContext | None = None,
    block_ads: bool = True,
    hide_cookie_banners: bool = True,
    write_warc: bool = True,
    app_version: str = "0",
) -> dict[str, Any]:
    """Open ``url`` once and produce MHTML, screenshot, optional WARC, HAR,
    console log, media-context screenshot, and a :class:`CaptureReport`.

    Returns a dict with keys: ``mhtml``, ``screenshot``, ``warc``,
    ``har``, ``console_log``, ``context_screenshot``, ``title``, ``headers``,
    ``chromium_version``, ``warcio_version``, ``report``.

    Wait policy: ``load`` → fonts → visible images (shadow DOM + same-origin
    iframes) → video readyState ≥ 2 (poster decoded, autoplay paused) →
    lazy-load (adaptive scroll, ``loading="lazy"`` promoted to eager) →
    images-after-scroll re-settle → networkidle (capped). Outer budget
    ``DEFAULT_RENDER_WAIT_BUDGET_MS`` enforced — once exceeded, remaining
    gates are skipped and ``readiness_timed_out`` is recorded.

    When ``write_warc`` is True the network log for THIS Playwright session
    is teed into a WARC/1.1 file via :class:`app.warc_writer.CdpWarcWriter`,
    so MHTML, PNG, and WARC come from a single navigation. The browsertrix
    subprocess fallback in :func:`_browsertrix_warc` handles cases where
    in-session WARC writing fails (no warcio, CDP error, etc.).
    """
    import time as _time
    from playwright.async_api import async_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    mhtml_path = out_dir / "page.mhtml"
    png_path = out_dir / "page.png"
    warc_path = out_dir / "page.warc.gz"
    har_path = out_dir / "page.har"
    console_path = out_dir / "page.console.json"
    context_png_path = out_dir / "page.context.png"

    blocked_log: list[str] = []
    waits: list[RenderWait] = []
    console_events: list[dict[str, Any]] = []
    error_count = 0
    blocklist_version: str | None = None
    banner_version: str | None = None
    animations_frozen = False
    animations_frozen_version: str | None = None
    lazy_promoted_count = 0
    lazy_load_max_height_px = 0
    videos_paused = 0
    shadow_dom_walked = True  # _wait_for_visible_images traverses by default
    iframes_seen = 0
    screenshot_truncated_at_px: int | None = None
    readiness_timed_out = False
    response_block: dict[str, Any] | None = None
    media_context_selector: str | None = None
    media_context_captured = False
    warc_record_count = 0
    warc_encoding_normalized = False
    warc_format_version: str | None = None
    warc_in_session = False
    warcio_v: str | None = None
    # CLAUDE.md §16 v0.11 bucket 2 #1/#3/#4: capture the exception class
    # name of any silently-swallowed step so the orchestrator can emit a
    # distinct audit row. None means the step either succeeded or did not
    # run.
    warc_in_session_error: str | None = None
    banner_hide_error: str | None = None
    console_sidecar_error: str | None = None

    render_wait_started = _time.monotonic()

    def _budget_remaining_ms() -> int:
        used = (_time.monotonic() - render_wait_started) * 1000
        return max(0, DEFAULT_RENDER_WAIT_BUDGET_MS - int(used))

    async def _gated(coro_factory, *, name: str, cap_ms: int) -> RenderWait:
        nonlocal readiness_timed_out
        remaining = _budget_remaining_ms()
        if remaining <= 0:
            readiness_timed_out = True
            return RenderWait(
                name=name, ok=False, elapsed_ms=0, timed_out=True,
                detail="budget-exceeded",
            )
        return await _wait_with_cap(coro_factory, name=name, cap_ms=min(cap_ms, remaining))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx_kwargs: dict[str, Any] = dict(
                user_agent=(
                    tab_context.user_agent
                    if tab_context and tab_context.user_agent
                    else (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 "
                        "Capsule/0.1"
                    )
                ),
                viewport={
                    "width": (
                        tab_context.viewport_width
                        if tab_context and tab_context.viewport_width
                        else DEFAULT_VIEWPORT_WIDTH
                    ),
                    "height": (
                        tab_context.viewport_height
                        if tab_context and tab_context.viewport_height
                        else DEFAULT_VIEWPORT_HEIGHT
                    ),
                },
                # Tier B — write a HAR for the whole session. Sensitive
                # headers and cookies[] arrays are stripped via
                # _redact_har_in_place after the context closes.
                record_har_path=str(har_path),
                record_har_omit_content=False,
            )
            if tab_context and tab_context.device_scale_factor:
                ctx_kwargs["device_scale_factor"] = float(tab_context.device_scale_factor)
            if tab_context and tab_context.timezone:
                ctx_kwargs["timezone_id"] = tab_context.timezone
            if tab_context and tab_context.locale:
                ctx_kwargs["locale"] = tab_context.locale
            if tab_context and tab_context.color_scheme in ("light", "dark", "no-preference"):
                ctx_kwargs["color_scheme"] = tab_context.color_scheme
            if tab_context and tab_context.reduced_motion is not None:
                ctx_kwargs["reduced_motion"] = "reduce" if tab_context.reduced_motion else "no-preference"
            if proxy_url:
                # Plan §U8: route Playwright traffic through the same tunnel
                # as yt-dlp so the page snapshot, the WARC, and the media all
                # originate from one identity.
                ctx_kwargs["proxy"] = {"server": proxy_url}
            ctx = await browser.new_context(**ctx_kwargs)
            if cookies:
                await ctx.add_cookies(cookies)

            # Network-layer ad/tracker blocking. Routes are installed at the
            # context level so they apply to every request the page makes,
            # including sub-frames. Blocked URLs are also dropped from the
            # WARC writer (the request never reaches loadingFinished).
            if block_ads:
                rules = blocklist_mod.default_rules()
                blocklist_version = rules.version
                await ctx.route("**/*", _route_handler_factory(rules, blocked_log))

            page = await ctx.new_page()

            # Tier B — browser console + page-error listeners. Captured
            # for the full lifetime of the page; written to a JSON sidecar
            # before the context closes.
            def _on_console(msg) -> None:
                try:
                    loc = msg.location or {}
                    console_events.append({
                        "type": msg.type,
                        "text": msg.text,
                        "url": loc.get("url") if isinstance(loc, dict) else getattr(loc, "url", None),
                        "line": loc.get("lineNumber") if isinstance(loc, dict) else getattr(loc, "lineNumber", None),
                        "ts": _time.time(),
                    })
                except Exception:
                    pass

            def _on_pageerror(exc) -> None:
                nonlocal error_count
                error_count += 1
                try:
                    console_events.append({
                        "type": "pageerror",
                        "text": str(exc),
                        "ts": _time.time(),
                    })
                except Exception:
                    pass

            page.on("console", _on_console)
            page.on("pageerror", _on_pageerror)

            # Tier D — open the in-session WARC writer BEFORE goto so every
            # request including the navigation itself is recorded. CDP
            # session is shared with the MHTML capture below.
            cdp = await ctx.new_cdp_session(page)
            warc_writer = None
            if write_warc:
                try:
                    from .warc_writer import CdpWarcWriter, warcio_version as _wv
                    warcio_v = _wv()
                    if warcio_v is not None:
                        warc_writer = CdpWarcWriter(
                            cdp,
                            warc_path,
                            app_version=app_version,
                            chromium_version=browser.version or "0",
                            target_uri=url,
                        )
                        await warc_writer.__aenter__()
                except Exception as exc:
                    warc_in_session_error = type(exc).__name__
                    warc_writer = None  # falls back to browsertrix subprocess

            goto_kwargs: dict[str, Any] = {
                "wait_until": "load",
                "timeout": timeout_ms,
            }
            if tab_context and tab_context.referrer:
                goto_kwargs["referer"] = tab_context.referrer
            goto_started = _time.monotonic()
            response = await page.goto(url, **goto_kwargs)
            goto_elapsed_ms = int((_time.monotonic() - goto_started) * 1000)
            waits.append(RenderWait(
                name="load", ok=True, elapsed_ms=goto_elapsed_ms, timed_out=False,
                detail=f"status:{response.status if response else 'no-response'}",
            ))

            # Tier B — capture the navigation response block (status,
            # redirect chain, sanitized headers) for meta.json.
            try:
                response_block = await _capture_response_block(response, elapsed_ms=goto_elapsed_ms)
            except Exception:
                response_block = None

            # Banner CSS hide injected as early as possible after navigation
            # so the page never paints a banner that we'd then hide.
            banner_hide_applied = False
            if hide_cookie_banners:
                rules_b = banner_hide_mod.default_rules()
                banner_version = rules_b.version
                try:
                    await page.add_style_tag(content=rules_b.css)
                    banner_hide_applied = True
                except Exception as exc:
                    banner_hide_applied = False
                    banner_hide_error = type(exc).__name__

            # Tier A — promote loading="lazy" → eager so subsequent
            # scroll surfaces every below-the-fold image on the wire.
            try:
                lazy_promoted_count = await _promote_lazy_attrs(page)
            except Exception:
                lazy_promoted_count = 0

            # Render-wait orchestration with budget enforcement.
            waits.append(await _gated(
                lambda: _wait_for_fonts(page),
                name="fonts", cap_ms=8_000,
            ))
            waits.append(await _gated(
                lambda: _wait_for_visible_images(page, timeout_ms=10_000),
                name="images", cap_ms=10_000,
            ))
            waits.append(await _gated(
                lambda: _wait_for_video_ready(page),
                name="video", cap_ms=8_000,
            ))
            videos_paused = await _read_videos_paused(page)

            async def _lazy_then_resettle():
                steps, final_h = await _warm_lazy_load(page)
                # Re-check images now that the page has been scrolled —
                # lazy-load surfaces new <img> elements that the first
                # `images` gate couldn't have seen.
                detail2 = await _wait_for_visible_images(page, timeout_ms=6_000)
                # Frame count is read here (after lazy-load) so multi-frame
                # widgets that hydrate late are still counted.
                try:
                    nf = int(await page.evaluate(
                        "document.querySelectorAll('iframe, frame').length"
                    ))
                except Exception:
                    nf = 0
                return f"lazy:steps={steps} h={final_h}px frames={nf} after_scroll:{detail2}"

            lazy_wait = await _gated(_lazy_then_resettle, name="lazy_load", cap_ms=20_000)
            waits.append(lazy_wait)
            # Best-effort parse of the lazy-load detail to extract numbers
            # for the structured capture report.
            if lazy_wait.detail:
                for token in lazy_wait.detail.split():
                    if token.startswith("h=") and token.endswith("px"):
                        try:
                            lazy_load_max_height_px = int(token[2:-2])
                        except ValueError:
                            pass
                    elif token.startswith("frames="):
                        try:
                            iframes_seen = int(token.split("=", 1)[1])
                        except ValueError:
                            pass

            async def _networkidle():
                await page.wait_for_load_state("networkidle", timeout=15_000)
                return "networkidle:reached"

            waits.append(await _gated(_networkidle, name="networkidle", cap_ms=15_000))

            if settle_ms > 0:
                await page.wait_for_timeout(settle_ms)

            # Restore the user's scroll position (extension-supplied) so the
            # PNG reflects the investigator's vantage; full-page mode below
            # captures everything regardless. The default is top-of-page.
            if tab_context and tab_context.scroll_y is not None:
                await page.evaluate(
                    "([x, y]) => window.scrollTo(x, y)",
                    [tab_context.scroll_x or 0, tab_context.scroll_y or 0],
                )
            else:
                await page.evaluate("window.scrollTo(0, 0)")

            title = await page.title()
            headers = dict(response.headers) if response else {}

            # CDP captureSnapshot — MHTML. Captured BEFORE animation freeze
            # so the archive retains the page's source-of-record CSS.
            snap = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
            mhtml_path.write_text(snap["data"], encoding="utf-8")

            # Tier A — freeze animations/transitions and pause autoplay
            # videos for the still PNG. Removed immediately afterwards.
            freeze_rules = animation_freeze_mod.default_rules()
            freeze_handle = None
            try:
                freeze_handle = await page.add_style_tag(content=freeze_rules.css)
                animations_frozen = True
                animations_frozen_version = freeze_rules.version
            except Exception:
                animations_frozen = False

            # Tier A — long-page screenshot cap. The MHTML and WARC are
            # never clipped; only the PNG.
            try:
                page_h = int(await page.evaluate("document.documentElement.scrollHeight"))
            except Exception:
                page_h = 0
            shot_kwargs: dict[str, Any] = {"path": str(png_path), "full_page": True}
            if page_h and page_h > DEFAULT_SCREENSHOT_MAX_HEIGHT_PX:
                vp_w = ctx_kwargs["viewport"]["width"]
                shot_kwargs = {
                    "path": str(png_path),
                    "full_page": False,
                    "clip": {"x": 0, "y": 0, "width": vp_w, "height": DEFAULT_SCREENSHOT_MAX_HEIGHT_PX},
                }
                screenshot_truncated_at_px = DEFAULT_SCREENSHOT_MAX_HEIGHT_PX
            await page.screenshot(**shot_kwargs)

            # Tier C — media-context viewport screenshot, scrolled to the
            # most prominent <video>/<iframe> on the page.
            try:
                media_context_selector = await _find_media_element(page)
                if media_context_selector:
                    handle = await page.query_selector(media_context_selector)
                    if handle is not None:
                        try:
                            await handle.scroll_into_view_if_needed(timeout=3_000)
                            # Center the element vertically — scroll_into_view
                            # only guarantees visibility.
                            await page.evaluate(
                                "(el) => el && el.scrollIntoView({block: 'center', inline: 'center'})",
                                handle,
                            )
                            await page.wait_for_timeout(250)
                            await page.screenshot(path=str(context_png_path), full_page=False)
                            media_context_captured = True
                        finally:
                            with __import__("contextlib").suppress(Exception):
                                await handle.dispose()
            except Exception:
                media_context_captured = False

            # Remove the freeze stylesheet so any post-screenshot DOM work
            # (CDP shutdown, etc.) doesn't see suspended animations.
            if freeze_handle is not None:
                try:
                    await freeze_handle.evaluate("(el) => el.remove()")
                except Exception:
                    pass

            chromium_v = browser.version or "0"

            # Close WARC writer BEFORE the browser context exits so all
            # CDP events have a chance to fire while the network domain is
            # still attached.
            if warc_writer is not None:
                try:
                    await warc_writer.__aexit__(None, None, None)
                    res = warc_writer.result
                    warc_record_count = res.record_count
                    warc_encoding_normalized = res.encoding_normalized
                    warc_format_version = res.format_version
                    warc_in_session = res.record_count > 0
                except Exception as exc:
                    warc_in_session = False
                    # Only set if not already populated by the setup path —
                    # the original exception is more informative than the
                    # __aexit__ failure that followed it.
                    if warc_in_session_error is None:
                        warc_in_session_error = type(exc).__name__
        finally:
            await browser.close()

    # Write console-events sidecar AFTER the browser closes so any
    # late-firing console messages have made it through.
    try:
        console_path.write_text(
            json.dumps({
                "captured_at": _time_iso_now(),
                "events": console_events,
            }, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception as exc:
        console_sidecar_error = type(exc).__name__
        console_path = None  # type: ignore[assignment]

    # Sanitize the HAR Playwright wrote — strip Cookie/Set-Cookie/Authorization
    # headers and cookies[] arrays before the file is ever hashed/signed.
    try:
        _redact_har_in_place(har_path)
    except Exception:
        pass

    final_warc: Path | None = warc_path if (write_warc and warc_in_session and warc_path.is_file() and warc_path.stat().st_size > 0) else None
    if write_warc and not warc_in_session:
        # Clean up the empty/stub file so postprocess.finalize doesn't try
        # to hash a zero-record archive.
        with __import__("contextlib").suppress(Exception):
            warc_path.unlink()

    # CLAUDE.md §16 v0.11 bucket 2 #4: if the console sidecar write
    # failed, the message count + error count in meta.json must NOT
    # claim values for an absent sidecar — that's a forensic inconsistency
    # a recipient can't reconcile. Zero them out so the absence of the
    # file matches the zero count.
    if console_sidecar_error is not None:
        console_message_count_out = 0
        console_error_count_out = 0
    else:
        console_message_count_out = len(console_events)
        console_error_count_out = error_count

    report = CaptureReport(
        render_waits=waits,
        blocked_requests=blocked_log,
        blocklist_version=blocklist_version,
        banner_hide_applied=banner_hide_applied,
        banner_hide_version=banner_version,
        tab_context_used=tab_context is not None,
        lazy_promoted_count=lazy_promoted_count,
        lazy_load_max_height_px=lazy_load_max_height_px,
        videos_paused=videos_paused,
        animations_frozen=animations_frozen,
        animations_frozen_version=animations_frozen_version,
        shadow_dom_walked=shadow_dom_walked,
        iframes_seen=iframes_seen,
        screenshot_truncated_at_px=screenshot_truncated_at_px,
        readiness_timed_out=readiness_timed_out,
        console_message_count=console_message_count_out,
        console_error_count=console_error_count_out,
        response=response_block,
        media_context_captured=media_context_captured,
        media_context_selector=media_context_selector,
        warc_captured_in_session=warc_in_session,
        warc_record_count=warc_record_count,
        warc_encoding_normalized=warc_encoding_normalized,
        warc_format_version=warc_format_version,
        warc_in_session_error=warc_in_session_error,
        banner_hide_error=banner_hide_error,
        console_sidecar_error=console_sidecar_error,
    )
    return {
        "mhtml": mhtml_path,
        "screenshot": png_path,
        "warc": final_warc,
        "har": har_path if har_path.is_file() else None,
        "console_log": console_path if (console_path and console_path.is_file()) else None,
        "context_screenshot": context_png_path if (media_context_captured and context_png_path.is_file()) else None,
        "title": title,
        "headers": headers,
        "chromium_version": chromium_v,
        "warcio_version": warcio_v,
        "report": report,
    }


def _time_iso_now() -> str:
    """ISO 8601 UTC string for sidecar metadata. CLAUDE.md §3 — UTC always."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    tab_context: TabContext | None = None,
    block_rules_file: Path | None = None,
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
    if tab_context and tab_context.user_agent:
        argv += ["--userAgent", tab_context.user_agent]
    if case_cookies_path and case_cookies_path.is_file():
        argv += ["--cookieFile", str(case_cookies_path)]
    if proxy_url:
        # Plan §U8: same-identity routing across all three producers.
        argv += ["--proxyServer", proxy_url]
    if block_rules_file and block_rules_file.is_file():
        # browsertrix-crawler natively supports a JSON block-rules file.
        argv += ["--blockRules", str(block_rules_file)]

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


def _write_browsertrix_block_rules(out_dir: Path) -> Path | None:
    """Materialise the bundled blocklist as a browsertrix-compatible JSON
    file. Returns the path, or ``None`` if the rules can't be loaded.
    """
    try:
        rules = blocklist_mod.default_rules()
    except Exception:
        return None
    if not rules.blocked_hosts:
        return None
    # browsertrix rules: list of objects with `url` (regex) and `type`. We
    # build a single rule with a regex matching any of the blocked hosts.
    import re
    pattern = "|".join(re.escape(h) for h in sorted(rules.blocked_hosts))
    body = [{"url": f"https?://[^/]*({pattern})(/.*)?$", "type": "block"}]
    target = out_dir / "block-rules.json"
    target.write_text(json.dumps(body), encoding="utf-8")
    return target


async def _browsertrix_warc(
    *,
    url: str,
    out_dir: Path,
    case_cookies_path: Path | None,
    timeout_s: int = DEFAULT_BROWSERTRIX_TIMEOUT_S,
    attempts: int = DEFAULT_BROWSERTRIX_ATTEMPTS,
    proxy_url: str | None = None,
    tab_context: TabContext | None = None,
    block_ads: bool = True,
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

    block_rules_file = _write_browsertrix_block_rules(out_dir) if block_ads else None

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
            tab_context=tab_context,
            block_rules_file=block_rules_file,
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
    tab_context: TabContext | None = None,
    cookies_path: Path | None = None,
    block_ads: bool = True,
    hide_cookie_banners: bool = True,
    warc_in_session: bool = True,
    app_version: str = "0",
) -> CaptureBundle:
    """Drive the capture pipeline and return a :class:`CaptureBundle`.

    ``work_dir`` is where artifacts are staged before postprocess moves them
    into the canonical sidecar dir; pass one in tests, otherwise a
    private tmp dir is created and the caller is responsible for moving
    the files out before it goes out of scope.

    ``cookies_path`` overrides the on-disk per-case cookie file — used by
    the ephemeral-cookies path so a one-shot job's cookies aren't persisted
    to the case directory.

    ``warc_in_session`` (default True) tees the Playwright session's CDP
    network events into a WARC/1.1 file via :class:`warc_writer.CdpWarcWriter`,
    so MHTML, PNG, and WARC come from a single navigation. Set to False (or
    when warcio is missing / writer fails) to fall back to the legacy
    browsertrix-crawler subprocess.
    """
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="capsule-capture-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    if cookies_path is not None:
        cookies = _load_cookies_from_path(cookies_path)
        cookies_for_browsertrix: Path | None = (
            cookies_path if is_social(url) else None
        )
    else:
        cookies = _load_cookies_for(case_slug)
        cookies_for_browsertrix = (
            cookies_mod.path_for(case_slug)
            if case_slug and cookies_mod.exists(case_slug) and is_social(url)
            else None
        )

    snap = await _playwright_snapshot(
        url=url,
        out_dir=work_dir,
        cookies=cookies,
        timeout_ms=timeout_ms,
        settle_ms=settle_ms,
        proxy_url=proxy_url,
        tab_context=tab_context,
        block_ads=block_ads,
        hide_cookie_banners=hide_cookie_banners,
        write_warc=warc_in_session,
        app_version=app_version,
    )

    warc_path: Path | None = snap["warc"]
    btx_v = "0"
    # Browsertrix subprocess is only run when the in-session WARC was not
    # produced (warcio missing, CDP error, or warc_in_session=False). This
    # preserves forensic completeness — every capture has a WARC — without
    # paying the dual-session cost when Tier D succeeded.
    if warc_path is None:
        warc_path, btx_v = await _browsertrix_warc(
            url=url,
            out_dir=work_dir,
            case_cookies_path=cookies_for_browsertrix,
            timeout_s=browsertrix_timeout_s,
            attempts=browsertrix_attempts,
            proxy_url=proxy_url,
            tab_context=tab_context,
            block_ads=block_ads,
        )

    return CaptureBundle(
        mhtml=snap["mhtml"],
        screenshot=snap["screenshot"],
        warc=warc_path,
        chromium_version=snap["chromium_version"],
        browsertrix_version=btx_v,
        page_title=snap["title"],
        response_headers=snap["headers"],
        report=snap["report"],
        har=snap["har"],
        console_log=snap["console_log"],
        context_screenshot=snap["context_screenshot"],
        warcio_version=snap["warcio_version"],
    )
