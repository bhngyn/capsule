<h1><img src="app/static/icons/brand/logomark.svg" alt="Capsule" height="32" /></h1>

*Capture the web, with proof.*

A web-evidence capture tool for investigators — researchers, journalists, lawyers, and legal-discovery practitioners. For every URL you paste, Capsule captures the page (MHTML + screenshot + WARC + HAR), pulls the media or image gallery if any, hashes everything, signs it, and files it under your case. Evidence exports as a signed zip + locale-aware PDF report your recipient can verify with a small bundled `verify.py` script.

> **v1.0.0** — see the [latest release](https://github.com/bhngyn/capsule/releases/latest) for double-clickable bundles for macOS (Apple Silicon + Intel) and Windows, plus a source build.

> Curious how this was built? See [docs/case-study/](docs/case-study/) — twelve real Claude Code exchanges, a methodology guide for decision-makers, and an editorial PDF with vector diagrams.

## What you get

- **Page snapshot** — MHTML + full-page PNG + WARC + HAR for every URL. The WARC is written in-session via a CDP→WARC writer (warcio) so the snapshot, the screenshot, and the archive all come from one navigation; `browsertrix-crawler` runs as a fallback if the in-session writer is unavailable.
- **Render fidelity** — lazy-loaded images promoted, autoplay videos paused on the first decoded frame, animations frozen for the still PNG only (MHTML/WARC keep the page's real animation behaviour), and an outer 60 s budget so a stalled site doesn't hang the capture.
- **Media-in-context screenshot** — a second viewport-sized PNG framed on the most prominent video player or video-host iframe, so a recipient can see what the investigator was actually looking at.
- **Media** — yt-dlp downloads anything yt-dlp can; full info.json + description + thumbnail preserved.
- **Image galleries** — when yt-dlp finds no video, gallery-dl steps in for image-only sources: Twitter image threads, Imgur albums, Pixiv posts, DeviantArt pages, Reddit galleries, Tumblr posts, image-board threads. Every image is hashed, indexed, and listed in the per-item manifest PDF.
- **Download options + per-job controls** — pick audio-only, cap quality (480p / 720p / 1080p / Best), or select subtitle languages from the Advanced disclosure on the home view. Every running capture has a per-job toolbar (Pause / Resume / Restart / Cancel); a stalled job (no progress for 90 s) gets an amber chip but is never auto-killed.
- **Canonical filenames** — `{platform}__{uploader}__{title}__{date}__{id}.{ext}` so a library copied between machines stays browsable.
- **Cryptographic integrity** — every artifact has MD5 + SHA-256; every meta.json is signed with an Ed25519 keypair generated on first launch.
- **Tamper-evident audit log** — every state-changing operation is hash-chained; tampering breaks the chain at the modified row.
- **URL-canonical de-duplication** — pasting `?utm_source=email` and `?utm_source=tweet` for the same video collapses to one entry; the duplicate-detection modal opens before any work is done so you can open the existing capture, re-capture as a new sibling entry (`__c2`, `__c3`, …), or cancel.
- **Per-case cookies, with a browser extension** — the recommended path is the bundled Capsule extension (Chrome / Firefox / Edge): pair it with the UI, click "Send this tab", and the active tab's cookies (including HttpOnly and partitioned) ride to the capture. A per-case `cookies.txt` upload remains as a fallback. Values are never logged or exported — only the cookie set's SHA-256 enters `meta.json`.
- **Evidence export** — signed zip + PDF + standalone `verify.py` bundled in. Recipient runs `python verify.py` to confirm integrity.
- **First-class RTL** — English, Japanese, Spanish, and Arabic ship as fully translated locales; Arabic flips the entire layout, mirrors direction-implying icons, and uses Noto Sans Arabic.
- **One folder per capture** — the page snapshot, the media file, the per-item manifest PDF, the per-item human-readable report PDF, and every signed sidecar live together in `/{case}/{stem}/`. Copy that one folder to share a single capture; the manifest PDF tells the recipient exactly what should be there and the hashes to expect.

## Install (recommended)

The fastest install is the bundled launcher — no terminal, no `docker pull`. From the [latest release](https://github.com/bhngyn/capsule/releases/latest), download the bundle for your platform:

| Platform                    | Download                          | Notes                                                    |
|-----------------------------|-----------------------------------|----------------------------------------------------------|
| **macOS — Apple Silicon**   | `Capsule-mac-applesilicon.zip`    | M1 / M2 / M3 / M4. ~430 MB compressed.                   |
| **macOS — Intel**           | `Capsule-mac-intel.zip`           | Pre-Apple-Silicon Macs. ~430 MB compressed.              |
| **Windows 10 / 11**         | `Capsule-windows.zip`             | x86_64 / amd64. ~430 MB compressed.                      |
| **Both arches in one zip**  | `Capsule.zip`                     | macOS + Windows launchers + both image tarballs. ~830 MB.|
| **Source build**            | `Capsule-source.zip`              | Builds the image locally from a vendored source tree.    |

Then:

1. **Install Docker Desktop** (free) and open it once so it's running. Docker is what lets Capsule run on your computer without you needing to install Python, browsers, or yt-dlp yourself.
   - macOS: <https://www.docker.com/products/docker-desktop>
   - Windows: <https://www.docker.com/products/docker-desktop>
2. Unzip the bundle.
3. **Double-click** the launcher inside it:
   - macOS: `Capsule.command`
   - Windows: `Capsule.bat`
4. The launcher loads the bundled image (~1.7 GB unpacked on first run, ~3 s after that) and opens your browser at <http://localhost:8080>.

Each bundle ships its own per-architecture image tag (`capsule:arm64` or `capsule:amd64`); the launcher refuses to silently fall back to a stale `capsule:latest`, so an Apple Silicon Mac never accidentally runs the AMD64 image (and vice versa).

The bundle also contains the locale-aware quick-start and user-guide PDFs (English, Japanese, Spanish, Arabic) so you can read them offline before you launch.

## Run from source (developers)

```bash
git clone https://github.com/bhngyn/capsule.git
cd capsule
docker build -t capsule:dev .
docker run --rm -p 8080:8080 \
  -v "$HOME/Documents/Capsule:/downloads" \
  -v "$HOME/.capsule-config:/config" \
  -e "CAPSULE_HOST_DOWNLOADS_DIR=$HOME/Documents/Capsule" \
  capsule:dev
```

Then open **<http://localhost:8080>**.

For a Python venv (no Docker):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,evidence,capture]'
playwright install chromium
CAPSULE_DOWNLOADS_DIR=$PWD/_dev/downloads CAPSULE_CONFIG_DIR=$PWD/_dev/config \
  uvicorn app.main:app --port 8080
```

To rebuild the dist bundles yourself, run [`scripts/build-dist.sh`](scripts/build-dist.sh) — it drives `docker buildx` for both arches, saves the per-arch tarballs with their content digests, and renders the launchers from `dist-templates/`.

## Status

v1.0.0 is the first stable release. The backend is feature-complete: cases, jobs, capture pipeline (Playwright + yt-dlp + gallery-dl + in-session CDP→WARC + HAR + console capture), post-processing, hash-chained audit log, signed meta + evidence export with bundled verifier. The frontend SPA in v1 surfaces only the **downloader** (paste a link or list, watch the four-phase progress, find results in the recent-captures list, with download-options and per-job pause/resume/restart/cancel controls) and **Settings** (language, signing-key fingerprint, browser-extension pairing, yt-dlp / gallery-dl updater). The case-management surfaces (Cases / Library / Item detail / Audit log) live on disk and over the API; the downloader uses them under the hood (every job lands in the default case — slug `downloads` for fresh installs, `quick-captures` preserved on legacy installs). EN, JA, ES, and AR ship as fully translated locales; the runtime ICU/RTL pipeline is the same shared path.

Out of scope for this release:

- Tailwind / Alpine / Lucide / IntlMessageFormat are still loaded from CDNs; self-hosting inside the image is a Dockerfile follow-up.
- RFC 3161 trusted timestamping is deferred to v2.

## Switching language

Click the language picker in the header, or visit `?lang=ja`, `?lang=es`, or `?lang=ar`. Japanese loads Noto Sans JP. Arabic switches the entire layout to RTL, swaps fonts to Noto Sans Arabic, and mirrors direction-implying icons.

## Keeping Capsule current

yt-dlp and gallery-dl ship updates several times a month — sites change, extractors break, fixes land fast. Capsule auto-checks once at launch (opt-out, default ON) and surfaces any updates in **Settings → Updates** with a per-component card showing installed and latest versions.

- **yt-dlp / gallery-dl** update in-place via a button. Effective until your next image rebuild.
- **Capsule itself** updates by `docker pull`-ing a new image. The Updates card shows the right per-arch command to copy.
- **ffmpeg, Chromium, browsertrix-crawler** ship with the Capsule image; they update when you pull a new image.

Auto-check fires once at startup, hits PyPI and (optionally) GitHub, and never installs anything by itself. Every check is recorded in the audit log. Investigators with strict threat-model concerns flip the toggle off in Settings. Full details in [`docs/UPDATING.md`](docs/UPDATING.md).

## Documentation

End-user docs (also bundled inside every release zip):

- [`docs/quickstart.en.md`](docs/quickstart.en.md) / [`.ja`](docs/quickstart.ja.md) / [`.es`](docs/quickstart.es.md) / [`.ar`](docs/quickstart.ar.md) — five-minute guide to your first capture.
- [`docs/user-guide.en.md`](docs/user-guide.en.md) / [`.ar`](docs/user-guide.ar.md) — the full investigator-facing guide. (PDF versions ride along in the release bundles and in `docs/`.)
- [`docs/COOKIES.md`](docs/COOKIES.md) — how to authenticate captures, including the recommended browser-extension path.
- [`docs/EXTENSION.md`](docs/EXTENSION.md) — installing and pairing the browser extension.
- [`docs/UPDATING.md`](docs/UPDATING.md) — how the auto-check works, the two update tiers, and how to apply a new Capsule image.

For developers and reviewers:

- [`CLAUDE.md`](CLAUDE.md) — the full project specification (what to read before changing on-disk layout, integrity, or i18n).
- [`docs/DESIGN.md`](docs/DESIGN.md) — visual language and component vocabulary.
- [`docs/HARDENING.md`](docs/HARDENING.md) — the v0.2 + v0.6 hardening passes (cookies, ad/tracker blocking, render fidelity, in-session WARC).
- [`docs/HANDOFF.md`](docs/HANDOFF.md) — bridge document used during development.
- [`docs/case-study/`](docs/case-study/) — annotated Claude Code transcripts that produced this codebase.

## Verifying evidence (for recipients)

Every evidence-export zip contains a standalone `verify.py` (~100 lines, `cryptography` is the only dependency). Run it inside the unzipped folder:

```bash
python verify.py
```

It re-hashes every file, checks the Ed25519 signature on `manifest.json`, validates each per-item `meta.json.sig`, and replays the audit-log hash chain. PASS / FAIL with details — no need to install Capsule itself.

## License

Capsule itself is MIT-licensed. See [LICENSE](LICENSE).

For the third-party software Capsule depends on, bundles, or invokes (notably `browsertrix-crawler`, which is AGPL-3.0-or-later and is shipped inside the Docker image as a fallback for the in-session WARC writer), see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). If you redistribute the Docker image or evidence-export bundles, include that file or a substantially-equivalent notice.
