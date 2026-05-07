# CLAUDE.md

This file gives Claude Code the full context for building this project. Read it completely before writing any code, and re-read the relevant section before starting each task.

---

## 1. Project: Capsule

*Capture the web, with proof.*

(Working directory: `ytdlp/`. Docker image: `capsule`. The "yt-dlp" naming in the working tree is historical; user-facing surfaces use "Capsule.")

A Dockerized, cross-platform (Windows + macOS), GUI-first **web-evidence capture tool** built around [yt-dlp](https://github.com/yt-dlp/yt-dlp), Playwright (Chromium), and [browsertrix-crawler](https://github.com/webrecorder/browsertrix-crawler). It is for **investigators** — researchers, journalists, lawyers, and legal-discovery practitioners — who need to capture online media and the surrounding web context in a way that survives later scrutiny by editors, peers, opposing counsel, or courts.

The audience is non-technical with respect to terminals, but **rigorous about evidence**. Every design decision should default toward provenance, integrity, and transparency.

### The problems we are solving

1. **Installation is a barrier.** Investigators struggle to install Python, yt-dlp, ffmpeg, browser engines, and their dependencies. Docker collapses this into "install Docker, run one command."
2. **Updates are a barrier.** yt-dlp ships frequent updates (often necessary when a site changes). Investigators don't know they need to update, and don't know how. The app surfaces version state and offers one-click updates — but only when the user asks (no silent telemetry, no automatic GitHub polling).
3. **Web evidence is broader than media.** A media file alone is weak evidence; a page snapshot alone is weak evidence. Investigators need both, captured at the same moment, from the same authenticated session. The tool always captures the full package — page snapshot (MHTML), full-page screenshot, full WARC, plus media if any — for every URL.
4. **Files become an unsorted mess.** Default filenames are noisy, inconsistent across sites, and lack provenance. We normalize for portability while preserving the originals in metadata.
5. **No audit trail.** Investigators must answer "where did this file come from, when, who authenticated, and is it intact?" — sometimes years later. We fix this with case-aware organization, full sidecar files, cryptographic checksums, detached signatures, and a tamper-evident audit log.
6. **Evidence handoff is fragile.** Investigators move work between editors, courts, and colleagues. We produce signed zip bundles + locale-aware PDF reports, with a standalone verifier so recipients can confirm integrity without installing our tool.
7. **Multilingual interfaces age badly.** Text-heavy UIs become unreadable when re-translated, especially when right-to-left and left-to-right are both first-class. We build visual-first: icons, colors, shapes, illustrations, thumbnails. Words support the visuals, not the other way around.

### The interface, in short

The app is a single-purpose downloader UI: paste a link (or a list), watch the four-phase capture progress, find the result in the recent-captures grid. Settings (language, signing key, browser-extension pairing, yt-dlp updater) is reachable from the header. The case-management surfaces the backend supports — Cases, Library, Item detail, Audit log — are **not exposed as UI in v1**; they live on disk and over the API for power users and evidence handoff. The full forensic package (hashes, signatures, audit log, MHTML, screenshot, WARC) is always written.

### Threat model

This release assumes a **safe operating environment**: the investigator's device is not assumed to be under physical seizure risk, and the local network is not assumed to be hostile. We do **not** ship Tor/proxy support, at-rest encryption, or anti-forensics features in v1. We **do** minimize unsolicited network traffic — no telemetry, no automatic update polling, no thumbnail prefetch unless the user opts in per case.

### Non-goals

- We are **not** a CLI. Don't build one. The Docker container exposes only an HTTP server and a browser UI.
- We are **not** a yt-dlp competitor. We wrap, we don't reinvent.
- We are **not** distributing yt-dlp. We pull it at container build time and update it at runtime.
- We are **not** a forensic disk-acquisition or anti-tamper-hardware tool. The container's evidence guarantees are software-level (hashes, signatures, audit logs); they do not substitute for offline sealed-storage handling for high-stakes legal proceedings.
- We are **not** a multi-user or networked-collaboration tool in v1. One investigator, one laptop. Sharing happens via signed evidence-export bundles.
- We are **not** a general web crawler. Captures are always single-page (with sub-resources), never site-wide.

---

## 2. Architecture (read this before designing anything)

```
┌──────────────────────────────────────────────────────────────┐
│  User's browser (http://localhost:8080)                      │
│  ─ Static frontend: HTML + Tailwind + Alpine.js              │
│  ─ Visual-first UI; icons (Lucide) over text labels          │
│  ─ Talks to backend via REST + Server-Sent Events            │
│  ─ Single-purpose downloader UI (Settings reachable from header) │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  Docker container (single image, ~1.7GB; investigator-grade) │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  FastAPI backend (Python 3.12)                         │  │
│  │  ─ /api/cases             POST/GET — list + create     │  │
│  │  ─ /api/jobs/batch        POST — submit captures       │  │
│  │  ─ /api/jobs/{id}/events  SSE — live progress          │  │
│  │  ─ /api/library           GET — browse captures        │  │
│  │  ─ /api/library/verify    POST — re-hash + sig check   │  │
│  │  ─ /api/cases/{id}/export POST — signed zip + PDF      │  │
│  │  ─ /api/audit             GET — append-only audit log  │  │
│  │  ─ /api/cookies           POST — upload cookies.txt    │  │
│  │  ─ /api/cookies/json      POST — extension cookies     │  │
│  │  ─ /api/system/version    GET                          │  │
│  │  ─ /api/system/profile    GET/PUT — speed profile      │  │
│  │  ─ /api/system/reveal     POST — open folder           │  │
│  │  ─ /api/system/update     POST — only when user clicks │  │
│  │  ─ /api/i18n/{lang}       GET                          │  │
│  │  ─ /api/extension/*       pair / capture / cases       │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Capture pipeline (per job, sequential, every URL):    │  │
│  │   1. URL classification:                               │  │
│  │      ─ resolve final URL + redirect chain              │  │
│  │      ─ identify platform (incl. social-media match)    │  │
│  │      ─ if social, attach case cookies for that domain  │  │
│  │   2. Page capture (Playwright + browsertrix-crawler):  │  │
│  │      MHTML, full-page PNG, WARC (scope=page+resources) │  │
│  │   3. Media download (yt-dlp subprocess) — may yield    │  │
│  │      zero files; that is fine, the capture is page+    │  │
│  │   4. Post-processor:                                   │  │
│  │      a. Compute MD5 + SHA-256 of every artifact        │  │
│  │      b. Rename media (if any) to canonical filename    │  │
│  │      c. Write sidecars (.info.json, .meta.json,        │  │
│  │         .checksums.txt, .description, .thumbnail,      │  │
│  │         .mhtml, .screenshot.png, .warc.gz)             │  │
│  │      d. Sign meta.json (Ed25519, detached → .sig)      │  │
│  │      e. Insert row into SQLite library DB              │  │
│  │      f. Append entry to audit_log (hash-chained)       │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  Bind mounts:                                                │
│   /downloads  → host folder (organized as /downloads/{case}) │
│   /config     → host folder (DB, settings, keys/, cookies)   │
└──────────────────────────────────────────────────────────────┘
```

**Why this stack:**
- **FastAPI + SSE** — async, simple, real-time progress without WebSocket complexity.
- **SQLite** — file-based, zero-config, perfect for single-investigator libraries up to tens of thousands of items.
- **Tailwind + Alpine.js** — no build step required, ships as static files, designer-friendly. (If richer interactions are needed, prefer htmx over a heavy SPA framework. Do not introduce React/Vue/Svelte without asking.)
- **Lucide icons** — consistent, free, ~1500 icons, easy to color and resize, RTL-aware where needed.
- **Playwright (Chromium)** — stable headless capture for MHTML and full-page screenshots; takes the same cookies as yt-dlp for authenticated sessions.
- **browsertrix-crawler** — produces forensic-grade WARC files with the same Chromium engine; scoped to `page+resources` so we capture the source page and every sub-resource it loaded, but not the entire site.
- **Ed25519 signing** — small keys, fast, widely supported. `cryptography` library provides everything.
- **Single container** — easier for users than docker-compose with multiple services. The image lands at ~1.7 GB on disk (~430 MB shipped, gzipped); we use `chromium --only-shell` so we ship the headless-shell variant only and skip the full headed Chromium.

---

## 3. Cross-platform requirements (Windows + macOS)

This is the most common source of bugs in Dockerized GUI tools. Follow these rules:

- **Use `linux/amd64` and `linux/arm64` multi-arch images.** Apple Silicon Macs need arm64 natively or Rosetta will slow downloads — and Playwright/Chromium — dramatically. The reproducible build script `scripts/build-dist.sh` drives `docker buildx` for both arches and produces per-arch image tags (`capsule:arm64`, `capsule:amd64`). Bundled-tar launchers (`dist/Capsule*/Capsule.{command,bat}`) run with the explicit per-arch tag, never `capsule:latest`, and force-reload the bundled tar whenever the loaded image's content digest doesn't match the one stamped into the launcher at build time. This is what stops a stale or wrong-arch `capsule:latest` from silently shadowing the right image (the Docker Desktop "AMD64" warning chip on Apple Silicon hosts is the symptom of that failure mode).
- **Bind-mount paths must be POSIX inside the container** (`/downloads`, `/config`) regardless of how the user mounts them on the host. Windows users will mount `C:\Users\foo\Documents\Capsule`, Mac users will mount `/Users/foo/Documents/Capsule` — both must map to `/downloads` inside.
- **Filename sanitization must satisfy the strictest host filesystem,** which is NTFS/exFAT on Windows. That means: no `< > : " / \ | ? *`, no trailing spaces or dots, no reserved names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`), and a 255-character limit per path component. Apply this sanitizer **even on Mac**, so a library copied between machines stays portable.
- **Never write absolute host paths into the database, sidecars, audit log, or evidence export.** Always store paths relative to `/downloads`. The host path differs per machine and leaks user-environment detail into evidence.
- **Line endings:** sidecar `.txt` files use `\n` (LF). Document this in the README so Windows users opening in Notepad know to use VS Code or Notepad++.
- **Default port 8080.** If unavailable, the docker run command should let users remap (`-p 9090:8080`).
- **Time zone:** the database, audit log, sidecars, and evidence-export manifests use **UTC ISO 8601** strings exclusively. The UI displays the user's local time zone via `Intl.DateTimeFormat`. Never store local-time strings.
- **Container size warning:** the image is ~1.7 GB on disk; the dist bundle that ships to users is ~430 MB per architecture (gzipped tar inside the zip). Document this in the README so users on metered connections aren't surprised.
- **Provide both `docker run` (one-liner) and `docker-compose.yml`** in the README. Most users will copy-paste the one-liner.

---

## 4. The interface (this is the heart of the project — read carefully)

The investigator spends 100% of their time in this UI. It must feel like a polished, modern desktop app — not a "developer tool with HTML stapled on." Spend time here.

### 4.1 Visual-first design language (multilingual-resilient)

Because the UI must serve English, Japanese, Arabic, and (eventually) more — including right-to-left scripts — **rely on visuals first, words second**. Words translate; icons mostly don't need to.

Rules:

1. **Status is conveyed by icon + color + shape, not color alone.** Color is for emphasis; the icon and shape carry the meaning. Satisfies WCAG color-contrast and colorblind safety.
2. **Every status chip and button has an icon.** Use Lucide. Compact icon-only buttons are allowed in toolbars when the icon is unambiguous (folder, download, three-dots-menu). Provide accessible labels via `aria-label`.
3. **Platform icons for every recognized source.** YouTube, Twitter/X, TikTok, Instagram, Facebook, LinkedIn, Reddit, Vimeo, SoundCloud, Bandcamp, Bilibili, generic-web. Stored as monochrome SVGs in `static/icons/platforms/`, tinted via `currentColor`.
4. **Capture phases are visualized as a 4-step progress strip,** not a sentence. Four icons: globe (page), download-cloud (media), hash (#) (checksum), shield-check (sign). Each lights up as it completes. Universally legible.
5. **Library uses a thumbnail-dominant card grid,** not a table. Thumbnails are 16:9 (media) or page-screenshot crop (page-only). Title is a single line, ellipsized, with `<bdi>` for bidi safety. Platform icon and integrity badge in opposite corners.
6. **Empty states are illustrated**, not blank with text. Pick a small set of friendly line-illustrations for case-empty, library-empty, audit-empty, error states. Avoid faces with strong cultural cues.
7. **Forms minimize text.** Placeholders inside inputs replace external labels where the icon is clear (a search input with a magnifier icon needs no "Search" label). External labels appear when the field is unfamiliar.
8. **Tooltips are not a replacement for clarity.** Don't bury essential information in hover state — tooltips are unavailable on touch and to keyboard-only users on many platforms. Use them only as supplements.
9. **Animation is purposeful and subtle.** Progress strip fills, integrity badge transitions from pending to verified, page snapshot fades in. Respect `prefers-reduced-motion`.
10. **Typography:** Inter for Latin, Noto Sans Arabic for Arabic. Bundled as webfonts. Set `font-feature-settings` for Arabic and proper number rendering.

### 4.2 Design principles (general)

1. **Calm, not loud.** Neutral palette (slate/zinc base) with a single accent color (teal-600). No rainbow gradients, no neon, no emoji-as-UI.
2. **One primary action per screen.** The home view's primary action is "Capture a link." Everything else is secondary.
3. **Whitespace is free.** Use it generously. Cramped UIs feel like CLIs in disguise.
4. **Progressive disclosure.** Format selection, quality, subtitles, etc. live behind an "Advanced" toggle, collapsed by default.
5. **No jargon in default views.** Investigators are domain experts in their work, not in codecs. Avoid "codec," "container," "muxer," "extractor" outside Advanced.

### 4.3 Screen inventory

The v1 UI has two surfaces: the **Downloader** (home) and **Settings**. Backend resources (cases, library, item detail, audit log) are not exposed as UI; investigators reach the captured files via the host filesystem and the API. Onboarding has been omitted — the app opens directly on the downloader.

- **Downloader (home).** Hero illustration + URL form (single-link or multi-line list, segmented pill toggle) + slow/fast speed pill + active-jobs panel (4-icon capture-phase progress strip per job, with retry/failure banners) + "where the files live" panel (host-side path with copy + open-folder buttons) + recent-captures **list** (one row per capture: platform icon, title, source-URL host on a secondary line, capture-kind chip, integrity badge, relative time, hover-revealed open-folder action; "Clear list" affordance gated by a destructive-action confirmation dialog per §15). The home view's recent strip is intentionally denser than the Library's thumbnail grid (§4.1 #5) — different surfaces, different goals: the home strip is an audit log of recent activity, the Library (no v1 UI yet) is for browsing.
- **Settings.** Language picker, signing-key fingerprint card, browser-extension pairing (collapsible install instructions + token issuance + paired-list management), yt-dlp updater. Reachable via the settings cog in the header.

### 4.4 Update prompt UX

- **No automatic version polling.** The app does not call out to GitHub on its own. Period.
- The Settings screen and the About screen each have a **"Check for updates" button.** Clicking it makes a single GitHub release-API call and shows the result.
- If a download fails with a known "extractor outdated" error pattern, surface a **contextual** "Check for yt-dlp update?" prompt next to the failed job — but require the user to click it; do not auto-check.
- Updating runs `pip install --upgrade yt-dlp` inside the container. Show a progress modal. On completion, show new version and a "Changelog" link.
- **Never auto-update.** Users with active captures could lose them, and silent updates would compromise the audit trail.

### 4.5 Internationalization (i18n)

The interface must be trivial to translate. **First-tier languages: English (`en`), Japanese (`ja`), and Arabic (`ar`).**

- **All user-facing strings live in `/app/i18n/{lang}.json` flat key-value files.** No nested objects, no plurals-as-objects — use [ICU MessageFormat](https://unicode-org.github.io/icu/userguide/format_parse/messages/) for plurals and interpolation. Arabic has six plural forms (`zero`, `one`, `two`, `few`, `many`, `other`); the runtime must handle them all.
- **Keys are dotted, semantic, English.** `home.url_input.placeholder`, not `t1` or `pasteHere`.
- **No string concatenation in code.** Never `"Downloaded " + count + " files"`. Always `t("library.downloaded_count", { count })`.
- **No strings in HTML attributes without a key.** All `placeholder`, `aria-label`, `title` attributes go through translation.
- **Right-to-left support is first-class, not a retrofit.** `<html dir>` is set from the active locale. CSS uses logical properties (`ms-*`, `me-*`, `ps-*`, `pe-*`, `start-*`, `end-*`) — never `ml`/`mr`/`left`/`right` for layout. Direction-implying icons (chevrons, progress arrows) mirror in RTL. Tailwind's `rtl:` variants are enabled.
- **Bidi text is normal.** User-content fields (titles, uploaders, URLs) appear inside `<bdi>` elements so a Latin URL inside an Arabic UI stays readable.
- **Date/number/filesize formatting** uses `Intl.DateTimeFormat` / `Intl.NumberFormat` — never hand-rolled. Arabic locales default to Arabic-Indic digits; do not override.
- **Translation bundle is fetched once on load** from `/api/i18n/{lang}` and cached. Language switch is instant (no reload).
- **Frontend i18n runtime: [`@formatjs/intl-messageformat`](https://formatjs.github.io/)**. Backend (for error responses and PDF reports): [Babel](https://babel.pocoo.org/). Both speak the same ICU syntax; `en.json` is shared.
- **Arabic font: Noto Sans Arabic**, bundled as a webfont alongside Inter for Latin. **Japanese font: Noto Sans JP**, bundled as a webfont alongside Inter for Latin and Noto Sans Arabic.
- **CI check:** a grep step fails the build if any visible HTML text or Python error string is hardcoded English instead of going through the translation layer.
- Document the translation workflow in `/docs/TRANSLATING.md`.

### 4.6 Accessibility

- Keyboard-navigable. Tab order makes sense in both LTR and RTL. Enter submits the URL form.
- Semantic HTML (`<button>`, `<nav>`, `<main>`), not `<div onclick>`.
- Color contrast meets WCAG AA. Status never relies on color alone — icon and shape always accompany.
- Respect `prefers-reduced-motion`.
- Respect `prefers-color-scheme` for the default theme.
- Icon-only buttons must have `aria-label`. SVGs without semantic content get `aria-hidden="true"`.

### 4.7 Error messages

The investigator is non-technical regarding terminal errors but **needs technical detail readily available** for bug reports, source-platform pushback, and court annexes. Every error surfaced in the UI is translated through `app/errors.py` into:

1. **A short, plain-language headline** (translatable, in `i18n/en.json` under the `errors.*` namespace).
2. **A likely cause** in one sentence.
3. **A suggested action** as a button when possible ("Check for yt-dlp update," "Try again," "Open logs," "Add cookies").
4. **A "Show technical details" expander** with a one-click copy button. Includes: source URL, timestamp (UTC), yt-dlp version, app version, full stderr, audit-log reference. Ready to paste into a bug report or court exhibit.

Error mapping table (seed; extend as needed):

| Pattern in yt-dlp output                              | Friendly headline                              | Suggested action                |
|-------------------------------------------------------|------------------------------------------------|---------------------------------|
| `Unsupported URL` / `No video formats found`          | "No media found on this page."                 | None — page snapshot still saved |
| `HTTP Error 403` / `Sign in to confirm`               | "This site is blocking the download."          | "Add cookies" or "Check for yt-dlp update" |
| `Video unavailable` / `Private video`                 | "This video is private or has been removed."   | None                            |
| `unable to download video data: HTTP Error 429`       | "The site is rate-limiting us. Wait a bit."    | "Try again in 5 minutes"        |
| `ERROR: ffmpeg not found`                             | "Internal error — please report this."         | "Open logs"                     |
| Network errors (`getaddrinfo`, `Connection refused`)  | "Can't reach the site. Check your connection." | "Try again"                     |

When in doubt: **"Something went wrong. We've saved the technical details for you."** Never show a raw Python traceback in the headline. Tracebacks go to `/config/logs/app.log` and the audit log entry, both linked from the error card.

Note: "no media found" is **not a fatal error** — the page snapshot is still preserved as a `page_only` capture.

---

## 5. Capture kinds, filename normalization, and path layout

Every capture produces, at minimum, a page snapshot package (MHTML + screenshot + WARC + meta + checksums + signature). A capture is `media` if yt-dlp also yields one or more media files; otherwise it is `page_only`.

### Path layout

```
/downloads/{case_slug}/{stem}/                              ← per-item folder
/downloads/{case_slug}/{stem}/{stem}.{ext}                  ← media file (if any)
/downloads/{case_slug}/{stem}/{stem}.meta.json              ← canonical record
/downloads/{case_slug}/{stem}/{stem}.meta.json.sig
/downloads/{case_slug}/{stem}/{stem}.checksums.txt
/downloads/{case_slug}/{stem}/{stem}.page.{mhtml,png,warc.gz}
/downloads/{case_slug}/{stem}/reports/                      ← human-readable PDFs
/downloads/{case_slug}/{stem}/reports/{stem}.manifest.pdf   ← per-item manifest PDF (full hashes, A4 landscape)
/downloads/{case_slug}/{stem}/reports/{stem}.report.pdf     ← per-item human-readable report PDF
...
```

Per-item folder keeps the case folder browsable: each capture is a single, self-contained folder with the media file, the page snapshot, and the forensic sidecars at the top tier, plus a `reports/` subfolder grouping the two human-readable PDFs so the case directory stays scannable. The PDFs render in the UI locale active at submission time (`lang` flows from the frontend through `JobBatch` → `JobOrchestrator.submit` → `CaptureInput.lang` → `pdf_report.render_item_{report,manifest}`).

### Canonical filename pattern (media kind)

```
{platform}__{uploader}__{title}__{upload_date}__{video_id}.{ext}
```

### Canonical stem pattern (page_only kind — no media file, but the sidecar folder still needs a stem)

```
{platform}__{page_title}__{capture_date}__{url_hash}
```

Where `url_hash` is the first 12 hex chars of `sha256(canonical(url_final))` — short enough to be readable, long enough to avoid collisions in a single case. The canonical form (see [`app/url_canonical.py`](app/url_canonical.py)) lowercases scheme/host, drops the fragment, strips a curated tracking-param list (`utm_*`, `fbclid`, `gclid`, `igshid`, `mc_eid`, `mc_cid`, `_ga`, `_gl`, `yclid`, `msclkid`, `ref`, `ref_src`, `ref_url`, `share_id`, `si`, `feature`, `mkt_tok`, `_hsenc`, `_hsmi`, `spm`, `scm`), normalizes the trailing slash, and sorts remaining query keys — so two paste-variants of the same URL collapse to the same dedup key. The originals (`url_submitted`, `url_final`) are always preserved verbatim in `meta.json`. When the user picks "Re-capture as new entry" in the §15 modal, the new sibling row's `url_hash` becomes `{base}__c{N+1}` (counter starts at 2) and `meta.json.force_recapture_index` records the integer index for forensic clarity.

### Sanitization rules

- **`platform`** — lowercase, ascii. From `extractor_key` mapped to a friendly name (`youtube`, `vimeo`, `twitter`, `tiktok`, `instagram`, `facebook`, `linkedin`, `reddit`, `soundcloud`, `bandcamp`, `bilibili`, `generic`). Maintain mapping in `app/platforms.py`. The same module exposes `is_social(domain)` for cookie-attachment logic in §11.
- **`uploader`** — sanitized channel/user name, truncated to 40 chars.
- **`title`** / **`page_title`** — sanitized, truncated to 80 chars. Preserve original capitalization.
- **`upload_date`** / **`capture_date`** — `YYYY-MM-DD`. Use yt-dlp's `upload_date` if present; otherwise download/capture date prefixed with `dl-` (e.g. `dl-2026-05-06`).
- **`video_id`** — yt-dlp's `id` field, raw. The unique anchor for media — never lose it.
- **`url_hash`** — first 12 hex chars of `sha256(canonical(url_final))` (see `app/url_canonical.py` for the canonicalization rules). The unique anchor for page-only captures and the dedup key against the `UNIQUE(case_id, capture_kind, url_hash)` constraint.
- **Separator is `__` (double underscore).** Visually distinct, never appears naturally in titles after sanitization, survives copy-paste.
- **Sanitization function** strips/replaces in this order: (1) Unicode NFKC normalize, (2) replace path-illegal chars with `-`, (3) collapse whitespace to single space, (4) strip leading/trailing whitespace and dots, (5) reject reserved Windows names, (6) truncate. Implement in `app/sanitize.py` with full test coverage including Arabic, Hebrew, and CJK fixtures.
- **Collisions:** if the canonical filename or stem already exists, append `__c2`, `__c3`, etc.

### Preservation rule (forensic — read carefully)

The canonical filename is for **portability and human navigation**. The forensic record lives in `meta.json` and the DB:

- Always preserve `title_original`, `uploader_original`, `description_original` (raw, unmodified, untruncated).
- Always preserve `url_submitted` (what the user pasted) and `url_final` (after redirects), plus `url_redirect_chain[]`.
- Always preserve HTTP response headers from yt-dlp's metadata fetch and from Playwright's page-load.
- Never modify the source media bytes. Suppress yt-dlp's metadata muxing (`--no-embed-metadata --no-embed-thumbnail --no-embed-subs`); if a video+audio merge is required, log the operation in the audit log with hashes of the input fragments AND the merged output.
- **Raw fragment retention is OFF by default.** The merged output is the canonical media file. Investigators who need fragment-level evidence can enable retention per case in case settings.

### Examples

```
youtube__veritasium__The_Most_Stubbornly_Misunderstood_Concept_in_Math__2024-08-12__abc123XYZ.mp4
twitter__Some_Important_Tweet_Title__dl-2026-05-06__a1b2c3d4e5f6        (page_only stem; no media file)
```

---

## 6. Item folder contents

For every capture, all files live together in `/downloads/{case_slug}/{stem}/`. The media file (if any) and every sidecar are stem-prefixed so they remain forensically identifiable when copied or extracted from the folder. The two human-readable PDFs live in a `reports/` subfolder so the case directory stays scannable; both are still referenced by hash in `meta.json` and therefore signed transitively via `meta.json.sig`.

| File                              | Always present? | Source / contents                                                                                                                                                                                                                                                                                                                                                                  |
|-----------------------------------|------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `reports/{stem}.manifest.pdf`     | Yes              | Locale-aware per-item evidence manifest (PDF, A4 landscape). Header with source URL, capture timestamp UTC, and signing-key fingerprint, then a table of every file in the item folder with **full** MD5 (32 hex) and **full** SHA-256 (64 hex) — verifier-ready, no truncation. Rendered in the UI locale active at submission time. Hash recorded in `meta.json` and `checksums.txt` and transitively signed via `meta.json.sig`. |
| `reports/{stem}.report.pdf`       | Yes              | Locale-aware per-item human-readable report (PDF). Provenance (URLs, redirects, capture timestamp UTC, uploader, title, upload date, duration, authenticated domains), full untruncated description (paginated), tools/versions table, and capture-side report (render-wait outcomes, blocked-request count, banner-hide flags, readiness, report locale). Companion to `reports/{stem}.manifest.pdf`. Rendered in the UI locale active at submission time. Hash recorded in `meta.json` and `checksums.txt` and transitively signed via `meta.json.sig`. |
| `{stem}.meta.json`         | Yes              | **Our** structured metadata. Includes capture_kind, filenames, original/final URLs, redirect chain, response headers, platform, uploader, title (sanitized + original), description, upload_date, capture_date (UTC), duration, format details, file sizes, MD5/SHA-256 of every artifact, app/yt-dlp/browsertrix/Chromium versions, signing key fingerprint, audit-log entry id, list of authenticated domains (no cookie values) |
| `{stem}.meta.json.sig`     | Yes              | Detached Ed25519 signature of `meta.json`                                                                                                                                                                                                                                                                                                                                          |
| `{stem}.checksums.txt`     | Yes              | Lines of `MD5  <hash>  <relpath>` and `SHA256  <hash>  <relpath>` for every artifact (compatible with `md5sum -c` / `sha256sum -c`)                                                                                                                                                                                                                                              |
| `{stem}.page.mhtml`        | Yes              | Single-file MHTML snapshot of the source page at capture time (Playwright)                                                                                                                                                                                                                                                                                                         |
| `{stem}.page.png`          | Yes              | Full-page screenshot at capture time (Playwright)                                                                                                                                                                                                                                                                                                                                  |
| `{stem}.page.warc.gz`      | Yes              | WARC archive of source page + every sub-resource (browsertrix scope=`page+resources`)                                                                                                                                                                                                                                                                                              |
| `{stem}.info.json`         | Media kind only  | yt-dlp's full `--write-info-json` output, untouched                                                                                                                                                                                                                                                                                                                                |
| `{stem}.description.txt`   | Media kind only  | Video description, plain text, LF line endings (yt-dlp `--write-description`)                                                                                                                                                                                                                                                                                                      |
| `{stem}.thumbnail.{ext}`   | Media kind only  | Best available thumbnail (yt-dlp `--write-thumbnail`)                                                                                                                                                                                                                                                                                                                              |
| `{stem}.{lang}.vtt`        | When requested   | Subtitles per language (yt-dlp `--write-subs`)                                                                                                                                                                                                                                                                                                                                     |
| `{stem}.user-browser.tab-context.json`    | Extension live capture | Investigator's UA / viewport / scroll / timezone / referrer / color-scheme. The backend canonical capture mirrors these fields. (v2)                                                                                                                                                                                                          |
| `{stem}.user-browser.session-state.json`  | Extension live capture | Per-origin localStorage and sessionStorage. Some sites carry session JWTs in localStorage; without this the backend re-fetch may render as logged-out even with valid cookies. (v2)                                                                                                                                                            |
| `{stem}.user-browser.dom-snapshot.html`   | Extension live capture | Click-time `document.documentElement.outerHTML` from the user's authenticated browser. Distinct from the Playwright MHTML — locks in exactly what the investigator was looking at. (v2)                                                                                                                                                       |
| `{stem}.user-browser.dom-snapshot.json`   | Extension live capture | Structural counts that go with the DOM HTML (node count, iframe count, video count, image total + visible). (v2)                                                                                                                                                                                                                              |

`{stem}.meta.json` is the canonical record. Schema lives at `/app/schemas/meta.schema.json` and is versioned (`"schema_version": 4`). When the schema changes, write a migration. v2 (hardening pass) adds:

- `capture` — the capture report: render-wait outcomes (`load`, `fonts`, `images`, `video`, `lazy_load`, `networkidle`), blocked-request count + sample, `blocklist_version`, `banner_hide_applied`, `banner_hide_version`, `tab_context_used`.
- `cookies_snapshot_sha256` — SHA-256 of the cookies file the job consumed; binds the capture to the exact cookie set without ever logging values.
- `ephemeral_cookies_used` — true iff the job used a one-shot ephemeral cookie file (extension-supplied, never persisted to the case directory).

v3 (Track A) adds the `manifest_pdf` artifact role + checksum and `capture.report_lang`. v4 adds the `report_pdf` artifact role + checksum (the per-item human-readable report PDF). Both PDFs are referenced by hash in `meta.json` and therefore transitively signed by `meta.json.sig` — no extra signing path required.

The new sidecars are referenced by hash in `meta.json` and therefore transitively signed by `meta.json.sig` — no extra signing path required.

---

## 7. Integrity: checksums and signatures

### Checksums

- Compute **MD5 and SHA-256** of every artifact (media file when present, MHTML, screenshot, WARC, every sidecar except `meta.json.sig`) after all post-processing finalizes.
- Store in: `meta.json`, the `checksums.txt` sidecar, and the SQLite library row.
- Use `hashlib` with chunked reads (1 MB chunks). Never load whole files into memory.

### Signatures

- On first launch, generate an **Ed25519** keypair using `cryptography.hazmat.primitives.asymmetric.ed25519`. Store `private_key.pem` (0600) and `public_key.pem` (0644) in `/config/keys/`.
- Sign every `meta.json` with a detached signature → `meta.json.sig`.
- Sign every evidence-export `manifest.json` with a detached signature → `manifest.sig`.
- Public key fingerprint is shown in Settings and on every evidence export.
- Investigators who want a stable cross-device key can import their own keypair from Settings → Signing key → Import. Replacing the key does **not** re-sign existing items; it applies to future captures only. The audit log records the change.
- RFC 3161 trusted timestamping is **not** in v1 (requires per-capture network calls to a TSA — incompatible with the "no unsolicited network traffic" stance). Tracked as a v2 opt-in feature.

### Verification

- Library has a per-item "Verify" action: re-hashes every artifact, checks signature, reports.
- Library has a "Verify all" bulk action.
- Mismatches surface as red integrity badges with full diff (expected vs. actual hashes, signature failure reason).
- A standalone `verify.py` is bundled with every evidence export so recipients can check signatures without installing this app — only `cryptography` is required.

---

## 8. Audit log (tamper-evident)

Every meaningful operation is recorded in an append-only `audit_log` table:

```sql
CREATE TABLE audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,             -- ISO 8601 UTC
    action        TEXT NOT NULL,             -- e.g. 'capture.started', 'page.captured', 'media.hashed', 'sig.created', 'item.verified', 'case.exported', 'mode.changed', 'key.imported'
    case_id       INTEGER,
    download_id   INTEGER,
    actor         TEXT NOT NULL,             -- 'system' or 'user' (single-user, but explicit)
    details_json  TEXT NOT NULL,             -- structured details (cookie values NEVER included)
    prev_hash     TEXT NOT NULL,             -- SHA-256 of previous row's canonical encoding (or all-zeros for row 1)
    row_hash      TEXT NOT NULL              -- SHA-256 of this row's canonical encoding incl. prev_hash
);
```

**Canonical encoding** is `JSON.dumps(row, sort_keys=True, separators=(",", ":"))` minus the `row_hash` field itself. Verifying the chain re-derives each `row_hash` and confirms continuity.

**Cookie-value leak guard.** `audit.append()` rejects any `details` key whose lowered name *contains* `"cookie"` (catches `cookie`, `cookies`, `set_cookie`, `Set-Cookie`, `cookies_raw`, `cookieJar`, etc. at any depth) — except for the small spec-blessed metadata allow-list `{cookie_domains, cookie_persistence, cookies_snapshot_sha256}` per §11. A regression that tries to write a credential-bearing key fails fast with `DetailLeakError` rather than silently leaking into evidence.

The audit log is **not** exposed in the UI in v1. Investigators get the full log on disk (`/config/library.db`, `audit_log` table), via `/api/audit`, and inside every evidence-export bundle (`audit_log.json`).

---

## 9. Library database (SQLite)

Path: `/config/library.db`. Single source of truth for cases and downloads.

### Tables

```sql
CREATE TABLE cases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL UNIQUE,    -- filesystem-safe, matches /downloads/{slug}/
    name            TEXT NOT NULL,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'closed' | 'archived'
    created_at      TEXT NOT NULL,           -- UTC
    updated_at      TEXT NOT NULL,
    settings_json   TEXT NOT NULL DEFAULT '{}'  -- per-case prefs: thumbnail_prefetch, retain_raw_fragments
);

