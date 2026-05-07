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
from pydantic import BaseModel, Field

from . import (
    __version__,
    audit,
    cases,
    config,
    cookies as cookies_mod,
    db as db_mod,
    evidence_export,
    extension_tokens,
    i18n,
    jobs as jobs_mod,
    paths,
    postprocess,
    profiles as profiles_mod,
    signing,
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


@asynccontextmanager
async def _lifespan(app: FastAPI):
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
    yield


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


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


# --- Static UI ---------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# --- i18n --------------------------------------------------------------------


@app.get("/api/i18n/{lang}")
async def get_i18n(lang: str) -> JSONResponse:
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


class CaseRename(BaseModel):
    name: str = Field(min_length=1)


class CaseStatusUpdate(BaseModel):
    status: str


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


@app.get("/api/cases/{case_id}")
async def get_case(case_id: int) -> dict[str, Any]:
    conn = _conn()
    try:
        c = cases.get(conn, case_id)
        if c is None:
            raise HTTPException(status_code=404, detail="case not found")
        return _case_to_dict(c)
    finally:
        conn.close()


@app.patch("/api/cases/{case_id}")
async def update_case(case_id: int, body: CaseRename) -> dict[str, Any]:
    conn = _conn()
    try:
        c = cases.rename(conn, case_id, body.name)
        return _case_to_dict(c)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@app.post("/api/cases/{case_id}/status")
async def set_case_status(case_id: int, body: CaseStatusUpdate) -> dict[str, Any]:
    conn = _conn()
    try:
        c = cases.update_status(conn, case_id, body.status)
        return _case_to_dict(c)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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


@app.get("/api/cookies")
async def get_cookies(case_id: int) -> dict[str, Any]:
    conn = _conn()
    try:
        case = cases.get(conn, case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        s = cookies_mod.summary(case.slug)
        if s is None:
            return {"case_id": case_id, "summary": None}
        return {"case_id": case_id, "summary": _summary_to_dict(s)}
    finally:
        conn.close()


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


class CookiesPreviewBody(BaseModel):
    content: str
    target_url: str | None = None
    case_id: int | None = None
    mode: str | None = None  # "replace" | "merge"


@app.post("/api/cookies/preview")
async def preview_cookies(body: CookiesPreviewBody) -> dict[str, Any]:
    """Validate raw Netscape text without persisting anything.

    The wizard calls this on every drop/paste so the investigator sees a
    parse result and a target-coverage check before committing. When
    ``case_id`` and ``mode='merge'`` are supplied, the response also
    includes a ``merge_preview`` block showing what the post-merge file
    would look like (added/replaced/kept counts plus the resulting
    summary). Parse failures return ``200`` with a populated ``errors``
    list so the UI can surface them inline; only request-shape problems
    become 4xx.
    """
    try:
        summary = cookies_mod.parse(body.content)
    except (ValueError, UnicodeDecodeError) as exc:
        return {
            "summary": None, "target": None, "errors": [str(exc)],
            "merge_preview": None,
        }
    coverage = cookies_mod.target_coverage(summary, body.target_url)

    merge_block: dict[str, Any] | None = None
    if body.mode == "merge" and body.case_id is not None:
        conn = _conn()
        try:
            case = cases.get(conn, body.case_id)
            if case is None:
                raise HTTPException(status_code=404, detail="case not found")
            resulting, stats = cookies_mod.merge_preview(case.slug, body.content)
            merge_block = {
                "added": stats.added,
                "replaced": stats.replaced,
                "kept": stats.kept,
                "resulting_summary": _summary_to_dict(resulting),
            }
        finally:
            conn.close()

    return {
        "summary": _summary_to_dict(summary),
        "target": coverage,
        "errors": [],
        "merge_preview": merge_block,
    }


class CookiesTextBody(BaseModel):
    case_id: int
    content: str
    target_url: str | None = None
    mode: str = "replace"  # "replace" | "merge"


@app.post("/api/cookies/text")
async def upload_cookies_text(body: CookiesTextBody) -> dict[str, Any]:
    """Save cookies submitted as raw Netscape text (paste-from-clipboard).

    With ``mode='replace'`` (default) the case's cookies file is written
    afresh. With ``mode='merge'``, incoming cookies are merged into the
    existing file: cookies present only in incoming are appended,
    cookies present in both are updated to the incoming version, cookies
    present only in existing are kept. The audit log records the mode
    and (for merges) the added/replaced/kept counts. Same disk +
    no-values guarantees as :func:`upload_cookies` / :func:`save`.
    """
    if body.mode not in ("replace", "merge"):
        raise HTTPException(status_code=400, detail=f"invalid mode: {body.mode!r}")
    conn = _conn()
    try:
        case = cases.get(conn, body.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        content_bytes = body.content.encode("utf-8")
        # Validate / preview before auditing so a malformed file doesn't
        # leave a phantom audit row behind.
        try:
            if body.mode == "merge":
                summary, merge_stats = cookies_mod.merge_preview(case.slug, body.content)
            else:
                summary = cookies_mod.parse(content_bytes)
                merge_stats = None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"malformed cookies file: {exc}") from exc
        coverage = cookies_mod.target_coverage(summary, body.target_url)
        details: dict[str, Any] = {
            "domains": [d.domain for d in summary.domains],
            "mode": body.mode,
        }
        if body.target_url:
            details["target_url"] = body.target_url
        if merge_stats is not None:
            details["added"] = merge_stats.added
            details["replaced"] = merge_stats.replaced
            details["kept"] = merge_stats.kept
        # Audit before disk write so a successful artifact never lacks an
        # audit row (CLAUDE.md §8 invariant).
        audit.append(
            conn,
            "cookies.uploaded",
            case_id=case.id,
            actor="user",
            details=details,
        )
        if body.mode == "merge":
            cookies_mod.save_merged(case.slug, content_bytes)
        else:
            cookies_mod.save(case.slug, content_bytes)
        return {
            "case_id": body.case_id,
            "summary": _summary_to_dict(summary),
            "target": coverage,
            "merge_stats": (
                {
                    "added": merge_stats.added,
                    "replaced": merge_stats.replaced,
                    "kept": merge_stats.kept,
                }
                if merge_stats is not None
                else None
            ),
        }
    finally:
        conn.close()


# --- Jobs --------------------------------------------------------------------


class JobSubmit(BaseModel):
    case_id: int
    url: str = Field(min_length=1)


class JobBatch(BaseModel):
    case_id: int | None = None
    urls: list[str] = Field(min_length=1, max_length=25)


@app.post("/api/jobs")
async def submit_job(body: JobSubmit) -> dict[str, Any]:
    conn = _conn()
    try:
        case = cases.get(conn, body.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
    finally:
        conn.close()
    job = await jobs_mod.orchestrator().submit(case_id=body.case_id, url=body.url)
    return job.to_dict()


@app.post("/api/jobs/batch")
async def submit_jobs_batch(body: JobBatch) -> dict[str, Any]:
    """Submit one or many URLs as captures.

    If ``case_id`` is omitted, the URLs are routed into the auto-managed
    ``quick-captures`` case that backs the Simple-mode downloader. The
    upper bound of 25 URLs per submission is enforced by the schema and
    keeps the active-jobs UI manageable; the orchestrator's own semaphore
    (default 2) bounds actual concurrency.
    """
    conn = _conn()
    try:
        if body.case_id is None:
            case = cases.ensure_quick(conn)
        else:
            case = cases.get(conn, body.case_id)
            if case is None:
                raise HTTPException(status_code=404, detail="case not found")
    finally:
        conn.close()

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
        job = await jobs_mod.orchestrator().submit(case_id=case.id, url=u)
        submitted.append(job.to_dict())
    return {"case_id": case.id, "jobs": submitted}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = jobs_mod.orchestrator().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_dict()


@app.get("/api/jobs")
async def list_jobs(
    case_id: int | None = None,
    status: str | None = None,
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    """List persisted jobs with counts-by-status — plan §U9 Queue view.

    Filters are optional; without them, returns every persisted job up to
    ``limit`` in reverse-creation order plus a summary of state counts so
    the frontend's queue view can show "47 of 50 done, 1 retrying" without
    a second round-trip.
    """
    conn = _conn()
    try:
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if case_id is not None:
            sql += " AND case_id = ?"
            params.append(case_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = list(conn.execute(sql, params))

        summary_rows = list(conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs"
            + (" WHERE case_id = ?" if case_id is not None else "")
            + " GROUP BY status",
            ([case_id] if case_id is not None else []),
        ))
        counts = {r["status"]: int(r["n"]) for r in summary_rows}
    finally:
        conn.close()
    return {
        "jobs": [jobs_mod.Job.from_row(r).to_dict() for r in rows],
        "counts": counts,
        "limit": limit,
    }


class PreflightRequest(BaseModel):
    url: str = Field(min_length=1)


@app.post("/api/jobs/preflight")
async def preflight_size(body: PreflightRequest) -> dict[str, Any]:
    """Ask yt-dlp how large this download would be — plan Phase E.

    Spawns yt-dlp with ``--print filesize_approx``. No download happens.
    Used by the Slow profile UI to surface "this will take ~3 h at your
    current speed" before the user commits.
    """
    info = await ytdlp_runner.preflight(body.url)
    return info


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


# --- Pause / resume / cancel (plan §U4) -------------------------------------


@app.post("/api/jobs/{job_id}/pause")
async def pause_job(job_id: str) -> dict[str, Any]:
    ok = await jobs_mod.orchestrator().pause(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="job is terminal or unknown")
    return {"ok": True, "job_id": job_id}


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str) -> dict[str, Any]:
    ok = await jobs_mod.orchestrator().resume(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="job is not paused")
    return {"ok": True, "job_id": job_id}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    ok = await jobs_mod.orchestrator().cancel(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="job is terminal or unknown")
    return {"ok": True, "job_id": job_id}


@app.post("/api/jobs/pause-all")
async def pause_all_jobs() -> dict[str, Any]:
    n = await jobs_mod.orchestrator().pause_all()
    return {"ok": True, "paused": n}


@app.post("/api/jobs/resume-all")
async def resume_all_jobs() -> dict[str, Any]:
    n = await jobs_mod.orchestrator().resume_all()
    return {"ok": True, "resumed": n}


# --- Network monitor (plan §U7) ---------------------------------------------


def _network_state_to_dict(s: Any) -> dict[str, Any]:
    return {
        "offline": s.offline,
        "offline_since": s.offline_since,
        "probe_url": s.probe_url,
        "probe_interval_s": s.probe_interval_s,
        "last_probe_at": s.last_probe_at,
        "last_probe_ok": s.last_probe_ok,
        "last_probe_error": s.last_probe_error,
        "failure_count_in_window": s.failure_count_in_window,
    }


class NetworkConfig(BaseModel):
    probe_url: str = Field(min_length=1)


@app.get("/api/system/network")
async def get_network_state() -> dict[str, Any]:
    return _network_state_to_dict(jobs_mod.orchestrator().network.state())


@app.patch("/api/system/network")
async def update_network_config(body: NetworkConfig) -> dict[str, Any]:
    monitor = jobs_mod.orchestrator().network
    monitor.set_probe_url(body.probe_url)
    return _network_state_to_dict(monitor.state())


@app.post("/api/system/network/probe")
async def probe_now() -> dict[str, Any]:
    monitor = jobs_mod.orchestrator().network
    ok = await monitor.force_probe_now()
    return {"ok": ok, **_network_state_to_dict(monitor.state())}


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


@app.get("/api/cases/{case_id}/profile")
async def get_case_profile(case_id: int) -> dict[str, Any]:
    conn = _conn()
    try:
        case = cases.get(conn, case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        resolution = profiles_mod.effective_for_case(case.settings)
        return {
            "case_settings": case.settings,
            "effective": resolution.settings.to_dict(),
            "base_name": resolution.base_name,
        }
    finally:
        conn.close()


@app.put("/api/cases/{case_id}/profile")
async def set_case_profile(
    case_id: int, body: ProfileChoice,
) -> dict[str, Any]:
    if body.profile not in profiles_mod.PROFILE_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown profile {body.profile!r}",
        )
    conn = _conn()
    try:
        case = cases.get(conn, case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        new_settings = dict(case.settings or {})
        new_settings["profile"] = body.profile
        if body.profile_overrides is not None:
            new_settings["profile_overrides"] = body.profile_overrides
        cases.update_settings(conn, case_id, new_settings)
        audit.append(
            conn, "case.profile_changed",
            case_id=case_id, actor="user",
            details={"profile": body.profile, "overrides": body.profile_overrides or {}},
        )
        resolution = profiles_mod.effective_for_case(new_settings)
        return {
            "case_settings": new_settings,
            "effective": resolution.settings.to_dict(),
            "base_name": resolution.base_name,
        }
    finally:
        conn.close()


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


class RefetchRequest(BaseModel):
    kind: str = Field(default="archive")  # 'archive' | 'media'


@app.post("/api/library/{download_id}/refetch")
async def refetch_artifact(
    download_id: int, body: RefetchRequest,
) -> dict[str, Any]:
    """Spawn a follow-up task to extend an existing library item with a
    re-fetched archive or media artifact (plan §U6 Phase D).

    Looks up (or creates) the parent ``capture_group`` and submits a job
    with the matching ``task_kind``. Returns the new job descriptor.
    """
    if body.kind not in ("archive", "media"):
        raise HTTPException(
            status_code=400, detail="kind must be 'archive' or 'media'",
        )
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, case_id, source_url, final_url FROM downloads WHERE id = ?",
            (download_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="download not found")
        grp_row = conn.execute(
            "SELECT id FROM capture_groups WHERE download_id = ?", (download_id,),
        ).fetchone()
        if grp_row is None:
            group_id = jobs_mod.ensure_capture_group(
                conn,
                case_id=int(row["case_id"]),
                url=row["final_url"] or row["source_url"],
                download_id=download_id,
            )
        else:
            group_id = grp_row["id"]
        case_id_local = int(row["case_id"])
        target_url = row["final_url"] or row["source_url"]
    finally:
        conn.close()

    task_kind = jobs_mod.TASK_ARCHIVE if body.kind == "archive" else jobs_mod.TASK_MEDIA
    job = await jobs_mod.orchestrator().submit(
        case_id=case_id_local,
        url=target_url,
        task_kind=task_kind,
        capture_group_id=group_id,
    )
    return job.to_dict()


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

    quick_dir = cases.downloads_dir_for(cases.QUICK_CASE_SLUG)
    return {
        "app": __version__,
        "yt_dlp": ytdlp_v,
        "chromium": "0",          # Phase 2 sets this
        "browsertrix": "0",       # Phase 2 sets this
        "signing_key_fingerprint": signing.fingerprint(kp.public),
        "paths": {
            "downloads_dir": str(config.DOWNLOADS_DIR),
            "quick_captures_dir": str(quick_dir),
            "host_downloads_dir": config.HOST_DOWNLOADS_DIR,
            "host_quick_captures_dir": _host(quick_dir),
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


@app.post("/api/system/update")
async def system_update() -> dict[str, Any]:
    """User-triggered yt-dlp upgrade. Never invoked automatically."""
    pip = shutil.which("pip") or sys.executable
    # Match exactly ``pip`` / ``pip3`` (or ``pip.exe`` / ``pip3.exe``) — anything
    # else (e.g. ``pipx``) falls through to the explicit ``python -m pip`` form.
    pip_name = Path(pip).stem if pip else ""
    cmd = [pip, "install", "--upgrade", "yt-dlp"] if pip_name in {"pip", "pip3"} else [
        sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    new_version = "unknown"
    try:
        new_version = await ytdlp_runner.version()
    except Exception:
        pass
    conn = _conn()
    try:
        audit.append(
            conn,
            "system.updated",
            actor="user",
            details={
                "component": "yt-dlp",
                "returncode": proc.returncode,
                "new_version": new_version,
            },
        )
    finally:
        conn.close()
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "new_version": new_version,
        "stdout_tail": out.decode(errors="replace")[-2000:],
        "stderr_tail": err.decode(errors="replace")[-2000:],
    }


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
        job = await jobs_mod.orchestrator().submit(case_id=case.id, url=u)
        submitted.append(job.to_dict())

    # Step 4: pair live captures with their submitted job by URL match.
    # The extension may send live captures for a subset of URLs; we route
    # each payload to the first matching job (insertion order). For
    # ephemeral cookies, each job gets its own freshly-written tmpdir
    # cookie file.
    live_by_url: dict[str, list[LiveCapturePayload]] = {}
    for payload in body.live_captures:
        live_by_url.setdefault(payload.url.strip(), []).append(payload)
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
        payloads = live_by_url.get(raw_url) or []
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
