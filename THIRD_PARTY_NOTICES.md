# Third-party notices

Capsule itself is MIT-licensed (see [LICENSE](LICENSE)). This document
inventories the third-party software it depends on, bundles, or invokes,
together with each component's license. Inclusion in this list does not
imply endorsement by the upstream projects.

If you redistribute Capsule (the source, the Docker image, or evidence
exports that bundle the standalone `verify.py`), include this file or a
substantially-equivalent notice.

## Direct Python dependencies (declared in `pyproject.toml`)

| Package            | License                       | Role                                                               |
|--------------------|-------------------------------|--------------------------------------------------------------------|
| FastAPI            | MIT                           | HTTP API framework                                                 |
| Uvicorn            | BSD-3-Clause                  | ASGI server                                                        |
| python-multipart   | Apache-2.0                    | multipart/form-data parsing for the cookies-upload endpoint        |
| Pydantic           | MIT                           | Request/response models (transitive via FastAPI; explicit usage)   |
| httpx              | BSD-3-Clause                  | Async HTTP client (used in `app/classify.py` for redirect walk)    |
| Playwright (Python)| Apache-2.0                    | Headless Chromium driver for the canonical page snapshot           |
| cryptography       | Apache-2.0 OR BSD-3-Clause    | Ed25519 keygen + sign/verify for the integrity record              |
| WeasyPrint         | BSD-3-Clause                  | HTML→PDF for the case report in evidence exports                   |
| Babel              | BSD-3-Clause                  | Backend-side i18n (error messages, PDF report)                     |
| yt-dlp             | Unlicense (public domain)     | Media downloader (subprocess invocation)                           |

All licenses listed above are compatible with Capsule's MIT license under
the standard "permissive code can use other permissive code" rule.

## Components Capsule invokes as a separate process

### browsertrix-crawler — AGPL-3.0-or-later

`browsertrix-crawler` is licensed under AGPL-3.0-or-later. Capsule invokes
it as an **external executable** via `asyncio.create_subprocess_exec`
(see [`app/capture.py`](app/capture.py)). Capsule does **not** statically
or dynamically link the browsertrix-crawler library, does not import any
Python module from it, and does not include any of its source code in
this repository.

Per the FSF's standard interpretation of GPL-family licenses, invoking a
program as a separate process via `exec` is "mere aggregation" and does
not place a copyleft requirement on the invoking software. Capsule
therefore remains under MIT.

If you redistribute the Capsule **Docker image**, however, the image
also contains a copy of `browsertrix-crawler` and your distribution must
honour AGPL-3.0-or-later for that copy: notably, when the image is run
as a network service, users interacting with it over the network must be
able to obtain the source for any modifications you made to
browsertrix-crawler. The unmodified upstream source is available at
<https://github.com/webrecorder/browsertrix-crawler>.

### Chromium

The Playwright Python distribution downloads a copy of Chromium at
install time. Chromium is BSD-3-Clause-licensed with a number of
component licenses; the license file is shipped with the Chromium
binaries. See <https://chromium.googlesource.com/chromium/src/+/main/LICENSE>.

## Bundled fonts (only present once the UI is built with fonts vendored)

| Font                | License                          |
|---------------------|----------------------------------|
| Inter               | SIL Open Font License 1.1        |
| Noto Sans Arabic    | SIL Open Font License 1.1        |

The OFL is GPL-compatible and permits redistribution, including as part
of a Docker image, provided each font is shipped with its license file
and is not sold by itself.

## Bundled iconography & illustrations

| Asset             | License                          | Notes                                                         |
|-------------------|----------------------------------|---------------------------------------------------------------|
| Lucide icons      | ISC                              | Bundled subset under `app/static/icons/lucide/`               |
| unDraw illustrations | Custom (free for commercial / non-commercial; no attribution required) | Bundled under `app/static/icons/illustrations/`               |
| Platform marks (YouTube, X, etc.) | Trademark of their respective owners | Used nominatively (to identify the platform a capture came from); not endorsed by those owners |

## Original works in this repository (MIT)

The following files were authored for Capsule and are MIT-licensed under
the project [LICENSE](LICENSE), notwithstanding any superficial similarity
to public-domain rule lists:

- **`app/static/blocklists/easylist-essentials.json`** — curated list of
  publicly-known third-party ad/tracker domains. Domain names are facts
  about commercial third-party services and are not, on their own,
  copyrightable; this file's curation choices are original work.
- **`app/static/blocklists/banner-hide.css`** — CSS selectors targeting
  the publicly-documented DOM identifiers used by named commercial
  consent-banner products. Element IDs and class names that third-party
  services attach to their widgets are facts about how those services
  ship their code; this file's selection is original work.
- **`extension/blocklists/easylist-essentials.json`** — byte-identical
  copy of the file above, shipped inside the extension because Chrome
  extensions cannot reach into `app/static/` at runtime.

## Cookie-export helper extensions (not bundled)

`docs/COOKIES.md` references several browser extensions investigators
may use to export `cookies.txt`. Those extensions are **not bundled** by
Capsule and have their own licenses; consult the relevant extension
store listing before relying on one.

## How to update this file

When adding a new dependency or vendoring a new asset:

1. Add a row to the matching table above.
2. Verify the upstream license is compatible with MIT.
3. If the new component is GPL-family, GPL-AGPL-LGPL, or any
   share-alike-style license, **stop** and discuss with the project
   maintainers — depending on how it's integrated, it can taint
   Capsule's redistribution terms.
