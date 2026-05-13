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
2. **Updates are a barrier.** yt-dlp and gallery-dl ship frequent updates (often necessary when a site changes); investigators don't know they need to update or how. The app auto-checks on launch (opt-out, default ON, one network call to PyPI + GitHub at startup, every check audit-logged) and surfaces an in-app prompt when something is behind. Updating itself remains user-triggered: no silent installs, no telemetry beyond the documented version pings.
3. **Web evidence is broader than media.** A media file alone is weak evidence; a page snapshot alone is weak evidence. Investigators need both, captured at the same moment, from the same authenticated session. The tool always captures the full package — page snapshot (MHTML), full-page screenshot, full WARC, plus media if any — for every URL.
4. **Files become an unsorted mess.** Default filenames are noisy, inconsistent across sites, and lack provenance. We normalize for portability while preserving the originals in metadata.
5. **No audit trail.** Investigators must answer "where did this file come from, when, who authenticated, and is it intact?" — sometimes years later. We fix this with case-aware organization, full sidecar files, cryptographic checksums, detached signatures, and a tamper-evident audit log.
6. **Evidence handoff is fragile.** Investigators move work between editors, courts, and colleagues. We produce signed zip bundles + locale-aware PDF reports, with a standalone verifier so recipients can confirm integrity without installing our tool.
7. **Multilingual interfaces age badly.** Text-heavy UIs become unreadable when re-translated, especially when right-to-left and left-to-right are both first-class. We build visual-first: icons, colors, shapes, illustrations, thumbnails. Words support the visuals, not the other way around.

### The interface, in short

The app is a single-purpose downloader UI: paste a link (or a list), watch the four-phase capture progress, find the result in the recent-captures grid. Settings (language, signing key, browser-extension pairing, yt-dlp updater) is reachable from the header. The case-management surfaces the backend supports — Cases, Library, Item detail, Audit log — are **not exposed as UI in v1**; they live on disk and over the API for power users and evidence handoff. The full forensic package (hashes, signatures, audit log, MHTML, screenshot, WARC) is always written.

### Threat model

This release assumes a **safe operating environment**: the investigator's device is not assumed to be under physical seizure risk, and the local network is not assumed to be hostile. We do **not** ship Tor/proxy support, at-rest encryption, or anti-forensics features in v1. We **do** minimize unsolicited network traffic — no telemetry, no thumbnail prefetch unless the user opts in per case, and no auto-update. The single exception is the launch-time update ping (PyPI + GitHub releases), which is opt-out via Settings, audit-logged on every fire, and never installs anything by itself (CLAUDE.md §15 v0.10).

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

