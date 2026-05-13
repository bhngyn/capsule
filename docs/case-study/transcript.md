# Capsule — development transcript

*Curated excerpts from the real Claude Code sessions that produced this codebase.*

---

## How this transcript was built

This is **not** a fabricated dialogue. Every user message and every Claude
response quoted below is a verbatim excerpt from a real Claude Code session log
captured on disk while Capsule was being built.

- **Source**: `~/.claude/projects/-Users-brian-Documents-ytdlp/` — 34 JSONL
  session files spanning 2026-05-06 14:31 UTC → 2026-05-08 03:03 UTC
  (≈ 36 hours of wall clock, ~89 commits, single author).
- **Selection**: of those 34 sessions, twelve are excerpted here. Picks were
  based on (a) clean correspondence to the v0.1 → v0.7 arc, (b) the presence
  of a teachable moment — a redirect, a clarification, a bug, a design pivot.
- **Editorial choices**:
  - User prompts are quoted **verbatim**, including casual phrasing and the
    occasional typo. The voice matters.
  - Claude's responses are often hundreds of lines (plans, tool calls, file
    edits). Excerpts here are usually the first 15–30 lines of substantive
    text, with `[…]` showing where I cut.
  - Tool-use blocks (`<Bash: …>`, `<Read: …>`, `<Agent>`, etc.) are summarized,
    not reproduced. The full event stream lives in the JSONL files.
  - Italic editor's notes in square brackets summarize what landed afterwards:
    files changed, version bumped, lessons.
- **Redaction policy**:
  - Cookie values, auth tokens, and API keys: none found (the project's
    audit-log spec already prohibits logging them); spot-grep confirmed.
  - Real third-party URLs the user pasted while debugging captures (e.g.
    twitter.com/x.com status URLs) are kept where they're load-bearing for
    the story (e.g. "twitter download failed") and replaced with
    `<example.com/case-target>` where a generic stand-in works fine.
  - The `~/Documents/ytdlp/` working directory path is kept as-is; it's the
    repo root and isn't sensitive.
