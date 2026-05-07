# Capsule Quick Start

*Capture the web, with proof — in five minutes.*

Capsule lets investigators save web pages, videos, and posts in a way that holds up to later scrutiny. Every capture saves the page exactly as it was, downloads the media if there is any, and signs the result so a recipient can later confirm nothing changed.

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
3. The first time, the launcher downloads Capsule (~2 GB). After that it takes about three seconds.
4. Your browser opens to the Cases dashboard.

That's it. No terminal, no commands.

![Cases dashboard](screenshots/dashboard.en.png)

---

## Your first capture

1. Click **+ New case**. Give it a short, memorable name. A case is one investigation — a folder for everything you collect about a topic.
2. Inside the case, click **Capture a link**.
3. Paste any URL — a video, a tweet, a news article. Press **Capture**.
4. Capsule does four things, in order: snapshots the page, downloads any media, hashes every file, and signs the result. You see each step light up.
5. When it finishes, the item appears in your library with a green integrity badge.

For every URL Capsule saves:

- A full-page screenshot,
- A self-contained snapshot of the page (MHTML),
- A WARC archive of the page and every sub-resource it loaded,
- The video or audio if there is any,
- A JSON sidecar with all the technical details,
- MD5 and SHA-256 hashes of every file,
- A signature you and others can verify.

Even on pages with no media, the page snapshot is preserved — so you always have something.

---

## Where things go

Capsule saves your captures to a folder you can browse like any other:

- macOS: `~/Documents/Capsule/`
- Windows: `%USERPROFILE%\Documents\Capsule\`

Each case is its own subfolder. Media files sit at the case root; the noisier sidecars (page snapshots, hashes, signatures) live in a `sidecars/` subfolder.

---

## Stop and restart

- **To stop the app:** quit Docker Desktop, or run `docker stop capsule` in a terminal.
- **To start it again:** double-click the launcher.
- Capsule **does not start automatically** when you turn on your computer. You launch it when you want to use it; it stays out of the way otherwise.

---

## Next steps

- The downloader is the whole UI in v1: paste a link, watch the four-phase progress, find the result in the Recent captures grid.
- Open **Settings** (cog in the header) to switch language, view your signing-key fingerprint, pair the browser extension, or check for yt-dlp updates.
- Forensic data — case-grouped folders, the hash-chained audit log, signed evidence-export bundles — is still produced for every capture. It lives on disk under `~/Documents/Capsule/quick-captures/` and over the API. See the **User Guide** for evidence handoff and verification.
- The **Help** menu in Docker Desktop is your friend for any Docker-related issues.

If something goes wrong, every error has a "Show technical details" button — copy that text into a bug report and we can help.