CREATE TABLE downloads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id         INTEGER NOT NULL REFERENCES cases(id),
    job_uuid        TEXT NOT NULL UNIQUE,
    capture_kind    TEXT NOT NULL,           -- 'media' | 'page_only'
    source_url      TEXT NOT NULL,           -- url_submitted (what user pasted)
    final_url       TEXT,                    -- url_final (after redirects)
    platform        TEXT NOT NULL,
    video_id        TEXT,                    -- nullable; null for page_only
    url_hash        TEXT NOT NULL,           -- sha256(final_url) first 12 hex; used for de-dup
    uploader        TEXT,
    title           TEXT NOT NULL,           -- sanitized
    title_original  TEXT NOT NULL,           -- raw, unmodified
    upload_date     TEXT,                    -- ISO 8601 date (media kind)
    capture_date    TEXT NOT NULL,           -- ISO 8601 datetime UTC
    relative_path   TEXT,                    -- relative to /downloads (null for page_only — no media file)
    sidecar_dir     TEXT NOT NULL,           -- relative to /downloads, always present
    file_size_bytes INTEGER,                 -- size of media file; null for page_only
    md5             TEXT,                    -- of media file; null for page_only
    sha256          TEXT,                    -- of media file; null for page_only
    duration_seconds INTEGER,
    ytdlp_version   TEXT NOT NULL,
    chromium_version TEXT NOT NULL,
    browsertrix_version TEXT NOT NULL,
    app_version     TEXT NOT NULL,
    signing_key_fp  TEXT NOT NULL,
    meta_json       TEXT NOT NULL,           -- the full meta.json blob
    UNIQUE(case_id, capture_kind, url_hash)  -- de-dup within a case (covers both kinds)
);

