"""Job orchestrator (CLAUDE.md §15, plan §U1).

Sits between the API layer and the capture primitives. Responsibilities:

* persist every job to ``jobs`` so the queue survives app restart
* serialise concurrent submissions through an ``asyncio.Semaphore``
  (default 2 — Chromium memory contention pushes back beyond that)
* hold per-job in-memory state (status, progress, log, result, error) so
  ``GET /api/jobs/{id}`` and ``GET /api/jobs/{id}/events`` (SSE) can serve
  it without re-reading the DB on every event
* drive the pipeline: classify → capture (page) → ytdlp_runner → postprocess
* schedule transient-failure retries with exponential backoff using the
  severity field on ``errors.classify`` (plan §U5)
* on app startup, rehydrate the queue from the DB: ``running`` → ``queued``
  (a crash interrupted them), and re-dispatch ``queued`` / ``retrying`` rows
  whose ``next_retry_at`` has elapsed
"""

from __future__ import annotations

import asyncio
import shutil
import contextlib
import datetime as _dt
import json
import logging
import sqlite3
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    audit,
    capture as capture_mod,
    cases,
    classify as classify_mod,
    config,
    cookies as cookies_mod,
    db as db_mod,
    errors as errors_mod,
    gallery_dl_runner,
    network as network_mod,
    postprocess,
    profiles as profiles_mod,
    ytdlp_runner,
)

_log = logging.getLogger(__name__)

__all__ = [
    "Job",
    "JobStatus",
    "JobOrchestrator",
    "DownloadOptions",
    "MAX_CONCURRENT",
    "MAX_RETRY_BACKOFF_S",
    "PROGRESS_FLUSH_INTERVAL_S",
    "STALL_THRESHOLD_S",
    "UserBrowserBundle",
    "attach_user_browser_bundle",
    "pop_user_browser_bundle",
    "discard_user_browser_bundle",
]

MAX_CONCURRENT = 2

# Cap on the per-job retry backoff. The plan calls for 24h on Slow profile
# and 1h on Fast; the universal default sits between, since profiles aren't
# wired yet.
MAX_RETRY_BACKOFF_S = 60 * 60  # 1h

# How often to flush the latest progress snapshot to the DB. Plan §U9 calls
# for ~30s — that's a survivable amount of progress to lose on crash, while
# keeping the write rate sane on long downloads.
PROGRESS_FLUSH_INTERVAL_S = 30.0

# CLAUDE.md §15 (v0.7): wall-clock seconds without a real progress event
# before the runner emits a synthetic ``stalled`` ProgressUpdate so the UI
# can amber-chip the job. Not a kill threshold — investigators decide.
STALL_THRESHOLD_S = 90


# Status values persisted in jobs.status. Keep this list in sync with the
# CHECK assumptions of consumers (frontend + audit).
JobStatus = str
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_RETRYING = "retrying"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_FAILED_PERMANENT = "failed_permanent"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = frozenset({STATUS_DONE, STATUS_FAILED_PERMANENT, STATUS_CANCELLED})

# Plan §U6 / Phase D: a job represents one *kind* of work.
# - 'full'     : legacy combined (snapshot + archive + media + finalize).
#                Default on submit so existing flows keep working.
# - 'snapshot' : Playwright MHTML + PNG only.
# - 'archive'  : browsertrix WARC only — extends an existing item.
# - 'media'    : yt-dlp only — extends an existing item.
TASK_FULL = "full"
TASK_SNAPSHOT = "snapshot"
TASK_ARCHIVE = "archive"
TASK_MEDIA = "media"
TASK_KINDS = frozenset({TASK_FULL, TASK_SNAPSHOT, TASK_ARCHIVE, TASK_MEDIA})


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _classify_internal_exception(exc: BaseException) -> tuple[str, str]:
    """Map a Python exception raised inside the orchestrator to a pair of
    i18n keys: ``(cause_key, suggested_action_key)``.

    Counterpart to ``app.errors.classify`` (which speaks yt-dlp stderr) for
    failures that originate inside our own code — most commonly when a hard
    runtime dependency (WeasyPrint, cryptography) is missing because the
    container was built from a stale Dockerfile.
    """
    if isinstance(exc, ModuleNotFoundError):
        return ("errors.cause.dep_missing", "errors.action.rebuild_image")
    if isinstance(exc, MemoryError):
        return ("errors.cause.out_of_memory", "errors.action.try_again")
    if isinstance(exc, (PermissionError, FileNotFoundError)):
        return ("errors.cause.fs_permission", "errors.action.check_mounts")
    return ("errors.cause.unknown", "errors.action.try_again")


# --- Extension-supplied "user-browser" capture bundles ----------------------
#
# The Capsule extension can hand the orchestrator a set of supplementary
# artifacts (MHTML, screenshot, HAR, environment JSON) captured from the
# investigator's live browser session. These are NEVER substitutes for the
# canonical container-Chromium capture — they ride alongside as additive
# evidence, hashed and signed identically (see app/postprocess.py).
#
# The stash lives in process memory (the bundle's payload is on a local
# tmpdir on disk), keyed by job_uuid. The orchestrator pops the bundle
# right before it constructs the ``CaptureInput`` for postprocess; if the
# job fails before that hand-off, ``discard_user_browser_bundle`` cleans
# the tmpdir up so we never leak partial evidence.


@dataclass(frozen=True)
class UserBrowserBundle:
    """File paths for one extension-supplied capture.

    Each path lives in a per-bundle tmpdir; ``tmpdir`` is recorded so the
    bundle can be cleaned up wholesale on terminal failure. Postprocess
    moves the files out, naturally emptying the tmpdir.

    Hardening additions: ``tab_context`` + ``session_state`` + ``dom_snapshot``
    + ``ephemeral_cookies`` extend the bundle without breaking the prior
    additive-evidence contract — every new artifact rides through the same
    sign-and-hash path.
    """

    tmpdir: Path
    mhtml: Path | None = None
    screenshot: Path | None = None
    har: Path | None = None
    environment: Path | None = None
    label: str | None = None  # extension label (audit only)
    # Hardening pass:
    tab_context: Path | None = None       # JSON: UA / viewport / scroll / tz / etc.
    session_state: Path | None = None     # JSON: per-origin localStorage / sessionStorage
    dom_snapshot_html: Path | None = None # raw outerHTML at click-time
    dom_snapshot_meta: Path | None = None # JSON: counts (nodes, iframes, videos, …)
    # Ephemeral cookies path. When set, the orchestrator uses this file
    # instead of (or in addition to) the case's persistent cookies file,
    # and discards it after the job ends.
    ephemeral_cookies: Path | None = None


_user_browser_bundles: dict[str, UserBrowserBundle] = {}


def attach_user_browser_bundle(job_id: str, bundle: UserBrowserBundle) -> None:
    """Stash a bundle for ``job_id``. Overwrites any prior bundle (the API
    layer is the only caller and only attaches once per job)."""
    prior = _user_browser_bundles.pop(job_id, None)
    if prior is not None:
        _cleanup_bundle(prior)
    _user_browser_bundles[job_id] = bundle


def pop_user_browser_bundle(job_id: str) -> UserBrowserBundle | None:
    """Remove and return the bundle for ``job_id`` so postprocess can move
    its files into the canonical sidecar dir."""
    return _user_browser_bundles.pop(job_id, None)


def discard_user_browser_bundle(job_id: str) -> None:
    """Drop a bundle without consuming it — used when a job terminates
    before postprocess (cancelled, permanent failure). Best-effort: leaves
    no temp files behind."""
    bundle = _user_browser_bundles.pop(job_id, None)
    if bundle is not None:
        _cleanup_bundle(bundle)


def _cleanup_bundle(bundle: UserBrowserBundle) -> None:
    for p in (
        bundle.mhtml,
        bundle.screenshot,
        bundle.har,
        bundle.environment,
        bundle.tab_context,
        bundle.session_state,
        bundle.dom_snapshot_html,
        bundle.dom_snapshot_meta,
    ):
        if p is None:
            continue
        try:
            p.unlink()
        except OSError:
            pass
    if bundle.ephemeral_cookies is not None:
        cookies_mod.discard_ephemeral(bundle.ephemeral_cookies)
    try:
        bundle.tmpdir.rmdir()
    except OSError:
        pass


# Allowed enums for the v0.9 container picker. Module-level so the argv
# builder (build_container_argv, build_format_spec) and the JobBatchItem
# validator share one source of truth — drift here would let an unknown
# string slip into yt-dlp's argv. None ⇒ "let yt-dlp pick" (back-compat).
VIDEO_CONTAINERS: tuple[str, ...] = ("mp4", "webm", "mkv")
AUDIO_CONTAINERS: tuple[str, ...] = ("mp3", "m4a", "opus", "wav", "flac")
_CAPTURE_MODE_VALUES: frozenset[str] = frozenset({"webpage", "media", "gallery"})


