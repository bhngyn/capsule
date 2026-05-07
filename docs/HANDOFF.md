# Handoff — pick up here

This is the bridge from the conversation that scaffolded Phase 0 to whichever conversation continues the work. Read it once, top to bottom, before you touch anything.

---

## TL;DR

**Capsule** is a Dockerized web-evidence capture tool for investigators (journalists, researchers, lawyers, legal-discovery). For every URL: page snapshot (MHTML + screenshot + WARC) + media (yt-dlp) + signed manifest + tamper-evident audit log + signed-zip + PDF export. First-tier languages **English + Arabic** (full RTL); Spanish + French follow. Court admissibility is a goal.

**Status: Phase 0 complete and verified.** The skeleton runs, the demo UI renders in EN and AR, and `/api/i18n/{lang}` works. The capture pipeline, signing, audit log, DB, cases, and full UI are unbuilt. **Phase 1 (backend core) is next.**

Working directory: `/Users/brian/Documents/ytdlp/`. The on-disk dir name is `ytdlp/` (historical); the user-facing brand and Docker image are **Capsule**.

---

## Read-in order (15 minutes)

1. **[CLAUDE.md](../CLAUDE.md)** — full project spec. Sections most likely to bite you: §5 (filename + path layout), §7 (signing), §8 (audit-log hash chain), §9 (DB schema), §11 (cookies), §13 (working agreements).
2. **[docs/DESIGN.md](DESIGN.md)** — visual language. Required reading before any frontend or PDF-report work.
3. **This file (`HANDOFF.md`)** — what you're reading.
4. **[README.md](../README.md)** — what users see.

CLAUDE.md is the source of truth. If anything in this handoff disagrees with CLAUDE.md, **CLAUDE.md wins** — and update this file.

---

## Decisions already locked (don't re-litigate)

