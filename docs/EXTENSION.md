# The Capsule browser extension

A small first-party browser extension that lets you send links — and the
cookies for those links — straight from your everyday browser into the
Capsule app running on your machine.

## What it does

- **Send the active tab.** Click the extension's icon, click *Send this
  tab*, and the URL is captured in your active case.
- **Send a list of URLs.** Paste up to 25 URLs; each becomes a capture.
- **Sync cookies for the current site.** One click pushes the live
  cookie set — including `HttpOnly` cookies that the page itself can't
  see — to the active case so the canonical capture comes from the same
  authenticated session you're already in.
- **(Optional) Live capture.** Off by default. When on, the extension
  also uploads what *your* browser rendered (MHTML, screenshot, HAR,
  browser-environment metadata) as supplementary evidence alongside the
  canonical clean-Chromium capture.

## What it does NOT do

- It does **not** replace the canonical capture. Capsule still runs the
  page through its controlled container Chromium for forensic
  reproducibility. The extension's contribution rides alongside, never
  on top of, that capture.
- It does **not** talk to the public internet. All traffic goes to the
  Capsule server URL you set during pairing (default
  `http://localhost:8080`).
- It does **not** persist cookie values. Cookies are read on demand,
  POSTed to your local Capsule server, and dropped from extension
  memory.

## Installing

1. Make sure Capsule is running and reachable at
   `http://localhost:8080`.
2. **Chrome / Edge.** Open `chrome://extensions`, enable Developer mode,
   click *Load unpacked*, pick the `extension/` folder.
   **Firefox.** Open `about:debugging#/runtime/this-firefox`, click
   *Load Temporary Add-on*, pick `extension/manifest.json`.
3. Open Capsule → *Settings → Browser extension* → *Pair a new
   extension*. Copy the token Capsule shows you.
4. Click the extension icon, then *Pair with Capsule*. Paste the
   server URL, the token, and a label.

The extension stores the token in `chrome.storage.local`. The raw token
never reaches the disk on the host that runs Capsule — only its SHA-256
hash is persisted.

## Cookies, plainly

When you click *Send this tab* (or *Send a list of URLs*), the
extension:

1. Calls `chrome.cookies.getAll({url})` for every URL you submit.
2. Sends the cookies to Capsule's `/api/extension/capture` along with
   the URLs.
3. Capsule converts them to the standard Netscape format (the same
   format yt-dlp / browsertrix / curl all read) and writes them at
   `$CAPSULE_CONFIG_DIR/cases/{case_slug}/cookies.txt` with mode 0600.
4. The capture pipeline picks them up and forwards them to yt-dlp and
   the Playwright/browsertrix snapshot.

`HttpOnly` cookies — which `document.cookie` cannot see, but which yt-dlp
and Playwright need for some authenticated sites — flow through this
path. That's the main practical reason to pair the extension instead of
exporting `cookies.txt` from a third-party browser extension.

## Forensic implications (read this before using *Live capture* in court)

The extension's *live capture* feature is **off by default**. When you
toggle it on, it uploads MHTML, a screenshot, an approximate HAR, and a
JSON record of your browser environment. Every artifact is hashed,
listed in `meta.json`, and signed by Capsule's case key alongside the
canonical clean-Chromium capture.

A reviewer needs to know:

1. **The canonical capture is still the source of truth.** Live-capture
   files are clearly labelled and stored at
   `sidecars/{stem}/{stem}.user-browser.*`. The chain of custody for
   the canonical capture is unchanged by enabling live capture.
2. **Your browser is non-reproducible.** Whatever extensions you have,
   whatever ad-blockers, whatever login state — that all shapes what
   live capture sees. `user-browser.environment.json` records your
   user-agent, language, platform, and capture timestamp so a reviewer
   can interpret the artifact in context.
3. **The audit log records every extension submission.** Look for the
   `extension.capture_submitted` action (extension label, URL count,
   cookie domains — never values) and `user_browser_capture.received`
   (which roles were attached).

If the case is destined for court and the canonical capture is already
sufficient, leave live capture off.

## Revoking

Settings → Browser extension lists every paired extension with its
last-used time. *Revoke* removes the hash from disk; the extension's
next request gets a 401 and the popup shows a "Server changed"-style
warning.

If you suspect the token has been exposed (e.g. you shared a screen
recording that captured the pairing screen), revoke it and pair again.

## Permissions, in detail

| Permission | Required? | What it's used for |
|---|---|---|
| `cookies` | always | Read cookies for sites you're capturing — including HttpOnly cookies. |
| `tabs`, `activeTab` | always | Read the active tab's URL and the open-tabs list. |
| `storage` | always | Persist the pairing token and active-case ID locally. |
| `scripting` | always | Inject the live-capture probe (only when live capture is on). |
| `<all_urls>` | optional, runtime-requested | Required if you want to send a list of URLs across many domains in one click. Decline it and the popup falls back to single-tab and active-tab flows. |

## See also

- `docs/COOKIES.md` — manual `cookies.txt` workflow if you don't want
  to install the extension.
- `docs/VERIFYING_EVIDENCE.md` — what a recipient does with an evidence
  bundle that contains both canonical and user-browser captures.