CREATE INDEX idx_downloads_case_id ON downloads(case_id);
CREATE INDEX idx_downloads_platform ON downloads(platform);
CREATE INDEX idx_downloads_capture_date ON downloads(capture_date);
CREATE INDEX idx_downloads_video_id ON downloads(video_id);
CREATE INDEX idx_downloads_url_hash ON downloads(url_hash);

-- audit_log: see §8
```

### Export

`/api/cases/{id}/export` produces the signed zip described in §10. `/api/library/export?format=csv|json` streams the entire library across cases (CSV uses RFC 4180 quoting; full JSON includes `meta_json`). The export is the investigator's escape hatch — they should be able to leave this app at any time with their full history intact, machine-readable, and signed.

---

## 10. Evidence export (signed zip + PDF report)

Per-case export bundle:

```
{case_slug}_{export_timestamp_utc}.zip
├── manifest.json                     ← list of every file with role, size, MD5, SHA-256
├── manifest.sig                      ← detached Ed25519 signature of manifest.json
├── public_key.pem                    ← so recipient can verify
├── case_report.pdf                   ← human-readable, locale-aware (RTL Arabic capable)
├── audit_log.json                    ← full audit-log entries for this case
├── verify.py                         ← standalone verifier (only `cryptography` dependency)
├── README.txt                        ← short instructions for the recipient
└── downloads/
    └── {stem}/                                 ← per-item folder
        ├── {stem}.{ext}                        ← media file (present for media kind)
        ├── {stem}.meta.json
        ├── {stem}.meta.json.sig
        ├── {stem}.checksums.txt
        ├── {stem}.page.mhtml
        ├── {stem}.page.png
        ├── {stem}.page.warc.gz
        ├── reports/
        │   ├── {stem}.manifest.pdf             ← per-item manifest PDF
        │   └── {stem}.report.pdf               ← per-item human-readable report PDF
        └── ...                                 ← media-only sidecars (info.json, description.txt, thumbnail) per §6
