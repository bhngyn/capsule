# Capsule browser extension

One-click captures and cookie sync for the Capsule app.

The extension does **not** replace Capsule's clean-Chromium capture — it
hands the active tab's URL (and, on demand, the live MHTML / screenshot /
HAR / browser-environment metadata) to your local Capsule server, which
runs the canonical capture in its controlled container Chromium and
persists everything alongside.

Forensic note: when you enable **live capture**, an additional
"as-rendered-by-the-investigator's-browser" set of artifacts rides
alongside the canonical capture. These are clearly labeled in `meta.json`
and the PDF report, with a record of your browser environment so a
reviewer can see exactly what context produced them.

## Load unpacked

1. Run Capsule (the local server must be reachable at `http://localhost:8080`).
2. In Chrome / Edge, open `chrome://extensions`, enable Developer mode,
   click **Load unpacked**, and pick this folder.
3. Open Capsule → Settings → Browser extension → **Pair a new extension**.
4. Click the extension icon → **Pair with Capsule**, paste the token.

## Permissions

| Permission | Why |
|---|---|
| `cookies` | Read cookies for sites you're capturing — including HttpOnly cookies that the page itself can't see. |
| `tabs`, `activeTab` | Read the active tab URL and the open-tabs list when you choose "Send open tabs". |
| `storage` | Persist the pairing token and the chosen active case in `chrome.storage.local`. The token is stored locally; it is never sent except to the paired Capsule server. |
| `scripting` | Inject the live-capture probe (only when you toggle live capture ON). |
| `<all_urls>` (optional) | Capsule asks for access to a site the first time you Send or Sync cookies on that site. You can revoke per-site access from `chrome://extensions` → Capsule → Site access. |
| `pageCapture` | Captures the page as a single MHTML file when **Live capture** is enabled. |

## What never leaves your machine

- The pairing token never leaves the browser except in `Authorization` headers to the paired Capsule server URL.
- Cookie values are sent only to the paired Capsule server, kept in process memory at request time only, never written to `chrome.storage`.
- The extension never makes outbound requests to the public internet — only to the Capsule server URL you set during pairing.