@dataclass
class DownloadOptions:
    """Per-job download-modification + reliability counters (CLAUDE.md §15 v0.7/v0.9).

    ``audio_only``, ``quality_cap``, ``subtitle_langs``, ``video_container``,
    and ``audio_container`` are the investigator-facing knobs; ``restart_count``
    and ``stalled_count`` are forensic counters bumped by the orchestrator/runner
    so the per-item PDF report can disclose them.

    Persisted as JSON on ``jobs.download_options_json`` and as a block on
    ``meta.json.download_options`` (schema v9) — the latter is signed
    transitively via ``meta.json.sig`` so a recipient can confirm what
    options were in effect.

    The v0.9 container fields are mux-only (``--merge-output-format`` for
    video; ``--audio-format`` for audio extraction). They never trigger
    re-encoding of the video stream — see CLAUDE.md §15 v0.9 forensic note.
    """
    audio_only: bool = False
    # 'audio' | '480' | '720' | '1080' | 'best' | None
    quality_cap: str | None = None
    subtitle_langs: list[str] = field(default_factory=list)
    # v0.9: 'mp4' | 'webm' | 'mkv' | None. None ⇒ yt-dlp default.
    video_container: str | None = None
    # v0.9: 'mp3' | 'm4a' | 'opus' | 'wav' | 'flac' | None. None ⇒ mp3
    # (the v0.7 default, preserved for back-compat).
    audio_container: str | None = None
    # When True, gallery-dl runs even if yt-dlp produced media. The
    # captured images attach to the same item as additional ``gallery_NNN``
    # artifacts; capture_kind stays ``media`` (yt-dlp's primary) when both
    # paths produce output. Useful for blog/article pages where yt-dlp may
    # only grab an embedded video while gallery-dl finds the surrounding
    # photo set.
    force_gallery_run: bool = False
    # v0.10: "webpage" | "media" | "gallery" | None.
    # Controls orchestrator routing — which tools run and in what order.
    # See CLAUDE.md §15 v0.10 capture mode semantics.
    capture_mode: str | None = None
    # Bumped by JobOrchestrator.restart(); persisted across the restart so
    # the meta.json block records it.
    restart_count: int = 0
    # Bumped by the orchestrator on every ``stalled`` SSE event from the
    # runner. Surfaced in meta.json.capture.stalled_count.
    stalled_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "audio_only": bool(self.audio_only),
            "quality_cap": self.quality_cap,
            "subtitle_langs": list(self.subtitle_langs or []),
            "video_container": self.video_container,
            "audio_container": self.audio_container,
            "force_gallery_run": bool(self.force_gallery_run),
            "capture_mode": self.capture_mode,
            "restart_count": int(self.restart_count),
            "stalled_count": int(self.stalled_count),
        }

    def to_json(self) -> str:
        return _dumps(self.to_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "DownloadOptions":
        if not raw or not isinstance(raw, dict):
            return cls()
        langs = raw.get("subtitle_langs") or []
        if not isinstance(langs, list):
            langs = []
        # Coerce types defensively — older rows may have nullable ints.
        # Container fields: only accept known enum values; unknown strings
        # fall back to None so a stale localStorage entry or a future-version
        # payload can't slip an arbitrary string into yt-dlp's argv.
        vc_raw = raw.get("video_container")
        ac_raw = raw.get("audio_container")
        video_container = (
            str(vc_raw)
            if vc_raw is not None and str(vc_raw) in VIDEO_CONTAINERS
            else None
        )
        audio_container = (
            str(ac_raw)
            if ac_raw is not None and str(ac_raw) in AUDIO_CONTAINERS
            else None
        )
        cm_raw = raw.get("capture_mode")
        capture_mode = (
            str(cm_raw)
            if cm_raw is not None and str(cm_raw) in _CAPTURE_MODE_VALUES
            else None
        )
        return cls(
            audio_only=bool(raw.get("audio_only", False)),
            quality_cap=(str(raw["quality_cap"])
                         if raw.get("quality_cap") not in (None, "")
                         else None),
            subtitle_langs=[str(s) for s in langs if s],
            video_container=video_container,
            audio_container=audio_container,
            force_gallery_run=bool(raw.get("force_gallery_run", False)),
            capture_mode=capture_mode,
            restart_count=int(raw.get("restart_count") or 0),
            stalled_count=int(raw.get("stalled_count") or 0),
        )

    @classmethod
    def from_json(cls, blob: str | None) -> "DownloadOptions":
        if not blob:
            return cls()
        try:
            return cls.from_dict(json.loads(blob))
        except (TypeError, ValueError):
            return cls()

    def is_default(self) -> bool:
        """True if no investigator-facing knob is set. Used by audit logic
        — we only emit ``download.options_applied`` when something actually
        differs from the profile defaults."""
        return (
            not self.audio_only
            and not self.quality_cap
            and not self.subtitle_langs
            and not self.video_container
            and not self.audio_container
            and not self.force_gallery_run
            and not self.capture_mode
        )


@dataclass
class Job:
    id: str
    case_id: int
    url: str
    status: JobStatus = STATUS_QUEUED
    phase: str | None = None
    attempts: int = 0
    progress: list[dict[str, Any]] = field(default_factory=list)
    classification: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    last_error_kind: str | None = None
    last_error_severity: str | None = None
    next_retry_at: str | None = None
    task_kind: str = TASK_FULL
    capture_group_id: str | None = None
    # UI locale at submission time. Threaded through to
    # ``CaptureInput.lang`` so the per-item manifest PDF picks up the
    # right labels + direction + font stack. ``None`` ⇒ resolve to
    # ``config.DEFAULT_LANG`` at run time.
    lang: str | None = None
    # CLAUDE.md §15: True iff the user clicked "Re-capture as new entry"
    # in the duplicate-handling modal. ``finalize`` will then suffix the
    # url_hash with ``__c{N+1}``. Not persisted across restarts — if the
    # app crashes mid-recapture the user simply re-triggers the modal.
    force_recapture: bool = False
    # CLAUDE.md §15: when force_recapture is set, this is the id of the
    # original ``downloads`` row the user is re-capturing. Used to bind
    # the audit-log entries together so a verifier can trace the chain.
    original_download_id: int | None = None
    # CLAUDE.md §15 v0.7: per-job download-modification + reliability
    # counters. Persisted as JSON on jobs.download_options_json.
    download_options: DownloadOptions = field(default_factory=DownloadOptions)
    # Volatile — set True by ``restart()`` so the next dispatch passes
    # ``restart=True`` to ytdlp_runner (which swaps --continue → --no-continue
    # and pre-deletes ``*.part``/``*.ytdl``). Cleared after dispatch so a
    # subsequent auto-retry uses ``--continue`` again.
    restart_pending: bool = False
    # Volatile UI hint: set True when a stalled SSE event fires; cleared on
    # the next real progress event. Not persisted; recovers naturally on
    # restart since the runner re-emits.
    stalled: bool = False
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "case_id": self.case_id,
            "url": self.url,
            "status": self.status,
            "phase": self.phase,
            "attempts": self.attempts,
            "classification": self.classification,
            "result": self.result,
            "error": self.error,
            "last_error_kind": self.last_error_kind,
            "last_error_severity": self.last_error_severity,
            "next_retry_at": self.next_retry_at,
            "task_kind": self.task_kind,
            "capture_group_id": self.capture_group_id,
            "lang": self.lang,
            "download_options": self.download_options.to_dict(),
            "stalled": self.stalled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        # ``task_kind``, ``capture_group_id``, and ``download_options_json``
        # may be missing on rows written by an older binary; fall back
        # gracefully.
        keys = row.keys() if hasattr(row, "keys") else []
        return cls(
            id=row["id"],
            case_id=int(row["case_id"]),
            url=row["source_url"],
            status=row["status"],
            phase=row["phase"],
            attempts=int(row["attempts"] or 0),
            classification=_loads_or_none(row["classification_json"]),
            result=_loads_or_none(row["result_json"]),
            error=_loads_or_none(row["error_json"]),
            last_error_kind=row["last_error_kind"],
            last_error_severity=row["last_error_severity"],
            next_retry_at=row["next_retry_at"],
            task_kind=(row["task_kind"] if "task_kind" in keys else TASK_FULL),
            capture_group_id=(
                row["capture_group_id"] if "capture_group_id" in keys else None
            ),
            download_options=DownloadOptions.from_json(
                row["download_options_json"]
                if "download_options_json" in keys else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _loads_or_none(blob: str | None) -> Any:
    if not blob:
        return None
    try:
        return json.loads(blob)
    except (TypeError, ValueError):
        return None


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --- DB layer ----------------------------------------------------------------


def _insert_job(conn: sqlite3.Connection, job: Job) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO jobs(
                id, case_id, source_url, status, phase, attempts,
                progress_json, classification_json, result_json, error_json,
                task_kind, capture_group_id,
                download_options_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, '{}', NULL, NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                job.id, job.case_id, job.url, job.status, job.phase,
                job.attempts, job.task_kind, job.capture_group_id,
                job.download_options.to_json(),
                job.created_at, job.updated_at,
            ),
        )


def ensure_capture_group(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    url: str,
    download_id: int | None = None,
    group_id: str | None = None,
) -> str:
    """Create or return a capture_group row id.

    A capture_group anchors all the jobs (snapshot/archive/media re-fetches)
    that contribute artifacts to a single library item.
    """
    if group_id is not None:
        row = conn.execute(
            "SELECT id FROM capture_groups WHERE id = ?", (group_id,),
        ).fetchone()
        if row:
            return row["id"]
    new_id = str(uuid.uuid4())
    now = _utcnow()
    with conn:
        conn.execute(
            """
            INSERT INTO capture_groups(id, case_id, source_url, download_id,
                                       created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id, case_id, url, download_id, now, now),
        )
    return new_id


def link_capture_group_to_download(
    conn: sqlite3.Connection, group_id: str, download_id: int,
) -> None:
    """Bind a capture_group to the library row it produced."""
    now = _utcnow()
    with conn:
        conn.execute(
            "UPDATE capture_groups SET download_id = ?, updated_at = ?"
            " WHERE id = ?",
            (download_id, now, group_id),
        )


def _update_job(
    conn: sqlite3.Connection,
    job: Job,
    *,
    started: bool = False,
    finished: bool = False,
) -> None:
    fields = [
        "status = ?",
        "phase = ?",
        "attempts = ?",
        "classification_json = ?",
        "result_json = ?",
        "error_json = ?",
        "last_error_kind = ?",
        "last_error_severity = ?",
        "next_retry_at = ?",
        "download_options_json = ?",
        "updated_at = ?",
    ]
    params: list[Any] = [
        job.status,
        job.phase,
        job.attempts,
        _dumps(job.classification) if job.classification is not None else None,
        _dumps(job.result) if job.result is not None else None,
        _dumps(job.error) if job.error is not None else None,
        job.last_error_kind,
        job.last_error_severity,
        job.next_retry_at,
        job.download_options.to_json(),
        job.updated_at,
    ]
    if started:
        fields.append("started_at = COALESCE(started_at, ?)")
        params.append(job.updated_at)
    if finished:
        fields.append("finished_at = ?")
        params.append(job.updated_at)
    params.append(job.id)
    with conn:
        conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", params)


def _flush_progress(conn: sqlite3.Connection, job_id: str, payload: dict[str, Any]) -> None:
    with conn:
        conn.execute(
            "UPDATE jobs SET progress_json = ?, updated_at = ? WHERE id = ?",
            (_dumps(payload), _utcnow(), job_id),
        )


# --- Orchestrator ------------------------------------------------------------


class JobOrchestrator:
    """DB-backed job registry + semaphore-bounded executor.

    One instance per app — the FastAPI app holds it on ``app.state``. The
    DB connection is opened on-demand per job because sqlite3 connections
    are not safe to share across asyncio tasks.
    """

    def __init__(
        self,
        *,
        max_concurrent: int | None = None,
        db_path: Path | None = None,
        network: network_mod.NetworkMonitor | None = None,
    ):
        # Plan §C: the orchestrator's process-wide knobs (concurrency,
        # network probe cadence) come from the app-wide profile when not
        # explicitly overridden. Per-case profile values that affect a
        # single job (rate limit, timeout, format) are read per-job in
        # ``_run_inner``.
        app_profile = profiles_mod.effective_for_case().settings
        if max_concurrent is None:
            max_concurrent = min(MAX_CONCURRENT, app_profile.concurrency)
        self._jobs: dict[str, Job] = {}
        self._sem = asyncio.Semaphore(max_concurrent)
        self._channels: dict[str, asyncio.Queue] = {}
        self._db_path = db_path or db_mod.DB_PATH
        # Plan §U4: subprocess + intent + retry-task tracking for pause/cancel.
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._user_intent: dict[str, str] = {}        # job_id -> 'pause' | 'cancel'
        self._retry_tasks: dict[str, asyncio.Task] = {}
        self._run_tasks: dict[str, asyncio.Task] = {}
        # Plan §U7: passive offline detection. Wire the resume callback so a
        # successful probe resumes any jobs we paused while offline.
        self._network = network or network_mod.NetworkMonitor(
            probe_interval_s=float(app_profile.probe_interval_s),
            probe_url=app_profile.probe_url or network_mod.DEFAULT_PROBE_URL,
            on_offline=self._on_network_offline,
            on_resume=self._on_network_resume,
        )

    # -- introspection --------------------------------------------------

    def list(self) -> list[Job]:
        return list(self._jobs.values())

    def get(self, job_id: str) -> Job | None:
        if job_id in self._jobs:
            return self._jobs[job_id]
        # Fall through to DB so a job that finished before this orchestrator
        # was constructed (e.g. after restart, before SSE catches up) can
        # still be inspected via the API.
        conn = db_mod.connect(self._db_path)
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
        return Job.from_row(row) if row else None

    # -- submit / run ---------------------------------------------------

    async def submit(
        self,
        *,
        case_id: int,
        url: str,
        task_kind: str = TASK_FULL,
        capture_group_id: str | None = None,
        lang: str | None = None,
        force_recapture: bool = False,
        original_download_id: int | None = None,
        download_options: DownloadOptions | None = None,
    ) -> Job:
        if task_kind not in TASK_KINDS:
            raise ValueError(f"unknown task_kind {task_kind!r}")
        # ``lang`` is the UI locale at submission time — propagated all
        # the way to ``CaptureInput`` so the per-item manifest PDF
        # renders in the right script. Resolved lazily in ``_run_inner``
        # if the caller didn't supply one.
        # ``force_recapture`` is set when the user picked "Re-capture as
        # new entry" in the §15 modal — finalize() then suffixes the
        # url_hash with ``__c{N+1}`` instead of raising DuplicateCapture.
        # ``download_options`` (CLAUDE.md §15 v0.7) carries audio_only,
        # quality_cap, and subtitle_langs through to the runner; counters
        # restart_count / stalled_count are filled in over the job's life.
        job = Job(
            id=str(uuid.uuid4()),
            case_id=case_id,
            url=url,
            task_kind=task_kind,
            capture_group_id=capture_group_id,
            lang=lang,
            force_recapture=force_recapture,
            original_download_id=original_download_id,
            download_options=download_options or DownloadOptions(),
        )
        # Plan §U6 / Phase D: every full submission gets a fresh capture
        # group; partial-task submissions (archive / media re-fetch) must
        # already carry one from the caller.
        if task_kind == TASK_FULL and capture_group_id is None:
            conn = db_mod.connect(self._db_path)
            try:
                job.capture_group_id = ensure_capture_group(
                    conn, case_id=case_id, url=url,
                )
            finally:
                conn.close()
        self._jobs[job.id] = job
        self._channels[job.id] = asyncio.Queue()
        conn = db_mod.connect(self._db_path)
        try:
            _insert_job(conn, job)
        finally:
            conn.close()
        self._run_tasks[job.id] = asyncio.create_task(self._run(job))
        return job

    async def rehydrate(self) -> list[Job]:
        """Resume the queue after an app restart.

        Marks ``running`` rows back to ``queued`` (they were interrupted),
        then re-dispatches every ``queued`` / ``retrying`` row whose
        ``next_retry_at`` has elapsed (or is null). Terminal rows are left
        alone. Returns the list of jobs that were re-dispatched.

        Idempotent: calling it twice in a row does nothing the second time.
        """
        conn = db_mod.connect(self._db_path)
        now = _utcnow()
        rehydrated: list[Job] = []
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE jobs SET status = ?, updated_at = ?
                     WHERE status = ?
                    """,
                    (STATUS_QUEUED, now, STATUS_RUNNING),
                )
            rows = conn.execute(
                """
                SELECT * FROM jobs
                 WHERE status IN (?, ?)
                   AND (next_retry_at IS NULL OR next_retry_at <= ?)
                 ORDER BY created_at
                """,
                (STATUS_QUEUED, STATUS_RETRYING, now),
            ).fetchall()
            for row in rows:
                job = Job.from_row(row)
                if job.id in self._jobs:
                    continue
                self._jobs[job.id] = job
                self._channels[job.id] = asyncio.Queue()
                rehydrated.append(job)
                audit.append(
                    conn,
                    "job.rehydrated",
                    case_id=job.case_id,
                    actor="system",
                    details={
                        "job_id": job.id,
                        "url": job.url,
                        "previous_status": row["status"],
                    },
                )
        finally:
            conn.close()
        for job in rehydrated:
            asyncio.create_task(self._run(job))
        return rehydrated

    async def events(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield ``{event, data}`` dicts until the job ends.

        ``event`` is one of ``status``, ``progress``, ``done``, ``error``.
        ``data`` is JSON-serialisable.
        """
        ch = self._channels.get(job_id)
        if ch is None:
            return
        while True:
            evt = await ch.get()
            if evt is None:
                return
            yield evt

    # -- pause / resume / cancel (plan §U4) -----------------------------

    @staticmethod
    def _terminate_with_kill_escalation(proc: Any, *, sigkill_after_s: float = 10.0) -> None:
        """Send SIGTERM, then escalate to SIGKILL after a deadline.

        yt-dlp / gallery-dl normally honour SIGTERM (and leave .part
        files for ``--continue``), but a wedged or hostile subprocess
        can ignore it. Without escalation, the orchestrator's
        ``await run_task`` hangs forever. Scheduled as a fire-and-
        forget asyncio task so the caller (pause/cancel) returns
        immediately.
        """
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            return

        async def _watchdog():
            try:
                await asyncio.sleep(sigkill_after_s)
                if proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError, OSError):
                        proc.kill()
            except asyncio.CancelledError:
                pass

        # Fire-and-forget — the watchdog only matters if the subprocess
        # ignores SIGTERM, in which case the orchestrator's normal
        # cleanup path is already stalled.
        asyncio.create_task(_watchdog())

    async def pause(self, job_id: str) -> bool:
        """Mark a job paused. SIGTERM any live subprocess, cancel any pending
        retry. Returns True if the job is now paused, False if it was already
        terminal or unknown.

        yt-dlp leaves ``.part`` files on SIGTERM, so a ``resume()`` call
        re-dispatches the job and ``--continue`` picks up where it stopped.
        """
        job = self._jobs.get(job_id) or self.get(job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return False
        self._user_intent[job_id] = "pause"
        # Cancel any sleeping retry task.
        rt = self._retry_tasks.pop(job_id, None)
        if rt and not rt.done():
            rt.cancel()
        # SIGTERM the live subprocess if any, escalating to SIGKILL
        # after 10s so a wedged child can't hang the orchestrator.
        proc = self._procs.get(job_id)
        if proc is not None:
            self._terminate_with_kill_escalation(proc)
        # If the job hadn't actually started yet (queued / retrying with no
        # in-flight proc), flip status directly so the UI updates without
        # waiting for a non-existent run to complete.
        if proc is None and job.status in (STATUS_QUEUED, STATUS_RETRYING):
            conn = db_mod.connect(self._db_path)
            try:
                self._set_status(conn, job, STATUS_PAUSED)
                audit.append(
                    conn, "job.paused",
                    case_id=job.case_id, actor="user",
                    details={"job_id": job_id, "from": job.status},
                )
            finally:
                conn.close()
            self._emit(job, "paused", {"status": STATUS_PAUSED})
            # Cancel the queued run task too (it's just waiting on the sema).
            rt = self._run_tasks.pop(job_id, None)
            if rt and not rt.done():
                rt.cancel()
        return True

    async def cancel(self, job_id: str) -> bool:
        """Mark a job cancelled. Terminate subprocess, drop partials, audit."""
        job = self._jobs.get(job_id) or self.get(job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return False
        self._user_intent[job_id] = "cancel"
        rt = self._retry_tasks.pop(job_id, None)
        if rt and not rt.done():
            rt.cancel()
        proc = self._procs.get(job_id)
        if proc is not None:
            self._terminate_with_kill_escalation(proc)
        if proc is None and job.status in (STATUS_QUEUED, STATUS_RETRYING, STATUS_PAUSED):
            conn = db_mod.connect(self._db_path)
            try:
                self._set_status(conn, job, STATUS_CANCELLED)
                audit.append(
                    conn, "job.cancelled",
                    case_id=job.case_id, actor="user",
                    details={"job_id": job_id, "from": job.status},
                )
            finally:
                conn.close()
            self._emit(job, "cancelled", {"status": STATUS_CANCELLED})
            rt = self._run_tasks.pop(job_id, None)
            if rt and not rt.done():
                rt.cancel()
            self._close_channel(job)
        return True

    async def resume(self, job_id: str) -> bool:
        """Re-enqueue a paused job. ``--continue`` picks up the .part file."""
        job = self._jobs.get(job_id) or self.get(job_id)
        if job is None or job.status != STATUS_PAUSED:
            return False
        self._user_intent.pop(job_id, None)
        # Re-hydrate in-memory if needed and re-dispatch.
        if job_id not in self._jobs:
            self._jobs[job_id] = job
            self._channels[job_id] = asyncio.Queue()
        conn = db_mod.connect(self._db_path)
        try:
            self._set_status(conn, job, STATUS_QUEUED)
            audit.append(
                conn, "job.resumed",
                case_id=job.case_id, actor="user",
                details={"job_id": job_id},
            )
        finally:
            conn.close()
        self._emit(job, "resumed", {"status": STATUS_QUEUED})
        self._run_tasks[job_id] = asyncio.create_task(self._run(job))
        return True

    async def pause_all(self) -> int:
        """Pause every non-terminal job. Returns count actually paused."""
        n = 0
        # Materialise the id list — pause() mutates _jobs.
        for jid in list(self._jobs.keys()):
            if await self.pause(jid):
                n += 1
        return n

    # -- restart (CLAUDE.md §15 v0.7) ----------------------------------

    async def restart(self, job_id: str) -> bool:
        """Force-restart a job: SIGTERM live subprocess, wipe ``.part`` and
        ``.ytdl`` files, reset attempts, re-dispatch.

        Distinct from ``resume()`` (which keeps ``.part`` and lets
        ``--continue`` pick up where the byte stream stopped). Restart is
        the escape hatch when a partial download is corrupted, when the
        site changed mid-capture, or when the user wants forensically
        clean bytes (no ambiguity about resumed fragments).

        Returns True if the job will be re-dispatched, False if it was
        unknown or in a state that can't be restarted (only ``done``
        rejects — every other state restarts cleanly).
        """
        job = self._jobs.get(job_id) or self.get(job_id)
        if job is None:
            return False
        if job.status == STATUS_DONE:
            # Successful captures are immutable — re-running would only
            # mint a duplicate row, which the §15 modal already handles.
            return False

        # Cancel any sleeping retry task and SIGTERM any live subprocess.
        # Different from cancel() — we route through restart_pending so the
        # next dispatch wipes .part files and uses --no-continue.
        rt = self._retry_tasks.pop(job_id, None)
        if rt and not rt.done():
            rt.cancel()
        proc = self._procs.get(job_id)
        if proc is not None:
            self._terminate_with_kill_escalation(proc)
        # Cancel any in-flight run task too — we'll re-dispatch a fresh one.
        run_task = self._run_tasks.pop(job_id, None)
        if run_task and not run_task.done():
            run_task.cancel()

        # Clear any prior pause/cancel intent — restart wins.
        self._user_intent.pop(job_id, None)

        # Re-hydrate the in-memory cache if a long-finished-failed job is
        # being restarted from a cold cache.
        if job_id not in self._jobs:
            self._jobs[job_id] = job
            self._channels[job_id] = asyncio.Queue()
        else:
            # Replace the SSE channel so a stale ``done`` sentinel from a
            # prior cancelled run doesn't terminate the new stream.
            self._channels[job_id] = asyncio.Queue()

        previous_status = job.status
        job.download_options.restart_count += 1
        job.attempts = 0
        job.error = None
        job.last_error_kind = None
        job.last_error_severity = None
        job.next_retry_at = None
        job.phase = None
        job.stalled = False
        job.restart_pending = True

        conn = db_mod.connect(self._db_path)
        try:
            self._set_status(conn, job, STATUS_QUEUED)
            audit.append(
                conn, "job.restarted",
                case_id=job.case_id, actor="user",
                details={
                    "job_id": job_id,
                    "from": previous_status,
                    "restart_count": job.download_options.restart_count,
                },
            )
        finally:
            conn.close()
        # Distinct SSE event so the UI can animate the transition (and
        # clear any local error/stalled state) without diffing status.
        self._emit(job, "restarted", {
            "status": STATUS_QUEUED,
            "restart_count": job.download_options.restart_count,
        })
        self._run_tasks[job_id] = asyncio.create_task(self._run(job))
        return True

    # -- network monitor accessors (plan §U7) ---------------------------

    @property
    def network(self) -> network_mod.NetworkMonitor:
        return self._network

    async def _on_network_offline(self) -> None:
        """Called by NetworkMonitor when the threshold is crossed.

        Pauses every non-terminal job. The audit entry distinguishes this
        from a user-driven pause so a follow-up evidence export reflects
        why the queue went quiet.
        """
        for jid in list(self._jobs.keys()):
            await self.pause(jid)
        conn = db_mod.connect(self._db_path)
        try:
            audit.append(
                conn, "network.offline_detected",
                actor="system",
                details={
                    "probe_url": self._network.probe_url,
                    "failure_count": self._network.state().failure_count_in_window,
                },
            )
        finally:
            conn.close()

    async def _on_network_resume(self) -> None:
        """Called by NetworkMonitor when a probe succeeds. Re-dispatch
        every previously-paused job."""
        n = await self.resume_all()
        conn = db_mod.connect(self._db_path)
        try:
            audit.append(
                conn, "network.online_detected",
                actor="system",
                details={"resumed": n, "probe_url": self._network.probe_url},
            )
        finally:
            conn.close()

    async def resume_all(self) -> int:
        n = 0
        # Pull all paused rows from DB so a long-paused job missing from
        # _jobs cache also resumes.
        conn = db_mod.connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status = ?", (STATUS_PAUSED,),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            if await self.resume(row["id"]):
                n += 1
        return n

    # -- internal -------------------------------------------------------

    def _emit(self, job: Job, event: str, data: Any) -> None:
        ch = self._channels.get(job.id)
        if ch is None:
            return
        ch.put_nowait({"event": event, "data": data})

    def _close_channel(self, job: Job) -> None:
        ch = self._channels.get(job.id)
        if ch is not None:
            ch.put_nowait(None)

    def _set_status(self, conn: sqlite3.Connection, job: Job, status: JobStatus) -> None:
        job.status = status
        job.updated_at = _utcnow()
        if status == STATUS_RUNNING:
            _update_job(conn, job, started=True)
        elif status in TERMINAL_STATUSES:
            _update_job(conn, job, finished=True)
        else:
            _update_job(conn, job)
        self._emit(job, "status", {"status": status})

    def _set_phase(self, conn: sqlite3.Connection, job: Job, phase: str) -> None:
        """Record an intra-run phase change (classifying, snapshotting...).

        Does not transition the persisted ``status`` — those move to
        ``running`` once and stay there until the job terminates. The
        emitted SSE 'status' event still uses the phase string, which is
        what the frontend renders into the 4-icon progress strip.
        """
        job.phase = phase
        job.updated_at = _utcnow()
        with conn:
            conn.execute(
                "UPDATE jobs SET phase = ?, updated_at = ? WHERE id = ?",
                (phase, job.updated_at, job.id),
            )
        self._emit(job, "status", {"status": phase})

    def _retry_delay_s(self, attempts: int) -> float:
        """Exponential backoff with cap. ``attempts`` is the count *after*
        this failure (1 for the first failure, 2 for the second, ...).

        15s, 30s, 60s, 120s ... up to ``MAX_RETRY_BACKOFF_S``.
        """
        base = 15.0
        delay = base * (2 ** max(attempts - 1, 0))
        return float(min(delay, MAX_RETRY_BACKOFF_S))

    async def _schedule_retry(
        self,
        conn: sqlite3.Connection,
        job: Job,
        *,
        i18n_key: str,
        severity: str,
        stderr_tail: str,
    ) -> None:
        """Mark the job for retry and schedule it.

        Bumps ``attempts``, computes a backoff, persists, then spawns a
        delayed re-dispatch task. The job stays in ``self._jobs`` so the
        SSE channel survives across the wait, but its ``status`` flips to
        ``retrying`` so consumers can render a "next attempt at ..." chip.
        """
        delay = self._retry_delay_s(job.attempts)
        retry_at = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=delay)
        job.next_retry_at = retry_at.isoformat(timespec="seconds")
        job.last_error_kind = i18n_key
        job.last_error_severity = severity
        job.error = {
            "i18n_key": i18n_key,
            "severity": severity,
            "stderr_tail": stderr_tail[-2000:],
            "next_retry_at": job.next_retry_at,
            "attempts": job.attempts,
        }
        self._set_status(conn, job, STATUS_RETRYING)
        self._emit(job, "error", job.error)
        audit.append(
            conn,
            "job.retry_scheduled",
            case_id=job.case_id,
            actor="system",
            details={
                "job_id": job.id,
                "i18n_key": i18n_key,
                "severity": severity,
                "attempts": job.attempts,
                "next_retry_at": job.next_retry_at,
                "delay_s": delay,
            },
        )
        self._retry_tasks[job.id] = asyncio.create_task(
            self._wait_and_redispatch(job, delay),
        )

    async def _wait_and_redispatch(self, job: Job, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            self._retry_tasks.pop(job.id, None)
            return
        self._retry_tasks.pop(job.id, None)
        # The user may have cancelled or paused while we were sleeping.
        live = self._jobs.get(job.id)
        if live is None or live.status != STATUS_RETRYING:
            return
        self._run_tasks[job.id] = asyncio.create_task(self._run(live))

    async def _run(self, job: Job) -> None:
        try:
            async with self._sem:
                # User may have requested pause/cancel while we waited on the
                # semaphore. Bail out cleanly without ever spawning the proc.
                intent = self._user_intent.pop(job.id, None)
                if intent in ("pause", "cancel"):
                    conn = db_mod.connect(self._db_path)
                    try:
                        target = STATUS_PAUSED if intent == "pause" else STATUS_CANCELLED
                        self._set_status(conn, job, target)
                        audit.append(
                            conn, f"job.{intent}d",
                            case_id=job.case_id, actor="user",
                            details={"job_id": job.id, "from": "queued"},
                        )
                    finally:
                        conn.close()
                    if intent == "cancel":
                        self._close_channel(job)
                    return
                try:
                    await self._run_inner(job)
                except Exception as exc:  # pragma: no cover — defensive
                    # Don't swallow silently. The investigator gets a §4.7-shaped
                    # error card (phase + cause + action + technical detail), the
                    # operator gets a full traceback in docker logs, and the
                    # audit log records the failure for evidence-export honesty.
                    _log.exception(
                        "job %s failed unexpectedly during phase %r",
                        job.id, job.phase,
                    )
                    cause_key, action_key = _classify_internal_exception(exc)
                    conn = db_mod.connect(self._db_path)
                    try:
                        job.error = {
                            "i18n_key": "errors.unknown",
                            "phase": job.phase,
                            "exc_type": type(exc).__name__,
                            "detail": f"{type(exc).__name__}: {exc}",
                            "cause_i18n_key": cause_key,
                            "suggested_action_i18n_key": action_key,
                        }
                        job.last_error_kind = "errors.unknown"
                        job.last_error_severity = "internal"
                        self._set_status(conn, job, STATUS_FAILED_PERMANENT)
                        audit.append(
                            conn,
                            "job.unexpected_failure",
                            case_id=job.case_id,
                            actor="system",
                            details={
                                "job_id": job.id,
                                "phase": job.phase,
                                "exc_type": type(exc).__name__,
                                "message": str(exc)[:500],
                            },
                        )
                    finally:
                        conn.close()
                    self._emit(job, "error", job.error)
        except asyncio.CancelledError:
            # Cancellation while waiting on semaphore: pause/cancel handled it
            # already via direct status set; just exit quietly.
            raise
        finally:
            self._run_tasks.pop(job.id, None)
            self._procs.pop(job.id, None)
            if job.status in TERMINAL_STATUSES:
                # Postprocess consumes the bundle on success via
                # ``pop_user_browser_bundle``; on cancel/fail-permanent,
                # discard so we never leak temp files holding evidence
                # that didn't make it to the canonical sidecar dir.
                discard_user_browser_bundle(job.id)
                self._close_channel(job)

    async def _run_inner(self, job: Job) -> None:
        # Plan §U6 / Phase D: dispatch on task_kind. The legacy ``full``
        # path runs the whole capture pipeline; ``archive`` and ``media``
        # extend an existing item with a single artifact + re-sign.
        if job.task_kind in (TASK_ARCHIVE, TASK_MEDIA):
            await self._run_extend_task(job)
            return
        conn = db_mod.connect(self._db_path)
        try:
            case = cases.get(conn, job.case_id)
            if case is None:
                job.error = {"i18n_key": "errors.unknown", "detail": "case not found"}
                job.last_error_kind = "errors.unknown"
                job.last_error_severity = "internal"
                self._set_status(conn, job, STATUS_FAILED_PERMANENT)
                self._emit(job, "error", job.error)
                return

            job.attempts += 1
            self._set_status(conn, job, STATUS_RUNNING)

            # Step 1: classify
            self._set_phase(conn, job, "classifying")
            classification = await classify_mod.classify(job.url, case_slug=case.slug)
            job.classification = classification.to_dict()
            self._emit(job, "classification", job.classification)
            with conn:
                conn.execute(
                    "UPDATE jobs SET classification_json = ? WHERE id = ?",
                    (_dumps(job.classification), job.id),
                )

            # Plan §C: resolve the effective profile up-front so every step
            # of the pipeline uses a consistent set of values.
            resolution = profiles_mod.effective_for_case(case.settings)
            profile = resolution.settings
            proxy_url = (case.settings or {}).get("proxy_url") or None

            # Read the extension-supplied bundle EARLY (before snapshot) so
            # the canonical Chromium capture can mirror the user's UA /
            # viewport / timezone, and so an ephemeral-cookies path is in
            # effect for both Playwright and yt-dlp.
            preview_bundle = _user_browser_bundles.get(job.id)
            tab_ctx_obj: capture_mod.TabContext | None = None
            tab_ctx_dict: dict[str, Any] | None = None
            if preview_bundle is not None and preview_bundle.tab_context is not None:
                try:
                    tab_ctx_dict = json.loads(preview_bundle.tab_context.read_text(encoding="utf-8"))
                    tab_ctx_obj = capture_mod.TabContext.from_dict(tab_ctx_dict)
                except (OSError, ValueError):
                    tab_ctx_obj = None
                    tab_ctx_dict = None
            ephemeral_cookies_path: Path | None = (
                preview_bundle.ephemeral_cookies if preview_bundle is not None else None
            )

            # Per-case capture toggles. Defaults are ON per CLAUDE.md §15.
            case_settings = case.settings or {}
            block_ads = bool(case_settings.get("block_ads", True))
            hide_cookie_banners = bool(case_settings.get("hide_cookie_banners", True))

            # Cookie-set provenance: hash whichever file the job will use.
            cookies_snapshot_sha256: str | None = None
            if ephemeral_cookies_path is not None:
                cookies_snapshot_sha256 = cookies_mod.snapshot_hash_path(
                    ephemeral_cookies_path,
                )
            else:
                cookies_snapshot_sha256 = cookies_mod.snapshot_hash(case.slug)

            # Freshness check (case path only — ephemeral cookies were just
            # written and are by definition fresh).
            if ephemeral_cookies_path is None:
                freshness = cookies_mod.validate_freshness(case.slug)
                if freshness is not None and (freshness.expired or freshness.expiring_soon):
                    audit.append(
                        conn,
                        "cookies.stale_at_capture",
                        case_id=case.id,
                        actor="system",
                        details={
                            "job_id": job.id,
                            "expired_domains": [d.domain for d in freshness.expired],
                            "expiring_soon_domains": [
                                d.domain for d in freshness.expiring_soon
                            ],
                            "snapshot_sha256": freshness.snapshot_sha256,
                        },
                    )
                    self._emit(job, "warning", {
                        "i18n_key": "warnings.cookies_stale",
                        "expired": [d.domain for d in freshness.expired],
                        "expiring_soon": [d.domain for d in freshness.expiring_soon],
                    })

            if tab_ctx_dict is not None:
                audit.append(
                    conn,
                    "extension.tab_context_received",
                    case_id=case.id,
                    actor="user",
                    details={
                        "job_id": job.id,
                        "user_agent": tab_ctx_dict.get("user_agent"),
                        "viewport": tab_ctx_dict.get("viewport"),
                        "timezone": tab_ctx_dict.get("timezone"),
                        "language": tab_ctx_dict.get("language"),
                        "color_scheme": tab_ctx_dict.get("color_scheme"),
                        "extension_label": preview_bundle.label if preview_bundle else None,
                    },
                )

            # Step 2a: page snapshot (Playwright + browsertrix)
            self._set_phase(conn, job, "snapshotting")
            try:
                bundle = await capture_mod.capture_page(
                    url=classification.url_final,
                    case_slug=case.slug,
                    proxy_url=proxy_url,
                    tab_context=tab_ctx_obj,
                    cookies_path=ephemeral_cookies_path,
                    block_ads=block_ads,
                    hide_cookie_banners=hide_cookie_banners,
                    app_version=postprocess.APP_VERSION,
                )
            except Exception as exc:
                bundle = capture_mod.CaptureBundle(
                    mhtml=None, screenshot=None, warc=None,
                    chromium_version="0", browsertrix_version="0",
                    page_title=None, response_headers=None,
                )
                audit.append(
                    conn,
                    "page.capture_failed",
                    case_id=case.id,
                    actor="system",
                    details={
                        "url_hash": classification.url_hash,
                        "error": type(exc).__name__,
                        "error_message": str(exc)[:500],
                    },
                )

            # Audit the auditable side-effects of the capture step.
            report = bundle.report
            if report.blocked_requests:
                audit.append(
                    conn,
                    "capture.ads_blocked",
                    case_id=case.id,
                    actor="system",
                    details={
                        "job_id": job.id,
                        "blocked_request_count": len(report.blocked_requests),
                        "blocklist_version": report.blocklist_version,
                    },
                )
            if report.banner_hide_applied:
                audit.append(
                    conn,
                    "capture.banners_hidden",
                    case_id=case.id,
                    actor="system",
                    details={
                        "job_id": job.id,
                        "banner_hide_version": report.banner_hide_version,
                    },
                )
            timed_out = [w.name for w in report.render_waits if w.timed_out]
            if timed_out:
                audit.append(
                    conn,
                    "capture.readiness_timed_out",
                    case_id=case.id,
                    actor="system",
                    details={
                        "job_id": job.id,
                        "timed_out_waits": timed_out,
                    },
                )

            # Surface forensic counters to the SSE stream so the UI's per-job
            # row can render compact icons (visual-first per CLAUDE.md §4.1).
            self._emit(job, "capture_report", {
                "render_waits": [
                    {"name": w.name, "ok": w.ok, "elapsed_ms": w.elapsed_ms,
                     "timed_out": w.timed_out, "detail": w.detail}
                    for w in report.render_waits
                ],
                "blocked_request_count": len(report.blocked_requests),
                "banner_hide_applied": report.banner_hide_applied,
                "tab_context_used": report.tab_context_used,
                "cookies_snapshot_sha256": cookies_snapshot_sha256,
                # v7 hardening counters — surfaced so the UI can show a
                # compact "page-faithfulness" tooltip without re-fetching
                # meta.json. None of these contain user data.
                "animations_frozen": report.animations_frozen,
                "videos_paused": report.videos_paused,
                "lazy_promoted_count": report.lazy_promoted_count,
                "iframes_seen": report.iframes_seen,
                "screenshot_truncated_at_px": report.screenshot_truncated_at_px,
                "readiness_timed_out": report.readiness_timed_out,
                "console_message_count": report.console_message_count,
                "console_error_count": report.console_error_count,
                "media_context_captured": report.media_context_captured,
                "warc_captured_in_session": report.warc_captured_in_session,
                "warc_record_count": report.warc_record_count,
            })

            # Step 2b: yt-dlp
            self._set_phase(conn, job, "downloading")
            # Emit an initial sub-status so the UI shows "Fetching metadata…"
            # immediately, before yt-dlp's first progress byte. Without this
            # the user sees an empty progress label for the first second or
            # two while yt-dlp resolves the URL.
            self._emit(
                job,
                "progress",
                {
                    "status": "downloading",
                    "downloaded_bytes": None,
                    "total_bytes": None,
                    "speed": None,
                    "eta": None,
                    "filename": None,
                    "sub_status": "metadata",
                },
            )
            # Cookie selection for yt-dlp:
            #   - ephemeral path (one-shot extension submission) wins if
            #     present;
            #   - else the case file when classification flagged
            #     authenticated domains.
            if ephemeral_cookies_path is not None:
                cookies_path = ephemeral_cookies_path
            else:
                cookies_path = (
                    cookies_mod.path_for(case.slug)
                    if cookies_mod.exists(case.slug) and classification.authenticated_domains
                    else None
                )
            progress_q: asyncio.Queue = asyncio.Queue()

            # CLAUDE.md §15 v0.7: emit one ``download.options_applied`` row
            # per dispatch when any investigator-facing knob is set, so a
            # recipient walking the audit log sees what was in effect for
            # this run (esp. after a restart with a new option set).
            if not job.download_options.is_default():
                audit.append(
                    conn,
                    "download.options_applied",
                    case_id=case.id,
                    actor="user",
                    details={
                        "job_id": job.id,
                        "options": job.download_options.to_dict(),
                    },
                )
            # restart_pending is set by ``restart()`` and consumed exactly
            # once: this dispatch wipes ``.part`` files and uses
            # --no-continue. Subsequent auto-retries revert to --continue.
            restart_now = job.restart_pending
            job.restart_pending = False

            # capture_mode == "gallery": skip yt-dlp entirely; jump straight
            # to gallery-dl. We manufacture an empty RunResult so the rest of
            # the dispatch (media_files filtering, gallery fallback logic) reads
            # naturally without a parallel code path.
            mode = job.download_options.capture_mode
            if mode == "gallery":
                run_result = ytdlp_runner.RunResult(
                    returncode=0, stdout="", stderr="", info=None
                )
                ytdlp_version = await ytdlp_runner.version()
            else:
                proc_holder: list = []
                run_task = asyncio.create_task(
                    ytdlp_runner.run(
                        url=classification.url_final,
                        case_dir=cases.downloads_dir_for(case.slug),
                        cookies_file=cookies_path,
                        progress_queue=progress_q,
                        proxy_url=proxy_url,
                        proc_holder=proc_holder,
                        socket_timeout_s=profile.socket_timeout_s,
                        limit_rate_kbps=profile.limit_rate_kbps,
                        format_spec=profile.default_format,
                        audio_only=job.download_options.audio_only,
                        quality_cap=job.download_options.quality_cap,
                        subtitle_langs=job.download_options.subtitle_langs or None,
                        video_container=job.download_options.video_container,
                        audio_container=job.download_options.audio_container,
                        restart=restart_now,
                    )
                )
                forward_task = asyncio.create_task(self._forward_progress(job, progress_q))

                # Register the live subprocess as soon as it exists so pause()
                # / cancel() can SIGTERM it. Poll until the subprocess starts or
                # the runner finishes — under heavy load, spawn can take longer
                # than a fixed deadline and a missed registration leaves the
                # process unkillable until it exits naturally.
                while not proc_holder and not run_task.done():
                    await asyncio.sleep(0.02)
                if proc_holder:
                    self._procs[job.id] = proc_holder[0]

                run_result = await run_task
                await forward_task
                self._procs.pop(job.id, None)

                # Plan §U4: user pause/cancel requested mid-run. The subprocess
                # was SIGTERM'd; route the post-run handling away from the
                # failure path and into the paused/cancelled terminus.
                intent = self._user_intent.pop(job.id, None)
                if intent == "pause":
                    self._set_status(conn, job, STATUS_PAUSED)
                    audit.append(
                        conn, "job.paused",
                        case_id=case.id, actor="user",
                        details={"job_id": job.id, "from": "running"},
                    )
                    return
                if intent == "cancel":
                    # Best-effort cleanup of the .part file so a future re-capture
                    # of the same URL doesn't accidentally resume a corrupted blob.
                    for p in run_result.produced_files:
                        try:
                            p.unlink()
                        except OSError:
                            pass
                    self._set_status(conn, job, STATUS_CANCELLED)
                    audit.append(
                        conn, "job.cancelled",
                        case_id=case.id, actor="user",
                        details={"job_id": job.id, "from": "running"},
                    )
                    self._close_channel(job)
                    return

                # Step 3: postprocess
                self._set_phase(conn, job, "finalizing")

                if not run_result.ok and not run_result.produced_files:
                    err = errors_mod.classify(run_result.stderr)
                    # Transient → schedule retry; permanent / internal → surface.
                    if err.severity == "transient":
                        # Plan §U7: feed the network monitor so a clustered
                        # outage flips us offline.
                        if err.i18n_key in ("errors.network", "errors.rate_limited"):
                            await self._network.record_failure()
                        await self._schedule_retry(
                            conn, job,
                            i18n_key=err.i18n_key,
                            severity=err.severity,
                            stderr_tail=run_result.stderr,
                        )
                        return
                    job.error = {
                        "i18n_key": err.i18n_key,
                        "severity": err.severity,
                        "suggested_action": err.suggested_action,
                        "stderr_tail": run_result.stderr[-2000:],
                        "returncode": run_result.returncode,
                    }
                    job.last_error_kind = err.i18n_key
                    job.last_error_severity = err.severity
                    audit.append(
                        conn,
                        "yt_dlp.failed",
                        case_id=case.id,
                        actor="system",
                        details={
                            "url_hash": classification.url_hash,
                            "i18n_key": err.i18n_key,
                            "severity": err.severity,
                            "returncode": run_result.returncode,
                        },
                    )
                    self._set_status(conn, job, STATUS_FAILED_PERMANENT)
                    self._emit(job, "error", job.error)
                    return

                ytdlp_version = await ytdlp_runner.version()

            # Step 3: postprocess (reached by both the normal yt-dlp path
            # and the gallery-mode path that skips yt-dlp entirely).
            self._set_phase(conn, job, "finalizing")

            media_files = [
                p for p in run_result.produced_files
                if not p.name.endswith(
                    (".info.json", ".description", ".live_chat.json")
                )
                and not p.name.endswith((".jpg", ".jpeg", ".png", ".webp"))
            ]
            extra_sidecars = [
                p for p in run_result.produced_files if p not in media_files
            ]

            # CLAUDE.md §15 Gallery pass v0.5: when yt-dlp returns no
            # media, fall back to gallery-dl. Image-only sources
            # (Twitter image threads, Imgur albums, Pixiv posts, Reddit
            # galleries, etc.) become a ``gallery`` capture instead of
            # collapsing to ``page_only``.
            gallery_files: list[Path] = []
            gallery_metadata_files: list[Path] = []
            gallery_extractor: str | None = None
            gallery_dl_version: str | None = None
            gallery_outcome = "skipped"
            gallery_work_dir: Path | None = None
            gallery_enabled = bool(case_settings.get("gallery_enabled", True))
            gallery_max_items = int(
                case_settings.get("gallery_max_items", gallery_dl_runner.DEFAULT_MAX_ITEMS)
            )
            # Per-job opt-in: when set, gallery-dl runs even if yt-dlp produced
            # media. The captured images attach as additional gallery_NNN
            # artifacts on the same item; capture_kind stays ``media`` (yt-dlp
            # wins as primary) when both paths produce output.
            force_gallery_run = bool(job.download_options.force_gallery_run)
            # capture_mode routing (v0.10):
            #   "gallery"  → forced; yt-dlp was skipped entirely above
            #   "webpage"  → always run gallery-dl (not just fallback)
            #   "media"    → current fallback behaviour (only if yt-dlp found nothing)
            #   None       → default (same as "media" + force_gallery_run opt-in)
            if mode == "gallery":
                gallery_should_run = True  # forced; yt-dlp was skipped
            elif mode == "webpage":
                gallery_should_run = gallery_enabled  # always run, not just fallback
            elif mode == "media":
                gallery_should_run = gallery_enabled and not media_files  # current fallback
            else:
                gallery_should_run = gallery_enabled and (
                    not media_files or force_gallery_run
                )
            if gallery_should_run:
                audit.append(
                    conn,
                    "gallery.started",
                    case_id=case.id,
                    actor="system",
                    details={
                        "job_id": job.id,
                        "url_hash": classification.url_hash,
                        "max_items": gallery_max_items,
                    },
                )
                gallery_work_dir = (
                    cases.downloads_dir_for(case.slug) / f"_gallery_{job.id}"
                )
                gallery_work_dir.mkdir(parents=True, exist_ok=True)
                gallery_progress_q: asyncio.Queue = asyncio.Queue()
                gallery_proc_holder: list = []
                self._emit(
                    job,
                    "progress",
                    {
                        "status": "downloading",
                        "downloaded_bytes": None,
                        "total_bytes": None,
                        "speed": None,
                        "eta": None,
                        "filename": None,
                        "sub_status": "gallery_image",
                    },
                )
                g_run_task = asyncio.create_task(
                    gallery_dl_runner.run(
                        url=classification.url_final,
                        work_dir=gallery_work_dir,
                        cookies_file=cookies_path,
                        max_items=gallery_max_items,
                        progress_queue=gallery_progress_q,
                        proxy_url=proxy_url,
                        proc_holder=gallery_proc_holder,
                        socket_timeout_s=profile.socket_timeout_s,
                    )
                )
                g_forward_task = asyncio.create_task(
                    self._forward_progress(job, gallery_progress_q)
                )
                while not gallery_proc_holder and not g_run_task.done():
                    await asyncio.sleep(0.02)
                if gallery_proc_holder:
                    self._procs[job.id] = gallery_proc_holder[0]
                gallery_result = await g_run_task
                await g_forward_task
                self._procs.pop(job.id, None)

                # Honour pause/cancel intent received during gallery-dl
                # (same flow as the yt-dlp branch).
                intent = self._user_intent.pop(job.id, None)
                if intent == "pause":
                    self._set_status(conn, job, STATUS_PAUSED)
                    audit.append(
                        conn, "job.paused",
                        case_id=case.id, actor="user",
                        details={"job_id": job.id, "from": "gallery_running"},
                    )
                    return
                if intent == "cancel":
                    self._set_status(conn, job, STATUS_CANCELLED)
                    audit.append(
                        conn, "job.cancelled",
                        case_id=case.id, actor="user",
                        details={"job_id": job.id, "from": "gallery_running"},
                    )
                    self._close_channel(job)
                    return

                try:
                    gallery_dl_version = await gallery_dl_runner.version()
                except (RuntimeError, OSError):
                    gallery_dl_version = None

                if gallery_result.image_files:
                    gallery_files = list(gallery_result.image_files)
                    gallery_metadata_files = list(gallery_result.metadata_files)
                    gallery_extractor = gallery_result.extractor
                    gallery_outcome = "captured"
                    audit.append(
                        conn,
                        "gallery.captured",
                        case_id=case.id,
                        actor="system",
                        details={
                            "job_id": job.id,
                            "url_hash": classification.url_hash,
                            "image_count": len(gallery_files),
                            "extractor": gallery_extractor,
                        },
                    )
                else:
                    # Differentiate the no-image outcomes — auth wall, rate
                    # limit, generic failure, or simply "no images." The
                    # postprocessor will finalize as page_only either way;
                    # the audit row is the chain-of-custody record.
                    stderr = (gallery_result.stderr or "")[-2000:]
                    g_classified = errors_mod.classify(stderr)
                    if g_classified.i18n_key == "errors.gallery_rate_limited":
                        gallery_outcome = "rate_limited"
                        action = "gallery.rate_limited"
                    elif g_classified.i18n_key == "errors.gallery_auth_required":
                        gallery_outcome = "auth_required"
                        action = "gallery.auth_required"
                    elif gallery_result.returncode == 0:
                        gallery_outcome = "empty"
                        action = "gallery.empty"
                    else:
                        gallery_outcome = "failed"
                        action = "gallery.failed"
                    audit.append(
                        conn,
                        action,
                        case_id=case.id,
                        actor="system",
                        details={
                            "job_id": job.id,
                            "url_hash": classification.url_hash,
                            "returncode": gallery_result.returncode,
                            "i18n_key": g_classified.i18n_key,
                        },
                    )

            # Record the gallery outcome on the capture report so it
            # rides into meta.json.capture (and the report PDF).
            # ``CaptureReport`` is frozen for safety; we patch the
            # serialized dict directly rather than mutate the dataclass.
            capture_report_dict = report.to_dict()
            capture_report_dict["gallery_attempted"] = bool(gallery_should_run)
            capture_report_dict["gallery_outcome"] = gallery_outcome

            # Drain any extension-supplied bundle so postprocess can move
            # those files into the canonical sidecar dir alongside the
            # clean-Chromium capture. ``pop`` returns None when no
            # extension was paired or live-capture was off.
            user_bundle = pop_user_browser_bundle(job.id)
            capture_input = postprocess.CaptureInput(
                case=case,
                job_uuid=job.id,
                url_submitted=job.url,
                url_final=classification.url_final,
                redirect_chain=classification.redirect_chain,
                capture_date=postprocess.utc_now(),
                media_files=media_files,
                info_json=run_result.info,
                extra_sidecars=extra_sidecars,
                gallery_files=gallery_files,
                gallery_metadata_files=gallery_metadata_files,
                gallery_extractor=gallery_extractor,
                gallery_dl_version=gallery_dl_version,
                page_mhtml=bundle.mhtml,
                page_screenshot=bundle.screenshot,
                page_warc=bundle.warc,
                page_har=bundle.har,
                page_console=bundle.console_log,
                page_context_screenshot=bundle.context_screenshot,
                authenticated_domains=classification.authenticated_domains,
                chromium_version=bundle.chromium_version,
                browsertrix_version=bundle.browsertrix_version,
                warcio_version=bundle.warcio_version,
                ytdlp_version=ytdlp_version,
                user_browser_mhtml=user_bundle.mhtml if user_bundle else None,
                user_browser_screenshot=user_bundle.screenshot if user_bundle else None,
                user_browser_har=user_bundle.har if user_bundle else None,
                user_browser_environment=user_bundle.environment if user_bundle else None,
                user_browser_label=user_bundle.label if user_bundle else None,
                user_browser_tab_context=user_bundle.tab_context if user_bundle else None,
                user_browser_session_state=user_bundle.session_state if user_bundle else None,
                user_browser_dom_snapshot_html=user_bundle.dom_snapshot_html if user_bundle else None,
                user_browser_dom_snapshot_meta=user_bundle.dom_snapshot_meta if user_bundle else None,
                capture_report=capture_report_dict,
                cookies_snapshot_sha256=cookies_snapshot_sha256,
                ephemeral_cookies_used=ephemeral_cookies_path is not None,
                lang=job.lang or config.DEFAULT_LANG,
                force_recapture=job.force_recapture,
                download_options=job.download_options.to_dict(),
            )

            # Whether or not postprocess succeeds, the ephemeral cookie
            # path is finished after this job: discard it now.
            if user_bundle and user_bundle.ephemeral_cookies is not None:
                cookies_mod.discard_ephemeral(user_bundle.ephemeral_cookies)
                audit.append(
                    conn,
                    "cookies.ephemeral_used",
                    case_id=case.id,
                    actor="user",
                    details={
                        "job_id": job.id,
                        "snapshot_sha256": cookies_snapshot_sha256,
                    },
                )

            try:
                result = postprocess.finalize(conn, capture_input)
            except postprocess.DuplicateCapture as dup:
                # The canonical evidence is whatever the original capture
                # already saved; the bytes yt-dlp just produced are
                # redundant. Leaving them in case_dir creates orphans —
                # and worse, a future yt-dlp run for a URL that resolves
                # to the same video id will hit --continue, skip the
                # download, and the before/after diff will report no
                # produced media → mis-classified as page_only.
                for p in run_result.produced_files:
                    try:
                        p.unlink()
                    except OSError:
                        pass
                # Same logic for the gallery work dir: postprocess didn't
                # consume it, so we have to remove it here.
                if gallery_work_dir is not None:
                    shutil.rmtree(gallery_work_dir, ignore_errors=True)
                job.error = {
                    "i18n_key": "errors.duplicate",
                    "severity": "permanent",
                    "existing_id": dup.existing_id,
                }
                job.last_error_kind = "errors.duplicate"
                job.last_error_severity = "permanent"
                self._set_status(conn, job, STATUS_FAILED_PERMANENT)
                self._emit(job, "error", job.error)
                return

            # Clean up the gallery work dir on the happy path too:
            # postprocess moved every image / metadata / info.json out, but
            # gallery-dl's per-extractor subdirs remain as empty
            # scaffolding. shutil.rmtree handles the recursive cleanup
            # without us having to walk them.
            if gallery_work_dir is not None:
                shutil.rmtree(gallery_work_dir, ignore_errors=True)

            job.result = {
                "download_id": result.download_id,
                "stem": result.stem,
                "capture_kind": result.capture_kind,
                "relative_media_path": result.relative_media_path,
                "relative_item_dir": result.relative_item_dir,
                "capture_group_id": job.capture_group_id,
            }
            # CLAUDE.md §15: when the user clicked "Re-capture as new
            # entry", anchor the new row to the original via the audit
            # log so the chain-of-custody is traceable. We log on success
            # only — a failed re-capture leaves no row to point to.
            if job.force_recapture:
                audit.append(
                    conn,
                    "duplicate.recaptured",
                    case_id=case.id,
                    download_id=result.download_id,
                    actor="user",
                    details={
                        "job_id": job.id,
                        "original_id": job.original_download_id,
                        "new_id": result.download_id,
                        "stem": result.stem,
                    },
                )
                # Refresh the new row's per-item audit sidecar so the
                # duplicate.recaptured row appears alongside the capture's
                # other forensic records.
                audit.write_item_sidecar(
                    conn,
                    download_id=result.download_id,
                    item_dir=config.DOWNLOADS_DIR / result.relative_item_dir,
                    stem=result.stem,
                )
            # Plan §U6 / Phase D: anchor the capture group to the library row
            # so future archive/media re-fetches know which item to extend.
            if job.capture_group_id:
                link_capture_group_to_download(
                    conn, job.capture_group_id, result.download_id,
                )
            await self._network.record_success()
            self._set_status(conn, job, STATUS_DONE)
            self._emit(job, "done", job.result)
        finally:
            conn.close()

    async def _run_extend_task(self, job: Job) -> None:
        """Run a single-artifact extend task (archive or media re-fetch).

        Plan §U6 / Phase D. The orchestrator looks up the parent
        ``capture_group``, runs only the relevant producer, and calls
        ``postprocess.extend_capture`` to merge + re-sign + audit.
        """
        conn = db_mod.connect(self._db_path)
        try:
            case = cases.get(conn, job.case_id)
            if case is None:
                job.error = {"i18n_key": "errors.unknown", "detail": "case not found"}
                self._set_status(conn, job, STATUS_FAILED_PERMANENT)
                self._emit(job, "error", job.error)
                return
            if not job.capture_group_id:
                job.error = {"i18n_key": "errors.unknown", "detail": "no capture group"}
                self._set_status(conn, job, STATUS_FAILED_PERMANENT)
                self._emit(job, "error", job.error)
                return
            grp = conn.execute(
                "SELECT download_id, source_url FROM capture_groups WHERE id = ?",
                (job.capture_group_id,),
            ).fetchone()
            if grp is None or grp["download_id"] is None:
                job.error = {
                    "i18n_key": "errors.unknown",
                    "detail": "capture group has no completed snapshot to extend",
                }
                self._set_status(conn, job, STATUS_FAILED_PERMANENT)
                self._emit(job, "error", job.error)
                return
            download_id = int(grp["download_id"])

            job.attempts += 1
            self._set_status(conn, job, STATUS_RUNNING)
            resolution = profiles_mod.effective_for_case(case.settings)
            profile = resolution.settings
            proxy_url = (case.settings or {}).get("proxy_url") or None

            work_dir = Path(tempfile.mkdtemp(prefix=f"capsule-{job.task_kind}-"))
            try:
                if job.task_kind == TASK_ARCHIVE:
                    self._set_phase(conn, job, "snapshotting")
                    cookies_path = (
                        cookies_mod.path_for(case.slug)
                        if cookies_mod.exists(case.slug)
                        else None
                    )
                    warc, _ = await capture_mod._browsertrix_warc(
                        url=job.url,
                        out_dir=work_dir,
                        case_cookies_path=cookies_path,
                        proxy_url=proxy_url,
                    )
                    if warc is None:
                        job.error = {
                            "i18n_key": "errors.no_media",
                            "severity": "permanent",
                            "detail": "browsertrix produced no WARC",
                        }
                        self._set_status(conn, job, STATUS_FAILED_PERMANENT)
                        self._emit(job, "error", job.error)
                        return
                    summary = postprocess.extend_capture(
                        conn, download_id=download_id, role="page_warc",
                        source=warc, actor="user",
                    )
                    job.result = summary
                    self._set_status(conn, job, STATUS_DONE)
                    self._emit(job, "done", job.result)
                    return

                # TASK_MEDIA
                self._set_phase(conn, job, "downloading")
                cookies_path = (
                    cookies_mod.path_for(case.slug)
                    if cookies_mod.exists(case.slug)
                    else None
                )
                progress_q: asyncio.Queue = asyncio.Queue()
                proc_holder: list = []
                # Extend tasks reuse the parent's download_options so a
                # media re-fetch keeps the same audio_only / quality_cap
                # the original job ran with. restart_pending is consumed
                # the same way as the full pipeline.
                restart_now = job.restart_pending
                job.restart_pending = False
                run_task = asyncio.create_task(
                    ytdlp_runner.run(
                        url=job.url,
                        case_dir=work_dir,
                        cookies_file=cookies_path,
                        progress_queue=progress_q,
                        proxy_url=proxy_url,
                        proc_holder=proc_holder,
                        socket_timeout_s=profile.socket_timeout_s,
                        limit_rate_kbps=profile.limit_rate_kbps,
                        format_spec=profile.default_format,
                        audio_only=job.download_options.audio_only,
                        quality_cap=job.download_options.quality_cap,
                        subtitle_langs=job.download_options.subtitle_langs or None,
                        restart=restart_now,
                    )
                )
                forward_task = asyncio.create_task(
                    self._forward_progress(job, progress_q),
                )
                # Track the proc the same way as the full pipeline — poll
                # until the subprocess starts or the runner finishes.
                while not proc_holder and not run_task.done():
                    await asyncio.sleep(0.02)
                if proc_holder:
                    self._procs[job.id] = proc_holder[0]
                run_result = await run_task
                await forward_task
                self._procs.pop(job.id, None)

                intent = self._user_intent.pop(job.id, None)
                if intent == "pause":
                    self._set_status(conn, job, STATUS_PAUSED)
                    audit.append(
                        conn, "job.paused",
                        case_id=case.id, actor="user",
                        details={"job_id": job.id, "from": "running", "task_kind": job.task_kind},
                    )
                    return
                if intent == "cancel":
                    self._set_status(conn, job, STATUS_CANCELLED)
                    audit.append(
                        conn, "job.cancelled",
                        case_id=case.id, actor="user",
                        details={"job_id": job.id, "from": "running", "task_kind": job.task_kind},
                    )
                    self._close_channel(job)
                    return

                if not run_result.ok or not run_result.produced_files:
                    err = errors_mod.classify(run_result.stderr)
                    if err.severity == "transient":
                        await self._schedule_retry(
                            conn, job,
                            i18n_key=err.i18n_key,
                            severity=err.severity,
                            stderr_tail=run_result.stderr,
                        )
                        return
                    job.error = {
                        "i18n_key": err.i18n_key,
                        "severity": err.severity,
                        "stderr_tail": run_result.stderr[-2000:],
                    }
                    self._set_status(conn, job, STATUS_FAILED_PERMANENT)
                    self._emit(job, "error", job.error)
                    return

                # Pick the primary media file (first non-sidecar).
                media_files = [
                    p for p in run_result.produced_files
                    if not p.name.endswith((".info.json", ".description"))
                    and not p.name.endswith((".jpg", ".jpeg", ".png", ".webp"))
                ]
                if not media_files:
                    job.error = {
                        "i18n_key": "errors.no_media",
                        "severity": "permanent",
                    }
                    self._set_status(conn, job, STATUS_FAILED_PERMANENT)
                    self._emit(job, "error", job.error)
                    return
                summary = postprocess.extend_capture(
                    conn, download_id=download_id, role="media",
                    source=media_files[0], actor="user",
                )
                job.result = summary
                await self._network.record_success()
                self._set_status(conn, job, STATUS_DONE)
                self._emit(job, "done", job.result)
            finally:
                # Best-effort cleanup of the per-job tempdir. Surface
                # OSError as a warning so a leak (permission denied, file
                # vanished, mount gone) is observable instead of silent.
                for p in work_dir.glob("**/*"):
                    if p.is_file():
                        try:
                            p.unlink()
                        except OSError as exc:
                            _log.warning("tempdir cleanup failed for %s: %s", p, exc)
                try:
                    work_dir.rmdir()
                except OSError as exc:
                    _log.warning("tempdir rmdir failed for %s: %s", work_dir, exc)
        finally:
            conn.close()

    async def _forward_progress(self, job: Job, queue: asyncio.Queue) -> None:
        last_flush = time.monotonic()
        last_persisted: dict[str, Any] | None = None
        flush_conn: sqlite3.Connection | None = None
        try:
            while True:
                update = await queue.get()
                if update is None:
                    if last_persisted is not None:
                        # Final flush so a crash right after the runner exits
                        # still leaves accurate progress on disk.
                        flush_conn = flush_conn or db_mod.connect(self._db_path)
                        _flush_progress(flush_conn, job.id, last_persisted)
                    return
                # CLAUDE.md §15 v0.7: synthetic stall events from the
                # runner's watchdog. Don't change job.status (still
                # 'running'); emit a distinct SSE event + audit row +
                # bump the forensic counter.
                if update.status == "stalled":
                    if not job.stalled:
                        job.stalled = True
                        job.download_options.stalled_count += 1
                        flush_conn = flush_conn or db_mod.connect(self._db_path)
                        try:
                            _update_job(flush_conn, job)
                        except sqlite3.Error as exc:
                            _log.warning(
                                "could not persist stalled_count for %s: %s",
                                job.id, exc,
                            )
                        elapsed = update.raw.get("elapsed_s") if isinstance(update.raw, dict) else None
                        self._emit(job, "stalled", {
                            "elapsed_s": elapsed,
                            "stalled_count": job.download_options.stalled_count,
                        })
                        try:
                            audit.append(
                                flush_conn,
                                "download.stalled",
                                case_id=job.case_id,
                                actor="system",
                                details={
                                    "job_id": job.id,
                                    "elapsed_s": elapsed,
                                    "stalled_count": job.download_options.stalled_count,
                                },
                            )
                        except sqlite3.Error as exc:
                            _log.warning(
                                "could not write download.stalled for %s: %s",
                                job.id, exc,
                            )
                    continue
                # Real progress after a stall — clear the UI flag and audit.
                if job.stalled and update.status in ("downloading", "running", "finished", "postprocess"):
                    job.stalled = False
                    flush_conn = flush_conn or db_mod.connect(self._db_path)
                    try:
                        audit.append(
                            flush_conn,
                            "download.stall_cleared",
                            case_id=job.case_id,
                            actor="system",
                            details={"job_id": job.id},
                        )
                    except sqlite3.Error as exc:
                        _log.warning(
                            "could not write download.stall_cleared for %s: %s",
                            job.id, exc,
                        )
                # Persisted payload (jobs-table progress_json) keeps the
                # original schema — sub_status is a UI-only affordance and
                # must not leak into forensic artifacts (CLAUDE.md §1).
                persisted = {
                    "status": update.status,
                    "downloaded_bytes": update.downloaded_bytes,
                    "total_bytes": update.total_bytes,
                    "speed": update.speed,
                    "eta": update.eta,
                    "filename": update.filename,
                }
                # SSE payload adds sub_status for the in-memory UI only.
                emit_payload = dict(persisted)
                if update.sub_status is not None:
                    emit_payload["sub_status"] = update.sub_status
                last_persisted = persisted
                job.progress.append(emit_payload)
                self._emit(job, "progress", emit_payload)
                now = time.monotonic()
                if now - last_flush >= PROGRESS_FLUSH_INTERVAL_S:
                    flush_conn = flush_conn or db_mod.connect(self._db_path)
                    _flush_progress(flush_conn, job.id, persisted)
                    last_flush = now
        finally:
            if flush_conn is not None:
                flush_conn.close()


_orchestrator: JobOrchestrator | None = None


def orchestrator() -> JobOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = JobOrchestrator()
    return _orchestrator


def reset_for_tests() -> None:
    global _orchestrator
    _orchestrator = None