```

### PDF report

- Generated with **WeasyPrint** (HTML→PDF; handles RTL natively, supports Noto Sans Arabic).
- Sections: case metadata, per-item details (thumbnail or page screenshot, source URL, capture timestamp UTC, upload date, hashes, signature status, capture-kind badge), tool versions table, public-key fingerprint, signature footer.
- Locale follows the active UI language at export time.
- Visual-first: the report uses the same icon set as the UI for capture-kind, platform, and integrity status, so a non-English-reading recipient can still parse the structure.

### Verifier script

`verify.py` is a ~100-line standalone Python script. Given the export folder, it:
1. Parses `manifest.json`, verifies `manifest.sig` with `public_key.pem`.
2. Re-hashes every file and compares to manifest.
3. Verifies each `meta.json.sig` against its `meta.json`.
4. Verifies the audit-log hash chain.
5. Prints PASS/FAIL with details.

The script is checked into the repo at `app/templates/verify.py.tmpl` and copied verbatim into each export.

---

## 11. Cookies & authenticated sessions

Investigators commonly need cookies/logged-in sessions. The cookies workflow is a **primary feature**, not Advanced.

- **Recommended path: the Capsule browser extension.** Pair it with the Capsule UI; click "Send this tab" and the extension iterates every cookie store (default + container + partitioned), strips the values to a Netscape file the backend writes, and submits the URL as a job. The extension handles HttpOnly cookies (which `document.cookie` cannot expose) and partitioned third-party cookies, and runs a pre-capture readiness gate so the live snapshot reflects a stable page.
- Fallback: per-case cookies file at `/config/cases/{case_slug}/cookies.txt` (Netscape format, 0600). Upload via UI: case detail → Cookies tab → upload `cookies.txt`. Same downstream consumers — yt-dlp, Playwright, browsertrix all read the same file.
- **Auto-attach for social-media domains.** When a pasted URL matches a domain that has cookies in the active case, the UI shows an "Authenticated as {domain}" chip on the capture preview, and the cookies are passed to **both yt-dlp and Playwright/browsertrix**. This ensures the page snapshot and the media come from the same authenticated session. The list of social-media domains is maintained in `app/platforms.py` (`is_social(domain)`), covering at minimum: Twitter/X, Facebook, Instagram, TikTok, LinkedIn, Reddit, YouTube (private/age-gated), Threads.
- **Ephemeral cookies (one-shot).** The extension popup exposes a per-submission "Ephemeral cookies" toggle. When set, cookies ride to the backend in a per-job tmpdir, are used by Playwright/browsertrix/yt-dlp for that one job, and are discarded after the job ends — never written to the case directory. The audit log records `cookies.ephemeral_used` with the snapshot hash, never values.
- **Freshness validation.** At job start, the backend hashes the cookies file (`cookies_snapshot_sha256`, recorded in `meta.json` and the audit log) and reports any expired or expiring-soon domains. Stale cookies are still attached (we don't second-guess the user) but the audit log gets a `cookies.stale_at_capture` entry and the SSE stream emits a warning event.
- Cookies are **never logged**, **never included in evidence exports**, and **never echoed in audit-log details**. Only the list of authenticated domains, the cookie-set SHA-256, and the persistence mode are logged.
- Provide `/docs/COOKIES.md` explaining how to export cookies from common browsers via established extensions (the Capsule first-party extension is the recommended path; the legacy `cookies.txt` upload remains for users who prefer it).

---

## 12. Project layout

```
Capsule/
├── CLAUDE.md
├── README.md
├── LICENSE
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── pyproject.toml
├── app/
│   ├── __init__.py
│   ├── main.py                ← FastAPI app
│   ├── config.py              ← env vars, defaults
│   ├── db.py                  ← SQLite access
│   ├── cases.py               ← case CRUD + filesystem layout
│   ├── jobs.py                ← job queue, lifecycle
│   ├── classify.py            ← URL classification: redirects, platform, social-cookie match
│   ├── ytdlp_runner.py        ← subprocess wrapper, progress parsing
│   ├── capture.py             ← Playwright (MHTML, screenshot) + browsertrix (WARC)
│   ├── postprocess.py         ← rename, hash, sidecars, signing, DB insert
│   ├── sanitize.py            ← filename sanitization
│   ├── platforms.py           ← extractor_key → friendly name; is_social(domain)
│   ├── signing.py             ← Ed25519 keygen, sign, verify
│   ├── audit.py               ← audit log writer + chain verifier
│   ├── evidence_export.py     ← manifest + zip + PDF + verify.py copy
│   ├── pdf_report.py          ← WeasyPrint case report
│   ├── cookies.py             ← per-case cookie management
│   ├── updater.py             ← yt-dlp version check (manual only)
│   ├── errors.py              ← yt-dlp output → friendly key mapping
│   ├── i18n.py                ← translation bundle loader
│   ├── schemas/
│   │   └── meta.schema.json
│   ├── i18n/
│   │   ├── en.json
│   │   ├── ja.json
│   │   ├── ar.json
│   │   └── ...
│   ├── templates/
│   │   ├── verify.py.tmpl     ← copied into evidence exports verbatim
│   │   ├── case_report.html   ← WeasyPrint source (case-level export PDF)
│   │   └── item_manifest.html ← WeasyPrint source (per-item manifest PDF)
│   └── static/                ← frontend
│       ├── index.html
│       ├── app.js
│       ├── styles.css
│       ├── fonts/
│       │   ├── Inter-*.woff2
│       │   └── NotoSansArabic-*.woff2
│       └── icons/
│           ├── lucide/        ← bundled subset
│           ├── platforms/     ← youtube.svg, twitter.svg, ...
│           └── illustrations/ ← onboarding, empty states
├── tests/
│   ├── test_sanitize.py
│   ├── test_postprocess.py
│   ├── test_db.py
│   ├── test_signing.py
│   ├── test_audit.py
│   ├── test_evidence_export.py
│   ├── test_classify.py
│   └── fixtures/
└── docs/
    ├── TRANSLATING.md
    ├── COOKIES.md
    ├── VERIFYING_EVIDENCE.md  ← for recipients of evidence bundles
    ├── DESIGN.md              ← visual language, icon usage, illustrations
    ├── ARCHITECTURE.md
    └── SCREENSHOTS/