- **Auto-check is opt-out, default ON** (CLAUDE.md §15 v0.10). One launch-time check fires from the FastAPI lifespan hook (`app/main.py:_lifespan`) right after job rehydration. Cadence: launch only — no 24h timer, no periodic poll. Investigators with strict threat-model concerns flip the toggle off in Settings → Updates.
- **Auto-update remains forbidden.** Updating ALWAYS requires an explicit user click. Active captures could lose state and silent updates would compromise the audit trail.
- The launch check and any manual "Check now" click hit two registries: `https://pypi.org/pypi/{pkg}/json` for Tier 1 (yt-dlp, gallery-dl) and `https://api.github.com/repos/{repo}/releases/latest` for Tier 2 (Capsule itself, when `CAPSULE_GITHUB_REPO` is set). Each network call is recorded in the audit log as `system.update_check` with `triggered_by` (`launch` | `manual`).
- The Settings → Updates section shows per-component cards with installed version, latest version, status badge (icon + colour + shape), and a Tier-1 "Update" button or Tier-2 "docker pull" copy-paste callout. The home view shows a dismissible amber banner when `updates_available > 0`; the cog dot persists past dismissal so a future reviewer can see the user knew.
- If a download fails with a known "extractor outdated" error pattern, surface a **contextual** "Check for yt-dlp update?" prompt next to the failed job — but require the user to click it; do not auto-check beyond the launch ping.
- Updating runs `pip install --upgrade yt-dlp` (or `gallery-dl`) inside the container. Show a progress modal. On completion, show the new version and a "Release notes" link. Tier-1 updates are reverted on the next `docker rm`; the UI surfaces this honestly.
- The audit log carries the chain-of-custody: every check, every dismissal, every install lands as a row that survives the next image rebuild via `evidence_export`.

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
- **Frontend i18n runtime: [`@formatjs/intl-messageformat`](https://formatjs.github.io/)**. Backend serves the raw ICU bundles via `/api/i18n/{lang}` and renders translated strings into PDFs via [`app/pdf_report.py`](app/pdf_report.py) by direct lookup against the same bundles — there is no separate Python ICU runtime. Error responses carry an `i18n_key` plus structured technical details; the frontend resolves the key against the active bundle, so the backend never assembles user-facing prose. `en.json` is the canonical bundle.
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
/downloads/{case_slug}/{stem}/{stem}.report.pdf             ← per-item human-readable report PDF
/downloads/{case_slug}/{stem}/{stem}.manifest.pdf           ← per-item manifest PDF (full hashes, A4 landscape)
/downloads/{case_slug}/{stem}/Captures/                     ← page snapshots
/downloads/{case_slug}/{stem}/Captures/{stem}.page.{mhtml,png,warc.gz}
/downloads/{case_slug}/{stem}/Captures/{stem}.page.{har,console.json,context.png}
/downloads/{case_slug}/{stem}/Captures/{stem}.user-browser.*  ← extension-supplied (when present)
/downloads/{case_slug}/{stem}/Media/                        ← media file(s) and visual sidecars
/downloads/{case_slug}/{stem}/Media/{stem}.{ext}            ← media file (if any)
/downloads/{case_slug}/{stem}/Media/{stem}.thumbnail.{ext}
/downloads/{case_slug}/{stem}/Media/{stem}.{lang}.vtt       ← subtitles
/downloads/{case_slug}/{stem}/Metadata/                     ← textual records + signatures
/downloads/{case_slug}/{stem}/Metadata/{stem}.meta.json     ← canonical record
/downloads/{case_slug}/{stem}/Metadata/{stem}.meta.json.sig
/downloads/{case_slug}/{stem}/Metadata/{stem}.checksums.txt
/downloads/{case_slug}/{stem}/Metadata/{stem}.info.json     ← yt-dlp info (media kind)
/downloads/{case_slug}/{stem}/Metadata/{stem}.description   ← video description
...
```

Per-item folder keeps the case folder browsable. The two locale-aware PDFs sit at the item root so a recipient sees them first; everything else is grouped under three subfolders by role:

- **`Captures/`** — page snapshots (MHTML, screenshot, WARC, HAR, console events, media-context PNG) plus any extension-supplied user-browser captures.
- **`Media/`** — the media file(s), gallery images, thumbnail, subtitles. Anything a viewer would *play* or *see*.
- **`Metadata/`** — canonical `meta.json` + detached signature + `checksums.txt`, plus textual sidecars (yt-dlp `info.json`, `description`, gallery-level + per-image JSON).

Each capture is still a single, self-contained folder. The PDFs render in the UI locale active at submission time (`lang` flows from the frontend through `JobBatch` → `JobOrchestrator.submit` → `CaptureInput.lang` → `pdf_report.render_item_{report,manifest}`).

### Canonical filename pattern (media kind)

```
{platform}__{uploader}__{title}__{upload_date}__{video_id}.{ext}
```

### Canonical stem pattern (page_only and gallery kinds — no single media file, but the per-item folder still needs a stem)

```
{platform}__{page_title}__{capture_date}__{url_hash}
```

For `gallery` captures (CLAUDE.md §15 v0.5), `platform` derives from gallery-dl's `category` (e.g. `pixiv`, `imgur`) via `platforms.gallery_friendly_name`; `page_title` falls back to the gallery's `subcategory` then to `url_final` if neither is available.

Where `url_hash` is the first 12 hex chars of `sha256(canonical(url_final))` — short enough to be readable, long enough to avoid collisions in a single case. The canonical form (see [`app/url_canonical.py`](app/url_canonical.py)) lowercases scheme/host, drops the fragment, strips a curated tracking-param list (`utm_*`, `fbclid`, `gclid`, `igshid`, `mc_eid`, `mc_cid`, `_ga`, `_gl`, `yclid`, `msclkid`, `ref`, `ref_src`, `ref_url`, `share_id`, `si`, `feature`, `mkt_tok`, `_hsenc`, `_hsmi`, `spm`, `scm`), normalizes the trailing slash, and sorts remaining query keys — so two paste-variants of the same URL collapse to the same dedup key. The originals (`url_submitted`, `url_final`) are always preserved verbatim in `meta.json`. When the user picks "Re-capture as new entry" in the §15 modal, the new sibling row's `url_hash` becomes `{base}__c{N+1}` (counter starts at 2) and `meta.json.force_recapture_index` records the integer index for forensic clarity.

### Sanitization rules

- **`platform`** — lowercase, ascii. For `media` and `page_only` kinds, derived from yt-dlp's `extractor_key` via `platforms.friendly_name` (`youtube`, `vimeo`, `twitter`, `tiktok`, `instagram`, `facebook`, `linkedin`, `reddit`, `soundcloud`, `bandcamp`, `bilibili`, `generic`). For `gallery` kind (§15 v0.5), derived from gallery-dl's `category` via `platforms.gallery_friendly_name` (`pixiv`, `deviantart`, `imgur`, `flickr`, `tumblr`, `artstation`, `patreon`, `mangadex`, plus the yt-dlp-overlap slugs which use the same name). Maintain both mappings in `app/platforms.py`. The same module exposes `is_social(domain)` for cookie-attachment logic in §11 — its `SOCIAL_DOMAINS` set covers both video and image-first sites.
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

For every capture, all files live together in `/downloads/{case_slug}/{stem}/`. The media file (if any) and every sidecar are stem-prefixed so they remain forensically identifiable when copied or extracted from the folder. The two human-readable PDFs sit at the item root so a recipient sees them first; everything else is grouped under three subfolders by role — `Captures/`, `Media/`, `Metadata/` — for v0.8 onward (CLAUDE.md §15 v0.8). The two PDFs are still referenced by hash in `meta.json` and therefore signed transitively via `meta.json.sig`.

| File                              | Always present? | Source / contents                                                                                                                                                                                                                                                                                                                                                                  |
|-----------------------------------|------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `{stem}.manifest.pdf`             | Yes              | Locale-aware per-item evidence manifest (PDF, A4 landscape). Header with source URL, capture timestamp UTC, and signing-key fingerprint, then a table of every file in the item folder with **full** MD5 (32 hex) and **full** SHA-256 (64 hex) — verifier-ready, no truncation. Rendered in the UI locale active at submission time. Hash recorded in `meta.json` and `checksums.txt` and transitively signed via `meta.json.sig`. |
| `{stem}.report.pdf`               | Yes              | Locale-aware per-item human-readable report (PDF). Provenance (URLs, redirects, capture timestamp UTC, uploader, title, upload date, duration, authenticated domains), full untruncated description (paginated), tools/versions table, and capture-side report (render-wait outcomes, blocked-request count, banner-hide flags, readiness, report locale). Companion to `{stem}.manifest.pdf`. Rendered in the UI locale active at submission time. Hash recorded in `meta.json` and `checksums.txt` and transitively signed via `meta.json.sig`. |
| `Metadata/{stem}.meta.json`       | Yes              | **Our** structured metadata. Includes capture_kind, filenames, original/final URLs, redirect chain, response headers, platform, uploader, title (sanitized + original), description, upload_date, capture_date (UTC), duration, format details, file sizes, MD5/SHA-256 of every artifact, app/yt-dlp/browsertrix/Chromium versions, signing key fingerprint, audit-log entry id, list of authenticated domains (no cookie values) |
| `Metadata/{stem}.meta.json.sig`   | Yes              | Detached Ed25519 signature of `meta.json`                                                                                                                                                                                                                                                                                                                                          |
| `Metadata/{stem}.checksums.txt`   | Yes              | Lines of `MD5  <hash>  <relpath>` and `SHA256  <hash>  <relpath>` for every artifact (compatible with `md5sum -c` / `sha256sum -c`)                                                                                                                                                                                                                                              |
| `Metadata/{stem}.audit.json`      | Yes              | Per-item slice of the global audit log — every `audit_log` row whose `download_id` matches this capture, in the same wrapper shape (`{download_id, stem, generated_at_utc, entries[]}`) used by case-level `audit_log.json`. **Mutable**: extended by `extend_capture`, the verify endpoint, and the recapture flow. Not in `meta.json.artifacts` and not signed by `meta.json.sig` — tamper-evidence rides the audit chain itself. The standalone `verify.py` cross-checks every `(id, row_hash)` against the case-level `audit_log.json` (which IS signed transitively via `manifest.sig`).                                                                                              |
| `Captures/{stem}.page.mhtml`      | Yes              | Single-file MHTML snapshot of the source page at capture time (Playwright)                                                                                                                                                                                                                                                                                                         |
| `Captures/{stem}.page.png`        | Yes              | Full-page screenshot at capture time (Playwright)                                                                                                                                                                                                                                                                                                                                  |
| `Captures/{stem}.page.warc.gz`    | Yes              | WARC archive of source page + every sub-resource (browsertrix scope=`page+resources`)                                                                                                                                                                                                                                                                                              |
| `Metadata/{stem}.info.json`       | Media kind only  | yt-dlp's full `--write-info-json` output, untouched                                                                                                                                                                                                                                                                                                                                |
| `Metadata/{stem}.description.txt` | Media kind only  | Video description, plain text, LF line endings (yt-dlp `--write-description`)                                                                                                                                                                                                                                                                                                      |
| `Media/{stem}.thumbnail.{ext}`    | Media kind only  | Best available thumbnail (yt-dlp `--write-thumbnail`)                                                                                                                                                                                                                                                                                                                              |
| `Media/{stem}.{lang}.vtt`         | When requested   | Subtitles per language (yt-dlp `--write-subs`)                                                                                                                                                                                                                                                                                                                                     |
| `Media/{stem}.NNN.{ext}`          | Gallery kind only | Gallery image #NNN (1-based 3-digit zero-padded index, sorted-by-name for deterministic ordering). Original extension preserved (`.jpg`/`.png`/`.webp`/`.gif`/`.mp4` for animated formats from Pixiv ugoira / DeviantArt clips). Each indexed under role `gallery_NNN`. (v6) |
| `Metadata/{stem}.NNN.json`        | Gallery kind only | gallery-dl's per-image `--write-metadata` JSON sidecar (renamed to share the stem). Indexed under role `gallery_NNN_meta`. (v6) |
| `Metadata/{stem}.gallery_info.json` | Gallery kind only | Gallery-level metadata from gallery-dl's `--write-info-json` (extractor `category`, source URL, gallery title, etc.). Indexed under role `gallery_info`. (v6) |
| `Captures/{stem}.user-browser.tab-context.json`    | Extension live capture | Investigator's UA / viewport / scroll / timezone / referrer / color-scheme. The backend canonical capture mirrors these fields. (v2)                                                                                                                                                                                             |
| `Captures/{stem}.user-browser.session-state.json`  | Extension live capture | Per-origin localStorage and sessionStorage. Some sites carry session JWTs in localStorage; without this the backend re-fetch may render as logged-out even with valid cookies. (v2)                                                                                                                                              |
| `Captures/{stem}.user-browser.dom-snapshot.html`   | Extension live capture | Click-time `document.documentElement.outerHTML` from the user's authenticated browser. Distinct from the Playwright MHTML — locks in exactly what the investigator was looking at. (v2)                                                                                                                                          |
| `Captures/{stem}.user-browser.dom-snapshot.json`   | Extension live capture | Structural counts that go with the DOM HTML (node count, iframe count, video count, image total + visible). (v2)                                                                                                                                                                                                                  |

`{stem}.meta.json` is the canonical record. Schema lives at `/app/schemas/meta.schema.json` and is versioned (`"schema_version": 9` for new captures; v2–v8 records continue to validate). When the schema changes, write a migration. v2 (hardening pass) adds:

- `capture` — the capture report: render-wait outcomes (`load`, `fonts`, `images`, `video`, `lazy_load`, `networkidle`), blocked-request count + sample, `blocklist_version`, `banner_hide_applied`, `banner_hide_version`, `tab_context_used`.
- `cookies_snapshot_sha256` — SHA-256 of the cookies file the job consumed; binds the capture to the exact cookie set without ever logging values.
- `ephemeral_cookies_used` — true iff the job used a one-shot ephemeral cookie file (extension-supplied, never persisted to the case directory).

v3 (Track A) adds the `manifest_pdf` artifact role + checksum and `capture.report_lang`. v4 adds the `report_pdf` artifact role + checksum (the per-item human-readable report PDF). Both PDFs are referenced by hash in `meta.json` and therefore transitively signed by `meta.json.sig` — no extra signing path required. v5 adds `url_canonical` and `force_recapture_index` for the §15 dedup pass. v6 (Gallery pass v0.5) adds the `gallery` capture_kind plus `gallery_count`, `gallery_extractor`, `tools.gallery_dl_version`, `capture.gallery_attempted`, `capture.gallery_outcome`, and the `gallery_NNN` / `gallery_NNN_meta` / `gallery_info` artifact roles. v7 (Page-preservation hardening v0.6) adds the `capture.warc` sub-block, `capture.response`, the `page_har` / `page_console` / `page_context_screenshot` artifact roles, and `tools.warcio_version`. v8 (Download options + reliability v0.7) adds the `download_options` block (`audio_only`, `quality_cap`, `subtitle_langs`, `restart_count`) and `capture.stalled_count`. v9 (Format choice v0.9) adds `download_options.video_container` and `download_options.audio_container`.

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
    └── {stem}/                                 ← per-item folder (v0.8 layout)
        ├── {stem}.report.pdf                   ← per-item human-readable report PDF
        ├── {stem}.manifest.pdf                 ← per-item manifest PDF
        ├── Captures/
        │   ├── {stem}.page.mhtml
        │   ├── {stem}.page.png
        │   ├── {stem}.page.warc.gz
        │   └── ...                             ← page.har, page.console.json, user-browser.* (when present)
        ├── Media/
        │   ├── {stem}.{ext}                    ← media file (present for media kind)
        │   └── ...                             ← thumbnail, subtitles, gallery images
        └── Metadata/
            ├── {stem}.meta.json
            ├── {stem}.meta.json.sig
            ├── {stem}.checksums.txt
            └── ...                             ← info.json, description, gallery_info.json per §6
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
- Fallback: per-case cookies file at `/config/cases/{case_slug}/cookies.txt` (Netscape format, 0600). Upload via UI: case detail → Cookies tab → upload `cookies.txt`. Same downstream consumers — yt-dlp, Playwright, browsertrix, and (since v0.5) gallery-dl all read the same file.
- **Auto-attach for social-media domains.** When a pasted URL matches a domain that has cookies in the active case, the UI shows an "Authenticated as {domain}" chip on the capture preview, and the cookies are passed to **yt-dlp, Playwright/browsertrix, and gallery-dl**. This ensures the page snapshot, the media, and the gallery images all come from the same authenticated session. The list of authenticated-content domains is maintained in `app/platforms.py` (`is_social(domain)`), covering at minimum: Twitter/X, Facebook, Instagram, TikTok, LinkedIn, Reddit, YouTube (private/age-gated), Threads, plus the v0.5 image-first additions: Pixiv, DeviantArt, Tumblr, Flickr, Imgur, Patreon, ArtStation, Fanbox.
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
7. **No silent network calls — with one documented exception.** No telemetry, no thumbnail prefetch unless the user opted in. If you write code that calls out to the internet, it must be triggered by an explicit user action OR by the opt-out launch-time update check (CLAUDE.md §4.4 + §15 v0.10), AND it must appear in the audit log. If you're tempted to add another exception, audit-log + opt-out toggle + one-pager in `docs/` is the bar. Don't ship a quiet poll without that.
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

### Gallery pass (v0.5)

Image-only sources (Twitter image threads, Instagram carousels, Imgur albums, Pixiv posts, DeviantArt pages, Reddit galleries, Tumblr posts, image-board threads) used to collapse to `page_only` because yt-dlp doesn't extract them. v0.5 adds [gallery-dl](https://github.com/mikf/gallery-dl) as a Phase-3 fallback so those URLs become first-class `gallery` captures with the same forensic guarantees as media (per-image MD5+SHA-256, signed `meta.json`, manifest PDF, audit log, evidence-export bundle).

- **New `gallery` capture_kind** alongside `media` and `page_only`. Mutually exclusive in the orchestrator's fallback model: yt-dlp wins → `media`; else gallery-dl wins → `gallery`; else → `page_only`. The DB `UNIQUE(case_id, capture_kind, url_hash)` constraint partitions the namespace cleanly so a future investigator could capture the same URL as both kinds.
- **Phase-3 fallback** — when yt-dlp returns no media AND `case.settings_json.gallery_enabled` is true (default), the orchestrator runs gallery-dl against the same final URL with the same Netscape `cookies.txt`. Per-job work dir `_gallery_{job_uuid}` under the case dir, rmtree'd on success and on `DuplicateCapture` rollback so per-job scaffolding never accumulates.
- **New runner module** [`app/gallery_dl_runner.py`](app/gallery_dl_runner.py) mirrors [`app/ytdlp_runner.py`](app/ytdlp_runner.py)'s contract — async subprocess wrapper, no DB / audit-log side effects. Per-file progress (one event per completed image), `RunResult` exposes `image_files` / `metadata_files` / `extractor` splits so postprocess doesn't re-classify. Pinned flags: `--write-metadata`, `--write-info-json`, `--no-mtime`, `-d <work_dir>`, `--range 1-{max_items}`, `--cookies <path>`. Pause/cancel via the same `proc_holder` flow as yt-dlp.
- **Per-item folder layout**:
  ```
  /downloads/{case_slug}/{stem}/
  ├── {stem}.001.jpg                  ← gallery image #1 (artifacts["gallery_001"])
  ├── {stem}.001.json                 ← per-image metadata (artifacts["gallery_001_meta"])
  ├── {stem}.002.png
  ├── {stem}.002.json
  ├── ...
  ├── {stem}.gallery_info.json        ← gallery-level metadata (artifacts["gallery_info"])
  ├── {stem}.meta.json
  ├── {stem}.meta.json.sig            ← transitively binds every image's hash
  ├── {stem}.checksums.txt
  ├── {stem}.page.{mhtml,png,warc.gz} ← still always present
  └── reports/
      ├── {stem}.manifest.pdf         ← lists every image with full hashes
      └── {stem}.report.pdf           ← human-readable; includes thumbnail strip
  ```
- **Canonical stem (gallery)** — reuses the page-only pattern `{platform}__{page_title}__{capture_date}__{url_hash}`. `platform` derives from gallery-dl's `category` (e.g. `pixiv`, `twitter`, `imgur`) via the new [`platforms.gallery_friendly_name`](app/platforms.py) map. Where the slug overlaps yt-dlp (twitter/reddit/instagram), the on-disk `{platform}` token stays stable across kinds.
- **Cookies** — same Netscape `cookies.txt` flows to gallery-dl. [`SOCIAL_DOMAINS`](app/platforms.py) (and the matching `_DOMAIN_HINTS`) gain `pixiv.net`, `deviantart.com`, `tumblr.com`, `flickr.com`, `imgur.com`, `patreon.com`, `artstation.com`, `fanbox.cc` so cookie auto-attachment fires for image-first sites too.
- **Schema v6** — `capture_kind` enum gains `"gallery"`; new fields at the root: `gallery_count` (int|null), `gallery_extractor` (string|null); new tools field: `gallery_dl_version` (string|null); new capture-report fields: `capture.gallery_attempted` (bool), `capture.gallery_outcome` (enum: `captured`, `empty`, `rate_limited`, `auth_required`, `failed`, `skipped`); new artifact roles: `gallery_NNN`, `gallery_NNN_meta`, `gallery_info`, `gallery_extra_NNN_meta` for stragglers. **No DB migration required** — the `capture_kind` column is `TEXT NOT NULL` with no CHECK constraint.
- **Audit actions** (no conflicts with prior names) — `gallery.started` (every gallery-dl invocation), then exactly one outcome: `gallery.captured` (with `image_count` + `extractor`), `gallery.empty` (rc=0, no images), `gallery.rate_limited` (matched by [`errors.classify`](app/errors.py)), `gallery.auth_required`, `gallery.failed` (rc≠0, no specific match). The `download.created` row carries `gallery_count` + `gallery_extractor` for gallery kinds.
- **Settings** — `case.settings_json.gallery_enabled` (bool, default true) for full opt-out; `case.settings_json.gallery_max_items` (int, default 200) caps the per-job image fetch via gallery-dl `--range 1-{N}`. Investigators raise the cap per-case for full-profile sweeps.
- **Updater** — `/api/system/update` now accepts `?component=yt-dlp|gallery-dl` (default `yt-dlp` for back-compat). `/api/system/version` returns `gallery_dl` alongside `yt_dlp`. Same `system.updated` audit pattern with the component label. Adding a future updatable runtime is one entry in `_UPDATABLE_COMPONENTS` plus the version-fetcher coroutine.
- **Error mapping** ([`app/errors.py`](app/errors.py)) — three new entries before the generic 429 rule so gallery-specific keys win precedence: `errors.gallery_rate_limited` (transient, try_again), `errors.gallery_auth_required` (permanent, add_cookies), `errors.gallery_no_images` (permanent, no action). All four bundles updated.
- **UI** — `recent.row.kind.gallery` ("Image gallery"/"画像ギャラリー"/"معرض صور"/"Galería de imágenes") with the Lucide `images` icon. Same chip in the duplicate-resolution modal. Replaces the binary `media`/`page_only` ternary with two helpers (`captureKindIcon` / `captureKindLabel`) so unknown kinds fall back to the page-only chip — forwards-compat for future enum additions.
- **Per-item report PDF** — when `capture_kind == "gallery"`, the report gains an "Image gallery" section with a 4-column thumbnail strip (file:// URIs anchored under `DOWNLOADS_DIR`, capped at 20 thumbnails so a 200-image gallery doesn't bloat the PDF — the manifest PDF still lists every image regardless) and a caption with the count + extractor. The tools table grows a "gallery-dl version" row.

### Page-preservation hardening (v0.6)

The Phase 2 capture (MHTML / screenshot / WARC) used to run a Playwright session for the MHTML+PNG and a separate `browsertrix-crawler` subprocess for the WARC — same cookies and blocklist, but two browsers, two navigations, and divergent timing/request order. Render-waits used a fixed schedule that didn't re-check images after lazy-load, didn't pause autoplay videos, didn't freeze animations before the still PNG, and didn't enforce its own outer budget. There was no media-in-context evidence, no console capture, and no per-page HAR.

- **Single-session capture** — new module [`app/warc_writer.py`](app/warc_writer.py) (`CdpWarcWriter`) tees Chromium's CDP `Network.*` events into a gzipped WARC/1.1 file via [`warcio`](https://github.com/webrecorder/warcio). MHTML, PNG, and WARC now come from one navigation in one browser context. `browsertrix-crawler` stays as a fallback when warcio is unavailable or the in-session writer fails — preserves the §6 "every capture has a WARC" guarantee.
  - **Body handling**: bodies fetched via `Network.getResponseBody`. `Content-Encoding` rewritten to `identity` (CDP returns decoded payloads), `Content-Length` recomputed. Recorded as `meta.json.capture.warc.encoding_normalized: true`. Bodies > 5 MB are replaced with a `metadata` record pointer rather than loaded into memory.
  - **Sensitive header redaction**: `Cookie` / `Set-Cookie` / `Authorization` / `Proxy-Authorization` are stripped from every WARC `request`/`response` record. Cookie values never leave the cookie file — only the SHA-256 of that file enters meta.json (per §11).
  - **Non-HTTP schemes** (`data:`, `blob:`, `ws:`/`wss:`, `chrome-extension:`) and `loadingFailed` events become WARC `metadata` records — the WARC stays a complete catalog without bloating with binary blobs.
- **Render fidelity**:
  - `loading="lazy"` promoted to `"eager"` before the lazy-load scroll so below-the-fold images hit the wire and the WARC + HAR see them. Recorded as `capture.lazy_promoted_count`.
  - Lazy-load step count is adaptive (`min(50, ceil(scrollHeight/innerHeight) + 4)`) instead of a flat 12; final document height recorded as `capture.lazy_load_max_height_px`.
  - Visible-images gate traverses shadow DOM and same-origin iframes; re-runs after lazy-load via a new `images_after_scroll` sub-detail so newly-surfaced lazy images are awaited.
  - Video gate waits for `readyState >= 2` (first frame decoded), pauses autoplay videos, and preloads `<video>.poster` images. Pause count surfaced as `capture.videos_paused`.
  - Animation/transition freeze CSS (new file [`app/static/blocklists/animation-freeze.css`](app/static/blocklists/animation-freeze.css), loaded by [`app/animation_freeze.py`](app/animation_freeze.py)) injected immediately before `page.screenshot()` and removed immediately after — the still PNG captures a stable frame, while the MHTML/WARC (already captured) retain the page's animation behavior. Recorded as `capture.animations_frozen` + `capture.animations_frozen_version`.
  - Long-page screenshot cap at 30,000px (configurable via `DEFAULT_SCREENSHOT_MAX_HEIGHT_PX`). Truncation is recorded as `capture.screenshot_truncated_at_px`. MHTML and WARC are never clipped.
  - Render-wait outer budget (60s, `DEFAULT_RENDER_WAIT_BUDGET_MS`) is now enforced — once exceeded, remaining gates are skipped and `capture.readiness_timed_out: true` is recorded. The audit row is `capture.readiness_budget_exceeded`, distinct from the per-gate `capture.readiness_timed_out` that fires when an individual wait times out.
- **Forensic instrumentation**:
  - Per-page HAR sidecar via Playwright `record_har_path`. After capture, `_redact_har_in_place()` strips `Cookie` / `Set-Cookie` / `Authorization` headers and `cookies[]` arrays from both request and response sides; the redaction count is stamped on `log._capsule_redacted_header_count`. Saved as `{stem}.page.har`, role `page_har`. Hashed and signed transitively via `meta.json.sig`.
  - Browser console + page-error capture via `page.on("console")` / `page.on("pageerror")`. Events written to `{stem}.page.console.json`, role `page_console`. Counts surfaced as `capture.console_message_count` + `capture.console_error_count`.
  - Navigation response block recorded as `meta.json.capture.response`: final URL, final status, redirect chain (each hop's URL/status/Location), sanitized headers, and `elapsed_ms`. Cookie/Authorization headers stripped via `_sanitize_response_headers()`.
- **Media-context screenshot** — second viewport-sized PNG framed on the most prominent `<video>` / video-host `<iframe>` on the page (YouTube, Vimeo, X, TikTok, Instagram, etc. — `_VIDEO_HOST_HINTS` in [`app/capture.py`](app/capture.py)). Saved as `{stem}.page.context.png`, role `page_context_screenshot`. Selector recorded for forensic clarity (`capture.media_context_selector`). Skipped gracefully when no media-ish element matches; the per-item PDF report's "Media context" section is suppressed in that case.
- **Schema v7** — additive only. New `capture.warc` sub-block (`captured_in_session`, `record_count`, `encoding_normalized`, `format_version`); new `capture.*` fields per the bullets above; new `capture.response` block; new `tools.warcio_version` (string|null). `tools.browsertrix_version` is now nullable in description but stays schema-required for back-compat with v2–v6. New artifact roles: `page_har`, `page_console`, `page_context_screenshot`. v7 records validate alongside v2–v6 records since every addition is a new optional field.
- **Audit actions** (additive, all conditional on the corresponding capture step firing) — `capture.warc_session_in_process` / `capture.warc_session_subprocess`, `capture.animations_frozen`, `capture.media_context_captured`, `capture.console_messages_recorded`, `capture.screenshot_truncated`, `capture.readiness_budget_exceeded`. The pre-existing `capture.ads_blocked` / `capture.banners_hidden` / `capture.readiness_timed_out` stay unchanged.
- **PDF report** — `_format_capture_report()` in [`app/pdf_report.py`](app/pdf_report.py) gains rows for animations frozen, console message + error counts, media-context status (with selector), final page height (or truncation note), and WARC session provenance. The per-item report HTML template gains a `{{media_context_section}}` placeholder that embeds `page_context_screenshot` inline above the description. New i18n keys under `pdf.report.field.capture.*` and `pdf.report.heading.media_context` in en/ja/ar/es.
- **Frontend** — the `capture_report` SSE event picks up the new counters (`animations_frozen`, `videos_paused`, `lazy_promoted_count`, `iframes_seen`, `screenshot_truncated_at_px`, `readiness_timed_out`, `console_message_count`, `console_error_count`, `media_context_captured`, `warc_captured_in_session`, `warc_record_count`) so a future "page-faithfulness" badge in the home view can render them without re-fetching meta.json.
- **Dependency** — `warcio>=1.7,<2` added to the `capture` extra in `pyproject.toml`. Justification: WARC/1.1 compliance is non-trivial to handroll; warcio is the de-facto reference (Webrecorder, pywb, ArchiveBox).
- **Distribution** — Dockerfile keeps `browsertrix-crawler` for the fallback path; removing it is tracked in §16 once in-session is proven across the corpus.

### Download options + reliability hardening (v0.7)

Closes two long-standing gaps in the v1 surface: (a) investigators had no way to *modify* the download (audio-only, quality cap, subtitles), and (b) slow/flaky captures had no UI escape hatch — pause/resume/cancel existed in the orchestrator but the v0.3 simple-view consolidation left no HTTP routes or buttons for them, and there was no stall detector or "wipe `.part` and start fresh" path. v0.7 wires three download-modification options through to yt-dlp, re-introduces pause/resume/cancel as HTTP routes + per-job UI toolbar, adds a distinct restart action with its own audit row, and lights up a stall watchdog so a stuck job can amber-chip rather than appear "running" forever.

- **Download options surface** — new `DownloadOptions` dataclass on [`app/jobs.py`](app/jobs.py): `audio_only`, `quality_cap` (`"audio"|"480"|"720"|"1080"|"best"|null`), `subtitle_langs` (multi-select; `"all"` sentinel maps to `all,-live_chat`), plus reliability counters `restart_count` / `stalled_count`. Persisted as JSON on `jobs.download_options_json` (migration 005). Threaded through `JobOrchestrator.submit()` → `CaptureInput` → `meta.json.download_options` (schema v8) → per-item PDF report's "Download options" section. The signature on `meta.json` transitively binds the block, so a recipient can confirm what knobs were in effect for that capture.
- **yt-dlp runner argv** — [`app/ytdlp_runner.py`](app/ytdlp_runner.py) gains `audio_only` / `audio_format` / `audio_quality` / `quality_cap` / `subtitle_langs` / `restart` kwargs on `run()`. Helpers: `build_format_spec()` resolves the `--format` precedence (`audio_only` ⇒ skip `--format` entirely + emit `-x --audio-format mp3 --audio-quality 0`; `quality_cap` height ⇒ `bestvideo[height<=N]+bestaudio/best[height<=N]` overriding the profile default; `quality_cap == "best"` lifts any profile cap; otherwise the profile fallback wins). `build_subtitle_argv()` emits `--write-subs --sub-langs <csv> --sub-format vtt/srt/best`. `_wipe_partial_files()` clears every `*.part`/`*.ytdl` from the case dir; called when `restart=True` so `--no-continue` has a clean slate.
- **Stall watchdog** — runner spawns a parallel `asyncio.Task` that wakes ~every 5s (or `threshold/3` for tests). When `monotonic() - last_progress_at >= stall_threshold_s` (default 90s, `DEFAULT_STALL_THRESHOLD_S`), it pushes a synthetic `ProgressUpdate(status="stalled", raw={"elapsed_s": N})`. The next real progress event clears the flag. **No SIGTERM** — stall is a UI signal, not a kill condition. The orchestrator's `_forward_progress` translates the synthetic event into a `stalled` SSE event + `download.stalled` audit row, and bumps `download_options.stalled_count`.
- **Restart vs. resume** — distinct `JobOrchestrator.restart(job_id)` method. Cancels any live subprocess, deletes `*.part`/`*.ytdl` (the runner does this once `restart=True` rides through), increments `download_options.restart_count`, resets `attempts=0` / `error=None` / `phase=None`, flips status to `STATUS_QUEUED`, and re-dispatches with the volatile `Job.restart_pending` flag set. Subsequent auto-retries (transient-failure retries scheduled by `_schedule_retry`) revert to `--continue`. Audit row: `job.restarted` with `from`/`restart_count`. Returns `False` on a `done` job — successful captures are immutable; the §15 modal covers the legitimate re-capture flow.
- **HTTP control routes** — [`app/main.py`](app/main.py) re-introduces `POST /api/jobs/{id}/{pause,resume,cancel,restart}` (the v0.3 prune is reverted, plus the new `restart`). Each returns the updated Job snapshot; 404 on unknown id, 409 on invalid transition. The frontend's wire payload extends `JobBatchItem` with `audio_only` / `quality_cap` / `subtitle_langs`; `_normalize_batch_items` plumbs the new fields through the within-batch dedup; the batch handler builds a `DownloadOptions` per item only when at least one knob is set.
- **SSE event additions** — `paused`, `resumed`, `cancelled`, `restarted`, `stalled` (additive; the existing `status`/`progress`/`classification`/`error`/`done`/`capture_report`/`warning` shape is unchanged). Distinct events let the UI animate transitions without diffing `status` fields.
- **Frontend** — [`app/static/index.html`](app/static/index.html) gains an Advanced `<details>` disclosure between the URL forms and the active-jobs panel: audio-only switch (`headphones`), quality segmented pill (Best/1080/720/480/Audio with the `monitor` icon), subtitle multi-select chips (en/ja/ar/es/fr/de/zh/pt/all with `subtitles`). Closed by default per §4.2 #4; auto-opens when any knob is non-default. Persisted in `localStorage` as `capsule.downloadOptions`. Each active-job card grows a per-job toolbar (Pause/Resume/Restart/Cancel as icon-only buttons with translated `aria-label`s); Cancel and Restart route through a destructive-action confirm dialog (same shape as the §15 clear-list dialog). Stalled jobs show an amber `clock` chip with elapsed-seconds; the chip clears on the next progress event. The failed-job retry button now calls `restartJob(j.id)` instead of resubmitting the URL — produces a clean `job.restarted` audit row instead of a duplicate `job.created`.
- **Per-item PDF report** — [`app/pdf_report.py`](app/pdf_report.py) emits a new "Download options" section when at least one knob differs from default: "Audio extracted only — page snapshot preserves the original video", "Quality cap: ≤720p", "Subtitles: en, ar", "Restarted N times", "Stalled N times during capture". Locale-aware via the existing labels loader. The companion HTML template gains `{{download_options_section}}` between the gallery/media-context blocks and the description. Both PDFs continue to ride the same artifact-binding transitive signature path — the new block is bound to `meta.json` via the existing Ed25519 signature.
- **Schema v8** — [`app/schemas/meta.schema.json`](app/schemas/meta.schema.json). Additive: new `download_options` block at the root (`audio_only`, `quality_cap`, `subtitle_langs`, `restart_count`) and `capture.stalled_count`. v2–v7 records validate without modification; v8 records emit the block always (defaults included) so absence-vs-default is unambiguous.
- **Audit actions** — `download.options_applied` (per dispatch when any knob is non-default), `download.stalled` / `download.stall_cleared`, `job.restarted`. Existing `job.paused` / `job.resumed` / `job.cancelled` continue to fire from the orchestrator's pause/resume/cancel paths.
- **Forensic note** — `audio_only=true` is a *download choice* (yt-dlp never fetches the video stream), not a *post-mutation*. The page snapshot (MHTML/PNG/WARC) still preserves the full video player from capture time. The `meta.json.download_options.audio_only` field plus the per-item PDF section make this unambiguous to a recipient — they're never left wondering why the on-disk media is `.mp3` while the screenshot shows a video.

### Per-item folder reorganization (v0.8)

The flat per-item folder from v0.2 — media, page snapshots, every textual sidecar, `meta.json` + signature, and a `reports/` subfolder for the two PDFs all jostling at the same level — read as a wall of `{stem}.*` files when an investigator opened it. The two human-readable PDFs (the only files a non-Capsule recipient reaches for first) were buried one level below everything else. v0.8 promotes the PDFs to the item root and groups the rest by role.

- **Per-item layout** — two PDFs at the item root; everything else in three role-named subfolders. `Captures/` holds page snapshots (MHTML, screenshot, WARC, HAR, console events, media-context PNG, plus extension-supplied user-browser captures). `Media/` holds the media file(s), gallery images, thumbnail, and subtitles — anything a viewer plays or sees. `Metadata/` holds `meta.json` + detached signature + `checksums.txt` and the textual sidecars (yt-dlp `info.json`, video description, gallery-level + per-image JSON). The legacy `reports/` subfolder is gone.
- **Routing** — [`app/postprocess.py`](app/postprocess.py) gains `SUBDIR_CAPTURES`/`SUBDIR_METADATA`/`SUBDIR_MEDIA` constants and `_subdir_for_sidecar(filename)` for yt-dlp sidecars whose subdir depends on the file extension (thumbnails + subtitles → `Media/`; everything else → `Metadata/`). Each `_move_into` callsite passes the subdir directly, so the relpath that flows into `meta.json.artifacts` already carries the prefix — `checksums.txt`, the manifest PDF's file table, and the evidence-export ZIP all pick it up without further wiring.
- **PDFs at the item root** — [`postprocess.finalize`](app/postprocess.py) writes `{stem}.report.pdf` and `{stem}.manifest.pdf` directly under `item_dir` instead of `item_dir/reports/`. Both still ride the existing artifact-binding transitive signature path: each PDF is hashed before `meta.json` is signed, so a recipient who verifies `meta.json.sig` transitively verifies both PDFs.
- **Schema** — additive only. `schema_version` stays at 8; existing fields are unchanged. The relpaths in `meta.json.artifacts` simply gain the `Captures/` / `Media/` / `Metadata/` prefix where appropriate. v2–v7 records on disk continue to validate as-is.
- **Backwards compatibility on disk** — items captured under the pre-v0.8 layout are NOT rewritten. Their `meta.json` records the relpaths as they were at capture time (signed bytes are immutable per CLAUDE.md §13.13), so `verify` and the evidence-export bundle keep resolving them. The verify endpoint ([`app/main.py`](app/main.py)) and `evidence_export._iter_artifact_paths` ([`app/evidence_export.py`](app/evidence_export.py)) prefer the v0.8 `Metadata/{stem}.meta.json` location and fall back to the item root when the new path is missing — a mixed library exports cleanly. Bundled `verify.py.tmpl` does the same fallback so a single verifier copy works against both old and new bundles.
- **`extend_capture`** — the role-to-name map gains a `(subdir, filename)` pair; new artifacts on legacy items still use the new subdirs (the old artifacts stay in place since their relpaths in `meta.json` remain the truth). When a legacy item's `meta.json` lives at the item root, `extend_capture` updates it in place rather than spawning a stranded sibling in `Metadata/`.
- **Documentation** — CLAUDE.md §5 path layout, §6 item-folder contents table, §10 evidence-bundle layout updated in lockstep per CLAUDE.md §13.15. `docs/quickstart.{en,ja,ar,es}.md` and `docs/user-guide.{en,ja,ar,es}.md` updated with the new tree diagrams.

### Format choice (v0.9)

The v0.7 download-options surface let investigators pick audio-only mode, a height cap, and subtitle languages — but said nothing about the on-disk **container**. `audio_only=true` always produced an MP3 (hardcoded `DEFAULT_AUDIO_FORMAT="mp3"` in [`app/ytdlp_runner.py`](app/ytdlp_runner.py)) and the video path took whatever container yt-dlp's `--format` resolved to (often `.webm` or `.mkv` from a DASH merge — surprising for an investigator expecting `.mp4`). v0.9 adds two container fields plus a UI restructure that consolidates the related controls.

- **Two new fields on [`DownloadOptions`](app/jobs.py)** — `video_container ∈ {"mp4","webm","mkv"} | None` and `audio_container ∈ {"mp3","m4a","opus","wav","flac"} | None`. Two fields (not one switched on `audio_only`) so an investigator's pick on each side survives toggling between video and audio modes. Module-level constants `jobs.VIDEO_CONTAINERS` / `jobs.AUDIO_CONTAINERS` are the single source of truth — the runner, the API validator (`JobBatchItem._validate_video_container` / `_validate_audio_container`), the dataclass coerce in `from_dict`, and the frontend's `_VIDEO_CONTAINERS` / `_AUDIO_CONTAINERS` arrays all read from the same enum so an unknown string can't slip through any layer.
- **Mux-only forensic stance** — [`build_container_argv()`](app/ytdlp_runner.py) emits `--merge-output-format <ext>` for the video path, which only chooses the muxer for yt-dlp's video+audio merge. The video bitstream is **never** re-encoded — that would be a forbidden mutation per CLAUDE.md §13 #13. The audio path uses `--audio-format <ext>` which DOES transcode (same as v0.7's hardcoded mp3); widening the choice doesn't change the forensic stance, and the user's pick is recorded in `meta.json.download_options.audio_container` and surfaced in the per-item PDF report.
- **Format-spec extension** — [`build_format_spec()`](app/ytdlp_runner.py) gains a `video_container` kwarg. When set, the spec prefers ext-matched streams via a four-branch cascade (ext-matched video + matching audio → ext-matched video + any audio → bestvideo + bestaudio → generic best), and combines cleanly with the existing `quality_cap` height clause. The audio-pair lookup `_AUDIO_PAIR_FOR_VIDEO_CONTAINER` pairs `mp4`/`mkv` with `m4a` (AAC) and `webm` with `webm` (Opus/Vorbis) per yt-dlp convention.
- **Schema v9** — additive only. [`app/schemas/meta.schema.json`](app/schemas/meta.schema.json) adds `download_options.video_container` (anyOf null|enum) and `download_options.audio_container` (anyOf null|enum). v2–v8 records on disk still validate. [`postprocess.finalize`](app/postprocess.py) emits `schema_version: 9` going forward.
- **Frontend restructure** — [`app/static/index.html`](app/static/index.html) collapses the v0.7 quality + audio-only + (new) format controls into a single **Output** section inside the Advanced disclosure. The legacy `audio` chip on the quality pill is removed since audio mode is now the toggle above (the runner still accepts `quality_cap == "audio"` for back-compat with extension-submitted payloads and stale localStorage entries). The Format pill is context-aware — surfaces `mp4 / webm / mkv` when audio-only is off, `mp3 / m4a / opus / wav / flac` when on, with an "Auto" leading chip mapping to `null`. The remembered value on the *opposite* side is preserved across the toggle.
- **i18n** — new keys under `download.options.output.heading`, `download.options.format.{label,aria.video,aria.audio,option.{auto,mp4,webm,mkv,mp3,m4a,opus,wav,flac}}`, and `pdf.report.field.download_options.format.{label,video,audio}`. All four bundles (en/ja/ar/es) updated; container acronyms (MP4/WebM/M4A/...) stay as proper-noun brands across locales.
- **Per-item PDF report** — [`_format_download_options_section()`](app/pdf_report.py) renders one extra row when a container is set: "Container: MP4 (mux-only, no re-encode)" on the video path, "Audio extracted as M4A" on the audio path. Gated by `audio_only` so a stale opposite-side value doesn't appear on the wrong report. Both PDFs are still hashed into `meta.json.artifacts` before `meta.json` is signed, so the format choice is bound transitively via `meta.json.sig`.
- **No DB migration** — `download_options_json` is a free-form JSON blob in `jobs.download_options_json`, so the new fields ride through without a schema migration.
- **Audit log** — the existing `download.options_applied` row picks up the new fields automatically via `DownloadOptions.to_dict()`. No new audit action needed; existing recipients walking the audit trail see the v0.9 keys appear in the `details.options` block.

### Update management hardening (v0.10)

CLAUDE.md §1 problem #2 ("Updates are a barrier") was only half-solved through v0.9: yt-dlp and gallery-dl had a "Check for updates" button buried at the bottom of Settings, but it ran `pip install --upgrade` blindly with no installed-vs-latest comparison, didn't surface ffmpeg/Chromium/browsertrix at all (correctly — those ride with image rebuilds), and never told the user when a fresher Capsule image was available. v0.10 turns this into a managed surface: a launch-time auto-check (opt-out, default ON), a tiered registry, per-component status cards in Settings, and a home-view banner. The forensic stance is preserved: every check is audit-logged, no install happens without an explicit user click, the registry-blessed `cookies` substring guard in [`audit.append`](app/audit.py) still rejects credential-bearing keys, and the launch check is the **only** documented exception to CLAUDE.md §13 #7's "no silent network calls" rule.

- **Two-tier registry on [`app/updates.py`](app/updates.py)** — single source of truth for what's updateable and how. Tier 1 = pip-installable in-container (`yt-dlp`, `gallery-dl`); Tier 2 = ships with the Capsule image (`capsule` itself). Each component declares its `source` (`pypi` | `github`), the package or repo string, and the tier. Adding a future download library (e.g. another extractor) is one entry in `_COMPONENTS` plus the i18n role key. ffmpeg, Chromium, and browsertrix-crawler are deliberately **not** in the registry — they change rarely and only at image-rebuild boundaries; surfacing them as "updateable" would be misleading. Their installed versions remain visible via the existing `/api/system/version` response for diagnostics.
- **Latest-version lookup** — PyPI JSON API (`https://pypi.org/pypi/{pkg}/json`) for Tier 1 and GitHub release API (`https://api.github.com/repos/{repo}/releases/latest`) for Tier 2. Plain `urllib.request` over `asyncio.to_thread` per CLAUDE.md §13 #3 (prefer the standard library); no new runtime dependency. 5s per-call timeout, parallelized via `asyncio.gather`. Network errors are recorded in the cache as `error: "network"` so the UI can render "couldn't reach the registry" without blanking the whole table.
- **Capsule self-update is opt-in** — the Tier 2 lookup uses `config.CAPSULE_GITHUB_REPO` (env var; unset by default). When unset, [`compute_components_view`](app/updates.py) hides the Capsule row entirely so dev builds without an upstream release stream don't show a permanently-dashed self-update card. Production launchers set the env var to `<owner>/capsule` and the row appears with a `docker pull capsule:{arm64|amd64}` copy-paste callout that mirrors the per-arch tags emitted by [scripts/build-dist.sh](scripts/build-dist.sh).
- **Auto-check default ON, launch only** — the FastAPI lifespan hook ([`_lifespan` in `app/main.py`](app/main.py)) fires `asyncio.create_task(updates.auto_check_on_launch(...))` after `jobs_mod.orchestrator().rehydrate()`. Fire-and-forget — startup is never blocked. No 24h periodic task. The setting lives at `/config/settings.json` under `updates.auto_check`, persisted via the existing [`profiles.load_app_default` / `save_app_default`](app/profiles.py) helpers; threat-model-conscious users flip it off in Settings. CLAUDE.md §4.4 + §13 #7 are rewritten in lockstep so future Claude reading the doc doesn't flag the network call as a violation.
- **Cache** — `/config/version_cache.json` is the single source of truth read by `GET /api/system/updates`. Atomic write (tmp → rename), survives container restarts. The GET endpoint never makes a network call — only `POST /api/system/updates/check` and the launch-task do. Empty cache reads as `{"components": [], "updates_available": 0, "last_checked_at": null}`; the UI renders this as "Not checked yet."
- **Routes** — `GET /api/system/updates` (read cache + auto-check setting; no network), `POST /api/system/updates/check` (refresh; audited as `system.update_check` with `triggered_by: "manual"`), `PUT /api/system/updates/auto_check` (toggle; audited as `system.auto_check_changed` only when the value actually flips), `POST /api/system/updates/dismiss_banner` (audit-only — the cog dot persists past dismissal so chain-of-custody survives). The legacy `POST /api/system/update?component=X` keeps its current install path for Tier 1 and now returns a 400 with `i18n_key: "errors.update.requires_image_rebuild"` (vs. `errors.update.unknown_component`) when called against a Tier 2 component, so the frontend can render localized error copy.
- **UI surfaces** — Settings → Updates is a redesigned section with: subtitle explaining what gets contacted, opt-out auto-check toggle, "Last checked: {relative} · [Check now]" row, and per-component cards with installed/latest/status/CTA. Status badges follow CLAUDE.md §4.1 #1 (icon + colour + shape — green check / amber dot / rose wifi-off / gray dash). Tier 1 gets an "Update" button + a forensic-stance one-liner ("Applied until the next Capsule image rebuild."). Tier 2 gets a `docker pull` copy-paste with both arch comments. The home view ([`index.html`](app/static/index.html)) shows a dismissible amber banner above the URL form when `updates_available > 0`, and a small accent dot persists on the header settings cog past dismissal.
- **Audit actions** — `system.update_check` (per-component results + `triggered_by`), `system.auto_check_changed` (only when value differs), `system.update_dismissed` (chain-of-custody for "user saw banner and ignored"). Existing `system.updated` (Tier 1 install completion) is unchanged.
- **i18n** — new keys under `settings.update.*`, `home.banner.*`, `errors.update.*`, `header.settings.updates_available_aria`. All four bundles (en/ja/ar/es) updated; Arabic uses six-form plurals (`zero`/`one`/`two`/`few`/`many`/`other`) for the "N updates available" plural per CLAUDE.md §4.5.
- **Tests** — [`tests/test_updates.py`](tests/test_updates.py) covers the registry, settings round-trip, atomic cache write, `fetch_latest` with mocked PyPI + GitHub responses, `compute_components_view`, and `auto_check_on_launch` honouring the toggle + swallowing network errors. [`tests/test_main_updates.py`](tests/test_main_updates.py) drives the HTTP routes via `httpx.ASGITransport`. [`tests/test_auto_check.py`](tests/test_auto_check.py) confirms the lifespan task fires when enabled, skips when disabled, and never blocks startup on network failure. Network is stubbed at the `urllib.request.urlopen` boundary; no real socket is opened during tests.

## 16. Still open

- **RFC 3161 trusted timestamping** — deferred to v2 as opt-in.
- **Drop `browsertrix-crawler` from the Docker image** — the v0.6 in-session CDP→WARC writer now produces the WARC for every capture; browsertrix only fires as a fallback when warcio is missing or the writer raises. Once we have a release's worth of corpus data showing the in-session path is robust across our target sites, the binary can leave the image (saves ~150 MB on disk) and the fallback can become "no WARC, use the existing `page.capture_failed` audit row." Tracked here so the Dockerfile and the §"Hardening pass (v0.6)" entry are removed in lockstep.
- **End-to-end WARC validity test** — the v0.6 unit tests cover the warc_writer helpers (header filtering, encoding rewrite, HTTP wire-format reconstruction). A fixture-driven test that drives a real Playwright session against a static HTML page, runs `warcio check` over the output, and replays it via [replayweb.page](https://replayweb.page) is still TODO. Should land before the browsertrix removal above.
