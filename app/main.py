"""Capsule FastAPI application (CLAUDE.md §2).

Phase 1 wires the backend-core endpoints: cases, cookies, jobs (incl. SSE),
library, library verification, audit log, system version + manual update,
i18n bundle. Page snapshot capture (Playwright + browsertrix) lands in
Phase 2 — ``postprocess`` already accepts the page artifacts so swapping in
the producer needs no changes here.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from contextlib import asynccontextmanager

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import (
    __version__,
    audit,
    cases,
    classify as classify_mod,
    config,
    cookies as cookies_mod,
    db as db_mod,
    evidence_export,
    extension_tokens,
    gallery_dl_runner,
    i18n,
    jobs as jobs_mod,
    postprocess,
    profiles as profiles_mod,
    signing,
    updates as updates_mod,
    url_canonical,
    ytdlp_runner,
)


def _conn():
    """Per-request connection. SQLite connections are cheap; we don't pool."""
    return db_mod.connect()


def _can_reveal() -> bool:
    """Can the backend spawn the OS file manager to reveal a folder?

    Native macOS / Windows: yes. Linux without ``DISPLAY`` (e.g. inside
    the Docker container): no — the simple-downloader UI then degrades to
    copy-to-clipboard for the path.
    """
    if sys.platform == "darwin":
        return shutil.which("open") is not None
    if sys.platform == "win32":
        return True
    return bool(os.environ.get("DISPLAY")) and shutil.which("xdg-open") is not None


def _ensure_schema() -> None:
    conn = _conn()
    try:
        db_mod.migrate(conn)
    finally:
        conn.close()


def _probe_runtime_deps() -> None:
    """Fail fast if a hard runtime dependency is missing from the image.

    Historically a stale ``capsule:dev`` image (built before the slim
    Python-3.12 + weasyprint Dockerfile) accepted captures and silently
    failed at the per-item PDF render — leaving partial artifacts on disk
    and only a generic "Something went wrong" banner in the UI. Probe at
    startup so the container exits loudly with a rebuild hint instead.
    """
    missing: list[str] = []
    for module in ("weasyprint", "cryptography"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        sys.stderr.write(
            "FATAL: hard runtime dependencies missing from this image: "
            f"{', '.join(missing)}. The Capsule pipeline cannot finalize "
            "captures without these. This usually means the running image "
            "was built from a stale Dockerfile. Rebuild the image:\n"
            "    docker rm -f capsule\n"
            "    docker build -t capsule:dev .\n"
            "and re-run the launcher (or `docker run … capsule:dev`).\n"
        )
        sys.exit(1)


def _record_update_check_audit(snapshot: updates_mod.CheckResult) -> None:
    """Audit-callback for ``updates.auto_check_on_launch`` and the manual
    check route. Lives here (not in ``updates.py``) so the updates module
    stays DB-free.
    """
    conn = _conn()
    try:
        audit.append(
            conn,
            "system.update_check",
            actor="system" if snapshot.triggered_by != "manual" else "user",
            details={
                "triggered_by": snapshot.triggered_by,
                "components": [
                    {
                        "key": c["key"],
                        "tier": c["tier"],
                        "source": c["source"],
                        "installed": c["installed"],
                        "latest": c["latest"],
                        "available": c["available"],
                        "error": c["error"],
                    }
                    for c in snapshot.components
                ],
                "updates_available": snapshot.updates_available,
            },
        )
    finally:
        conn.close()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _probe_runtime_deps()
    _ensure_schema()
    # Generating the keypair on startup means a freshly-installed instance
    # is ready to sign on first capture without a separate setup step.
    signing.ensure_keypair()
    # Plan §U1: resume any jobs that were running when we last shut down.
    # ``rehydrate`` is idempotent, but tests construct multiple lifespans —
    # they pre-reset the orchestrator with ``jobs_mod.reset_for_tests``.
    try:
        await jobs_mod.orchestrator().rehydrate()
    except Exception:
        # Refuse to fail startup over a queue we can't read; log and proceed.
        # The ``jobs`` table is durable so the queue can be inspected and
        # re-resumed manually if a startup hiccup ever stops us here.
        pass
    # CLAUDE.md §15 v0.10: opt-out auto-check on launch. Fire-and-forget so
    # network latency on a slow link never delays uvicorn readiness. The
    # task is tracked so tests can await it deterministically; production
    # runs simply detach and let the asyncio loop drive it to completion.
    auto_check_task: asyncio.Task | None = None
    if updates_mod.auto_check_enabled():
        auto_check_task = asyncio.create_task(
            updates_mod.auto_check_on_launch(
                audit_callback=_record_update_check_audit,
            ),
            name="capsule.updates.auto_check_on_launch",
        )
    app.state.auto_check_task = auto_check_task
    yield
    # Best-effort cancellation on shutdown so the test client's lifespan
    # exit doesn't trip "Task was destroyed but it is pending!".
    if auto_check_task is not None and not auto_check_task.done():
        auto_check_task.cancel()
        try:
            await auto_check_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(
    title="Capsule",
    description="Capture the web, with proof.",
    version=__version__,
    lifespan=_lifespan,
)

# Browser-extension origins. The Capsule extension talks to localhost only,
# so adding CORS for ``chrome-extension://`` and ``moz-extension://`` is the
# minimum required for the popup's fetches to clear the browser pre-flight.
# The main UI is same-origin and tokenless; these regex origins only get a
# response on the routes that explicitly require a Bearer token below.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome-extension|moz-extension|safari-web-extension)://[A-Za-z0-9\-]+$",
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)


# --- Extension auth ---------------------------------------------------------


