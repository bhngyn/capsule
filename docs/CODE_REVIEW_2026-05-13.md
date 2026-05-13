# Code Review — 2026-05-13

Scope: correctness, memory leaks, integrity / chain-of-custody. Reviewer
worked from `main @ 961ca45` (v1.0.0 + v0.7–v0.10 hardening). Three
parallel Explore passes over (1) capture pipeline, (2) integrity / evidence
/ updates, (3) HTTP / extension / DB. Every CRIT/HIGH claim below was
verified by reading the source — agent claims that did not survive
verification are listed at the end as false positives.

Each finding lists the fix branch that lands it.

## CRIT

### CRIT-1 — bundled verifier rejects every per-case evidence export

**Branch:** `fix/verify-audit-chain`

[app/templates/verify.py.tmpl:111-133](../app/templates/verify.py.tmpl)
initialises `expected_prev = "0" * 64` and demands the first audit-log
entry's `prev_hash` match it.
[app/evidence_export.py:239](../app/evidence_export.py) ships a per-case
slice (`audit.iter_entries(conn, case_id=case_id)`), so the first row
of a case slice almost never has id=1 — its `prev_hash` points at
whatever audit row immediately preceded it in the global log.

**Impact:** the standalone verifier — the recipient-facing trust anchor
per CLAUDE.md §10 — prints `FAIL` on every legitimate evidence bundle.
Recipients (editors, opposing counsel, courts) see "chain broken at
id=N" and reject the evidence. No existing test catches it because
tests likely exercise only case-1-with-row-1 fixtures.

**Fix:** export the full audit log (not the per-case slice). Forensically
stronger — the recipient sees the unbroken chain back to row 1.

### CRIT-2 — WARC writer fetches full response body before applying size cap

**Branch:** `fix/warc-body-precheck`

[app/warc_writer.py:493-526](../app/warc_writer.py) (`_finish_record`):
lines 496-502 fetch the body via CDP `Network.getResponseBody`,
base64-decode (or utf-8 encode), and assign to `body` *before* the
`if len(body) > self._max_inline` check at line 513. The "defensive
cap" comment at line 514 is correct about intent but the cap fires
after the load.

**Impact:** capturing a page with a 500 MB media stream balloons
Python heap by ~500 MB (plus base64 buffer) per response — easy OOM
on a laptop. Repeat across multiple large responses in one capture
and the container can be killed by Linux OOM.

**Fix:** use the `encoded_data_length` from `Network.loadingFinished`
(already passed into `_finish_record`) as a pre-check. If
`encoded_data_length > _max_inline`, write the metadata record
directly without calling `getResponseBody`. Keep the post-fetch
check as belt-and-braces.

## HIGH

### HIGH-1 — `i18n.py` path traversal via `lang` route param

**Branch:** `fix/i18n-lang-validation`

[app/i18n.py:27-34](../app/i18n.py) computes
`code = lang.split("-", 1)[0]` with no validation;
[app/main.py:292-304](../app/main.py) passes `lang` straight through.
`config.I18N_DIR / f"{code}.json"` will resolve `..` segments lazily,
so `GET /api/i18n/../some/other.json` is reachable.

**Impact:** an attacker who can reach the API can read any `*.json`
file the process can read (JSON-parseable only, but still a real
validation gap). `lru_cache` also caches anything read, so a
successful traversal sticks.

**Fix:** restrict `lang` to `[a-z]{2,3}(-[A-Za-z]{2,4})?` at the
route boundary; reject anything else with 400 before reaching
`i18n.load`.

### HIGH-2 — `warc_writer.__aexit__` silently drops late CDP events

**Branch:** `fix/warc-drain-and-sanitize`

[app/warc_writer.py:283-295](../app/warc_writer.py): drain loop is 20
iterations × 0.05 s = 1 s max wait for `_pending` to drain. Late
`Network.loadingFinished` events that arrive after that 1-second
window are silently dropped — the `_pending` entries leak and the
response is never recorded in the WARC.

**Impact:** WARC `record_count` undercounts; `meta.json.capture.warc.
record_count` is wrong; a forensic reviewer comparing the WARC to the
HAR sees inconsistency.

**Fix:** at exit, walk remaining `_pending` and emit a `metadata`
WARC record for each (kind `body_not_received_in_drain_window`).
This preserves the catalog.

### HIGH-3 — `sanitize_component` can leave a trailing dot

**Branch:** `fix/warc-drain-and-sanitize`

