"""Frozen single-file HTML capture (CLAUDE.md §15 v0.12).

Companion artifact alongside MHTML / PNG / WARC. Replicates archive.today's
core technique: render the URL in a real browser, serialize the post-JS
DOM, inline computed styles, embed images as ``data:`` URIs (via the
shared CDP session so cross-origin CDN images don't break), strip all
JavaScript. The result is a single ``.html`` file that any browser, any
year, will render — even when the source site is gone, the source JS
breaks, or MHTML support is dropped.

This is explicitly a **derived view, not a substitute** for MHTML or
WARC. Those remain the forensic source of record. The frozen HTML is
the format a recipient opens first when they just want to *see* the
page (CLAUDE.md §13 #13 + #14).

Forensic stance:

* Generated AFTER the animation-freeze stylesheet is removed, so the
  inlined computed styles reflect the page's source CSS — not Capsule's
  overlay. A PNG/frozen-html visual disagreement on a running carousel
  is honest divergence; baking ``animation-play-state: paused`` into
  every element's ``style=""`` would be a silent capture-side mutation.
* Hashed and bound transitively by ``meta.json.sig``.
* Three-tier size ladder (256 KB / 64 KB / external) to keep
  ``frozen.html`` under a 10 MB target; hard cap 25 MB above which the
  artifact is omitted (``error="size_budget_exceeded"``) and an audit
  row records the gap.
* Cross-origin images are pulled via CDP ``Network.getResponseBody`` on
  the SAME session that produced the WARC — same channel MHTML uses.
  Failures fall through silently (``<img src>`` left absolute; recipient
  has the WARC for offline replay) and are counted under
  ``external_image_count``.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "FrozenHtmlResult",
    "generate",
    "FROZEN_HTML_VERSION",
    "SIZE_BUDGET_BYTES",
    "HARD_CAP_BYTES",
    "TIER_FULL_LIMIT",
    "TIER_SMALL_LIMIT",
]


# Version pin for the on-disk artifact format. Bump when the serializer's
# output shape changes (e.g. a new placeholder dialect). Recorded in
# ``meta.json.capture.frozen_html.version`` so a future verifier can
# correlate output style to the writer that produced it.
FROZEN_HTML_VERSION = "1"

# Three-tier size ladder. Targets are chosen so the typical news article
# or social-media post round-trips under the budget; pathological cases
# (40 4K hero images) downgrade gracefully.
SIZE_BUDGET_BYTES = 10 * 1024 * 1024     # 10 MB — soft target
HARD_CAP_BYTES = 25 * 1024 * 1024        # 25 MB — abort + omit artifact
TIER_FULL_LIMIT = 256 * 1024             # 256 KB per image at the "full" tier
TIER_SMALL_LIMIT = 64 * 1024             # 64 KB per image at the "small_only" tier


@dataclass(frozen=True)
class FrozenHtmlResult:
    """Outcome of one ``generate`` call.

    ``path`` is ``None`` if generation failed; ``error`` carries the
    reason (one of: ``"size_budget_exceeded"``, ``"cdp_unavailable"``,
    ``"evaluate_raised"``, ``"write_io_error"``, plus the exception
    class name suffix). The capture pipeline continues regardless;
    ``meta.json.capture.frozen_html.*`` records the outcome.
    """

    path: Path | None
    version: str
    tier: str | None            # "full" | "small_only" | "external" | None
    byte_count: int | None
    inlined_image_count: int
    external_image_count: int
    stripped_script_count: int
    stripped_iframe_count: int
    stripped_font_face_count: int
    shadow_root_omitted_count: int
    error: str | None


# --- DOM walker (in-browser JS) ---------------------------------------------

# Single in-browser script. Walks the live document tree once with
# TreeWalker, emits each visible element with inline computed styles,
# and records the list of <img> URLs the host needs to inline. Returns
# a JSON-able dict so Python can post-process resources via CDP.
#
# Pragmatic-fidelity rules per the design review:
#   * Skip display:none subtrees entirely (cuts noise + size).
#   * <script>, <link rel="stylesheet">, <style>, @font-face → stripped
#     (counted for the audit). <noscript> content is PRESERVED inline as
#     if JS were disabled — often the most archive-faithful representation.
#   * <iframe> body replaced with a visible placeholder showing src/title.
#   * Shadow roots replaced with a visible marker. Same-origin iframe
#     bodies are NOT walked recursively (kept under pragmatic fidelity).
#   * `<bdi>` elements and `dir` attributes preserved verbatim (CLAUDE §4.5).
#   * No mutation of the live DOM — we read getComputedStyle() and emit
#     a fresh string buffer; the page never sees our changes.
_FROZEN_HTML_JS = r"""
async ({ version }) => {
    'use strict';
    const out = [];
    const imageRefs = [];          // [{idx, url, mime}, ...]; idx is the placeholder index
    let imageIdx = 0;
    let strippedScriptCount = 0;
    let strippedIframeCount = 0;
    let strippedFontFaceCount = 0;
    let shadowRootOmittedCount = 0;

    const VOID_ELEMENTS = new Set([
        'area','base','br','col','embed','hr','img','input',
        'link','meta','param','source','track','wbr',
    ]);
    const STRIP_TAGS = new Set(['script','link','style','noscript']);

    const escAttr = (s) => String(s)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    const escText = (s) => String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');

    const isVisible = (el) => {
        // Cheap reject: display:none / visibility:hidden cut the whole
        // subtree. opacity:0 is preserved because some pages animate
        // it for a reveal — the frozen HTML should match what the
        // pixels show, and opacity:0 is still a layout-occupying
        // element.
        const style = getComputedStyle(el);
        if (style.display === 'none') return false;
        if (style.visibility === 'collapse') return false;
        return true;
    };

    // Inline computed style — only the properties an offline reader
    // actually needs. Walking every computed property would balloon
    // file size and confuse a recipient (think `-webkit-text-stroke`
    // on 4000 nodes). This is the pragmatic subset.
    const VISUAL_PROPS = [
        'display','position','top','right','bottom','left',
        'width','height','min-width','min-height','max-width','max-height',
        'margin','padding','border','border-radius','box-sizing','box-shadow',
        'background','background-color','background-image','background-size',
        'background-position','background-repeat',
        'color','font','font-family','font-size','font-weight','font-style',
        'line-height','letter-spacing','text-align','text-decoration',
        'text-transform','white-space','word-break','overflow-wrap',
        'flex','flex-direction','flex-wrap','justify-content','align-items',
        'align-content','gap','grid-template-columns','grid-template-rows',
        'grid-column','grid-row','grid-area',
        'list-style','vertical-align','opacity','transform','filter',
        'cursor','direction','unicode-bidi','z-index','visibility',
    ];

    const emitInlineStyle = (el) => {
        const cs = getComputedStyle(el);
        const parts = [];
        for (const prop of VISUAL_PROPS) {
            const v = cs.getPropertyValue(prop);
            if (!v) continue;
            // Drop default-y values that bloat output without changing
            // appearance. Conservative: only the truly inert ones.
            if (v === 'normal' || v === 'none' || v === 'auto') continue;
            if (v === 'rgba(0, 0, 0, 0)' || v === 'transparent') continue;
            parts.push(prop + ':' + v);
        }
        return parts.join(';');
    };

    // Build a stable selector for img placeholder substitution after
    // CDP fetches the body. The frozen HTML uses ``__CAPSULE_IMG_<idx>__``
    // markers; Python rewrites these to ``data:`` URIs or absolute URLs.
    const recordImageRef = (src) => {
        const idx = imageIdx++;
        imageRefs.push({ idx: idx, url: src });
        return '__CAPSULE_IMG_' + idx + '__';
    };

    const walk = (node) => {
        if (node.nodeType === Node.TEXT_NODE) {
            out.push(escText(node.nodeValue));
            return;
        }
        if (node.nodeType !== Node.ELEMENT_NODE) return;
        const tag = node.tagName.toLowerCase();
        if (STRIP_TAGS.has(tag)) {
            // <noscript>: render its text content as if JS were disabled
            // (archive.today's behavior — often the most archive-faithful
            // representation). Other strip tags drop entirely.
            if (tag === 'noscript') {
                out.push(node.innerHTML || '');
            } else if (tag === 'script') {
                strippedScriptCount++;
            } else if (tag === 'style') {
                // Strip @font-face declarations from the count even
                // though the whole <style> is dropped — gives a
                // reviewer a sense of what was lost.
                const css = node.textContent || '';
                const m = css.match(/@font-face\b/g);
                if (m) strippedFontFaceCount += m.length;
            }
            return;
        }
        if (tag === 'iframe') {
            strippedIframeCount++;
            const src = node.getAttribute('src') || '';
            const title = node.getAttribute('title') || '';
            out.push(
                '<div data-capsule-fidelity="iframe-stripped" style="border:1px dashed #888;padding:8px;color:#666;font:12px monospace">' +
                '[Capsule: iframe content not preserved in this view — see page.warc.gz]<br>' +
                '<small>src: ' + escText(src) + '</small>' +
                (title ? '<br><small>title: ' + escText(title) + '</small>' : '') +
                '</div>'
            );
            return;
        }
        if (!isVisible(node)) return;

        if (node.shadowRoot) {
            shadowRootOmittedCount++;
            out.push(
                '<div data-capsule-fidelity="shadow-root-omitted" style="border:1px dashed #aaa;padding:8px;color:#777;font:12px monospace">' +
                '[Capsule: shadow-DOM widget not preserved in this view — see page.warc.gz]' +
                '</div>'
            );
            return;
        }

        // Open tag with inline style + preserved attrs.
        out.push('<' + tag);
        for (const attr of node.attributes) {
            const name = attr.name.toLowerCase();
            // Strip event handlers (on*) and style="" (we replace).
            if (name.startsWith('on')) continue;
            if (name === 'style') continue;
            // <img src> → placeholder marker; Python rewrites later.
            if (tag === 'img' && name === 'src') {
                const real = node.currentSrc || node.src || attr.value;
                if (real) {
                    out.push(' src="' + recordImageRef(real) + '"');
                }
                continue;
            }
            // Preserve dir / lang / bdi attrs (CLAUDE.md §4.5 bidi).
            out.push(' ' + name + '="' + escAttr(attr.value) + '"');
        }
        const inline = emitInlineStyle(node);
        if (inline) out.push(' style="' + escAttr(inline) + '"');
        out.push('>');

        if (VOID_ELEMENTS.has(tag)) return;

        // Recurse children.
        for (const child of node.childNodes) walk(child);

        out.push('</' + tag + '>');
    };

    // Doctype + walk the documentElement (preserves <html lang> and dir).
    const doctype = '<!DOCTYPE html>\n';
    const root = document.documentElement;
    // Emit the <html> tag explicitly so we can pin lang+dir at the top.
    const lang = root.getAttribute('lang') || '';
    const dir = root.getAttribute('dir') || '';
    out.push('<html');
    if (lang) out.push(' lang="' + escAttr(lang) + '"');
    if (dir) out.push(' dir="' + escAttr(dir) + '"');
    out.push(' data-capsule-frozen-html-version="' + escAttr(version) + '"');
    out.push('>');
    // Walk head children (we already emitted <html>).
    if (document.head) {
        out.push('<head>');
        for (const child of document.head.childNodes) walk(child);
        // Inject a tiny <meta> documenting the freeze for any future reader.
        out.push('<meta name="capsule-frozen-html" content="v' + escAttr(version) + '">');
        out.push('</head>');
    }
    if (document.body) {
        out.push('<body');
        for (const attr of document.body.attributes) {
            const name = attr.name.toLowerCase();
            if (name.startsWith('on') || name === 'style') continue;
            out.push(' ' + name + '="' + escAttr(attr.value) + '"');
        }
        const bodyInline = emitInlineStyle(document.body);
        if (bodyInline) out.push(' style="' + escAttr(bodyInline) + '"');
        out.push('>');
        for (const child of document.body.childNodes) walk(child);
        out.push('</body>');
    }
    out.push('</html>');
    return {
        html: doctype + out.join(''),
        image_refs: imageRefs,
        stripped_script_count: strippedScriptCount,
        stripped_iframe_count: strippedIframeCount,
        stripped_font_face_count: strippedFontFaceCount,
        shadow_root_omitted_count: shadowRootOmittedCount,
    };
}
"""


# --- Generate entrypoint ----------------------------------------------------


async def generate(
    *,
    page: Any,
    work_dir: Path,
) -> FrozenHtmlResult:
    """Produce ``page.frozen.html`` under ``work_dir``.

    The file is named ``page.frozen.html`` here to match the rest of the
    capture-stage artifacts (``page.mhtml``, ``page.png``, ``page.warc.gz``);
    :func:`app.postprocess.finalize` renames it to
    ``Captures/{stem}.page.frozen.html`` in the per-item folder.

    ``page`` is the Playwright ``Page`` — the same one that produced
    MHTML/PNG/WARC. Image bodies are fetched via
    ``page.request.fetch()`` which uses the same browser context
    (cookies, auth, redirects) but bypasses CORS because the request
    is issued by Playwright at the browser layer. Cross-origin CDN
    images work without the in-browser-fetch CORS failures the design
    review flagged.

    Forensic note: ``page.request.fetch`` makes a fresh request, so the
    bytes might differ slightly from what the page saw at capture time
    (CDN refresh, A/B rotation). The WARC remains the canonical record;
    ``frozen.html`` is the pragmatic-fidelity derived view (CLAUDE.md
    §13 #13 + #14). The whole frozen file is hashed and bound by
    ``meta.json.sig`` so post-capture tampering is detectable.

    Never raises. Capture continues regardless; the audit log + meta.json
    record the outcome via ``meta.json.capture.frozen_html.*``.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / "page.frozen.html"
    started = time.monotonic()

    # Run the in-browser serializer. Wrap in a broad try — any DOM-walk
    # failure should leave the rest of the capture pipeline untouched.
    try:
        raw = await page.evaluate(_FROZEN_HTML_JS, {"version": FROZEN_HTML_VERSION})
    except Exception as exc:
        return FrozenHtmlResult(
            path=None, version=FROZEN_HTML_VERSION, tier=None,
            byte_count=None,
            inlined_image_count=0, external_image_count=0,
            stripped_script_count=0, stripped_iframe_count=0,
            stripped_font_face_count=0, shadow_root_omitted_count=0,
            error=f"evaluate_raised:{type(exc).__name__}",
        )
    if not isinstance(raw, dict) or "html" not in raw:
        return FrozenHtmlResult(
            path=None, version=FROZEN_HTML_VERSION, tier=None,
            byte_count=None,
            inlined_image_count=0, external_image_count=0,
            stripped_script_count=0, stripped_iframe_count=0,
            stripped_font_face_count=0, shadow_root_omitted_count=0,
            error="evaluate_raised:UnexpectedReturnShape",
        )

    html_skeleton: str = raw["html"]
    image_refs: list[dict[str, Any]] = list(raw.get("image_refs") or [])
    stripped_script_count = int(raw.get("stripped_script_count") or 0)
    stripped_iframe_count = int(raw.get("stripped_iframe_count") or 0)
    stripped_font_face_count = int(raw.get("stripped_font_face_count") or 0)
    shadow_root_omitted_count = int(raw.get("shadow_root_omitted_count") or 0)

    # Three-tier ladder. Build a list of (idx, data_uri | None) pairs per
    # tier; ``None`` means "leave the absolute URL in place." Each tier
    # tries inlining with a different per-image cap; we fall back to the
    # next tier when the resulting document exceeds SIZE_BUDGET_BYTES.
    bodies = await _fetch_image_bodies(page, image_refs)

    for tier_name, per_image_cap in (
        ("full", TIER_FULL_LIMIT),
        ("small_only", TIER_SMALL_LIMIT),
        ("external", 0),
    ):
        rendered, inlined, external = _render_with_tier(
            skeleton=html_skeleton,
            image_refs=image_refs,
            bodies=bodies,
            per_image_cap=per_image_cap,
        )
        size = len(rendered.encode("utf-8"))
        if size > HARD_CAP_BYTES:
            # Even the external tier blew past the hard cap — pathological
            # page. Omit the artifact rather than ship something that
            # would clog an evidence bundle.
            return FrozenHtmlResult(
                path=None, version=FROZEN_HTML_VERSION, tier=None,
                byte_count=size,
                inlined_image_count=0, external_image_count=0,
                stripped_script_count=stripped_script_count,
                stripped_iframe_count=stripped_iframe_count,
                stripped_font_face_count=stripped_font_face_count,
                shadow_root_omitted_count=shadow_root_omitted_count,
                error="size_budget_exceeded",
            )
        if size <= SIZE_BUDGET_BYTES or tier_name == "external":
            try:
                target.write_text(rendered, encoding="utf-8")
            except OSError as exc:
                return FrozenHtmlResult(
                    path=None, version=FROZEN_HTML_VERSION, tier=tier_name,
                    byte_count=size,
                    inlined_image_count=inlined, external_image_count=external,
                    stripped_script_count=stripped_script_count,
                    stripped_iframe_count=stripped_iframe_count,
                    stripped_font_face_count=stripped_font_face_count,
                    shadow_root_omitted_count=shadow_root_omitted_count,
                    error=f"write_io_error:{type(exc).__name__}",
                )
            _ = started  # reserved for future telemetry; suppress unused warning
            return FrozenHtmlResult(
                path=target, version=FROZEN_HTML_VERSION, tier=tier_name,
                byte_count=size,
                inlined_image_count=inlined, external_image_count=external,
                stripped_script_count=stripped_script_count,
                stripped_iframe_count=stripped_iframe_count,
                stripped_font_face_count=stripped_font_face_count,
                shadow_root_omitted_count=shadow_root_omitted_count,
                error=None,
            )

    # Unreachable — the loop's last iteration always either writes the
    # external-tier output or hits the hard cap. Defensive return.
    return FrozenHtmlResult(
        path=None, version=FROZEN_HTML_VERSION, tier=None,
        byte_count=None,
        inlined_image_count=0, external_image_count=0,
        stripped_script_count=stripped_script_count,
        stripped_iframe_count=stripped_iframe_count,
        stripped_font_face_count=stripped_font_face_count,
        shadow_root_omitted_count=shadow_root_omitted_count,
        error="evaluate_raised:UnreachableLadderExit",
    )


# --- CDP image-body fetcher -------------------------------------------------


async def _fetch_image_bodies(
    page: Any, image_refs: list[dict[str, Any]]
) -> dict[int, tuple[str, bytes]]:
    """Fetch each image's body via ``page.request.fetch``.

    Returns ``{idx: (mime, body_bytes)}``. URLs that fail (404, network
    error, timeout) are silently skipped — those become external refs
    at render time and the recipient relies on the WARC for offline
    replay. Cross-origin URLs work because Playwright issues the
    request at the browser layer, not from the page's script context.

    Per-URL timeout is intentionally short (5s); a slow image shouldn't
    block the frozen-html generation on the critical capture path. Data
    and blob URLs are also handled — ``page.request.fetch`` doesn't
    accept them, so we synthesize a body from the URL itself.
    """
    if not image_refs:
        return {}
    out: dict[int, tuple[str, bytes]] = {}
    seen: dict[str, tuple[str, bytes] | None] = {}
    for ref in image_refs:
        idx = int(ref.get("idx", -1))
        url = str(ref.get("url") or "")
        if idx < 0 or not url:
            continue
        # Deduplicate — many pages reuse the same logo / spacer image
        # dozens of times. Cache the result so we issue one fetch per
        # unique URL even when the DOM references it N times.
        if url in seen:
            cached = seen[url]
            if cached is not None:
                out[idx] = cached
            continue
        # data: URLs already carry their own bytes — round-trip them
        # without a network request.
        if url.startswith("data:"):
            seen[url] = None  # already a data URI; render-time keeps it as-is
            continue
        # blob: URLs are tied to the page's runtime and can't be re-
        # fetched — leave external.
        if url.startswith("blob:"):
            seen[url] = None
            continue
        try:
            response = await page.request.fetch(url, timeout=5_000)
        except Exception:
            seen[url] = None
            continue
        try:
            if not response.ok:
                seen[url] = None
                continue
            body = await response.body()
            headers = response.headers or {}
            mime_raw = headers.get("content-type") or headers.get("Content-Type") or "image/jpeg"
            mime = mime_raw.split(";", 1)[0].strip() or "image/jpeg"
            pair = (mime, bytes(body))
            seen[url] = pair
            out[idx] = pair
        except Exception:
            seen[url] = None
            continue
    return out


# --- Render with a given tier's per-image budget ---------------------------


def _render_with_tier(
    *,
    skeleton: str,
    image_refs: list[dict[str, Any]],
    bodies: dict[int, tuple[str, bytes]],
    per_image_cap: int,
) -> tuple[str, int, int]:
    """Substitute ``__CAPSULE_IMG_<idx>__`` placeholders.

    Returns ``(rendered_html, inlined_count, external_count)``.

    ``per_image_cap == 0`` forces the "external" tier where every image
    keeps its absolute URL. Otherwise: bodies under the cap become
    ``data:`` URIs; oversized ones fall back to the absolute URL.
    """
    inlined = 0
    external = 0
    rendered = skeleton
    for ref in image_refs:
        idx = int(ref.get("idx", -1))
        url = str(ref.get("url") or "")
        placeholder = f"__CAPSULE_IMG_{idx}__"
        body_pair = bodies.get(idx)
        if per_image_cap > 0 and body_pair is not None:
            mime, body = body_pair
            if len(body) <= per_image_cap:
                data_uri = f"data:{mime};base64,{base64.b64encode(body).decode('ascii')}"
                rendered = rendered.replace(placeholder, data_uri)
                inlined += 1
                continue
        # Fallback: leave the absolute URL in place. Recipient relies on
        # the WARC for offline replay; ``frozen.html`` still renders
        # correctly online for as long as the source serves the bytes.
        rendered = rendered.replace(placeholder, url)
        external += 1
    return rendered, inlined, external
