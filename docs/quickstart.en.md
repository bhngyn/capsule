# Capsule Quick Start

*Capture the web, with proof — in five minutes.*

Capsule lets investigators save web pages, videos, and image galleries in a way that holds up to later scrutiny. Every capture preserves the page exactly as it was, downloads the media if there is any, and signs the result so a recipient can later confirm nothing changed.

This guide gets you from zero to your first capture.

---

## What you need

- A computer running **macOS 12+** or **Windows 10/11**.
- **Docker Desktop** (free). Capsule runs inside it so you don't have to install Python, browsers, or yt-dlp yourself.
- About **2 GB of free disk space** for the first download.

> Docker Desktop is a free tool that lets Capsule run on your computer without you needing to install anything else. Download it at <https://www.docker.com/products/docker-desktop>.

---

## Install in one click

1. Install Docker Desktop and open it once. Wait until the icon in the menu bar (macOS) or system tray (Windows) shows that it is running.
2. **Double-click** the launcher in the Capsule folder:
    - macOS: `Capsule.command`
    - Windows: `Capsule.bat`
3. The first time, the launcher loads Capsule from the bundled image (~2 GB unpacked). After that it takes about three seconds.
4. Your browser opens directly to Capsule's downloader.

That's it. No terminal, no commands.

![Capsule downloader](screenshots/downloader.en.png)

---

## Your first capture

The whole UI is one screen: paste a link at the top, watch the four-phase progress strip, find the result in the recent-captures list below.

1. Paste any URL into the input box — a YouTube video, a tweet, an image gallery, a news article. Press **Capture**.
2. To capture several URLs at once, switch to the **Many links** tab, paste one URL per line, and press **Capture all**.
3. Capsule does four things, in order: snapshots the page, downloads any media, hashes every file, and signs the result. You see each phase light up in turn.
4. When it finishes, the item appears in the **Recent captures** list with an integrity hint and a capture-kind chip (Media, Page snapshot, or Image gallery).
5. If you paste a URL that's already been captured in this case, an **Already captured** dialog opens before any work is done. Pick **Open existing** to jump to the saved folder, **Re-capture as new entry** to keep both copies side-by-side, or **Cancel**.

For every URL Capsule saves:

- A full-page screenshot,
- A self-contained snapshot of the page (MHTML),
- A WARC archive of the page and every sub-resource it loaded,
- The video, audio, or every image of an image gallery (Pixiv, Imgur, Twitter image threads, DeviantArt, Reddit galleries, Tumblr, …) when there is any,
- A JSON sidecar with all the technical details,
- MD5 and SHA-256 hashes of every file,
- A locale-aware **manifest PDF** (verifier-ready, full hashes) and a **report PDF** (human-readable: provenance, description, tools, capture report),
- A signature you and others can verify.

Even on pages with no media, the page snapshot is preserved — so you always have something.

---

## Where things go

Capsule saves your captures to a folder you can browse like any other:

- macOS: `~/Documents/Capsule/`
- Windows: `%USERPROFILE%\Documents\Capsule\`

Every capture lives in its own self-contained per-item folder under the case directory:

```
~/Documents/Capsule/downloads/
└── {stem}/
    ├── {stem}.{ext}                  ← media file (if any)
    ├── {stem}.meta.json              ← canonical metadata record
    ├── {stem}.meta.json.sig          ← detached Ed25519 signature
    ├── {stem}.checksums.txt          ← md5sum/sha256sum compatible
    ├── {stem}.page.mhtml             ← page snapshot
    ├── {stem}.page.png               ← full-page screenshot
    ├── {stem}.page.warc.gz           ← WARC archive
    └── reports/
        ├── {stem}.manifest.pdf       ← full hashes (A4 landscape)
        └── {stem}.report.pdf         ← human-readable report
```

Image-gallery captures add `{stem}.001.jpg`, `{stem}.001.json`, … and `{stem}.gallery_info.json` next to the other files. The `downloads/` slug is the default case; additional cases live in sibling folders (`~/Documents/Capsule/{case-slug}/`).

---

## Stop and restart

- **To stop the app:** quit Docker Desktop, or run `docker stop capsule` in a terminal.
- **To start it again:** double-click the launcher.
- Capsule **does not start automatically** when you turn on your computer. You launch it when you want to use it; it stays out of the way otherwise.

---

## Next steps

- The downloader is the whole UI in v1: paste a link, watch the four-phase progress, find the result in the recent-captures list. The **Clear list** button at the top of that list lets you wipe the case (with a destructive-action confirmation that also offers to export an evidence bundle first).
- **Download options** sit in an Advanced disclosure on the home view — pick **Audio only**, cap quality at **720p**, or select subtitle languages. Each running capture has a per-job toolbar (**Pause** / **Resume** / **Restart** / **Cancel**) and an amber chip if it stalls (no progress for 90 s). Capsule never auto-kills a stalled job; the chip is informational and clears the moment progress resumes.
- Open **Settings** (cog in the header) to switch language, view your signing-key fingerprint, pair the browser extension, or check for yt-dlp / gallery-dl updates.
- Forensic data — case-grouped folders, the hash-chained audit log, signed evidence-export bundles — is still produced for every capture. It lives on disk under `~/Documents/Capsule/downloads/` and over the API. See the **User Guide** for evidence handoff and verification.
- The **Help** menu in Docker Desktop is your friend for any Docker-related issues.

If something goes wrong, every error has a "Show technical details" button — copy that text into a bug report and we can help.