[app/sanitize.py:70](../app/sanitize.py): `s.strip().strip(".").strip()`
does not handle interleaved `". ."` correctly. Example: input
`"name. .  "` → `.strip()` → `"name. ."` → `.strip(".")` → `"name. "`
→ `.strip()` → `"name."`. Trailing dot survives. NTFS forbids
trailing dots.

**Impact:** a copy-paste title produces an item folder `name.` which
Windows silently rewrites to `name`, breaking the relpath stored in
`meta.json`.

**Fix:** replace the three-call chain with a loop that strips both
classes until idempotent.

### HIGH-4 — SSE consumer task hangs after idle client disconnect

**Branch:** `fix/jobs-sse-and-sigkill`

[app/main.py:898-905](../app/main.py) and
[app/jobs.py:782-795](../app/jobs.py): `is_disconnected()` is only
checked after `ch.get()` returns. For paused/stalled/idle jobs,
`ch.get()` blocks indefinitely; when a client disconnects, the
orchestrator channel keeps a live coroutine reference until the job
ends.

**Impact:** N idle SSE connections × hours of paused jobs = N pending
generators + N channel queues held open.

**Fix:** wrap `ch.get()` in `asyncio.wait_for(timeout=5s)`; on
timeout, poll `is_disconnected()` and either continue waiting or
return.

### HIGH-5 — `updates.auto_check_on_launch` swallows exceptions silently

**Branch:** `fix/jobs-sse-and-sigkill`

[app/updates.py:449-467](../app/updates.py): `except Exception: return`
with no logging and no audit-log row. The launch update-ping is the
*only* documented exception to CLAUDE.md §13 #7 "no silent network
calls"; the spec says every check is audit-logged (§4.4 + v0.10). A
silent failure breaks that contract — the audit log lies about
whether the check happened.

**Fix:** on exception, write a `system.update_check_failed` audit row
with the exception class name (not message — could carry tokens),
log at WARNING. Keep the swallow so startup never blocks.

## MED

### MED-1 — Extension capture dedup uses raw URL strings

**Branch:** `fix/jobs-sse-and-sigkill`

[app/main.py:1942-1948](../app/main.py) compares raw strings;
`/api/jobs/batch` uses `url_canonical.canonicalize` for the same
dedup. Same user submitting `?utm_source=tweet` and `?utm_source=email`
through the extension produces two captures; through the batch UI,
one. **Fix:** canonicalise before the `seen` check.

### MED-2 — `proc.terminate()` has no SIGKILL escalation

**Branch:** `fix/jobs-sse-and-sigkill`

[app/jobs.py:815-821](../app/jobs.py): SIGTERM with no follow-up
timeout-and-SIGKILL. A wedged subprocess that ignores SIGTERM hangs
the orchestrator forever. **Fix:** `wait_for(proc.wait(),
timeout=10)`; on timeout, `proc.kill()` then re-await.

### MED-3 — Settings/extension-token files written non-atomically

**Branch:** `fix/jobs-sse-and-sigkill`

[app/profiles.py](../app/profiles.py) `save_app_default` and
[app/extension_tokens.py:103-109](../app/extension_tokens.py):
`path.write_text(payload)` then `os.chmod(path, 0o600)`. A crash
between the two leaves the file world-readable. **Fix:** match the
`cookies._atomic_write_bytes` pattern (write tmp 0600, fsync,
rename).

### MED-4 — yt-dlp runner buffers unbounded stderr in memory

**Branch:** `fix/jobs-sse-and-sigkill`

[app/main.py:1130, 1605](../app/main.py) cap the audit-row stderr at
2000 chars, but the runner accumulates the full stderr in memory.
A hostile site pumping 100 MB of repetitive errors keeps 100 MB
resident until the job ends. **Fix:** bound the runner's stderr to
~16 KB (full log still goes to `/config/logs/app.log`).

## False positives rejected after verification

These were claimed by Explore agents but disproved by reading the source:

- "Extension capture lets `force_recapture` bypass dedup" — false:
  `main.py:1888-2012` never reads `force_recapture` from
  `ExtensionCaptureBody`; orchestrator default is `False`.
- "meta.json written before all artifacts hashed" — false: PDFs are
  hashed at `postprocess.py:675, 710` (well before meta dict is
  built at line 836).
- "sanitize.py reserved-name check misses `file.CON`" — false:
  Windows reserves the *first* path segment only; `file.CON` is
  legal on NTFS.
- "audit.append nested walk is DoS-able by deep nesting" — internal
  callers only; not reachable from network input.