- **One known gap**: the very first session — the one in which CLAUDE.md was
  authored from scratch — is **not** in this project's log directory. CLAUDE.md
  already existed when the earliest log we have starts ("Examine the claude.md
  and create an implementation plan…"). That spec-first move predates the
  project log dir and was almost certainly done in a separate session in a
  different working directory. We're being explicit about that gap rather than
  fabricating its content. The methodology document treats this gap as itself
  instructive — see [methodology.md §3](methodology.md).

A reproducibility script lives at
[`scripts/extract_session_excerpts.py`](../../scripts/extract_session_excerpts.py).
Run it and you'll get the same chronological index of sessions any future
reader can audit against the JSONL files.

---

## The arc, at a glance

| When (UTC)              | What landed                                             | Section |
|-------------------------|---------------------------------------------------------|---------|
| 2026-05-06 14:31        | i18n kickoff → "investigators" pivot                    | §1      |
| 2026-05-06 15:27        | Plan from HANDOFF.md                                    | §2      |
| 2026-05-06 17:44        | Simple downloader UI                                    | §3      |
| 2026-05-06 18:29        | "the logo looks like a button"                          | §4      |
| 2026-05-06 19:08        | First real-world bug: a Twitter download failure        | §5      |
| 2026-05-06 23:14        | End-of-day-1 code review                                | §6      |
| 2026-05-06 23:58        | "remove the court setting i only want the simple UI"   | §7      |
| 2026-05-07 02:08–04:14  | Storage reclaim → Japanese → Spanish (parallel tracks)  | §8      |
| 2026-05-07 16:37        | Smallest-possible distribution                          | §9      |
| 2026-05-07 18:55        | "handle duplicate videos better"                        | §10     |
| 2026-05-07 20:19        | "handle images as much as possible. perhaps gallery-dl?"| §11     |
| 2026-05-07 22:34        | Capsule.command failed on Apple Silicon                 | §12     |
| 2026-05-08 00:25        | "harden the page preservation"                          | §13     |
| 2026-05-08 01:38        | "ways to modify the download… restart…"                 | §14     |

---

## §1 — The "investigators" pivot

*Session `2c41f444-ff8e-4bf6-917b-03c69ef98adc`, 2026-05-06 14:31 UTC.*

This is the earliest session in the log directory. CLAUDE.md already exists
and is the only file in the repo. The conversation begins as an i18n planning
exercise — and ninety seconds in, a single user sentence rewrites the entire
brief.

> **User (14:31:49):**
> Examine the claude.md and create an implementation plan. first language to target is arabic and english. plan to extend to spanish and french

> **Claude (14:33:01):**
> Greenfield repo — only CLAUDE.md exists. Here's the implementation plan, ordered so Arabic/English ship first and Spanish/French slot in as data-only changes later.
>
> # Implementation plan — yt-dlp Studio
>
> ## Guiding constraints (non-negotiable, surface early)
>
> - **Arabic is RTL.** Layout, icons, progress bars, and CSS must use logical properties from day one. Bolting RTL on later is a rewrite, not a tweak.
> - **ICU MessageFormat plurals are required.** Arabic has six plural forms (`zero`, `one`, `two`, `few`, `many`, `other`) […]
> - **Bidi text is normal, not exceptional.** URLs (LTR) embedded in Arabic UI (RTL) appear constantly. Use `<bdi>` / Unicode bidi isolates […]

> **User (14:33:01):**
> This will be a tool for investigators

> **Claude (14:33:49):**
> That changes the design center. "Investigator" implies the artifacts may need to survive scrutiny — by editors, courts, opposing counsel, or peer researchers — possibly years after capture. Integrity, provenance, and operational security become first-class, not features.
>
> Let me save this context, then re-plan around it.

*[Editor's note: that one-line pivot is the most consequential message in
the entire log. Claude saved an "audience" memory file and re-planned around
forensic integrity. Every subsequent feature — Ed25519 signatures, hash-chained
audit log, transitive `meta.json.sig` binding, bundled `verify.py` — traces
back to this minute. The lesson: **front-load audience context before the
plan, not after**. The cost of changing course three sentences in was zero.
The cost of changing course three weeks in would have been a rewrite.]*

---

## §2 — Plan from HANDOFF.md

*Session `43785a64-de89-42c9-b7f2-32144a11f910`, 2026-05-06 15:27 UTC.*

The previous session ended with a written handoff (`docs/HANDOFF.md`); this
one picks it up. This is the spec-first pattern in microcosm: the previous
Claude wrote a structured handoff document so the next Claude could pick up
without context loss.

> **User (15:27:32):**
> look at handoff.md and create a plan to implement

*[Editor's note: 409 turns, 178 user messages. This session produced the
v0.1 backbone — FastAPI scaffolding, the four-phase capture pipeline, the
SQLite library schema, the Ed25519 signing module, and the audit-log
hash chain. The handoff-document pattern recurs throughout the project:
CLAUDE.md §15 plays the same role across sessions, and the per-version
"Resolved decisions" entries are the long-form handoff for whoever picks
up next.]*

---

## §3 — "Strip away everything but downloading"

*Session `0fe6328b-0eb6-4393-9794-5001bbb2dd85`, 2026-05-06 17:44 UTC.*

After the v0.1 backbone landed, the UI surface was the standard "investigator
workspace" the spec described: cases, library, item detail. Brian started
walking it back almost immediately.

> **User (17:44:00):**
> I want you to add a simple downloader interface that is the primary interface. It should strip away everything but the downloading and web page archiving features. It should allow users to start from either a single link or from a list of links

*[Editor's note: this is the seed of what CLAUDE.md §4.3 now calls the
"Downloader (home)" surface — the only UI the v1 ships. The structured
backend (cases, library, audit log) stayed intact; they just stopped having
UI in front of them. This is one of the project's most useful patterns:
**don't delete capability, just hide it from the UI**. The cases/library/
audit endpoints remained reachable via API the whole time, and the
extension uses them.]*

---

## §4 — "the logo looks like a button"

*Session `7502169f-d117-4e49-b9c0-0d1a50803e57`, 2026-05-06 18:29 UTC.*

A small, sharp design note that produced an outsized result.

> **User (18:29:38):**
> the logo looks like a button i can use. i want you to think deeply about something else that can represent this app. it's made to capture and preserve digital artifacts like videos or websites. the logo should have that in its spirit

> **Claude (18:30:21):**
> I'll take a quick look at any existing logo assets so I'm responding to the actual artifact, then think through directions.

*[Editor's note: this exchange produced the bell-jar-over-browser-window
mark documented in CLAUDE.md §15: "preservation/museum metaphor, deliberately
not a UI button." The "deliberately not a UI button" phrasing reads, in
hindsight, as a direct echo of Brian's "looks like a button I can use."
**Lesson: design feedback that names the wrong feeling ("looks like
something interactive when it shouldn't") is more useful than design feedback
that prescribes a fix.**]*

---

## §5 — First real-world bug

*Session `68dcbf35-d019-4b44-90f7-c916d1288515`, 2026-05-06 19:08 UTC.*

Six hours in, the first capture against a live target failed. This is one
of the moments the case study can't sanitize: the URL is real because it
mattered for diagnosis.

> **User (19:08:10):**
> something went wrong trying to download this video https://x.com/CuiMao/status/2051924735907504431 in the active container please look into it
> [Image: original 2940x1912, displayed at 2576x1675 — screenshot of the failure UI]

> **Claude (19:08:19):**
> I'll investigate this download failure. Let me start by exploring the codebase and the active container state.

*[Editor's note: the bug was a yt-dlp extractor mismatch on a video-thread
post; the cookies file wasn't being attached because the X/Twitter domain
wasn't yet in `is_social()`. Fix: extend `app/platforms.py` to recognize
`x.com` alongside `twitter.com`. This is the moment the
**`is_social(domain)` registry** earned its keep. Every social-media domain
that's been added since (Threads, Pixiv, DeviantArt, Tumblr, Patreon…) is
one line in that file.]*

---

## §6 — End-of-day-1 code review

*Session `51100bfa-19e8-4848-90f5-183ec31c50ce`, 2026-05-06 23:14 UTC.*

> **User (23:14:18):**
> conduct a code revirew

*[Editor's note: typo and all. The terseness is the point — by the
12-hour mark Brian had enough trust in the workflow to hand the AI a
five-character instruction and walk away. The session produced 264 turns
of output identifying redaction gaps, missing audit actions, and the
fact that `signing.verify()` was catching too broad an exception class.
Several of the v0.2 hardening-pass items (CLAUDE.md §15, "Hardening pass
v0.2") trace directly to this review. **Pattern: a periodic structured
code review, requested at a quiet moment, produces more value than ad-hoc
"is this right?" questions.**]*

---

## §7 — "remove the court setting"

*Session `35f719f9-0549-493c-bdc1-ee61212c8d55`, 2026-05-06 23:58 UTC.*

This is the session that became v0.3 ("Simple-view consolidation"). It is
also a textbook example of why Claude should clarify before pruning.

> **User (23:58:46):**
> remove the court setting i only want the simple UI

> **Claude (00:01:46):**
> Before I can plan this, I need to clarify something. The implementation has diverged from the CLAUDE.md spec in an important way: in the actual code, **Simple mode is just the downloader (paste-a-link)**, while **Court mode contains the entire investigator workspace** — cases, library, item detail, and the audit log. Top-level navigation (Cases / Library / Audit) only appears in Court.
>
> So "remove Court, keep Simple" could mean two very different things, and I want to make sure I get it right.

*[Editor's note: Claude paused, opened `AskUserQuestion`, and confirmed the
intent — keep the UI minimal, keep the backend intact. Without that pause,
"remove Court mode" would have ripped out the cases / library / audit log
endpoints — which v0.7's pause/resume controls now lean on, and which the
extension talks to directly. This is the cleanest argument in the entire
log for `AskUserQuestion`-style clarification: **a 30-second clarification
prevented a multi-day cleanup.**

The same session also pruned `POST /api/jobs/preflight`,
`POST /api/jobs/{id}/{pause,resume,cancel}`, and a handful of mutation
routes that "the simple view doesn't surface." Three of those routes —
pause, resume, cancel — were re-added in v0.7 (§14). That round-trip is
not a failure; it's the right shape. See methodology §11.]*

---

## §8 — Storage cleanup → Japanese → Spanish

*Sessions `9e2ff300-72eb-4f5b-bc89-4aa6f8a9535d` (02:08), `e5bb0ce5…` (02:25),
`b3ee9e3b…` (03:11), `bfb554df…` (03:58), `70c4a277…` (04:14), all
2026-05-07.*

A tight cluster of sessions that, together, illustrate the parallel-track
pattern documented in CLAUDE.md §15 ("Track A / Track B / Track C").

> **User (02:08, on `9e2ff300…`):**
> help me reclaim storage on this computer so i can build a dist for this tool. last time things crashed because i ran out of space

> **User (02:25, on `e5bb0ce5…`):**
> port the tool to japanese. remove the languages that we haven't addressed yet

> **User (03:11, on `b3ee9e3b…`):**
> extend the app toe spanish

> **User (03:58, on `bfb554df…`):**
> make sure that the translated versions are correct and have full coverage

> **User (04:14, on `70c4a277…`):**
> is the spanish addition implemented?

*[Editor's note: five sessions, three of them on parallel branches
(`feat/i18n-japanese-arabic-backend`, `feat/i18n-spanish`), interleaved with
a host-side "free up disk" detour. They all merge cleanly. The pattern is:
each session has a tightly-scoped goal; CLAUDE.md is the shared coordination
artifact; merges land back to `main` as soon as a track is verified. The
"is the spanish addition implemented?" question at 04:14 — three sessions
after the Spanish kickoff — is the verification pass: **don't trust your
own merge log, ask the AI to walk it.**]*

---

## §9 — Smallest possible distribution

*Session `5037463d-fa18-419f-845f-ca4e9c135934`, 2026-05-07 16:37 UTC.*

> **User (16:37:30):**
> I want you to determine how you can make the smallest complete app image for distribution for the users. Users may not have strong internet access to download a very large file.

> **Claude (16:37:42):**
> I'll plan how to shrink the distributable image. Let me start by exploring the current build setup and distribution mechanism in parallel.

*[Editor's note: this session produced the switch from the
`mcr.microsoft.com/playwright/python` base (≈ 2 GB) to a custom slim base
that ships only headless Chromium and three font families, plus the
`scripts/build-dist.sh` rewrite. End state: ≈ 1.7 GB on disk, ≈ 430 MB
gzipped per arch in the dist bundle. The "Users may not have strong
internet access" framing is what locked in the per-arch `tar.gz` layout
documented in CLAUDE.md §15 ("Distribution: per-arch image tags +
reproducible build script"). **Lesson: a one-line constraint about a
real user's environment is more valuable than a generic "make it
smaller" ask.**]*

---

## §10 — "handle duplicate videos better"

*Session `03d7efbd-20ee-42e8-bb1c-05cafa72487b`, 2026-05-07 18:55 UTC.*

> **User (18:55:04):**
> I want it to handle duplicate videos better

> **Claude (18:55:18):**
> I'll plan a "handle duplicates better" change. Let me first explore how duplicates currently work end-to-end so I can ask the right clarifying questions.

*[Editor's note: this single short prompt produced the v0.4 dedup pass —
a single 60-file commit that introduced [`app/url_canonical.py`](../../app/url_canonical.py), the new
`POST /api/jobs/preflight` route (re-introduced after v0.3 pruned its
predecessor), the §15 §"Resolved decisions: duplicate handling" block,
the dedup modal in the UI, the `__c2`/`__c3` sibling-row pattern, and
migration `004_canonicalize_url_hash.py`. The workflow: explore → plan →
clarify → implement, all in one session. The fact that *one short prompt
produced two thousand lines of diff* is exactly what teaches the
"vibe coding" pattern: **terse prompts work when the spec is crisp**.]*

---

## §11 — "handle images as much as possible"

*Session `148654ad-2ff4-4082-b132-90a91d47d84e`, 2026-05-07 20:19 UTC.*

> **User (20:19:19):**
> I also want this to handle images as much as possible. perhaps a gallery-dl integration?

> **Claude (20:19:24):**
> I'll explore the codebase to understand how the existing capture pipeline works, then design a plan for adding image-handling via gallery-dl integration.

*[Editor's note: 698 turns, 301 user messages — the longest session in
the log. Produced the v0.5 gallery-pass: a brand-new `gallery` capture-kind,
a Phase-3 fallback in the orchestrator, [`app/gallery_dl_runner.py`](../../app/gallery_dl_runner.py)
(mirrors yt-dlp_runner's contract), schema v6, eight new image-first
domains added to `is_social()`, the manifest-PDF thumbnail strip, and 12
sequential commits stacked in dependency order (runner → jobs → postprocess
→ schema → UI → PDF). No reverts.

The user's prompt suggested a tool by name (`gallery-dl`); Claude evaluated
it against the existing yt-dlp + Playwright setup and adopted it. **Pattern:
when a user names a tool in a "perhaps" sentence, treat it as a hypothesis
to be tested, not a directive — but in this case the hypothesis was right
and the integration was clean.**]*

---

## §12 — When the launcher failed on Apple Silicon

*Session `a342bfb4-25e9-435a-809e-4f2ec53ef89f`, 2026-05-07 22:34 UTC.*

The most important real-world test of a self-healing launcher: when it
*didn't* heal cleanly, what got fixed and why.

> **User (22:34:07):**
> I tried to open the docker container via capsule.command here /Users/brian/Documents/ytdlp/dist/Capsule-mac-applesilicon/Capsule.command

> **Claude (22:34:13):**
> I'll investigate what happened when you tried to open `Capsule.command`. Let me first check what's there and understand the launcher behavior.

*[Editor's note: the diagnosis: Docker Desktop had loaded a stale `latest`
tag from a previous build, masking the per-arch image the launcher had
just installed. The fix landed in `dist-templates/Capsule.command.in`:
the launcher now compares the loaded image's content digest against the
digest stamped in at build time, and force-reloads the bundled tar on
mismatch. CLAUDE.md §15 ("Distribution") documents this fix. **The pattern
this exchange teaches: every "the launcher didn't work" report should
become a new pre-flight check in the launcher template — bugs in
distribution code repay 100× because every user trips them.**]*

---

## §13 — "harden the page preservation"

*Session `15502680-982c-4aae-8f86-d57e2a2420a9`, 2026-05-08 00:25 UTC.*

The biggest single technical pass in the log.

> **User (00:25:01):**
> harden the page preservation. I want to be sure the screenshots, .warcs and .mhtml have the best representation of the page and the context the piece of media appears in as possible. are there ways to improve it?

> **Claude (00:25:10):**
> *[launches three parallel Explore subagents to map the current capture pipeline]*

*[Editor's note: this prompt produced v0.6 — the page-preservation
hardening (CLAUDE.md §15). New module [`app/warc_writer.py`](../../app/warc_writer.py) (single-session
CDP→WARC writer via `warcio`), animation-freeze CSS injection right
before screenshot, adaptive lazy-load step counting, shadow-DOM traversal,
media-context screenshots, per-page HAR sidecars with header redaction,
console capture, schema v7, six new audit actions. Note the structure
of the prompt: "I want to be sure X has the best representation … are
there ways to improve it?" — a question, not a directive. **Pattern:
when you don't know which improvements matter most, ask the AI to
enumerate the option space first and then pick.** The session opened
with three parallel Explore agents because the surface was large and
under-mapped.]*

---

## §14 — "ways to modify the download… restart…"

*Session `8d12d6f1-1b9f-4c2d-a207-dfd00307313e`, 2026-05-08 01:38 UTC.*

The final feature pass before the documentation cleanup. v0.7 closed two
long-standing gaps in one coherent batch.

> **User (01:38:34):**
> I want to add ways to modify the download like download audio only, etc. we also talked about implementing ways to harden the process for slow downloads like allowing for downloads to restart, etc. make sure all those features are implemented and identify gaps

> **Claude (01:38:44):**
> I'll start by mapping the current state of the download pipeline so I can identify what's implemented vs. what's missing for "modify the download" (audio-only, etc.) and "harden slow downloads" (restart, retries, timeouts).
> […]
> Got it — switching to plan mode. Let me load the required deferred tools, then launch exploration agents to understand the current state.

*[Editor's note: this is the round-trip moment foreshadowed in §7. The
pause/resume/cancel HTTP routes that v0.3 pruned came back here, joined
by a new `restart` action that wipes `.part` files for a clean retry, a
stall watchdog that converts ≥ 90s of progress silence into a UI signal,
and three new `DownloadOptions` knobs (audio-only, quality cap, subtitle
languages) threaded through the form → batch → runner pipeline → signed
`meta.json`. Schema v8 (additive). **Lesson: features pruned in one
version should not be kept out for principle's sake when a later
version's UI needs them. The forensic discipline of the codebase —
audit log, signed meta, additive schema — made the round-trip cheap.**]*

---

## What this transcript can't show

A few things are deliberately out of scope:

- **The CLAUDE.md authoring session.** As noted at the top, that session
  predates this project's log directory. Whatever process produced
  CLAUDE.md — the constitution — is the most consequential meta-decision
  in the project, and we don't have its trace. The methodology document
  treats this gap as itself instructive (you don't need a perfect log to
  see that *something* deliberate happened first).
- **Tool-call output.** Each Claude turn here is a thin slice of what was
  actually a long sequence of file reads, agent dispatches, edits, tests,
  and re-reads. Including the full tool stream would balloon the document
  to 50× its current size for marginal pedagogical value. The scripted
  extractor (`scripts/_session_dump.py`) can reproduce any session in full
  if a reader needs to audit a specific claim.
- **The 19 sessions not selected.** They cover smaller feature work
  (cookies UI, browser-extension pairing, slow-network strategies,
  download visualizer, several individual bug fixes, the documentation
  refresh in `cb61899f…`). They don't disappear from the record — they're
  in the index and in the JSONL files — they just don't add new lessons
  to the arc above.

---

## Continuing reading

- **[methodology.md](methodology.md)** — the patterns above, generalized into a
  best-practices guide for non-technical decision-makers thinking about
  whether to adopt this style of AI-assisted development for their own
  project.
- **[CLAUDE.md §15](../../CLAUDE.md)** — the project's own decision log,
  written *as* features landed. The transcript above is the live tape;
  §15 is the structured, signed-off recap.
