# Capsule v0.1 → v0.2 hardening notes

This release tightens the forensic guarantees of every capture and makes
the browser extension the recommended path for cookies and authenticated
sessions. The changes are additive: existing captures and v1 schema
records continue to verify against the bundled `verify.py`.

## What changed, and what it means for your evidence

### 1. The extension now ships the user's environment

Every capture submitted via the extension carries a structured
**tab_context** envelope: the user's UA string, viewport size, device pixel
ratio, scroll position, timezone, locale, color scheme, reduced-motion
preference, and the page's referrer. The backend Chromium uses these to
mirror the user's environment when it produces the canonical capture, so
mobile pages render as mobile, dark-mode pages render dark, and pages that
adapt to the user's locale render as the user saw them.

For your evidence, this means the canonical screenshot and MHTML now
reflect what the investigator actually saw — not a generic 1440×900
desktop view of the same URL.

### 2. localStorage / sessionStorage and DOM-at-click-time

Beyond cookies, the extension now captures per-origin localStorage and
sessionStorage (some sites carry session JWTs there) and a click-time
snapshot of `document.documentElement.outerHTML` plus structural counts
(node count, iframe count, video count, image count). These ride
alongside the canonical capture as additive sidecars
(`{stem}.user-browser.session-state.json`,
`{stem}.user-browser.dom-snapshot.html`,
`{stem}.user-browser.dom-snapshot.json`).

For your evidence, this preserves "what the investigator was looking at"
at the moment of the click, separate from the backend's reproducible
re-fetch.

### 3. Pre-capture readiness gate

Before the extension grabs the screenshot or the MHTML, it waits — with
per-gate caps and a 30-second hard ceiling — for:

- `document.readyState === "complete"`
- fonts loaded (`document.fonts.ready`)
- viewport-visible images: `complete && naturalHeight > 0`
- every `<video>` element: `readyState >= HAVE_METADATA`
- network-quiet for 1.5 seconds (no new resource entries)

Each gate's outcome (ok / timed_out, plus elapsed milliseconds) is recorded
into the capture's `tab_context.readiness_report` and into the audit log.
The capture is never silently aborted on a timeout; instead, the
forensic record shows exactly what was awaited.

The backend's canonical capture orchestrates an equivalent set of waits
(load → fonts → visible images → video → lazy-scroll → networkidle), with
each outcome surfaced in `meta.json.capture.render_waits[]`.

### 4. Ad and tracker blocking

The capture pipeline now blocks a curated set of ad and tracker hosts at
the network layer:

- The **extension** uses `declarativeNetRequest` so blocked requests
  never even reach the user's tab.
- The backend **Playwright** capture uses a route handler that aborts
  blocked requests.
- The backend **browsertrix-crawler** invocation passes the same rules
  via its `--blockRules` flag.

Every blocked request URL is recorded in `meta.json.capture.blocked_requests_sample`
(capped at 200) and counted in `blocked_request_count`. The audit log
gets a per-job `capture.ads_blocked` entry. The blocklist version
(e.g. `2026-05-06`) is recorded alongside, so the exact ruleset is
reproducible at review time.

This is **on by default** and configurable per case via
`case.settings_json["block_ads"] = false`.

The list is intentionally conservative — a few hundred high-signal
hosts, well under the 30k declarativeNetRequest limit — and is the
**single source of truth**: the backend at `app/static/blocklists/easylist-essentials.json`
and the extension at `extension/blocklists/easylist-essentials.json` are
byte-identical (a test in `tests/test_blocklist.py` enforces this).

### 5. Cookie / consent banner CSS hide

The backend Playwright capture injects a small, vendored slice of CSS
that hides common cookie/consent banner elements (OneTrust, Cookiebot,
TrustArc, Quantcast Choice, Didomi, Sourcepoint, Usercentrics, Cookiehub,
Iubenda) **visually only**.

The DOM is never modified. The MHTML and WARC retain the banner element
in source — a forensic reviewer can still answer "did the page show a
consent banner?" by inspecting the archive. We don't auto-click "Reject
all" or "Accept", because that would change the site's consent state and
is forbidden by Capsule policy (CLAUDE.md §13).

