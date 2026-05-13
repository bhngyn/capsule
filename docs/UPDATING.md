# Keeping Capsule current

Capsule's job is to capture the web; the web changes daily; the libraries that
do the capturing — yt-dlp and gallery-dl in particular — have to keep up. This
guide explains how Capsule surfaces updates and how to apply them.

The short version:

- Capsule asks PyPI and (optionally) GitHub **once at launch** whether newer
  versions exist.
- It **never installs anything by itself.** Every update needs your click.
- Every check, every dismissal, every install is recorded in the audit log.

You can switch the launch check off in Settings → Updates if you'd rather
Capsule make zero unsolicited network calls.

## What you'll see

### A dot on the settings cog

When the launch check finds something behind, the gear icon in the header
gets a small amber dot. The dot stays until the update is applied. You can
dismiss the home-view banner without clearing the dot — that's deliberate
(chain-of-custody).

### A banner above the URL form

If updates are available, a dismissible amber row appears at the top of the
home view: "*N updates available. Review in Settings.*" Clicking
**Review in Settings** takes you to the Updates section.

### Settings → Updates

Three things live here:

1. **Auto-check toggle.** Default ON. One launch-time check, no periodic
   polling. Flip off if you'd rather check manually.
2. **Last checked / Check now.** Refresh the cache on demand.
3. **Per-component cards.** One row per updateable runtime, with installed
   version, latest version, a status badge, and either an **Update** button
   (Tier 1) or a `docker pull` copy-paste callout (Tier 2).

## The two update tiers

### Tier 1 — yt-dlp and gallery-dl

These run inside the Capsule container and are pip-installable. When you
click **Update**, Capsule runs `pip install --upgrade <package>` in-process
and refreshes the version display.

The forensic small print: an in-container pip update **is reverted on the
next image rebuild**. That's normal — the next time you `docker pull` a
newer Capsule image, it ships its own pinned baseline. The audit log records
both events, so a future reviewer can trace the lifecycle.

If you need a long-lived bump for a specific extractor, the right path is
"wait for the next Capsule image" or "rebuild the image with a pinned
version" rather than relying on the in-container update surviving forever.

### Tier 2 — Capsule itself

Capsule the application is shipped as a Docker image. The Capsule update
path lives outside the container — you can't `docker pull` from inside the
running container. So the Tier 2 card shows a copy-paste command instead of
a button:

```
docker pull capsule:arm64    # Apple Silicon
# docker pull capsule:amd64    # Intel / Windows
# Then re-run the Capsule launcher.
```

Pick the line for your machine, paste it into a terminal, then run the
launcher (`Capsule.command` on macOS, `Capsule.bat` on Windows) again. The
launcher detects the new image digest and starts using it on the next run.

The Tier 2 row only appears if the operator has set the
`CAPSULE_GITHUB_REPO` environment variable — dev builds without an upstream
release stream skip this lookup entirely.

### What about ffmpeg, Chromium, browsertrix?

These ride with the Capsule image too, but they don't have their own update
card. Reason: they change rarely (often once or twice a year, vs. several
times a month for yt-dlp), and a mid-session bump risks shifting the
`chromium_version` recorded in `meta.json` for any in-flight capture, which
is a forensic chain-of-custody problem we'd rather avoid. Pulling a new
Capsule image is the path for these.

You can still inspect their installed versions via
`GET /api/system/version` — the diagnostic surface is unchanged.

## Privacy and audit trail

The launch check makes at most three network calls per startup:

- `https://pypi.org/pypi/yt-dlp/json`
- `https://pypi.org/pypi/gallery-dl/json`
- `https://api.github.com/repos/<owner>/capsule/releases/latest` (only if
  `CAPSULE_GITHUB_REPO` is set)

Capsule sends a `User-Agent` header identifying the version and nothing
else — no machine ID, no usage telemetry. Each call has a 5-second timeout;
the launch check never blocks startup. PyPI and GitHub log requesting IPs
in their normal access logs as a function of being public registries; that's
the same exposure as `pip install` or `git pull`.

Every check writes a `system.update_check` row to the audit log, with the
trigger (`launch` or `manual`) and the per-component results. The row joins
the rest of the hash-chained audit trail, so a future reviewer can confirm:

- When auto-check was on or off.
- What the registry replied.
- Which updates the user dismissed.
- Which updates the user actually applied.

## Disabling auto-check

Settings → Updates → flip off **Check for updates automatically**. The
toggle persists in `/config/settings.json` and survives container
restarts. With auto-check off, Capsule makes zero unsolicited network calls
related to versions; you can still hit **Check now** at any time. Toggling
off is itself audit-logged (`system.auto_check_changed`).

## Programmatic surface

For automation (CI, headless deployments) the same data is on:

- `GET /api/system/updates` — read the cached snapshot. No network call.
- `POST /api/system/updates/check` — refresh the snapshot. One network call
  per source. Audited as `system.update_check`.
- `PUT /api/system/updates/auto_check` (`{"enabled": true|false}`) — toggle.
- `POST /api/system/update?component=yt-dlp` — install a Tier 1 update.
  Returns 400 with `i18n_key: "errors.update.requires_image_rebuild"` for
  Tier 2 components.