def _bearer_token(
    authorization: str | None = Header(default=None),
    x_extension_id: str | None = Header(default=None, alias="X-Extension-Id"),
) -> extension_tokens.Token:
    """Validate ``Authorization: Bearer <token>``. Used as a FastAPI dep on
    every extension-only route. The validated :class:`Token` is returned so
    handlers can attach the extension label to audit entries.

    Hardening pass: tokens paired with an ``extension_id`` reject requests
    whose ``X-Extension-Id`` header doesn't match. Mismatches are recorded
    as 403 ``extension.id_mismatch`` audit entries.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    raw = authorization.split(" ", 1)[1].strip()
    try:
        record = extension_tokens.verify(raw, extension_id=x_extension_id)
    except extension_tokens.ExtensionIdMismatch:
        conn = _conn()
        try:
            audit.append(
                conn,
                "extension.id_mismatch",
                actor="system",
                details={
                    "presented_extension_id": x_extension_id or "",
                },
            )
        finally:
            conn.close()
        raise HTTPException(status_code=403, detail="extension id mismatch")
    if record is None:
        raise HTTPException(status_code=401, detail="invalid bearer token")
    extension_tokens.touch(record.id)
    return record


# Static files default to heuristic caching, which leaves a freshly
# hot-swapped container serving stale UI bytes to users with the tab
# already open. ``no-cache`` forces a conditional GET on every request
# but the browser still gets a 304 when the file hasn't changed — so the
# wire cost is one HEAD-equivalent round trip, not a re-download.
class _NoCacheStaticFiles(StaticFiles):
    def is_not_modified(self, response_headers, request_headers) -> bool:
        response_headers["Cache-Control"] = "no-cache"
        return super().is_not_modified(response_headers, request_headers)

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _NoCacheStaticFiles(directory=config.STATIC_DIR), name="static")


# --- Static UI ---------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(
        config.STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# --- i18n --------------------------------------------------------------------


@app.get("/api/i18n/{lang}")
async def get_i18n(lang: str) -> JSONResponse:
    # Reject malformed locale codes at the boundary. Without this, a
    # value like ``../../some/other.json`` would resolve via
    # ``I18N_DIR / f"{lang}.json"`` and read any JSON file the process
    # can open — and ``i18n.load``'s ``lru_cache`` would pin the
    # result indefinitely.
    if not i18n.is_valid_lang(lang):
        raise HTTPException(status_code=400, detail="invalid lang code")
    try:
        bundle = i18n.merged_with_fallback(lang)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(
        {
            "lang": lang,
            "dir": "rtl" if config.is_rtl(lang) else "ltr",
            "messages": bundle,
        }
    )


# --- Cases -------------------------------------------------------------------


class CaseCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""


def _case_to_dict(c: cases.Case) -> dict[str, Any]:
    return {
        "id": c.id,
        "slug": c.slug,
        "name": c.name,
        "description": c.description,
        "status": c.status,
        "settings": c.settings,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


@app.get("/api/cases")
async def list_cases(include_archived: bool = False) -> dict[str, Any]:
    conn = _conn()
    try:
        items = cases.list_all(conn) if include_archived else cases.list_open(conn)
        return {"cases": [_case_to_dict(c) for c in items]}
    finally:
        conn.close()


@app.post("/api/cases")
async def create_case(body: CaseCreate) -> dict[str, Any]:
    conn = _conn()
    try:
        c = cases.create(conn, name=body.name, description=body.description)
        return _case_to_dict(c)
    finally:
        conn.close()


# --- Cookies -----------------------------------------------------------------


def _summary_to_dict(s: cookies_mod.CookiesSummary) -> dict[str, Any]:
    return {
        "total_cookies": s.total_cookies,
        "domains": [
            {
                "domain": d.domain,
                "count": d.count,
                "earliest_expiry": d.earliest_expiry,
                "has_expired": d.has_expired,
            }
            for d in s.domains
        ],
    }


@app.post("/api/cookies")
async def upload_cookies(
    case_id: int = Form(...),
    file: UploadFile = File(...),
    target_url: str | None = Form(None),
) -> dict[str, Any]:
    content = await file.read()
    conn = _conn()
    try:
        case = cases.get(conn, case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        # Validate (parse-only) before auditing so a malformed file doesn't
        # leave a phantom audit row behind.
        try:
            summary = cookies_mod.parse(content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"malformed cookies file: {exc}") from exc
        coverage = cookies_mod.target_coverage(summary, target_url)
        # Audit the upload — never the values, only the domain list and the
        # target URL the investigator was trying to cover (not sensitive).
        # Audit is committed BEFORE the disk write so a successful disk
        # artifact never lacks an audit row (CLAUDE.md §8 invariant).
        details: dict[str, Any] = {"domains": [d.domain for d in summary.domains]}
        if target_url:
            details["target_url"] = target_url
        audit.append(
            conn,
            "cookies.uploaded",
            case_id=case.id,
            actor="user",
            details=details,
        )
        cookies_mod.save(case.slug, content)
        return {
            "case_id": case_id,
            "summary": _summary_to_dict(summary),
            "target": coverage,
        }
    finally:
        conn.close()


# --- Jobs --------------------------------------------------------------------


_QUALITY_CAP_VALUES = {"audio", "480", "720", "1080", "best"}
_KNOWN_SUB_LANGS = {
    "en", "ja", "ar", "es", "fr", "de", "zh", "pt",
    "all",
}
# CLAUDE.md §15 v0.9 — single source of truth lives on jobs_mod
# (jobs_mod.VIDEO_CONTAINERS / AUDIO_CONTAINERS); referenced here by name
# so the validator and the runner can't drift.


class JobBatchItem(BaseModel):
    url: str = Field(min_length=1)
    # CLAUDE.md §15: True iff the user picked "Re-capture as new entry"
    # in the duplicate-handling modal. ``finalize`` then suffixes the
    # url_hash with ``__c{N+1}`` so the row sits as a sibling.
    force_recapture: bool = False
    original_download_id: int | None = None
    # CLAUDE.md §15 v0.7 — per-submission download options.
    audio_only: bool = False
    # 'audio' | '480' | '720' | '1080' | 'best' | None
    quality_cap: str | None = None
    subtitle_langs: list[str] | None = None
    # CLAUDE.md §15 v0.9 — container picker. None ⇒ yt-dlp default.
    video_container: str | None = None
    audio_container: str | None = None
    # When True, gallery-dl runs even if yt-dlp produced media. The captured
    # images attach as additional ``gallery_NNN`` artifacts on the same item;
    # capture_kind stays ``media`` (yt-dlp's primary) when both paths produce
    # output. Useful for blog/article pages where yt-dlp may only grab an
    # embedded video while gallery-dl finds the surrounding photo set.
    force_gallery_run: bool = False
    # CLAUDE.md §15 v0.10 — capture mode routing.
    # "webpage" | "media" | "gallery" | None (None ⇒ current default behaviour).
    capture_mode: str | None = None

    @field_validator("quality_cap")
    @classmethod
    def _validate_quality_cap(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in _QUALITY_CAP_VALUES:
            raise ValueError(
                f"quality_cap must be one of {sorted(_QUALITY_CAP_VALUES)}"
            )
        return v

    @field_validator("video_container")
    @classmethod
    def _validate_video_container(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if v not in jobs_mod.VIDEO_CONTAINERS:
            raise ValueError(
                f"video_container must be one of {list(jobs_mod.VIDEO_CONTAINERS)}"
            )
        return v

    @field_validator("audio_container")
    @classmethod
    def _validate_audio_container(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if v not in jobs_mod.AUDIO_CONTAINERS:
            raise ValueError(
                f"audio_container must be one of {list(jobs_mod.AUDIO_CONTAINERS)}"
            )
        return v

    @field_validator("capture_mode")
    @classmethod
    def _validate_capture_mode(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if v not in jobs_mod._CAPTURE_MODE_VALUES:
            raise ValueError(
                f"capture_mode must be one of {sorted(jobs_mod._CAPTURE_MODE_VALUES)}"
            )
        return v

    @field_validator("subtitle_langs")
    @classmethod
    def _validate_sub_langs(cls, v: list[str] | None) -> list[str] | None:
        if not v:
            return None
        # Accept anything BCP-47-ish (lowercase letters / digits / hyphens),
        # plus the literal 'all' sentinel. Reject untrusted long strings
        # that could blow up the yt-dlp argv.
        cleaned: list[str] = []
        for s in v:
            if not isinstance(s, str):
                raise ValueError("subtitle_langs must be strings")
            tag = s.strip().lower()
            if not tag or len(tag) > 32:
                continue
            if not all(ch.isalnum() or ch == "-" for ch in tag):
                raise ValueError(f"invalid subtitle language tag: {s!r}")
            cleaned.append(tag)
        return cleaned or None


class JobBatch(BaseModel):
    case_id: int | None = None
    # Legacy plain-list shape (extension and v0 frontend). Either ``urls``
    # or ``items`` must be present, not both.
    urls: list[str] | None = Field(default=None, max_length=25)
    items: list[JobBatchItem] | None = Field(default=None, max_length=25)
    # UI locale at submission time (Track A). Drives the per-item
    # manifest PDF's labels + RTL/LTR + font stack. ``None`` ⇒ the
    # orchestrator falls back to ``config.DEFAULT_LANG``.
    lang: str | None = None


def _normalize_batch_items(body: JobBatch) -> list[JobBatchItem]:
    """Resolve ``body.urls`` xor ``body.items`` into a canonical item list.

    Within-batch dedup keys on the *canonical URL* — different paste
    variants of the same URL collapse so the user never accidentally
    fires two jobs for the same video. Forced re-captures (``force_recapture
    is True``) are exempt from the dedup so multiple sibling re-captures
    can coexist in one submission.
    """
    if body.urls is not None and body.items is not None:
        raise HTTPException(
            status_code=400, detail="provide either urls or items, not both"
        )
    raw_items: list[JobBatchItem] = []
    if body.items is not None:
        raw_items = list(body.items)
    elif body.urls is not None:
        # Strip whitespace + filter empty strings up-front so the
        # JobBatchItem ``min_length=1`` constraint isn't tripped by
        # blanks the user accidentally pasted.
        raw_items = [JobBatchItem(url=u.strip()) for u in body.urls if u and u.strip()]
    if not raw_items:
        raise HTTPException(status_code=400, detail="no URLs")
    if len(raw_items) > 25:
        raise HTTPException(status_code=400, detail="too many URLs (max 25)")

    seen_canon: set[str] = set()
    out: list[JobBatchItem] = []
    for it in raw_items:
        url = it.url.strip()
        if not url:
            continue
        canon = url_canonical.canonicalize(url)
        if not it.force_recapture and canon in seen_canon:
            continue
        seen_canon.add(canon)
        out.append(JobBatchItem(
            url=url,
            force_recapture=it.force_recapture,
            original_download_id=it.original_download_id,
            audio_only=it.audio_only,
            quality_cap=it.quality_cap,
            subtitle_langs=it.subtitle_langs,
            video_container=it.video_container,
            audio_container=it.audio_container,
            force_gallery_run=it.force_gallery_run,
            capture_mode=it.capture_mode,
        ))
    if not out:
        raise HTTPException(status_code=400, detail="no URLs")
    return out


@app.post("/api/jobs/batch")
async def submit_jobs_batch(body: JobBatch) -> dict[str, Any]:
    """Submit one or many URLs as captures.

    If ``case_id`` is omitted, the URLs are routed into the auto-managed
    default case that backs the Simple-mode downloader (slug ``downloads``
    on fresh installs, ``quick-captures`` on legacy installs). The upper
    bound of 25 URLs per submission is enforced by the schema and keeps the
    active-jobs UI manageable; the orchestrator's own semaphore (default 2)
    bounds actual concurrency.

    The endpoint accepts either ``urls: list[str]`` (legacy / extension)
    or ``items: list[JobBatchItem]`` (frontend, after preflight). The
    item shape carries ``force_recapture`` for the §15 modal flow.
    """
    conn = _conn()
    try:
        if body.case_id is None:
            case = cases.ensure_default_case(conn)
        else:
            case = cases.get(conn, body.case_id)
            if case is None:
                raise HTTPException(status_code=404, detail="case not found")
    finally:
        conn.close()

    items = _normalize_batch_items(body)

    submitted = []
    for it in items:
        # Build DownloadOptions iff any v0.7/v0.9 knob is set; otherwise
        # pass None so the orchestrator uses the dataclass defaults.
        opts: jobs_mod.DownloadOptions | None = None
        if (
            it.audio_only
            or it.quality_cap
            or it.subtitle_langs
            or it.video_container
            or it.audio_container
            or it.force_gallery_run
            or it.capture_mode
        ):
            opts = jobs_mod.DownloadOptions(
                audio_only=it.audio_only,
                quality_cap=it.quality_cap,
                subtitle_langs=list(it.subtitle_langs or []),
                video_container=it.video_container,
                audio_container=it.audio_container,
                force_gallery_run=it.force_gallery_run,
                capture_mode=it.capture_mode,
            )
        job = await jobs_mod.orchestrator().submit(
            case_id=case.id,
            url=it.url,
            lang=body.lang,
            force_recapture=it.force_recapture,
            original_download_id=it.original_download_id,
            download_options=opts,
        )
        submitted.append(job.to_dict())
    return {"case_id": case.id, "jobs": submitted}


# --- Job control routes (CLAUDE.md §15 v0.7) ---------------------------------
# These re-introduce the pause/resume/cancel HTTP surface that v0.3 removed,
# alongside a new ``restart`` route. The orchestrator already owns the
# state-machine work; these routes just invoke it and surface 404/409 in
# the right shape for the frontend's banner system (CLAUDE.md §4.7).


def _job_or_404(job_id: str) -> jobs_mod.Job:
    job = jobs_mod.orchestrator().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.post("/api/jobs/{job_id}/pause")
async def pause_job(job_id: str) -> dict[str, Any]:
    job = _job_or_404(job_id)
    ok = await jobs_mod.orchestrator().pause(job_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="job is already terminal or not pausable",
        )
    # Refresh after the transition so the response carries the new status.
    return _job_or_404(job_id).to_dict()


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str) -> dict[str, Any]:
    _job_or_404(job_id)
    ok = await jobs_mod.orchestrator().resume(job_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="job is not paused",
        )
    return _job_or_404(job_id).to_dict()


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    _job_or_404(job_id)
    ok = await jobs_mod.orchestrator().cancel(job_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="job is already terminal",
        )
    return _job_or_404(job_id).to_dict()


@app.post("/api/jobs/{job_id}/restart")
async def restart_job(job_id: str) -> dict[str, Any]:
    """Force-restart: SIGTERM live subprocess, wipe ``.part``/``.ytdl``,
    re-dispatch with ``--no-continue`` (CLAUDE.md §15 v0.7).

    Distinct from resume: resume preserves ``.part`` files and lets
    yt-dlp's ``--continue`` pick up where it stopped. Restart is the
    investigator's escape hatch when the partial bytes are corrupted
    or when forensically clean re-fetch is desired.
    """
    _job_or_404(job_id)
    ok = await jobs_mod.orchestrator().restart(job_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="job cannot be restarted (already done or unknown)",
        )
    return _job_or_404(job_id).to_dict()


# --- Preflight (CLAUDE.md §15) -----------------------------------------------


class PreflightBody(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=25)
    case_id: int | None = None


@app.post("/api/jobs/preflight")
async def preflight_jobs(body: PreflightBody) -> dict[str, Any]:
    """Classify URLs and surface duplicates *before* the capture pipeline runs.

    For each URL we resolve the redirect chain (cap-budgeted at ~15s by
    ``classify``), compute the canonical url_hash, and probe the
    downloads table. The frontend uses this to drive the §15 duplicate-
    handling modal and the batch-summary chips, avoiding the wasted
    ~30s of yt-dlp + Playwright work that the late-detection fallback
    in ``postprocess`` would otherwise discard.
    """
    conn = _conn()
    try:
        if body.case_id is None:
            case = cases.ensure_default_case(conn)
        else:
            case = cases.get(conn, body.case_id)
            if case is None:
                raise HTTPException(status_code=404, detail="case not found")
    finally:
        conn.close()

    # De-duplicate within the submitted list by raw string first so we
    # don't classify the same URL twice. Within-batch canonical
    # collapsing happens after classification (so ``utm_*`` variants
    # of the same URL get flagged as ``within_batch_duplicate``).
    raw_seen: set[str] = set()
    submitted: list[str] = []
    for raw in body.urls:
        u = raw.strip()
        if u and u not in raw_seen:
            raw_seen.add(u)
            submitted.append(u)
    if not submitted:
        raise HTTPException(status_code=400, detail="no URLs")

    sem = asyncio.Semaphore(4)

    async def _one(url: str) -> dict[str, Any]:
        async with sem:
            try:
                cls = await classify_mod.classify(url, case_slug=case.slug)
            except Exception as exc:  # noqa: BLE001 — best-effort preview
                return {
                    "url_submitted": url,
                    "status": "classification_failed",
                    "error": f"{type(exc).__name__}",
                }
            return {
                "url_submitted": url,
                "url_final": cls.url_final,
                "url_canonical": cls.url_canonical,
                "url_hash": cls.url_hash,
                "platform": cls.platform,
                "redirect_chain_length": len(cls.redirect_chain),
                "authenticated_domains": list(cls.authenticated_domains),
                "classification_error": cls.error,
            }

    classified = await asyncio.gather(*[_one(u) for u in submitted])

    # Probe the DB for each URL to find existing duplicates. A single
    # connection for the whole batch is fine — preflight is read-only.
    conn = _conn()
    try:
        canon_first_seen: dict[str, int] = {}
        results: list[dict[str, Any]] = []
        for idx, info in enumerate(classified):
            if info.get("status") == "classification_failed":
                results.append(info)
                continue
            canon = info["url_canonical"]
            if canon in canon_first_seen:
                first_idx = canon_first_seen[canon]
                results.append({
                    **info,
                    "status": "within_batch_duplicate",
                    "first_seen_at_index": first_idx,
                })
                continue
            canon_first_seen[canon] = idx
            existing_rows = conn.execute(
                "SELECT id, capture_kind, title, platform, capture_date, "
                "item_dir, source_url FROM downloads "
                "WHERE case_id = ? AND (url_hash = ? OR url_hash LIKE ?) "
                "ORDER BY capture_date DESC LIMIT 1",
                (case.id, info["url_hash"], info["url_hash"] + "__c%"),
            ).fetchall()
            if existing_rows:
                row = existing_rows[0]
                # Audit the detection — chain-of-custody anchor (§8/§15).
                audit.append(
                    conn,
                    "duplicate.detected",
                    case_id=case.id,
                    download_id=int(row["id"]),
                    actor="system",
                    details={
                        "url_hash": info["url_hash"],
                        "redirect_chain_length": info["redirect_chain_length"],
                        "submitted": info["url_submitted"],
                    },
                )
                results.append({
                    **info,
                    "status": "duplicate",
                    "existing": {
                        "id": int(row["id"]),
                        "title": row["title"],
                        "platform": row["platform"],
                        "capture_kind": row["capture_kind"],
                        "capture_date": row["capture_date"],
                        "item_dir": row["item_dir"],
                        "source_url": row["source_url"],
                    },
                })
            else:
                results.append({**info, "status": "new"})
    finally:
        conn.close()

    summary = {
        "new": sum(1 for r in results if r.get("status") == "new"),
        "duplicates_blocked": sum(
            1 for r in results if r.get("status") == "duplicate"
        ),
        "within_batch_duplicates": sum(
            1 for r in results if r.get("status") == "within_batch_duplicate"
        ),
        "classification_failed": sum(
            1 for r in results if r.get("status") == "classification_failed"
        ),
    }
    return {"case_id": case.id, "results": results, "summary": summary}


# --- Duplicate-modal outcome audit (CLAUDE.md §15) ---------------------------


class DuplicateOutcomeBody(BaseModel):
    case_id: int
    existing_id: int
    outcome: str = Field(pattern="^(opened_existing|cancelled)$")


@app.post("/api/jobs/duplicate-outcome")
async def duplicate_outcome(body: DuplicateOutcomeBody) -> dict[str, Any]:
    """Record the user's choice from the duplicate-handling modal.

    ``recaptured`` is logged from the orchestrator on a successful
    re-capture finalize (so the audit row points at the new download
    row). ``opened_existing`` and ``cancelled`` go through this route —
    they have no follow-up backend work, only the audit entry.
    """
    conn = _conn()
    try:
        case = cases.get(conn, body.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        action = f"duplicate.{body.outcome}"
        entry_id = audit.append(
            conn,
            action,
            case_id=case.id,
            download_id=body.existing_id,
            actor="user",
            details={"existing_id": body.existing_id},
        )
    finally:
        conn.close()
    return {"ok": True, "audit_id": entry_id}


@app.get("/api/jobs/{job_id}/events")
async def stream_job_events(job_id: str, request: Request) -> StreamingResponse:
    job = jobs_mod.orchestrator().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    async def event_source():
        async for evt in jobs_mod.orchestrator().events(job_id):
            if await request.is_disconnected():
                return
            payload = {"event": evt["event"], "data": evt["data"]}
            yield f"event: {evt['event']}\ndata: {json.dumps(payload['data'])}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


# --- Profiles (plan §C) ------------------------------------------------------


class ProfileChoice(BaseModel):
    profile: str = Field(min_length=1)
    profile_overrides: dict[str, Any] | None = None


@app.get("/api/system/profile")
async def get_app_profile() -> dict[str, Any]:
    settings = profiles_mod.load_app_default()
    resolution = profiles_mod.effective_for_case(app_settings=settings)
    return {
        "app_settings": settings,
        "effective": resolution.settings.to_dict(),
        "available": list(profiles_mod.PROFILE_NAMES),
    }


@app.put("/api/system/profile")
async def set_app_profile(body: ProfileChoice) -> dict[str, Any]:
    if body.profile not in profiles_mod.PROFILE_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown profile {body.profile!r}",
        )
    settings = profiles_mod.load_app_default()
    settings["profile"] = body.profile
    if body.profile_overrides is not None:
        settings["profile_overrides"] = body.profile_overrides
    profiles_mod.save_app_default(settings)
    conn = _conn()
    try:
        audit.append(
            conn, "profile.changed",
            actor="user",
            details={"profile": body.profile, "overrides": body.profile_overrides or {}},
        )
    finally:
        conn.close()
    resolution = profiles_mod.effective_for_case(app_settings=settings)
    return {
        "app_settings": settings,
        "effective": resolution.settings.to_dict(),
    }


# --- Library -----------------------------------------------------------------


@app.get("/api/library")
async def list_library(
    case_id: int | None = None,
    platform: str | None = None,
    capture_kind: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    conn = _conn()
    try:
        # ``has_user_browser_capture`` is a cheap server-side flag for the
        # item-detail "supplementary capture" chip. A ``LIKE`` test on the
        # meta_json blob avoids parsing JSON for every row in a large library.
        sql = (
            "SELECT id, case_id, capture_kind, source_url, final_url, platform, "
            "video_id, uploader, title, capture_date, relative_path, item_dir, "
            "file_size_bytes, md5, sha256, signing_key_fp, "
            "(meta_json LIKE '%\"user_browser_%') AS has_user_browser_capture "
            "FROM downloads WHERE 1=1"
        )
        params: list[Any] = []
        if case_id is not None:
            sql += " AND case_id = ?"
            params.append(case_id)
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        if capture_kind is not None:
            sql += " AND capture_kind = ?"
            params.append(capture_kind)
        sql += " ORDER BY capture_date DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = [dict(r) for r in conn.execute(sql, params)]
        return {"items": rows, "limit": limit, "offset": offset}
    finally:
        conn.close()


@app.post("/api/library/verify")
async def verify_library(download_id: int | None = None) -> dict[str, Any]:
    """Re-hash artifacts and re-verify signatures.

    With ``download_id`` set, verifies one item; otherwise verifies all.
    """
    conn = _conn()
    try:
        if download_id is not None:
            rows = list(conn.execute(
                "SELECT * FROM downloads WHERE id = ?", (download_id,)
            ))
            if not rows:
                raise HTTPException(status_code=404, detail="download not found")
        else:
            rows = list(conn.execute("SELECT * FROM downloads"))

        report = []
        for row in rows:
            r = dict(row)
            meta = json.loads(r["meta_json"])
            item_dir = config.DOWNLOADS_DIR / r["item_dir"]
            stem = item_dir.name
            # v0.8 layout: meta.json + sig live in Metadata/. Fall back to
            # the item root so existing items captured under the pre-v0.8
            # layout still verify without manual reorganization.
            meta_path = item_dir / "Metadata" / f"{stem}.meta.json"
            sig_path = item_dir / "Metadata" / f"{stem}.meta.json.sig"
            if not meta_path.is_file():
                meta_path = item_dir / f"{stem}.meta.json"
                sig_path = item_dir / f"{stem}.meta.json.sig"
            issues: list[str] = []
            sig_ok = False
            if meta_path.is_file() and sig_path.is_file():
                sig_ok = signing.verify(
                    meta_path.read_bytes(), sig_path.read_bytes()
                )
                if not sig_ok:
                    issues.append("signature_mismatch")
            else:
                issues.append("signature_missing")

            artifacts_ok = True
            for role, rel in meta.get("artifacts", {}).items():
                p = config.DOWNLOADS_DIR / rel
                if not p.is_file():
                    issues.append(f"missing:{role}")
                    artifacts_ok = False
                    continue
                actual = postprocess.compute_hashes(p)
                expected = meta.get("checksums", {}).get(role, {})
                if actual["sha256"] != expected.get("sha256"):
                    issues.append(f"hash_mismatch:{role}")
                    artifacts_ok = False
            audit.append(
                conn,
                "item.verified",
                case_id=int(r["case_id"]),
                download_id=int(r["id"]),
                actor="user",
                details={
                    "stem": stem,
                    "sig_ok": sig_ok,
                    "artifacts_ok": artifacts_ok,
                    "issues": issues,
                },
            )
            audit.write_item_sidecar(
                conn,
                download_id=int(r["id"]),
                item_dir=item_dir,
                stem=stem,
            )
            report.append(
                {
                    "download_id": r["id"],
                    "ok": sig_ok and artifacts_ok,
                    "issues": issues,
                }
            )
        return {"results": report}
    finally:
        conn.close()


# --- Audit -------------------------------------------------------------------


@app.get("/api/audit")
async def list_audit(
    case_id: int | None = None,
    since: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, Any]:
    conn = _conn()
    try:
        rows = list(audit.iter_entries(conn, case_id=case_id, since=since, limit=limit))
        ok, broken = audit.verify_chain(conn)
        return {"chain_ok": ok, "broken_at": broken, "entries": rows}
    finally:
        conn.close()


# --- Clear case (CLAUDE.md §15, plan §I) -------------------------------------


@app.post("/api/cases/{case_id}/clear")
async def clear_case(case_id: int) -> dict[str, Any]:
    """Permanently delete every capture in the case.

    The case row itself, its cookies file, and the signing key all stay.
    Only the on-disk per-item folders and the ``downloads`` rows for the
    case go. The audit log keeps every prior entry untouched and gains a
    single ``library.cleared`` row carrying a snapshot (id, url_hash,
    media sha256, capture_date, sha256 of meta.json) of what was
    deleted — the chain-of-custody anchor for the deletion event.

    This is the single most destructive route in the app. The frontend
    gates the call behind a confirmation dialog with an explicit "Delete
    N captures" button.
    """
    conn = _conn()
    try:
        case = cases.get(conn, case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")

        rows = conn.execute(
            "SELECT id, url_hash, capture_date, sha256, item_dir, meta_json "
            "FROM downloads WHERE case_id = ?",
            (case.id,),
        ).fetchall()
        if not rows:
            return {"deleted_count": 0, "freed_bytes": 0, "audit_id": None}

        import hashlib as _hashlib  # local: keeps the route self-contained
        snapshot: list[dict[str, Any]] = []
        freed_bytes = 0
        for r in rows:
            item_dir_rel = r["item_dir"]
            item_dir = config.DOWNLOADS_DIR / item_dir_rel if item_dir_rel else None
            if item_dir and item_dir.exists():
                for p in item_dir.rglob("*"):
                    try:
                        if p.is_file():
                            freed_bytes += p.stat().st_size
                    except OSError:
                        pass
                try:
                    shutil.rmtree(item_dir)
                except OSError:
                    pass
            meta_json = r["meta_json"] or ""
            meta_sha = (
                _hashlib.sha256(meta_json.encode("utf-8")).hexdigest()
                if meta_json
                else None
            )
            snapshot.append({
                "id": int(r["id"]),
                "url_hash": r["url_hash"],
                "capture_date": r["capture_date"],
                "media_sha256": r["sha256"],
                "meta_json_sha256": meta_sha,
            })

        # Drop refetch capture-group bookkeeping so the orchestrator
        # doesn't try to extend rows that no longer exist.
        try:
            conn.execute(
                "DELETE FROM capture_groups WHERE case_id = ?", (case.id,),
            )
        except Exception:
            # capture_groups table may not exist on older fixture DBs.
            pass

        with conn:
            conn.execute("DELETE FROM downloads WHERE case_id = ?", (case.id,))

        audit_id = audit.append(
            conn,
            "library.cleared",
            case_id=case.id,
            actor="user",
            details={
                "count": len(snapshot),
                "freed_bytes": freed_bytes,
                "items": snapshot,
            },
        )
        return {
            "deleted_count": len(snapshot),
            "freed_bytes": freed_bytes,
            "audit_id": audit_id,
        }
    finally:
        conn.close()


# --- Evidence export ---------------------------------------------------------


@app.post("/api/cases/{case_id}/export")
async def export_case(
    case_id: int,
    lang: str | None = Query(default=None),
) -> FileResponse:
    """Build a signed evidence-export bundle for ``case_id``.

    Returns the zip directly so the frontend can stream it to the user as
    a single download. The bundle is also persisted to
    ``$CAPSULE_CONFIG_DIR/exports/`` for re-download if needed.

    ``lang`` selects the locale for the rendered case_report.pdf inside
    the bundle. Defaults to ``config.DEFAULT_LANG``.
    """
    conn = _conn()
    try:
        try:
            result = evidence_export.build_bundle(
                conn, case_id=case_id, lang=lang or config.DEFAULT_LANG,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            path=str(result.zip_path),
            filename=result.zip_path.name,
            media_type="application/zip",
        )
    finally:
        conn.close()


# --- System ------------------------------------------------------------------


@app.get("/api/system/version")
async def system_version() -> dict[str, Any]:
    try:
        ytdlp_v = await ytdlp_runner.version()
    except Exception:
        ytdlp_v = "unknown"
    try:
        gallery_dl_v = await gallery_dl_runner.version()
    except Exception:
        gallery_dl_v = "unknown"
    kp = signing.ensure_keypair()
    # Translate a container path under DOWNLOADS_DIR to its host equivalent
    # when the launcher passed CAPSULE_HOST_DOWNLOADS_DIR. Used by the UI to
    # surface a path the user can actually paste into Finder/Explorer.
    def _host(path: Path) -> str | None:
        if not config.HOST_DOWNLOADS_DIR:
            return None
        try:
            rel = path.resolve().relative_to(config.DOWNLOADS_DIR.resolve())
        except ValueError:
            return None
        # Preserve the host's separator style: backslash on Windows host,
        # forward slash everywhere else. The launcher writes the env var
        # using the host's native form so we just append our segments.
        host = config.HOST_DOWNLOADS_DIR.rstrip("/\\")
        sep = "\\" if "\\" in host else "/"
        rel_str = str(rel).replace("/", sep) if rel.parts else ""
        return f"{host}{sep}{rel_str}" if rel_str else host

    # Resolve the active default-case slug at runtime: legacy users with a
    # ``quick-captures`` row continue to see that path, fresh installs get
    # the new ``downloads`` slug. The frontend reads ``default_case_slug``
    # rather than hardcoding either value (CLAUDE.md §15).
    conn = _conn()
    try:
        default_case = cases.ensure_default_case(conn)
    finally:
        conn.close()
    default_dir = cases.downloads_dir_for(default_case.slug)
    return {
        "app": __version__,
        "yt_dlp": ytdlp_v,
        "gallery_dl": gallery_dl_v,
        "chromium": "0",          # Phase 2 sets this
        "browsertrix": "0",       # Phase 2 sets this
        "signing_key_fingerprint": signing.fingerprint(kp.public),
        "default_case_slug": default_case.slug,
        "paths": {
            "downloads_dir": str(config.DOWNLOADS_DIR),
            "default_case_dir": str(default_dir),
            "host_downloads_dir": config.HOST_DOWNLOADS_DIR,
            "host_default_case_dir": _host(default_dir),
            "can_reveal": _can_reveal(),
        },
    }


class RevealRequest(BaseModel):
    relative_path: str = Field(default="")


@app.post("/api/system/reveal")
async def reveal(body: RevealRequest) -> dict[str, Any]:
    """Open a folder under ``DOWNLOADS_DIR`` in the OS file manager.

    The path is interpreted relative to ``config.DOWNLOADS_DIR`` and must
    not escape it. Inside Docker (Linux, no DISPLAY) the call returns
    ``{ok: false, reason: "no_desktop"}`` so the frontend can fall back to
    copy-to-clipboard.
    """
    rel = body.relative_path.lstrip("/\\")
    base = config.DOWNLOADS_DIR.resolve()
    target = (config.DOWNLOADS_DIR / rel).resolve() if rel else base
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="path escapes downloads dir") from exc
    if not target.exists():
        raise HTTPException(status_code=404, detail="path not found")
    if not _can_reveal():
        return {"ok": False, "reason": "no_desktop"}
    if sys.platform == "darwin":
        cmd = ["open", str(target)]
    elif sys.platform == "win32":
        cmd = ["explorer", str(target)]
    else:
        cmd = ["xdg-open", str(target)]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}
    return {"ok": True, "target": str(target)}


# Components updatable via /api/system/update. Each entry is the user-facing
# component name → (pip package, version-fetcher coroutine). Adding a new
# updatable runtime is one entry in ``app.updates.COMPONENTS`` (registry) +
# one entry here (pip install path).
_UPDATABLE_COMPONENTS: dict[str, tuple[str, Callable[[], Awaitable[str]]]] = {
    "yt-dlp": ("yt-dlp", ytdlp_runner.version),
    "gallery-dl": ("gallery-dl", gallery_dl_runner.version),
}


@app.post("/api/system/update")
async def system_update(component: str = "yt-dlp") -> dict[str, Any]:
    """User-triggered runtime upgrade. Never invoked automatically.

    ``component`` selects which downloader to upgrade — ``yt-dlp`` (default,
    back-compat) or ``gallery-dl``. Both run the same ``pip install
    --upgrade <pkg>`` flow, fetch the new version, and audit-log the result
    with the component label.

    Tier 2 components (e.g. Capsule itself) are documented in the registry
    but cannot be installed in-container — the UI surfaces a copy-paste
    ``docker pull`` command instead. Posting one of those keys here returns
    a 400 with ``i18n_key`` so the frontend can render a localized message.
    """
    # Distinguish "tier 2 component, must rebuild image" from "no such
    # component" so the UI can show the right error copy.
    if component not in _UPDATABLE_COMPONENTS:
        registered = next(
            (c for c in updates_mod.COMPONENTS if c.key == component),
            None,
        )
        if registered is not None and registered.tier == updates_mod.TIER_IMAGE_REBUILD:
            raise HTTPException(
                status_code=400,
                detail={
                    "i18n_key": "errors.update.requires_image_rebuild",
                    "component": component,
                },
            )
        raise HTTPException(
            status_code=400,
            detail={
                "i18n_key": "errors.update.unknown_component",
                "component": component,
            },
        )
    pkg_name, version_fn = _UPDATABLE_COMPONENTS[component]

    pip = shutil.which("pip") or sys.executable
    # Match exactly ``pip`` / ``pip3`` (or ``pip.exe`` / ``pip3.exe``) — anything
    # else (e.g. ``pipx``) falls through to the explicit ``python -m pip`` form.
    pip_name = Path(pip).stem if pip else ""
    cmd = [pip, "install", "--upgrade", pkg_name] if pip_name in {"pip", "pip3"} else [
        sys.executable, "-m", "pip", "install", "--upgrade", pkg_name
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    new_version = "unknown"
    try:
        new_version = await version_fn()
    except Exception:
        pass
    conn = _conn()
    try:
        audit.append(
            conn,
            "system.updated",
            actor="user",
            details={
                "component": component,
                "returncode": proc.returncode,
                "new_version": new_version,
            },
        )
    finally:
        conn.close()
    return {
        "ok": proc.returncode == 0,
        "component": component,
        "returncode": proc.returncode,
        "new_version": new_version,
        "stdout_tail": out.decode(errors="replace")[-2000:],
        "stderr_tail": err.decode(errors="replace")[-2000:],
    }


# --- Update management (CLAUDE.md §15 v0.10) --------------------------------
#
# /api/system/updates       — read cache (no network; cheap; called on every
#                             page load + after every navigation)
# /api/system/updates/check — refresh cache (one HTTP call per source; audited
#                             as system.update_check; manual or launch-fired)
# /api/system/updates/auto_check — read/write the opt-out toggle (audited as
#                                  system.auto_check_changed)


def _updates_response(snapshot: updates_mod.CheckResult | None) -> dict[str, Any]:
    """Shape the cache (or a fresh snapshot) into the wire format the UI
    expects. Always includes ``auto_check`` so the toggle reflects the
    current setting on every read.
    """
    auto = updates_mod.auto_check_enabled()
    if snapshot is not None:
        payload = snapshot.to_dict()
        payload["auto_check"] = auto
        return payload
    cache = updates_mod.read_cache()
    return {
        "auto_check": auto,
        "last_checked_at": cache.get("last_checked_at"),
        "components": list(cache.get("components") or []),
        "updates_available": int(cache.get("updates_available") or 0),
        "triggered_by": cache.get("triggered_by"),
    }


@app.get("/api/system/updates")
async def updates_status() -> dict[str, Any]:
    """Return the cached update view + the live auto-check setting.

    Never makes a network call. The cache is populated by
    ``POST /api/system/updates/check`` (manual) or the lifespan auto-check.
    """
    return _updates_response(None)


@app.post("/api/system/updates/check")
async def updates_check_now() -> dict[str, Any]:
    """Force a fresh round of installed + latest probes.

    Audits one ``system.update_check`` row with ``triggered_by: "manual"``.
    Returns the same shape as the GET endpoint.
    """
    snapshot = await updates_mod.perform_check(triggered_by="manual")
    _record_update_check_audit(snapshot)
    return _updates_response(snapshot)


class AutoCheckUpdate(BaseModel):
    enabled: bool


@app.put("/api/system/updates/auto_check")
async def updates_set_auto_check(body: AutoCheckUpdate) -> dict[str, Any]:
    """Toggle the opt-out auto-check setting. Audited."""
    previous = updates_mod.auto_check_enabled()
    updates_mod.set_auto_check(body.enabled)
    if previous != bool(body.enabled):
        conn = _conn()
        try:
            audit.append(
                conn,
                "system.auto_check_changed",
                actor="user",
                details={"enabled": bool(body.enabled), "previous": previous},
            )
        finally:
            conn.close()
    return _updates_response(None)


class DismissUpdateBanner(BaseModel):
    components: list[str] = Field(default_factory=list)


@app.post("/api/system/updates/dismiss_banner")
async def updates_dismiss_banner(body: DismissUpdateBanner) -> dict[str, Any]:
    """Audit-only endpoint: the user clicked Dismiss on the home banner.

    The setting cog dot stays lit (chain-of-custody — the dot tracks
    "update available", not "user has seen this"). Returns the cache view
    so the frontend can re-sync after dismissal.
    """
    conn = _conn()
    try:
        audit.append(
            conn,
            "system.update_dismissed",
            actor="user",
            details={"components": list(body.components)},
        )
    finally:
        conn.close()
    return _updates_response(None)


# --- Browser-extension surface ----------------------------------------------
#
# These routes pair the Capsule UI with a first-party browser extension that
# (a) sends URLs from the active tab(s) into the active case, (b) syncs cookies
# for the live browser session — including HttpOnly, which document.cookie
# cannot expose — and optionally (c) uploads a supplementary "as-rendered-by-
# the-investigator's-browser" capture (MHTML/screenshot/HAR/environment) that
# rides alongside the canonical container-Chromium capture as additive
# evidence.
#
# All routes require Bearer-token auth (see ``_bearer_token``) — tokens are
# minted by the UI's pairing flow and stored hashed on disk, never echoed.
#
# Forensic invariants (also see plan):
#   1. Canonical capture is unchanged. user_browser_* artifacts are additive.
#   2. Cookie *values* are never logged, never round-tripped, never returned.
#   3. Bundle temp files are unlinked on terminal failure (jobs.py finally).


class ExtensionPairBody(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    extension_id: str | None = Field(default=None, max_length=120)


@app.post("/api/extension/pair")
async def extension_pair(body: ExtensionPairBody) -> dict[str, Any]:
    """Mint a new pairing token. Token is shown to the investigator once,
    then discarded by the UI. The hash is persisted; ``last_used_at`` is
    bumped on every authenticated request so a stale extension is visible
    in Settings.
    """
    record, raw = extension_tokens.issue(body.label, extension_id=body.extension_id)
    conn = _conn()
    try:
        kp = signing.ensure_keypair()
        audit.append(
            conn,
            "extension.paired",
            actor="user",
            details={
                "token_id": record.id,
                "label": record.label,
                "extension_id": record.extension_id,
            },
        )
        return {
            "token": raw,
            "token_id": record.id,
            "label": record.label,
            "created_at": record.created_at,
            "server_fingerprint": signing.fingerprint(kp.public),
        }
    finally:
        conn.close()


@app.get("/api/extension/tokens")
async def extension_list_tokens() -> dict[str, Any]:
    """List paired extensions (id, label, last-used, etc.) for Settings.

    Same-origin route (called from the Capsule UI), so no Bearer required —
    the UI is already trusted with the disk on which the tokens live.
    """
    rows = [
        {
            "id": t.id,
            "label": t.label,
            "extension_id": t.extension_id,
            "created_at": t.created_at,
            "last_used_at": t.last_used_at,
        }
        for t in extension_tokens.list_tokens()
    ]
    return {"tokens": rows}


@app.delete("/api/extension/pair/{token_id}")
async def extension_revoke(token_id: str) -> dict[str, Any]:
    record = extension_tokens.revoke(token_id)
    if record is None:
        raise HTTPException(status_code=404, detail="token not found")
    conn = _conn()
    try:
        audit.append(
            conn,
            "extension.revoked",
            actor="user",
            details={"token_id": record.id, "label": record.label},
        )
    finally:
        conn.close()
    return {"ok": True, "token_id": record.id}


@app.post("/api/extension/pair/{token_id}/rotate")
async def extension_rotate(token_id: str) -> dict[str, Any]:
    """Issue a replacement token for an existing pairing.

    Same-origin only — same trust posture as ``GET /api/extension/tokens``.
    The label and extension_id binding carry over; the new raw token is
    shown to the investigator once and never persisted on the host.
    """
    result = extension_tokens.rotate(token_id)
    if result is None:
        raise HTTPException(status_code=404, detail="token not found")
    record, raw = result
    conn = _conn()
    try:
        kp = signing.ensure_keypair()
        audit.append(
            conn,
            "extension.token_rotated",
            actor="user",
            details={
                "old_token_id": token_id,
                "new_token_id": record.id,
                "label": record.label,
                "extension_id": record.extension_id,
            },
        )
        return {
            "token": raw,
            "token_id": record.id,
            "label": record.label,
            "created_at": record.created_at,
            "server_fingerprint": signing.fingerprint(kp.public),
        }
    finally:
        conn.close()


@app.get("/api/extension/cases")
async def extension_list_cases(
    token: extension_tokens.Token = Depends(_bearer_token),
) -> dict[str, Any]:
    """Lightweight case list for the popup's case picker. Mirrors
    ``GET /api/cases`` but is callable from the extension origin via the
    Bearer-token middleware."""
    conn = _conn()
    try:
        items = cases.list_open(conn)
        return {"cases": [_case_to_dict(c) for c in items]}
    finally:
        conn.close()


# Browser-extension cookie object — matches Chrome's chrome.cookies.getAll
# / Firefox's browser.cookies.getAll shape. Only a few fields are required;
# the rest are tolerated for forward-compat with future browser changes.
class ExtensionCookie(BaseModel):
    name: str = Field(min_length=1)
    value: str
    domain: str = Field(min_length=1)
    path: str = "/"
    expirationDate: float | None = None
    secure: bool = False
    httpOnly: bool = False
    hostOnly: bool = False
    sameSite: str | None = None

    model_config = {"extra": "allow"}


class CookiesJsonBody(BaseModel):
    case_id: int
    cookies: list[ExtensionCookie] = Field(default_factory=list)
    target_url: str | None = None


@app.post("/api/cookies/json")
async def upload_cookies_json(
    body: CookiesJsonBody,
    token: extension_tokens.Token = Depends(_bearer_token),
) -> dict[str, Any]:
    """Persist browser-extension JSON cookies as the case's Netscape file.

    Reuses the existing 0600 path so the rest of the capture pipeline
    (yt-dlp, Playwright, browsertrix) is identical. Cookie values are
    never logged; the audit entry records only the domain list.
    """
    conn = _conn()
    try:
        case = cases.get(conn, body.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        try:
            summary = cookies_mod.write_json(
                case.slug,
                [c.model_dump() for c in body.cookies],
                target_url=body.target_url,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"malformed cookie: {exc}") from exc
        coverage = cookies_mod.target_coverage(summary, body.target_url)
        details: dict[str, Any] = {
            "domains": [d.domain for d in summary.domains],
            "source": "extension",
            "extension_label": token.label,
        }
        if body.target_url:
            details["target_url"] = body.target_url
        audit.append(
            conn,
            "cookies.uploaded",
            case_id=case.id,
            actor="user",
            details=details,
        )
        return {
            "case_id": body.case_id,
            "summary": _summary_to_dict(summary),
            "target": coverage,
        }
    finally:
        conn.close()


# Live-capture artifacts the extension may attach per URL. All optional —
# the investigator can disable live capture and just send URLs + cookies.
#
# Hardening pass: tab_context (UA / viewport / scroll / tz), session_state
# (per-origin local/sessionStorage), and the click-time DOM snapshot are
# additive; missing fields fall back to engine defaults / no record.
class LiveCapturePayload(BaseModel):
    url: str = Field(min_length=1)
    mhtml_b64: str | None = None
    screenshot_b64: str | None = None
    har: dict[str, Any] | None = None
    environment: dict[str, Any] | None = None
    # Hardening additions:
    tab_context: dict[str, Any] | None = None
    session_state: list[dict[str, Any]] | None = None  # [{origin, localStorage, sessionStorage, captured_at}, ...]
    dom_snapshot_html_b64: str | None = None
    dom_snapshot_meta: dict[str, Any] | None = None
    capture_warnings: list[str] | None = None  # client-side partial-capture warnings


class ExtensionCaptureBody(BaseModel):
    case_id: int
    urls: list[str] = Field(min_length=1, max_length=25)
    cookies: list[ExtensionCookie] = Field(default_factory=list)
    live_captures: list[LiveCapturePayload] = Field(default_factory=list)
    # 'case' (default, current behavior) persists cookies as the case file.
    # 'ephemeral' writes them to a per-job tmpdir, used for one capture,
    # discarded after — never written to the case directory.
    cookie_persistence: str = Field(default="case", pattern=r"^(case|ephemeral)$")
    # UI locale at submission time (Track A). Threads through to the
    # per-item manifest PDF; falls back to ``config.DEFAULT_LANG``.
    lang: str | None = None
    # Capture mode override from the extension popup ("webpage" | "media" |
    # "gallery" | None). Applied uniformly to all URLs in this submission.
    # Mirrors the ``download_options.capture_mode`` field in DownloadOptions.
    capture_mode: str | None = None


def _decode_b64(blob: str | None, *, label: str) -> bytes | None:
    if blob is None:
        return None
    try:
        return base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"invalid base64 for {label}: {exc}"
        ) from exc


def _stash_live_capture(
    job_id: str,
    payload: LiveCapturePayload | None,
    label: str,
    *,
    ephemeral_cookies: Path | None = None,
) -> None:
    """Materialise an extension live-capture payload onto a per-job tmpdir
    and stash a :class:`UserBrowserBundle` for the orchestrator to pick up.

    Cleanup of the tmpdir is the orchestrator's job (success → postprocess
    consumes; failure → ``jobs._cleanup_bundle``). When ``ephemeral_cookies``
    is set, the bundle carries the path so the orchestrator wires it into
    the capture pipeline and discards it after the job ends.
    """
    tmp_root = config.CONFIG_DIR / "extension_inbox"
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix=f"job-{job_id[:8]}-", dir=str(tmp_root)))

    mhtml_path: Path | None = None
    shot_path: Path | None = None
    har_path: Path | None = None
    env_path: Path | None = None
    tab_path: Path | None = None
    session_path: Path | None = None
    dom_html_path: Path | None = None
    dom_meta_path: Path | None = None

    if payload is not None:
        if payload.mhtml_b64:
            data = _decode_b64(payload.mhtml_b64, label="mhtml_b64")
            mhtml_path = tmpdir / "user-browser.mhtml"
            mhtml_path.write_bytes(data or b"")

        if payload.screenshot_b64:
            data = _decode_b64(payload.screenshot_b64, label="screenshot_b64")
            shot_path = tmpdir / "user-browser.png"
            shot_path.write_bytes(data or b"")

        if payload.har is not None:
            har_path = tmpdir / "user-browser.har"
            har_path.write_text(
                json.dumps(payload.har, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

        if payload.environment is not None:
            env_path = tmpdir / "user-browser.environment.json"
            env_path.write_text(
                json.dumps(payload.environment, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

        if payload.tab_context is not None:
            tab_path = tmpdir / "user-browser.tab-context.json"
            tab_path.write_text(
                json.dumps(payload.tab_context, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

        if payload.session_state is not None:
            session_path = tmpdir / "user-browser.session-state.json"
            session_path.write_text(
                json.dumps(payload.session_state, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

        if payload.dom_snapshot_html_b64:
            data = _decode_b64(payload.dom_snapshot_html_b64, label="dom_snapshot_html_b64")
            dom_html_path = tmpdir / "user-browser.dom-snapshot.html"
            dom_html_path.write_bytes(data or b"")

        if payload.dom_snapshot_meta is not None:
            dom_meta_path = tmpdir / "user-browser.dom-snapshot.json"
            dom_meta_path.write_text(
                json.dumps(payload.dom_snapshot_meta, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

    artifacts_present = any((
        mhtml_path, shot_path, har_path, env_path,
        tab_path, session_path, dom_html_path, dom_meta_path,
    ))
    if artifacts_present or ephemeral_cookies is not None:
        bundle = jobs_mod.UserBrowserBundle(
            tmpdir=tmpdir,
            mhtml=mhtml_path,
            screenshot=shot_path,
            har=har_path,
            environment=env_path,
            label=label,
            tab_context=tab_path,
            session_state=session_path,
            dom_snapshot_html=dom_html_path,
            dom_snapshot_meta=dom_meta_path,
            ephemeral_cookies=ephemeral_cookies,
        )
        jobs_mod.attach_user_browser_bundle(job_id, bundle)
    else:
        # Nothing to stash — clean up the empty tmpdir.
        try:
            tmpdir.rmdir()
        except OSError:
            pass


@app.post("/api/extension/capture")
async def extension_capture(
    body: ExtensionCaptureBody,
    token: extension_tokens.Token = Depends(_bearer_token),
) -> dict[str, Any]:
    """Atomic capture submission from the browser extension.

    Steps (in this order so a failure rolls back cleanly):
      1. Validate the case.
      2. If cookies were supplied, persist them via the same 0600 path
         used by ``/api/cookies/json``.
      3. Submit each URL as a job (max 25, matching ``/api/jobs/batch``).
      4. For each ``live_capture`` entry, materialise the artifacts onto a
         per-job tmpdir and stash a :class:`UserBrowserBundle` for the
         orchestrator. Postprocess will move them into the canonical
         sidecar directory alongside the clean-Chromium capture.
      5. Append an ``extension.capture_submitted`` audit entry recording
         the extension label, URL count, and authenticated domains —
         **never** cookie values.
    """
    # Step 1: validate case.
    conn = _conn()
    try:
        case = cases.get(conn, body.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
    finally:
        conn.close()

    # Step 2: cookies (optional). For 'case' persistence (default), the
    # cookies file is written to the case directory before jobs run. For
    # 'ephemeral', cookies are NOT persisted — each job gets a one-shot
    # tmpdir cookie file that's discarded after the job ends.
    domains: list[str] = []
    cookies_dicts: list[dict[str, Any]] = []
    if body.cookies:
        cookies_dicts = [c.model_dump() for c in body.cookies]
        if body.cookie_persistence == "case":
            try:
                summary = cookies_mod.write_json(case.slug, cookies_dicts)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"malformed cookie: {exc}") from exc
            domains = [d.domain for d in summary.domains]
        else:
            # Ephemeral: parse only, to validate; persist per-job at submit time.
            try:
                text = cookies_mod.to_netscape(cookies_dicts)
                summary = cookies_mod.parse(text)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"malformed cookie: {exc}") from exc
            domains = [d.domain for d in summary.domains]

    # Step 3: deduplicate + submit jobs. Mirrors /api/jobs/batch's logic so
    # the popup's "send list" form behaves identically to the in-app batch.
    seen: set[str] = set()
    urls: list[str] = []
    for raw in body.urls:
        u = raw.strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    if not urls:
        raise HTTPException(status_code=400, detail="no URLs")
    submitted = []
    for u in urls:
        opts: jobs_mod.DownloadOptions | None = None
        if body.capture_mode:
            opts = jobs_mod.DownloadOptions(capture_mode=body.capture_mode)
        job = await jobs_mod.orchestrator().submit(
            case_id=case.id, url=u, lang=body.lang,
            download_options=opts,
        )
        submitted.append(job.to_dict())

    # Step 4: pair live captures with their submitted job by URL match.
    # The extension may send live captures for a subset of URLs; we route
    # each payload to the first matching job (insertion order). For
    # ephemeral cookies, each job gets its own freshly-written tmpdir
    # cookie file.
    live_by_url: dict[str, list[LiveCapturePayload]] = {}
    for payload in body.live_captures:
        canonical_key = url_canonical.canonicalize(payload.url.strip())
        live_by_url.setdefault(canonical_key, []).append(payload)
    for job_dict, raw_url in zip(submitted, urls):
        ephemeral_path: Path | None = None
        if body.cookie_persistence == "ephemeral" and cookies_dicts:
            try:
                ephemeral_path, _ = cookies_mod.write_ephemeral(
                    job_dict["id"], cookies_dicts,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=f"malformed cookie: {exc}",
                ) from exc
        payloads = live_by_url.get(url_canonical.canonicalize(raw_url)) or []
        # Always stash if either (a) live capture artifacts present or
        # (b) ephemeral cookies need to ride to the orchestrator.
        if payloads or ephemeral_path is not None:
            _stash_live_capture(
                job_dict["id"],
                payloads[0] if payloads else None,
                label=token.label,
                ephemeral_cookies=ephemeral_path,
            )

    # Step 5: audit. Cookie values never enter ``details`` (audit module
    # rejects forbidden keys at any depth, but we also never include them).
    conn = _conn()
    try:
        audit.append(
            conn,
            "extension.capture_submitted",
            case_id=case.id,
            actor="user",
            details={
                "extension_label": token.label,
                "extension_token_id": token.id,
                "url_count": len(urls),
                "cookie_domains": domains,
                "cookie_persistence": body.cookie_persistence,
                "live_capture_urls": sorted(live_by_url.keys()),
            },
        )
    finally:
        conn.close()

    return {
        "case_id": case.id,
        "jobs": submitted,
        "event_urls": [f"/api/jobs/{j['id']}/events" for j in submitted],
    }


@app.get("/api/extension/download")
async def extension_download() -> StreamingResponse:
    """Stream a zip of the bundled extension source.

    Same-origin only — the Capsule UI calls this to give investigators a
    one-click download instead of asking them to find the project folder
    on disk (which doesn't exist on a Docker-only install). No auth: the
    contents are public source files already shipped with the app.
    """
    import io
    import zipfile

    ext_dir = config.PROJECT_ROOT / "extension"
    if not ext_dir.is_dir():
        raise HTTPException(status_code=404, detail="extension folder not found")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in ext_dir.rglob("*"):
            if p.is_file():
                # Inside the zip we want a flat ``extension/...`` prefix so
                # the user can unzip and "Load unpacked" on the resulting
                # folder directly.
                arc = "extension/" + str(p.relative_to(ext_dir)).replace(os.sep, "/")
                zf.write(p, arcname=arc)
    buf.seek(0)
    headers = {"Content-Disposition": 'attachment; filename="capsule-extension.zip"'}
    return StreamingResponse(buf, media_type="application/zip", headers=headers)