Recorded in `meta.json.capture.banner_hide_applied` and
`meta.json.capture.banner_hide_version`. **On by default**, configurable
via `case.settings_json["hide_cookie_banners"] = false`.

### 6. Real HAR via `chrome.debugger` (opt-in)

For cases where the lightweight Resource Timing approximation isn't
enough, the extension can attach `chrome.debugger` to the active tab and
record the full `Network.*` event stream — request methods, status codes,
response headers, timing.

Chrome shows a persistent yellow "<extension> is debugging this browser"
banner while attached. That's intentional: the elevated capability is
visible to the user. **Off by default**; enable per-capture from the
extension popup's Settings → Capture full network log toggle.

### 7. Cookie pipeline hardening

- **Full cookie store coverage.** The extension iterates
  `chrome.cookies.getAllCookieStores()` so cookies in container tabs and
  partitioned storage survive.
- **Freshness validation at job start.** If any cookie is past its
  expiry or expiring within 24 hours, the job emits a SSE warning and
  records a `cookies.stale_at_capture` audit entry — never logging
  values, only the affected domains.
- **Snapshot hash.** Every job records a `cookies_snapshot_sha256` that
  binds the capture to the exact cookie set it used. Two captures of the
  same URL minutes apart can be proven to have used the same (or a
  different) cookie set without ever recording cookie values.
- **Ephemeral cookies.** A new `cookie_persistence: "ephemeral"` option
  on `POST /api/extension/capture` writes cookies to a per-job tmpdir
  and discards them after the job ends. The case directory is never
  touched. Toggle from the extension popup → Settings → "Ephemeral
  cookies (one-shot)".

### 8. Token hardening

- Tokens minted with an `extension_id` now require the same id on every
  authenticated request (the extension sends `X-Extension-Id:
  <chrome.runtime.id>`). A 403 with an `extension.id_mismatch` audit
  entry results from a mismatch.
- Legacy tokens (no `extension_id` recorded) are grandfathered.
- Tokens can now be rotated via `POST /api/extension/pair/{token_id}/rotate`
  — issues a new raw token, revokes the old, label and binding carry
  over. The extension popup exposes this as "Rotate token".

### 9. Schema v2

`meta.json` now writes `schema_version: 2` and adds:

- `capture` — the capture report (render waits, blocked requests, banner
  hide flag, blocklist + banner versions, tab-context-used flag).
- `cookies_snapshot_sha256`
- `ephemeral_cookies_used`

The new sidecars (`tab_context`, `session_state`, `dom_snapshot.html`,
`dom_snapshot.json`) are listed in `artifacts` and hashed in `checksums`,
so they're transitively signed by the existing `meta.json.sig`.

The bundled `verify.py` continues to verify v1 records as before. No
migration is required for existing captures.

## How to verify a capture from this release

The `verify.py` shipped in every evidence-export bundle continues to
work. To check a v2 capture by hand:

1. Read `{stem}.meta.json`. Confirm `schema_version: 2`.
2. Re-hash every file listed in `artifacts`; compare to `checksums`.
3. Verify `meta.json.sig` against `meta.json` using `public_key.pem`.
4. (Optional) Review `meta.json.capture` for the list of blocked
   requests, banner-hide flag, and render-wait outcomes — these are the
   capture-side mutations recorded for transparency.
5. (Optional) Check `cookies_snapshot_sha256` if you need to confirm
   that two captures used the same cookie set.

## Default settings (and how to change them)

| Setting                        | Default | Where                          |
|--------------------------------|---------|--------------------------------|
| Block ads in canonical capture | ON      | `case.settings_json.block_ads` |
| CSS-hide cookie banners        | ON      | `case.settings_json.hide_cookie_banners` |
| Block ads in user's tab        | ON      | extension popup → Settings    |
| Real HAR via chrome.debugger   | OFF     | extension popup → Settings    |
| Cookie persistence             | "case"  | extension popup → Settings    |
| Live capture (MHTML/PNG/etc.)  | OFF     | extension popup → Settings    |

## Forbidden (still)

- DOM-mutating consent dismissal (auto-clicking "Reject all").
- Silent network calls. Every blocked request is recorded; every banner
  hide is recorded.
- Cookie values in logs, audit entries, evidence exports, or API
  responses.
- Automatic yt-dlp version polling.