| Topic | Decision |
|---|---|
| Project name | **Capsule.** Tagline: *Capture the web, with proof.* |
| Audience | Investigators: journalists, researchers, lawyers, legal-discovery |
| Threat model | Safe environment (no Tor/proxy/at-rest-encryption in v1) |
| Multi-user | No. Single investigator, single laptop |
| UI surface | v1 ships only the **downloader** (paste-a-link or batch list) plus **Settings**. Backend Cases / Library / Item / Audit endpoints stay intact and are reachable via the API |
| Accent | Tailwind `teal-600` (#0D9488). Single fixed accent — no mode-aware switching |
| Capture scope | Every URL gets the full preservation package; media is optional (`capture_kind` = `media` \| `page_only`) |
| Page capture | Playwright (MHTML + screenshot) + browsertrix-crawler WARC, scope=`page+resources` |
| Cookies | Per-case file at `/config/cases/{slug}/cookies.txt`; auto-attach for known social-media domains; **never** logged or exported |
| Signing | Auto-generate Ed25519 keypair on first run; importable; fingerprint exposed |
| Updates | Manual button only. **No automatic GitHub polling.** No telemetry |
| Raw fragment retention | Off by default; per-case opt-in |
| Frontend stack | Tailwind + Alpine.js + Lucide icons + IntlMessageFormat. No build step at runtime; Tailwind compiled at Docker build time (Phase 2) |
| Time | UTC in storage; local TZ in display via `Intl.DateTimeFormat` |
| First-tier langs | `en`, `ar` (generic Arabic, MSA). Then `es`, `fr` |
| Concurrent captures | Default 2 (Chromium memory contention). Configurable in Settings |
| Duplicate handling | Modal: Open existing / Re-capture as new (`__c2`/`__c3` suffix) / Cancel |
| Illustrations | unDraw — single-colour, recolourable to active accent |
| Image size | ~2GB once Playwright + browsertrix land. Documented in README |

The only items still genuinely open are **logo refinement** (designer task, non-blocking) and **RFC 3161 trusted timestamping** (deferred to v2).

---

## What Phase 0 produced

```
ytdlp/
├── CLAUDE.md, README.md, LICENSE, .dockerignore
├── Dockerfile, docker-compose.yml, pyproject.toml
├── .venv/                              ← local: fastapi + uvicorn installed
├── app/
│   ├── __init__.py, config.py, i18n.py, main.py
│   ├── i18n/{en,ar}.json               ← 49 keys each, ICU plurals
│   └── static/{index.html, app.js, styles.css}
└── docs/{DESIGN.md, HANDOFF.md}
```

Verified working endpoints:
- `GET /` → demo HTML
- `GET /api/i18n/{en,ar}` → bundle + `dir`
- `GET /api/system/version` → `{"app":"0.1.0"}`
- `GET /static/{app.js,styles.css}` → 200

The demo HTML showcases every visual-language element (paste preview, phase strip, integrity badges, empty state) but does no real work yet. CDN-loaded Tailwind/Alpine/Lucide/IntlMessageFormat — to be self-hosted in Phase 2.

---

## What's next: Phase 1 (backend core)

Per CLAUDE.md plan: backend, no UI, no capture pipeline yet. Suggested order — each item is self-contained and testable before moving on:

1. **`app/sanitize.py` + `tests/test_sanitize.py`** — pure logic. Fixtures must include Arabic, Hebrew, CJK, Windows-reserved names, NFKC tricks, length truncation, collision suffix logic. **Start here** — it's the lowest-risk way to validate your test infra.
2. **`app/platforms.py`** — `extractor_key` → friendly name; `is_social(domain)` returning bool. List in CLAUDE.md §5/§11.
3. **`app/db.py` + migration runner** — create the schema from CLAUDE.md §8/§9. SQLite at `/config/library.db`. Schema-version table for future migrations.
4. **`app/signing.py` + tests** — Ed25519 keygen on first call, persist to `/config/keys/`, sign + verify helpers. `cryptography` from PyPI.
5. **`app/audit.py` + tests** — append-only writer with hash-chain (SHA-256 of canonical-encoded prev row). Chain verifier. CLAUDE.md §8 has the schema and canonical-encoding rule.
6. **`app/cases.py`** — case CRUD; creates `/downloads/{slug}/` and `/config/cases/{slug}/`; slug sanitization shares `app.sanitize`.
7. **`app/cookies.py`** — per-case Netscape cookies parser; lists domains + expiries; **never logs values**.
8. **`app/errors.py`** — yt-dlp output → friendly i18n key mapping (§4.7 table).
9. **`app/classify.py`** — given a URL: resolve redirects, identify platform, decide `capture_kind` and which cookies to attach.
10. **`app/ytdlp_runner.py`** — subprocess wrapper. Suppress metadata muxing. Parse `--progress-template` JSON to an `asyncio.Queue`.
11. **`app/postprocess.py`** — rename, hash, write all sidecars, sign meta.json, insert DB row, append audit entries.
12. **API endpoints in `app/main.py`** — wire up `/api/cases`, `/api/jobs`, `/api/audit`, `/api/cookies`, etc. (see §2 list).

Phase 2 is the capture pipeline (Playwright + browsertrix); Phase 3 is the frontend EN+AR; Phase 4 is evidence export; Phase 6 is ES + FR translations.

The agreement in CLAUDE.md §13.4 is binding: anything that touches integrity, filenames, or evidence ships with tests.

---

## Verify Phase 0 still works (30 seconds)

```bash
cd /Users/brian/Documents/ytdlp
source .venv/bin/activate
uvicorn app.main:app --port 8080 &
sleep 2
curl -s http://127.0.0.1:8080/api/i18n/ar | python -c "import sys,json; d=json.load(sys.stdin); assert d['dir']=='rtl' and len(d['messages'])>=49; print('OK')"
kill %1
```

If that passes, the scaffold is intact. If it fails, fix it before adding new code — Phase 1 builds on Phase 0.

---

## Working agreements with this user

Inferred from how the project came together. Not exhaustive; observe and adjust.

- **Direct, terse, action-biased.** When in doubt, do the work and report. They will course-correct if needed.
- **Defaults preferred over questions** when the decision is low-stakes. Batch genuinely-load-bearing questions; don't spam.
- **CLAUDE.md is the source of truth.** When the spec changes, edit CLAUDE.md — don't carry context only in conversation.
- **No telemetry, no silent network calls, ever.** This project's audience demands it. If you write code that calls out to the internet, it must be triggered by an explicit user click and must appear in the audit log.
- **Arabic is not an afterthought.** Build screens with `<html dir="rtl">` first. If a screen works in Arabic, it works everywhere; the reverse is not true.
- **Visual-first.** Reach for an icon before a string. Status uses icon + colour + shape, never colour alone.

---

## Non-obvious things that will save you 20 minutes

- **The on-disk directory is `ytdlp/`, not `capsule/`.** The brand pivot to "Capsule" came mid-spec; the working directory was kept as-is. Docker image and user-facing surfaces use `capsule`.
- **Phase 0 uses CDN dependencies** (Tailwind Play, Lucide, Alpine, IntlMessageFormat). They're documented as temporary in `index.html`. Phase 2 self-hosts them inside the container.
- **`tailwind.config` is inlined in `index.html`** for the same reason — Tailwind compiled at Docker build time arrives in Phase 2.
- **The i18n bundle is merged with English fallback** server-side (`app/i18n.py:merged_with_fallback`). This means a half-translated locale never produces undefined keys on the frontend. Don't break this property.
- **`app/static/styles.css` declares `--accent` / `--accent-ink` CSS variables** holding teal-600. The Tailwind config maps `accent` / `accent-soft` / `accent-ink` to those variables. Use those tokens, never raw teal.
- **`<bdi>` wraps every user-content text node** (titles, URLs, uploader names) in `index.html`. Preserve this rule — bidi text in Arabic UI breaks horribly without it.
- **The `.icon-directional` class** mirrors icons under RTL. Apply to chevrons, arrows. **Never** to brand or platform marks.
- **`prefers-reduced-motion` is wired up globally** in `styles.css` to kill all transitions. Keep it that way.
- **No persistent memory entries.** The user's memory directory at `~/.claude/projects/-Users-brian-Documents-ytdlp/memory/` is intentionally empty — the audience/decisions all live in CLAUDE.md, which is loaded automatically. Don't duplicate spec content into memory.

---

## If you find yourself uncertain

Ask. The user prefers a focused question over a wrong-direction half-day.

For genuinely-load-bearing decisions, present 2–3 options with a recommendation and the main tradeoff. For low-stakes choices (variable names, helper module organization), pick one and move on.
