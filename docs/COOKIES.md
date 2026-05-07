# Cookies in Capsule

Some pages — most social-media platforms, paywalled archives, age-gated videos — only show their full content to a logged-in browser. Capsule can capture them as you'd see them, but it needs a copy of your browser session: your cookies.

This document is a long-form companion to the in-app **"Help me get cookies"** wizard. The wizard is the recommended path for most investigators — it's tailored to your browser and validates that the cookies you import actually cover the site you're trying to capture. Read this if you want the bigger picture or you're stuck.

## Two paths

| Path | When to use |
|------|-------------|
| **In-app wizard** (Cookies tab → "Help me get cookies") | First time, or whenever you're not sure how. |
| **Direct upload** (Cookies tab → "Upload cookies.txt") | You already have a `cookies.txt` exported and just want to drop it in. |

Both paths land in the same place on disk and give Capsule the same capability. The wizard adds guidance and a coverage check; the direct upload skips them.

## Why Capsule can't read your browser's cookies for you

Browsers deliberately stop one site (Capsule's `localhost`) from reading another site's (`twitter.com`'s) cookies. That boundary protects you from malicious local apps, and we won't try to bypass it. Inside Docker we also can't reach your host browser's cookie store directly: on macOS the Keychain holds the decryption key; on Windows it's DPAPI. So a small browser extension that runs *inside the browser you're already using* is the cleanest way to extract a cookies file.

## What the wizard does

1. Asks for the URL you want to capture so it can verify coverage later.
2. Detects your browser and points you at one open-source extension (one click to install).
3. Walks you through exporting a `cookies.txt` from that extension.
4. Lets you import the file by drag-and-drop *or* by pasting its contents.
5. Shows a domain summary and tells you whether the imported cookies actually cover the site from step 1. If they don't, you go back and try again.

The recommended extensions are all open source, work entirely on your machine, and don't make network calls of their own:

- **Chrome / Edge / Brave / other Chromium browsers:** [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) (MIT-licensed)
- **Firefox:** [cookies.txt (LOCALLY)](https://addons.mozilla.org/firefox/addon/cookies-txt/)
- **Safari:** No good free cookie-export extension. Sign in to the target site in Firefox or Chrome instead and use that browser's extension.

## What happens to the cookies after you import them

- Stored at `/config/cases/{case_slug}/cookies.txt` inside the container, with file mode `0600` on macOS/Linux.
- **Same file feeds every downloader.** yt-dlp (video), Playwright + browsertrix (page snapshot), and gallery-dl (image galleries — Twitter image threads, Pixiv, DeviantArt, Imgur, etc.) all read the same Netscape `cookies.txt`. You log in once per site; every Capsule downloader sees the same authenticated session.
- The file is **never** included in evidence-export bundles.
- Cookie *values* are **never** logged, **never** echoed in the audit trail, and **never** returned by any API. Only the list of domains and per-domain expiry information is surfaced.
- The audit log records that an upload happened, which domains it covered, and which target URL the investigator was trying to cover — but nothing that could be replayed to log into the site.

## Multiple sites in one case

A single `cookies.txt` can cover many domains at once. You have two options when adding a new site to a case that already has cookies:

- **Add to existing cookies** (the wizard's default when you have any). The incoming cookies are merged in: cookies for new sites are appended, cookies that match existing ones (same `domain` + `path` + `name`) are updated to the newer values, and everything else is left alone. The wizard's review step shows exactly how many cookies will be added, updated, and kept before you save.
- **Replace existing cookies**. Wipes the case's `cookies.txt` and writes only what you imported. Useful when an investigation pivots and the old session is no longer relevant.

Both paths are recorded in the audit log with the mode and (for merges) the added/updated/kept counts.

The simple "Upload cookies.txt" button (next to the wizard) always replaces — it's for power users who already know what they want.

## Cookies expire

Login cookies typically live for hours, days, or weeks depending on the site. Capsule shows an "expired" badge next to any domain whose cookies have lapsed; re-export from your browser when that happens.

## See also

- [`docs/VERIFYING_EVIDENCE.md`](VERIFYING_EVIDENCE.md) — what the recipient of an evidence export sees (cookies are absent, by design).
- `app/cookies.py` — the parser and validator. Single source of truth for what a "valid" cookies file looks like in Capsule.
