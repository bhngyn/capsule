<h1><img src="app/static/icons/brand/logomark.svg" alt="Capsule" height="32" /></h1>

*Capture the web, with proof.*

A web-evidence capture tool for investigators — researchers, journalists, lawyers, and legal-discovery practitioners. For every URL you paste, Capsule captures the page (MHTML + screenshot + WARC), pulls the media if any, hashes everything, signs it, and files it under your case. Evidence exports as a signed zip + PDF report your recipient can verify with a small bundled script.

## What you get

- **Page snapshot** — MHTML + full-page PNG + WARC (browsertrix `page+resources`) of every URL.
- **Media** — yt-dlp downloads anything yt-dlp can; full info.json + description + thumbnail preserved.
- **Canonical filenames** — `{platform}__{uploader}__{title}__{date}__{id}.{ext}` so a library copied between machines stays browsable.
- **Cryptographic integrity** — every artifact has MD5 + SHA-256; every meta.json is signed with an Ed25519 keypair generated on first launch.
- **Tamper-evident audit log** — every state-changing operation is hash-chained; tampering breaks the chain at the modified row.
- **Per-case cookies** — upload a `cookies.txt` per case for authenticated capture; values are never logged or exported.
- **Evidence export** — signed zip + PDF + standalone `verify.py` bundled in. Recipient runs `python verify.py` to confirm integrity.
- **First-class RTL** — English, Japanese, and Arabic ship as fully translated locales.
- **One folder per capture** — the page snapshot, the media file, the per-item manifest PDF, and every signed sidecar live together in `/{case}/{stem}/`. Copy that one folder to share a single capture; the manifest PDF tells the recipient exactly what should be there and the hashes to expect.

## Run

```bash
docker build -t capsule:dev .
docker run --rm -p 8080:8080 \
  -v "$HOME/Documents/Capsule:/downloads" \
  -v "$HOME/.capsule-config:/config" \
  -e "CAPSULE_HOST_DOWNLOADS_DIR=$HOME/Documents/Capsule" \
  capsule:dev
```

Then open **http://localhost:8080**.

The container bundles a headless Chromium engine (Playwright) and yt-dlp; the image is about 1.7 GB on disk after first launch, downloaded as a ~430 MB gzipped bundle in the dist archive (see [scripts/build-dist.sh](scripts/build-dist.sh)).

For local development without Docker:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,evidence,capture]'
playwright install chromium
CAPSULE_DOWNLOADS_DIR=$PWD/_dev/downloads CAPSULE_CONFIG_DIR=$PWD/_dev/config \
  uvicorn app.main:app --port 8080
```

## Status

The backend is feature-complete through Phase 4: cases, jobs, capture pipeline (Playwright + yt-dlp), post-processing, hash-chained audit log, signed meta + evidence export with bundled verifier. The frontend SPA in v1 surfaces only the **downloader** (paste a link or list, watch the four-phase progress, find results in the recent-captures grid) and **Settings** (language, signing-key fingerprint, browser-extension pairing, yt-dlp updater). The case-management surfaces (Cases / Library / Item detail / Audit log) live on disk and over the API; the downloader uses them under the hood (every job lands in the default case — slug `downloads` for fresh installs, `quick-captures` preserved on legacy installs). EN, JA, ES, and AR ship as fully translated locales; the runtime ICU/RTL pipeline is the same shared path.

Out of scope for this release:

- WARC capture requires `browsertrix-crawler` on PATH; absent, the WARC artifact is skipped and the meta.json reflects that.
- Tailwind/Alpine/Lucide/IntlMessageFormat are still loaded from CDNs; self-hosting inside the image is a Dockerfile follow-up.
- RFC 3161 trusted timestamping is deferred to v2.

## Switching language

Click the language picker in the header, or visit `?lang=ja` or `?lang=ar`. Japanese loads Noto Sans JP. Arabic switches the entire layout to RTL, swaps fonts, and mirrors direction-implying icons.

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — the full project specification.
- [`docs/DESIGN.md`](docs/DESIGN.md) — visual language and component vocabulary.
- [`docs/HANDOFF.md`](docs/HANDOFF.md) — bridge document used during development.

## License

Capsule itself is MIT-licensed. See [LICENSE](LICENSE).

For the third-party software Capsule depends on, bundles, or invokes
(notably `browsertrix-crawler`, which is AGPL-3.0-or-later and is
shipped inside the Docker image), see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). If you redistribute
the Docker image or evidence-export bundles, include that file or a
substantially-equivalent notice.