```

---

## 13. Working agreements for Claude Code

When working on this project:

1. **Read the relevant section of this file before starting any task.** Filename work → re-read §5. UI work → §4. Integrity work → §7 + §8. Cookies/auth → §11.
2. **Ask before adding dependencies.** Every dependency is a maintenance cost, a translation/security surface, and a piece of the chain of custody. Justify each one.
3. **Prefer the standard library.** Capture pipeline necessarily depends on Playwright + browsertrix-crawler + WeasyPrint + cryptography; everything else should lean on `pathlib`, `hashlib`, `sqlite3`, `subprocess`, `asyncio`.
4. **Tests for any logic that touches integrity, filenames, or evidence.** `sanitize.py`, `postprocess.py`, `signing.py`, `audit.py`, `evidence_export.py`, `classify.py` need full coverage. Use `pytest`.
5. **Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/).** `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.
6. **Never hardcode user-facing strings outside `i18n/{lang}.json`.** This is the single rule most likely to be violated; check yourself and the CI grep.
7. **No silent network calls.** No telemetry, no automatic update polling, no thumbnail prefetch unless the user opted in. If you write code that calls out to the internet, it must be triggered by an explicit user action and must appear in the audit log.
8. **Visual-first UI.** When you reach for a string, ask whether an icon plus a translatable `aria-label` would do better. Use Lucide icons consistently. Status uses icon + color + shape, never color alone.
9. **UI vs. backend separation.** The v1 UI surfaces only the downloader and Settings. The backend keeps full case/library/audit/evidence-export functionality intact (those endpoints are still load-bearing — the downloader uses them under the hood). When adding a feature, decide whether it needs UI: if not, leave it as an API-only capability and document it in `/docs/`.
10. **Treat the UI as a first-class deliverable.** A scratch HTML file with three buttons is not acceptable. If you're unsure how to make it look good, stop and ask for design direction.
11. **Document as you go.** Public functions get docstrings. The README updates when behavior changes. Integrity-affecting changes update `/docs/VERIFYING_EVIDENCE.md`. Visual-language changes update `/docs/DESIGN.md`.
12. **When in doubt, choose boring.** Boring is debuggable, translatable, and shippable.
13. **Preserve, don't modify.** Suppress yt-dlp's metadata muxing. When transformation is unavoidable (e.g., merging separate video+audio streams), log it in the audit log with input and output hashes.
14. **Capture-side mutations are recorded, never silent.** Network blocks (ad/tracker blocklist) and CSS-only banner hides are explicitly recorded in `meta.json.capture.*` and the audit log (`capture.ads_blocked`, `capture.banners_hidden`, `capture.readiness_timed_out`). Mutations that touch the DOM — auto-clicking consent banners, removing tracker pixel elements, modifying the source HTML — remain forbidden. CSS hiding is OK because the underlying DOM is preserved in the MHTML and WARC; clicking would change the site's consent state and we don't.
15. **When changing on-disk layout, update §5 and §6 in lockstep with the code.** The path layout diagram, the item-folder contents table, and the post-processor implementation must agree. Drift here breaks evidence-export bundles and the verifier; reviewers should reject layout changes that touch only one side.

---

## 14. README requirements (audience: investigators, not developers)

The README is the user's first contact. Write for an investigator who has never opened a terminal:

- **Top of the page:** a screenshot of the downloader home view, not a feature list.
- **Install section starts with "Install Docker Desktop,"** with separate links and one-line descriptions for Windows and macOS. One sentence: "Docker is a free tool that lets this app run on your computer without you needing to install Python, browsers, or anything else."
- **One copy-paste command to run the app.** Format as a fenced block. The command must:
  - Pull the image from a registry (we'll publish to GHCR or Docker Hub).
  - Mount a host folder for downloads (`~/Documents/Capsule` on Mac, `%USERPROFILE%\Documents\Capsule` on Windows).
  - Mount a config folder.
  - Map port 8080.
  - Use `--restart unless-stopped` so it survives reboots.
- **Image size note:** "The first launch unpacks about 1.7 GB on your disk; the actual download is a ~430 MB gzipped bundle. The app bundles a headless Chromium engine so your captures are forensically complete."
- **"Then open http://localhost:8080 in your browser."** Bold, on its own line.
- **A short "What this tool gives you" section** for the audience: page snapshot + screenshot + WARC for every URL, optional media download, canonical filenames, signed evidence bundles, manual update control. Three sentences each, no jargon.
- **Troubleshooting section** for the three most common failures: port already in use, folder permission denied, Docker Desktop not running.
- **Screenshots throughout.** Live in `/docs/SCREENSHOTS/` and referenced with relative paths.
- **No mentions of `pip`, `venv`, `git clone`, or `python` in the install path.** Those belong in `/docs/CONTRIBUTING.md`.
- **A "How a recipient verifies your evidence" pointer** linking to `/docs/VERIFYING_EVIDENCE.md`.

---

## 15. Resolved decisions

- **Project name:** Capsule. Tagline: *Capture the web, with proof.* Docker image: `capsule`.
- **Frontend stack:** Tailwind + Alpine.js (no build step at runtime; Tailwind compiled at Docker build time).
- **Icon set:** Lucide. Custom platform-mark SVGs in `static/icons/platforms/`.
- **Illustrations:** [unDraw](https://undraw.co/) — single-color, recolorable, professional-neutral. Tinted to active accent.
- **Audience:** investigators (researchers, journalists, lawyers, legal-discovery practitioners). Court admissibility is a goal.
- **Threat model:** safe (no Tor/proxy/at-rest-encryption in v1).
- **Multi-user:** no. Single investigator, single laptop. Sharing via signed evidence bundles.
- **Cases:** first-class on the backend (filesystem grouping under `/downloads/{case_slug}/`). The downloader uses a default case implicitly; v1 has no UI to switch cases, but the API and on-disk layout still support arbitrary cases. **Default slug:** `downloads` for fresh installs (folder: `~/Documents/Capsule/downloads/`). **Forward-only legacy fallback:** existing installs that already have a `quick-captures` case row keep using it — `cases.ensure_default_case()` prefers `downloads` first, then falls back to `quick-captures` if that row exists, else creates `downloads`. No on-disk migration; the legacy slug name is preserved indefinitely for those users so existing evidence chains stay intact.
- **Accent color:** teal-600 throughout.
- **Concurrent captures:** default 2 (Chromium memory contention beyond that). Configurable in Settings.
- **Duplicate handling:** the duplicate-detection flow runs *before* the capture pipeline via `POST /api/jobs/preflight` — so duplicates surface as a modal in seconds, not after 30s of wasted yt-dlp + Playwright work. The modal shows the existing capture's preview (platform icon, title, capture date, source URL) and three buttons: "Open existing" (calls `/api/system/reveal` with the existing item's folder), "Re-capture as new entry" (re-submits with `force_recapture: true`; the new row's `url_hash` is suffixed `__c2`/`__c3`/…), "Cancel." Multi-duplicate batches queue with a "1 of N" indicator. Audit actions: `duplicate.detected` (preflight hit, before user choice), `duplicate.opened_existing`, `duplicate.recaptured` (logged from the orchestrator on the new row's successful finalize, with `original_id` and `new_id`), `duplicate.cancelled`. The legacy late-detection path (`postprocess.DuplicateCapture`) stays as defense in depth for callers that skip preflight (extension, races, future code).
- **Capture scope:** every URL gets the full preservation package; media is optional.
- **Cookies:** primary feature, per case, never logged, never exported. Auto-attach for social-media domains.
- **Page capture:** MHTML + full-page PNG + WARC (browsertrix scope=`page+resources`).
- **Output handoff:** signed zip + WeasyPrint PDF + bundled standalone `verify.py`.
- **Signing key:** auto-generated Ed25519 keypair on first run, importable, fingerprint exposed.
- **Updates:** manual only. No automatic GitHub polling.
- **Raw fragment retention:** off by default. Per-case opt-in.
- **Time:** UTC in storage; user's local TZ in display via `Intl.DateTimeFormat`.
- **First-tier languages:** English + Japanese + Arabic (`ar`, generic — revisit if specific dialect requested).
- **Image size:** ~1.7 GB on disk (down from ~2 GB after switching off the Playwright/python base image). The shipped dist bundle is a ~430 MB gzipped tar per architecture (see [scripts/build-dist.sh](scripts/build-dist.sh)). Documented in README.
- **Logo:** bell jar over a browser-window specimen on a plinth — preservation/museum metaphor, deliberately not a UI button. The mark stays neutral graphite; the accent dot inside the window's title bar uses the app accent (teal-600). Variants live under `app/static/icons/brand/` (`logo.svg`, `logo-mono.svg`, `logo-favicon.svg`, `logomark.svg`); the brand-mark section of `docs/DESIGN.md` is the canonical spec.

### Hardening pass (v0.2)

- **Browser extension is the recommended cookie path.** The legacy `cookies.txt` upload remains as a fallback for users who prefer it.
- **Ad/tracker blocking default ON** at both the backend Playwright capture and the extension's user tab. Single source-of-truth blocklist at `app/static/blocklists/easylist-essentials.json`; extension copy is byte-identical (asserted by `tests/test_blocklist.py`). Toggle: `case.settings_json.block_ads`.
- **Cookie/consent banner CSS-hide default ON** at the backend Playwright capture. CSS-only — DOM preserved in MHTML and WARC. Toggle: `case.settings_json.hide_cookie_banners`.
- **Auto-clicking banners forbidden.** CLAUDE.md §13 #14 codifies this.
- **Real HAR via `chrome.debugger`** is opt-in (default OFF). The yellow Chrome banner is intentional: elevated capability is visible to the user.
- **Render-wait default profile = "standard"** with caps: load (45s) → fonts (5s) → visible images (10s) → video readyState (8s) → lazy-scroll → networkidle (15s); 60s outer ceiling.
- **Extension-ID binding enforced for new tokens.** Tokens minted with `extension_id` reject requests whose `X-Extension-Id` header doesn't match. Legacy unbound tokens grandfathered.
- **Token rotation** via `POST /api/extension/pair/{token_id}/rotate` — new raw token issued, old revoked, label and binding carry over.
- **Ephemeral cookies opt-in per submission** via `cookie_persistence: "ephemeral"` on `POST /api/extension/capture`. One-shot tmpdir, never written to the case directory, discarded after job completion.
- **Cookie-set provenance hash** (`cookies_snapshot_sha256`) recorded per job in `meta.json` and the audit log.
- **New audit actions:** `extension.tab_context_received`, `extension.id_mismatch`, `extension.token_rotated`, `cookies.stale_at_capture`, `cookies.ephemeral_used`, `capture.ads_blocked`, `capture.banners_hidden`, `capture.readiness_timed_out`.
- **Per-item PDF manifest** at capture time, locale-aware, hashed in `meta.json` and signed transitively via `meta.json.sig`. Lives at `reports/{stem}.manifest.pdf` inside the per-item folder. As of schema v3, the file table emits **full** MD5 (32 hex) and SHA-256 (64 hex) — no truncation — and the page is A4 landscape so the hashes wrap inside their column without overflow.
- **Per-item PDF report** (`reports/{stem}.report.pdf`, schema v4) — locale-aware human-readable companion to the manifest PDF. Provenance + full untruncated description + tools/versions + capture report. Hashed and added to `artifacts["report_pdf"]` *before* the manifest PDF renders so the manifest's file table includes the report row, and `meta.json.sig` transitively binds both PDFs.
- **Layout:** per-item folder holds media, snapshot, and forensic sidecars at the root tier; the two human-readable PDFs are grouped under a `reports/` subfolder so the case directory stays scannable. Old `sidecars/{stem}/` subfolder removed.
- **PDF locale follows the active UI language at submission time.** The frontend submits `lang: this.locale` with each `/api/jobs/batch` body; `JobBatch.lang` flows through `JobOrchestrator.submit` → `Job.lang` → `CaptureInput.lang` → `pdf_report.render_item_{report,manifest}`. Falls back to `config.DEFAULT_LANG` when the caller (e.g. extension) omits the field.
- **Distribution: per-arch image tags + reproducible build script.** `scripts/build-dist.sh` drives `docker buildx` for both arm64 and amd64, saves the per-arch tars with their content digests, and renders launchers from `dist-templates/Capsule.{command,bat}.in`. Bundled-tar launchers run with the explicit per-arch tag (`capsule:arm64` / `capsule:amd64`) and force-reload the bundled tar whenever the loaded image's digest doesn't match the one stamped into the launcher at build time. Fixes the "AMD64" warning chip on Apple Silicon hosts.

### Simple-view consolidation (v0.3)

The v1 UI is intentionally limited to two surfaces (Downloader + Settings, §4.3). The backend HTTP API was pruned to the intersection of "live consumed by the simple-view UI or the extension" + "load-bearing per CLAUDE.md §2 / §7 / §10 / §11." Internal orchestrator capabilities (NetworkMonitor, pause/resume/cancel, capture groups, retries) are unchanged — only their HTTP surfaces went.

- **Removed HTTP routes (use the orchestrator or post-capture artifacts directly):**
  - `GET /api/cases/{id}`, `PATCH /api/cases/{id}`, `POST /api/cases/{id}/status` — the simple view fetches the case list and matches by slug; mutation belongs to a future case-detail UI.
  - `POST /api/jobs`, `GET /api/jobs`, `GET /api/jobs/{id}` — `POST /api/jobs/batch` is the only submission path; in-flight state lives in the SSE stream.
  - `POST /api/jobs/{id}/{pause,resume,cancel}`, `POST /api/jobs/{pause-all,resume-all}` — internal orchestrator methods retained; HTTP exposure not yet justified by UI. (`POST /api/jobs/preflight` was removed in v0.3 and re-added in v0.4 with new semantics — duplicate-detection probe ahead of the capture pipeline; see §"Dedup pass (v0.4)".)
  - `GET|PATCH /api/system/network`, `POST /api/system/network/probe` — `NetworkMonitor` still runs internally and auto-pauses jobs when offline; no HTTP surface.
  - `GET|PUT /api/cases/{id}/profile` — speed profile is global; `/api/system/profile` is the live UI hook.
  - `POST /api/library/{id}/refetch` — the underlying capture-group + `task_kind` mechanism is intact for orchestrator use.
  - `GET /api/cookies`, `POST /api/cookies/preview`, `POST /api/cookies/text` — `POST /api/cookies` (Netscape-file multipart) and `POST /api/cookies/json` (extension) are the two surviving cookie paths per §11.
- **Removed code:**
  - Root `Capsule.command` / `Capsule.bat` — superseded by the rendered per-arch launchers in `dist/Capsule*/` (see Distribution above).
  - `cases.QUICK_CASE_SLUG`, `cases.QUICK_CASE_NAME`, `cases.ensure_quick()` — deprecated aliases. The on-disk legacy `quick-captures` *slug fallback* inside `ensure_default_case()` is preserved indefinitely (existing evidence chains continue to resolve).
  - `ytdlp_runner.preflight()` — sole caller was the removed `/api/jobs/preflight` route.
  - `cookies.merge`, `cookies.merge_preview`, `cookies.save_merged`, `cookies.MergeStats` — sole callers were the removed `/api/cookies/text` and `/api/cookies/preview` routes.
- **Correctness fixes that landed alongside the prune:**
  - `signing.verify()` now narrows its catch to `cryptography.exceptions.InvalidSignature` only — corrupted-key failures propagate (CLAUDE.md §7).
  - `audit.append()` substring blocklist on cookie-bearing keys with a tiny spec-blessed allow-list (see §8).
  - `postprocess.finalize()` narrows its rollback `except` to `(OSError, sqlite3.Error, DuplicateCapture)` — unexpected failures preserve artifacts on disk for human inspection rather than silently cleaning up.
  - Frontend `relTime()` uses `Intl.RelativeTimeFormat` keyed off the active locale instead of hard-coded English.
  - `_postBatch()` errors render as an inline rose banner with copyable technical details, not a native `alert()` (§4.7).
  - SSE transport-error counter + per-job amber Reconnect banner replaces the silent "phantom running" state when the EventSource socket dies.

### Dedup pass (v0.4)

Closes the long-standing §15 gaps: duplicates were being detected only at postprocess time (after ~30s of yt-dlp + Playwright work), surfaced as a generic red error banner, never modeled in the audit log, and treated `?utm_source=email` and `?utm_source=tweet` as different videos. The home view also had no way to clear the recent-captures list and rendered them as a thumbnail grid (heavier than the workflow benefits from).

- **URL canonicalization** — new module [`app/url_canonical.py`](app/url_canonical.py) is the single source of truth for the canonical form of a URL. `url_hash = sha256(canonical(url_final))[:12]`. Originals (`url_submitted`, `url_final`) are preserved verbatim in `meta.json`; only the dedup key uses the canonical form.
- **Preflight endpoint** — `POST /api/jobs/preflight` (re-introduced from v0.3 deletion with new semantics). Classifies each URL and probes the `downloads` table per `(case_id, capture_kind, url_hash)`. Returns `{results: [...], summary: {new, duplicates_blocked, within_batch_duplicates, classification_failed}}` for the frontend's batch-summary chip and §15 modal queue.
- **Batch payload** — `POST /api/jobs/batch` now accepts either `urls: list[str]` (legacy / extension) or `items: list[JobBatchItem]`. The item shape carries `force_recapture: bool` and `original_download_id: int | None` for the §15 forced-re-capture path. Within-batch dedup keys on the canonical URL.
- **Re-capture as new entry** — `CaptureInput.force_recapture=True` makes `postprocess.finalize` skip the early-dedup probe and instead derive `url_hash = base_hash + "__c{N+1}"`. The on-disk per-item folder picks up the matching `__c{N+1}` stem via the existing `_resolve_collisions` mechanism. `meta.json.force_recapture_index` records the integer index.
- **Audit actions** — `duplicate.detected` (preflight hit, before user choice), `duplicate.opened_existing`, `duplicate.recaptured` (logged from the orchestrator on the new row's successful finalize, bound to `original_id` and `new_id`), `duplicate.cancelled`, `duplicate.url_canonicalized` (migration 004 summary).
- **Migration 004** — recomputes `downloads.url_hash` for every existing row using the canonical form. Rows whose canonical hashes collide are resolved by the same `__c2/__c3` suffix mechanism (oldest row wins the unsuffixed slot in `capture_date` order). On-disk `meta.json` files are NOT touched — they're signed and frozen at capture time. Migration adds a single rolled-up `duplicate.url_canonicalized` audit row. The migrate runner also gained `.py` migration support (`def upgrade(conn)`); SQL migrations still work unchanged.
- **Schema v5** — adds `url_canonical` (canonical form of url_final, used to derive url_hash) and `force_recapture_index` (integer or null) to `meta.json`. v4 → v5 upgrade is captured automatically by capturing a new row; the migration above handles existing rows' DB column.
- **Clear list** — new `POST /api/cases/{id}/clear` deletes every per-item folder under `/downloads/{slug}/`, drops the `downloads` rows for the case, and cascades orphaned `capture_groups`. Preserves: the case row, the case directory itself, `/config/cases/{slug}/cookies.txt`, the signing key, and **every** prior `audit_log` row. Adds a single `library.cleared` row carrying a snapshot (id, url_hash, capture_date, media sha256, sha256 of meta.json) per deleted item — the chain-of-custody anchor for the deletion event. Frontend gates the call behind a destructive-action confirmation dialog with an "Export evidence bundle first" link.
- **Recent-captures list** — replaces the home view's 4-column thumbnail grid with a one-row-per-capture list. Same visual cues (platform icon, capture-kind chip, integrity hint, relative time) at higher density. The Library proper (no v1 UI) keeps the §4.1 #5 thumbnail-grid spec for when it ships.
- **i18n** — new keys under `duplicate.*` and `recent.clear.*`; `duplicate.batch.summary.{new,dup,ib}` are split rather than one mega ICU template so empty branches don't leave dangling separators. All four bundles (en/ja/ar/es) updated; Arabic uses the full six-form plural set.

## 16. Still open

- **RFC 3161 trusted timestamping** — deferred to v2 as opt-in.
