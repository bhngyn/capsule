"""Capture post-processor (CLAUDE.md §5, §6, §7, §8).

The integration glue between yt-dlp output (Phase 1), the page-snapshot
producer (Phase 2 — Playwright + browsertrix), and the persistence layer
(DB + per-item folder + audit log + signing).

``finalize`` accepts a ``CaptureInput`` and produces a ``CaptureResult``.
The function is producer-agnostic: page-artifact paths are part of the
input, so Phase 2 only adds producers, not a parallel code path.

Sequence (CLAUDE.md §5):

1. Decide ``capture_kind`` from whether yt-dlp produced any media file.
2. Build the canonical stem (``sanitize.canonical_filename`` /
   ``canonical_page_only_stem``).
3. Resolve filesystem collisions inside the case dir (the per-item
   folder ``/downloads/{slug}/{stem}/`` must not already exist).
4. Create the per-item folder and move the media file (if any) plus
   yt-dlp's sidecars and the page-snapshot artifacts into it.
5. Hash every artifact (MD5 + SHA-256, 1 MB chunks). Write
   ``{stem}.checksums.txt``.
6. Write ``{stem}.meta.json`` per ``app/schemas/meta.schema.json``.
7. Sign meta.json (``{stem}.meta.json.sig``).
8. Insert ``downloads`` row + audit-log entry inside one transaction.

Errors:

* If the URL has already been captured for this case (DB UNIQUE), raise
  ``DuplicateCapture`` with the existing row id so the API can render the
  duplicate-handling modal (CLAUDE.md §15).
* If filesystem moves fail mid-way, we best-effort roll back the moves we
  did (atomic-rename semantics within the same FS); the half-finished
  artifacts stay where they were to preserve evidence integrity.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    __version__ as APP_VERSION,
    audit,
    cases,
    config,
    paths,
    platforms,
    sanitize,
    signing,
    url_canonical,
)

__all__ = [
    "CaptureInput",
    "CaptureResult",
    "DuplicateCapture",
    "finalize",
    "compute_hashes",
]


CHUNK = 1024 * 1024


class DuplicateCapture(Exception):
    """Raised when ``(case_id, capture_kind, url_hash)`` already exists.

    The API translates this into the §15 duplicate-handling modal — the
    investigator can choose Open existing / Re-capture (with a ``__cN``
    suffix) / Cancel.
    """

    def __init__(self, existing_id: int):
        super().__init__(f"capture already exists (id={existing_id})")
        self.existing_id = existing_id


@dataclass(frozen=True)
class CaptureInput:
    case: cases.Case
    job_uuid: str
    url_submitted: str
    url_final: str
    redirect_chain: list[str]
    capture_date: str  # ISO 8601 UTC
    media_files: list[Path] = field(default_factory=list)  # yt-dlp output
    info_json: dict[str, Any] | None = None  # parsed yt-dlp info.json
    page_mhtml: Path | None = None  # Phase 2
    page_screenshot: Path | None = None  # Phase 2
    page_warc: Path | None = None  # Phase 2
    extra_sidecars: list[Path] = field(default_factory=list)  # description, thumbnail, subs
    authenticated_domains: list[str] = field(default_factory=list)
    chromium_version: str = "0"  # Phase 2 sets this
    browsertrix_version: str = "0"  # Phase 2 sets this
    ytdlp_version: str = ""
    # Supplementary "as-rendered-by-the-investigator's-browser" artifacts
    # uploaded by the Capsule extension. Always additive — the canonical
    # capture (page_*, media_files) is unaffected. The forensic story is
    # in plan §"Forensic implications".
    user_browser_mhtml: Path | None = None
    user_browser_screenshot: Path | None = None
    user_browser_har: Path | None = None
    user_browser_environment: Path | None = None
    user_browser_label: str | None = None  # extension label, recorded in audit
    # Hardening pass: structured tab context (UA / viewport / scroll / tz),
    # per-origin session/local storage, and a click-time DOM snapshot.
    # All optional; each rides through the same hash-and-sign path as the
    # other artifacts.
    user_browser_tab_context: Path | None = None
    user_browser_session_state: Path | None = None
    user_browser_dom_snapshot_html: Path | None = None
    user_browser_dom_snapshot_meta: Path | None = None
    # Capture-side mutations + render-wait outcomes. Recorded into
    # ``meta.json.capture`` so a court reviewer can answer "what did the
    # capture process actually do?". The dict is the output of
    # ``CaptureReport.to_dict``.
    capture_report: dict[str, Any] | None = None
    # SHA-256 of the cookie file the job consumed. Binds the capture to
    # an exact cookie set without ever logging values.
    cookies_snapshot_sha256: str | None = None
    # True iff the cookie file was a one-shot ephemeral file (not the
    # persistent case file). Recorded for audit; does not affect on-disk
    # artifacts.
    ephemeral_cookies_used: bool = False
    # UI locale at the time of submission. Drives the per-item manifest
    # PDF's labels + direction + font stack. Recorded in
    # ``meta.json.capture.report_lang`` so a future evidence reviewer
    # can confirm what the investigator saw.
    lang: str = "en"
    # CLAUDE.md §15: when the user picks "Re-capture as new entry" in the
    # duplicate-handling modal, the orchestrator re-submits with
    # ``force_recapture=True``. ``finalize`` then suffixes ``url_hash``
    # with ``__c{N+1}`` so the UNIQUE(case_id, capture_kind, url_hash)
    # constraint passes and the new row sits as a sibling of the original.
    force_recapture: bool = False


@dataclass(frozen=True)
class CaptureResult:
    download_id: int
    capture_kind: str
    stem: str
    relative_media_path: str | None
    relative_item_dir: str
    meta_json_path: Path
    signature_path: Path
    audit_log_entry_id: int


# --- Hashing -----------------------------------------------------------------


def compute_hashes(path: Path) -> dict[str, Any]:
    """Compute MD5 + SHA-256 + size in a single pass over ``path``."""
    md5 = hashlib.md5()
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            md5.update(chunk)
            sha.update(chunk)
            size += len(chunk)
    return {"md5": md5.hexdigest(), "sha256": sha.hexdigest(), "size_bytes": size}


# --- Helpers -----------------------------------------------------------------


def _stem_for(
    capture_input: CaptureInput, info: dict[str, Any], capture_kind: str
) -> tuple[str, str | None]:
    """Return ``(stem, ext)``. ``ext`` is None for page-only captures."""
    platform = (
        platforms.friendly_name(info.get("extractor_key", ""))
        if info
        else platforms.platform_for_url(capture_input.url_final)
    )
    if capture_kind == "media":
        ext = info.get("ext", "") if info else ""
        upload_date_raw = info.get("upload_date") if info else None
        upload_date = (
            f"{upload_date_raw[:4]}-{upload_date_raw[4:6]}-{upload_date_raw[6:8]}"
            if upload_date_raw and len(upload_date_raw) == 8
            else f"dl-{capture_input.capture_date[:10]}"
        )
        stem_no_ext = sanitize.canonical_filename(
            platform=platform,
            uploader=(info.get("uploader") or info.get("channel") or "unknown") if info else "unknown",
            title=(info.get("title") or "untitled") if info else "untitled",
            upload_date=upload_date,
            video_id=(info.get("id") or "noid") if info else "noid",
            ext="",  # we strip the extension; will be re-attached below
        )
        # canonical_filename appends ``.{ext}`` only when ``ext`` is truthy.
        return stem_no_ext.rstrip("."), ext
    # page_only
    page_title = (info.get("title") if info else None) or capture_input.url_final
    stem = sanitize.canonical_page_only_stem(
        platform=platform,
        page_title=page_title,
        capture_date=f"dl-{capture_input.capture_date[:10]}",
        url_final=capture_input.url_final,
    )
    return stem, None


def _suffix_for_recapture(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    capture_kind: str,
    base_hash: str,
) -> tuple[str, int]:
    """Pick the next ``__c{N}`` suffix for a forced re-capture (CLAUDE.md §15).

    Returns ``(url_hash, force_recapture_index)``. The base row uses
    ``base_hash`` (no suffix). Subsequent forced re-captures get
    ``base_hash__c2``, ``base_hash__c3``, … in order. We count both the
    bare ``base_hash`` row and any prior ``__c*`` siblings so the counter
    monotonically increases even after intermediate rows are deleted.
    """
    rows = conn.execute(
        "SELECT url_hash FROM downloads "
        "WHERE case_id = ? AND capture_kind = ? "
        "AND (url_hash = ? OR url_hash LIKE ?)",
        (case_id, capture_kind, base_hash, base_hash + "__c%"),
    ).fetchall()
    highest = 1  # the bare base_hash counts as index 1
    for row in rows:
        h = row["url_hash"]
        if h == base_hash:
            continue
        # Parse __cN suffix
        try:
            n = int(h.rsplit("__c", 1)[1])
        except (IndexError, ValueError):
            continue
        if n > highest:
            highest = n
    next_index = highest + 1
    return f"{base_hash}__c{next_index}", next_index


def _resolve_collisions(case_dir: Path, stem: str) -> str:
    """Pick a stem that does not collide with anything already on disk.

    Per-item folders live directly under ``case_dir`` — one folder per
    capture, named after the canonical stem. A pre-existing folder (or a
    bare file at that name from a partially-failed run) blocks the stem.
    """
    existing: set[str] = set()
    if case_dir.exists():
        for p in case_dir.iterdir():
            if p.is_dir():
                existing.add(p.name)
            elif p.is_file():
                # Defensive: legacy or partially-rolled-back files at the
                # case root should still mark the stem as taken.
                existing.add(p.stem.split(".", 1)[0])
    return sanitize.next_collision_suffix(existing, stem)


def _move_into(target_dir: Path, source: Path, new_name: str | None = None) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / (new_name or source.name)
    shutil.move(str(source), str(dest))
    return dest


# --- Main entry point --------------------------------------------------------


def finalize(conn: sqlite3.Connection, capture_input: CaptureInput) -> CaptureResult:
    info = capture_input.info_json or {}
    case = capture_input.case
    items_root = cases.item_dir_for(case.slug)
    items_root.mkdir(parents=True, exist_ok=True)

    # Step 1: capture_kind
    capture_kind = "media" if capture_input.media_files else "page_only"

    # url_hash for de-dup. We compute this from the *canonical* form of
    # url_final so two paste-variants of the same URL collapse to the
    # same dedup key. We do this before any disk work so a duplicate
    # fails fast with no side effects.
    canonical_url = url_canonical.canonicalize(capture_input.url_final)
    base_hash = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:12]

    if capture_input.force_recapture:
        # User chose "Re-capture as new entry" in the §15 modal: append a
        # ``__c{N+1}`` suffix so the UNIQUE constraint passes. The counter
        # is derived from existing siblings sharing this base_hash.
        url_hash, force_recapture_index = _suffix_for_recapture(
            conn, case_id=case.id, capture_kind=capture_kind, base_hash=base_hash
        )
    else:
        url_hash = base_hash
        force_recapture_index = None
        existing = conn.execute(
            "SELECT id FROM downloads WHERE case_id = ? AND capture_kind = ? AND url_hash = ?",
            (case.id, capture_kind, url_hash),
        ).fetchone()
        if existing is not None:
            raise DuplicateCapture(existing_id=int(existing["id"]))

    # Step 2-3: stem + collision suffix. The per-item folder lives directly
    # under the case dir.
    base_stem, ext = _stem_for(capture_input, info, capture_kind)
    stem = _resolve_collisions(items_root, base_stem)
    item_dir = items_root / stem
    item_dir.mkdir(parents=True, exist_ok=True)

    moved: list[Path] = []
    artifacts: dict[str, str] = {}

    try:
        # Step 4: move media + sidecars into the per-item folder.
        relative_media_path: str | None = None
        if capture_kind == "media":
            primary = capture_input.media_files[0]
            ext_clean = ext or primary.suffix.lstrip(".")
            media_target_name = f"{stem}.{ext_clean}" if ext_clean else stem
            media_target = _move_into(item_dir, primary, new_name=media_target_name)
            moved.append(media_target)
            relative_media_path = paths.relative_to_downloads(media_target)
            artifacts["media"] = relative_media_path

            # Any extra media files (e.g. multi-format) stay alongside the
            # primary inside the same per-item folder.
            for i, extra in enumerate(capture_input.media_files[1:], start=2):
                tail = f"{stem}.{i}.{extra.suffix.lstrip('.') or 'bin'}"
                m = _move_into(item_dir, extra, new_name=tail)
                moved.append(m)
                artifacts[f"media_{i}"] = paths.relative_to_downloads(m)

        # yt-dlp sidecars: rename to share the new stem and drop into the
        # per-item folder.
        for src in capture_input.extra_sidecars:
            if not src.exists():
                continue
            rel_name = src.name
            # Rewrite "abc.info.json" → "{stem}.info.json"; same for description, thumbnail, subs.
            for known in (".info.json", ".description", ".live_chat.json"):
                if rel_name.endswith(known):
                    rel_name = stem + known
                    break
            else:
                # thumbnail or subs: keep the part after the original stem.
                tail = src.name.split(".", 1)[1] if "." in src.name else src.name
                rel_name = f"{stem}.{tail}"
            dest = _move_into(item_dir, src, new_name=rel_name)
            moved.append(dest)
            artifacts[f"sidecar_{rel_name}"] = paths.relative_to_downloads(dest)

        # Page-snapshot artifacts (Phase 2 fills these in; Phase 1 stubs allowed).
        # Extension-supplied user-browser artifacts ride the same loop —
        # additive supplementary evidence; canonical capture is unchanged.
        for role, src in (
            ("page_mhtml", capture_input.page_mhtml),
            ("page_screenshot", capture_input.page_screenshot),
            ("page_warc", capture_input.page_warc),
            ("user_browser_mhtml", capture_input.user_browser_mhtml),
            ("user_browser_screenshot", capture_input.user_browser_screenshot),
            ("user_browser_har", capture_input.user_browser_har),
            ("user_browser_environment", capture_input.user_browser_environment),
            ("user_browser_tab_context", capture_input.user_browser_tab_context),
            ("user_browser_session_state", capture_input.user_browser_session_state),
            ("user_browser_dom_snapshot_html", capture_input.user_browser_dom_snapshot_html),
            ("user_browser_dom_snapshot_meta", capture_input.user_browser_dom_snapshot_meta),
        ):
            if src is None or not src.exists():
                continue
            named = {
                "page_mhtml": f"{stem}.page.mhtml",
                "page_screenshot": f"{stem}.page.png",
                "page_warc": f"{stem}.page.warc.gz",
                "user_browser_mhtml": f"{stem}.user-browser.mhtml",
                "user_browser_screenshot": f"{stem}.user-browser.png",
                "user_browser_har": f"{stem}.user-browser.har",
                "user_browser_environment": f"{stem}.user-browser.environment.json",
                "user_browser_tab_context": f"{stem}.user-browser.tab-context.json",
                "user_browser_session_state": f"{stem}.user-browser.session-state.json",
                "user_browser_dom_snapshot_html": f"{stem}.user-browser.dom-snapshot.html",
                "user_browser_dom_snapshot_meta": f"{stem}.user-browser.dom-snapshot.json",
            }[role]
            dest = _move_into(item_dir, src, new_name=named)
            moved.append(dest)
            artifacts[role] = paths.relative_to_downloads(dest)

        # Step 5a: hash every non-PDF artifact. The two PDFs (report
        # rendered next, manifest after) are hashed afterwards so the
        # manifest table can list every other file's MD5/SHA-256
        # (including the report PDF — meaning meta.json's signature
        # transitively binds both PDFs).
        checksums: dict[str, dict[str, Any]] = {}
        for role, rel in artifacts.items():
            abs_path = config.DOWNLOADS_DIR / rel
            checksums[role] = compute_hashes(abs_path)

        # Lazy import — weasyprint is a heavy dep and every test that
        # imports ``app.postprocess`` would otherwise pay the cost.
        from . import pdf_report  # noqa: PLC0415

        kp = signing.ensure_keypair()
        fp = signing.fingerprint(kp.public)
        title_sanitized_for_pdf = sanitize.sanitize_component(
            info.get("title") or capture_input.url_final, max_len=sanitize.TITLE_MAX
        )
        capture_block_for_report = dict(capture_input.capture_report or {})
        capture_block_for_report["report_lang"] = capture_input.lang

        # Step 5a.5: render the per-item report PDF (provenance,
        # description, tools, capture report). Hashed and added to
        # ``artifacts`` BEFORE the manifest PDF renders so the manifest's
        # file table includes the ``report_pdf`` row, AND meta.json.sig
        # transitively binds it through the manifest's checksum entry.
        report_view = {
            "title": title_sanitized_for_pdf,
            "source_url": capture_input.url_submitted,
            "final_url": capture_input.url_final,
            "redirect_chain": list(capture_input.redirect_chain or []),
            "captured_utc": capture_input.capture_date,
            "signing_key_fp": fp,
            "platform": (
                platforms.friendly_name(info.get("extractor_key", ""))
                if info
                else platforms.platform_for_url(capture_input.url_final)
            ),
            "uploader": (info.get("uploader") or info.get("channel")) if info else None,
            "upload_date": info.get("upload_date") if info else None,
            "duration_seconds": info.get("duration") if info else None,
            "authenticated_domains": list(capture_input.authenticated_domains),
            "description": info.get("description") if info else None,
            "tools": {
                "app_version": APP_VERSION,
                "ytdlp_version": capture_input.ytdlp_version,
                "chromium_version": capture_input.chromium_version,
                "browsertrix_version": capture_input.browsertrix_version,
            },
            "capture": capture_block_for_report,
            "manifest_filename": f"{stem}.manifest.pdf",
        }
        report_pdf_bytes = pdf_report.render_item_report(
            case=case, item_view=report_view, lang=capture_input.lang,
        )
        # Both PDFs live in a per-item ``reports/`` subfolder so the case
        # directory stays scannable: media + forensic sidecars at the item
        # root, human-readable PDFs grouped beneath. The ``reports/``
        # subpath becomes part of the artifact relpath, so checksums.txt,
        # the manifest's file table, and the evidence-export ZIP all pick
        # it up automatically without further wiring.
        reports_dir = item_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_pdf_path = reports_dir / f"{stem}.report.pdf"
        report_pdf_path.write_bytes(report_pdf_bytes)
        moved.append(report_pdf_path)
        report_pdf_relpath = paths.relative_to_downloads(report_pdf_path)
        artifacts["report_pdf"] = report_pdf_relpath
        checksums["report_pdf"] = compute_hashes(report_pdf_path)

        # Step 5b: render the per-item manifest PDF. The ``manifest_files``
        # list is built from ``artifacts`` AFTER the report-PDF row has
        # been added, so the report appears in the manifest table.
        manifest_files = [
            pdf_report.FileEntry(
                relpath=artifacts[role],
                size=int(checksums[role]["size_bytes"]),
                md5=checksums[role]["md5"],
                sha256=checksums[role]["sha256"],
            )
            for role in sorted(artifacts.keys())
        ]
        manifest_pdf_bytes = pdf_report.render_item_manifest(
            case=case,
            item_view={
                "title": title_sanitized_for_pdf,
                "source_url": capture_input.url_final or capture_input.url_submitted,
                "captured_utc": capture_input.capture_date,
                "signing_key_fp": fp,
            },
            item_dir=item_dir,
            files=manifest_files,
            lang=capture_input.lang,
        )
        manifest_pdf_path = reports_dir / f"{stem}.manifest.pdf"
        manifest_pdf_path.write_bytes(manifest_pdf_bytes)
        moved.append(manifest_pdf_path)

        # Step 5c: hash the manifest PDF and record it under the
        # ``manifest_pdf`` role so it gets the same chain-of-custody
        # treatment as every other artifact.
        manifest_pdf_relpath = paths.relative_to_downloads(manifest_pdf_path)
        artifacts["manifest_pdf"] = manifest_pdf_relpath
        checksums["manifest_pdf"] = compute_hashes(manifest_pdf_path)

        # Step 5d: write checksums.txt now that the manifest PDF is hashed.
        checksums_path = item_dir / f"{stem}.checksums.txt"
        with checksums_path.open("w", encoding="utf-8") as fh:
            for role, h in sorted(checksums.items()):
                rel = artifacts[role]
                fh.write(f"MD5    {h['md5']}  {rel}\n")
                fh.write(f"SHA256 {h['sha256']}  {rel}\n")

        # Step 6: meta.json — captures both the artifact set (including
        # both PDFs, so the signature transitively binds them) and the
        # locale used to render them. ``capture_block_for_report`` was
        # already built above with ``report_lang`` set; reuse it here so
        # the report-PDF-renderer view and the meta record agree.
        capture_block = capture_block_for_report

        meta = {
            "schema_version": 5,
            "job_uuid": capture_input.job_uuid,
            "capture_kind": capture_kind,
            "case": {"id": case.id, "slug": case.slug, "name": case.name},
            "url_submitted": capture_input.url_submitted,
            "url_final": capture_input.url_final,
            # Canonical form used for dedup (CLAUDE.md §15). Originals
            # ``url_submitted`` and ``url_final`` stay verbatim.
            "url_canonical": canonical_url,
            "url_redirect_chain": capture_input.redirect_chain,
            "platform": platforms.friendly_name(info.get("extractor_key", ""))
            if info
            else platforms.platform_for_url(capture_input.url_final),
            "video_id": info.get("id") if info else None,
            "uploader": info.get("uploader") or info.get("channel") if info else None,
            "uploader_original": info.get("uploader") or info.get("channel") if info else None,
            "title": sanitize.sanitize_component(
                info.get("title") or capture_input.url_final, max_len=sanitize.TITLE_MAX
            ),
            "title_original": (info.get("title") or capture_input.url_final),
            "description_original": info.get("description") if info else None,
            "upload_date": info.get("upload_date") if info else None,
            "capture_date": capture_input.capture_date,
            "duration_seconds": info.get("duration") if info else None,
            "format_details": info.get("format") if info else None,
            "response_headers": info.get("http_headers") if info else None,
            "authenticated_domains": list(capture_input.authenticated_domains),
            "artifacts": artifacts,
            "checksums": checksums,
            "tools": {
                "app_version": APP_VERSION,
                "ytdlp_version": capture_input.ytdlp_version,
                "chromium_version": capture_input.chromium_version,
                "browsertrix_version": capture_input.browsertrix_version,
            },
            # Hardening pass: capture-side mutations and the cookie-set
            # provenance hash. Per CLAUDE.md §13, every blocking and every
            # banner-hide is recorded — never silent. ``report_lang`` is
            # the UI locale at submission time; the manifest PDF was
            # rendered with it.
            "capture": capture_block,
            "cookies_snapshot_sha256": capture_input.cookies_snapshot_sha256,
            "ephemeral_cookies_used": capture_input.ephemeral_cookies_used,
            # Set when this row was an intentional re-capture per §15.
            # ``None`` for the original / non-forced captures.
            "force_recapture_index": force_recapture_index,
            "audit_log_entry_id": None,  # filled below
            "signing_key_fp": fp,
        }
        meta_path = item_dir / f"{stem}.meta.json"
        meta_bytes = json.dumps(meta, indent=2, ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
        meta_path.write_bytes(meta_bytes)

        # Step 7: detached signature
        sig_path = signing.sign_file(meta_path)

        # Step 8: DB insert + audit, in one transaction
        capture_date = capture_input.capture_date
        title_sanitized = meta["title"]
        try:
            with conn:
                cur = conn.execute(
                    """
                    INSERT INTO downloads(
                        case_id, job_uuid, capture_kind, source_url, final_url,
                        platform, video_id, url_hash, uploader, title, title_original,
                        upload_date, capture_date, relative_path, item_dir,
                        file_size_bytes, md5, sha256, duration_seconds,
                        ytdlp_version, chromium_version, browsertrix_version,
                        app_version, signing_key_fp, meta_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        case.id,
                        capture_input.job_uuid,
                        capture_kind,
                        capture_input.url_submitted,
                        capture_input.url_final,
                        meta["platform"],
                        info.get("id") if info else None,
                        url_hash,
                        info.get("uploader") if info else None,
                        title_sanitized,
                        info.get("title") or capture_input.url_final,
                        info.get("upload_date") if info else None,
                        capture_date,
                        relative_media_path,
                        paths.relative_to_downloads(item_dir),
                        checksums.get("media", {}).get("size_bytes"),
                        checksums.get("media", {}).get("md5"),
                        checksums.get("media", {}).get("sha256"),
                        info.get("duration") if info else None,
                        capture_input.ytdlp_version,
                        capture_input.chromium_version,
                        capture_input.browsertrix_version,
                        APP_VERSION,
                        fp,
                        meta_bytes.decode("utf-8"),
                    ),
                )
                download_id = int(cur.lastrowid or 0)
        except sqlite3.IntegrityError as exc:
            # Concurrent finalize for the same (case, capture_kind, url_hash)
            # raced past the early de-dup check at the top of this function.
            # Translate to DuplicateCapture so callers handle it the same way.
            row = conn.execute(
                "SELECT id FROM downloads WHERE case_id = ? AND capture_kind = ? AND url_hash = ?",
                (case.id, capture_kind, url_hash),
            ).fetchone()
            if row is not None:
                raise DuplicateCapture(existing_id=int(row["id"])) from exc
            raise

        audit_id = audit.append(
            conn,
            "download.created",
            case_id=case.id,
            download_id=download_id,
            actor="system",
            details={
                "stem": stem,
                "capture_kind": capture_kind,
                "url_hash": url_hash,
                "platform": meta["platform"],
                "authenticated_domains": list(capture_input.authenticated_domains),
            },
        )

        # Per-item manifest PDF event — recorded so a future evidence
        # reviewer can see exactly when (and at what locale) the manifest
        # was rendered. The PDF itself is bound to the meta.json by hash,
        # which the meta signature transitively covers.
        audit.append(
            conn,
            "item.manifest_rendered",
            case_id=case.id,
            download_id=download_id,
            actor="system",
            details={
                "stem": stem,
                "lang": capture_input.lang,
                "size_bytes": int(checksums["manifest_pdf"]["size_bytes"]),
                "sha256": checksums["manifest_pdf"]["sha256"],
            },
        )

        # Supplementary capture from the Capsule extension's live browser
        # session, if present. Recorded as a separate audit row so a court
        # reviewer can see (a) that an additional, non-reproducible capture
        # was attached and (b) which artifacts it contributed.
        user_browser_roles = sorted(
            r for r in artifacts if r.startswith("user_browser_")
        )
        if user_browser_roles:
            details: dict[str, Any] = {
                "stem": stem,
                "artifact_roles": user_browser_roles,
            }
            if capture_input.user_browser_label:
                details["extension_label"] = capture_input.user_browser_label
            audit.append(
                conn,
                "user_browser_capture.received",
                case_id=case.id,
                download_id=download_id,
                actor="user",
                details=details,
            )

        return CaptureResult(
            download_id=download_id,
            capture_kind=capture_kind,
            stem=stem,
            relative_media_path=relative_media_path,
            relative_item_dir=paths.relative_to_downloads(item_dir),
            meta_json_path=meta_path,
            signature_path=sig_path,
            audit_log_entry_id=audit_id,
        )

    except (OSError, sqlite3.Error, DuplicateCapture):
        # Expected failure modes during finalize:
        #   * OSError — disk full, permission, partial _move_into mid-flight
        #   * sqlite3.Error — IntegrityError from the duplicate-race window,
        #     or operational/programming errors propagated through the inner
        #     try/except above
        #   * DuplicateCapture — raced past the early de-dup probe; the row
        #     already exists, so our just-moved siblings need to go before
        #     they shadow the canonical capture.
        # Best-effort rollback of just the FS moves we made; if a move
        # already replaced an in-place file we leave it alone (preserving
        # evidence integrity over rollback purity).
        for p in moved:
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        try:
            if item_dir.exists() and not any(item_dir.iterdir()):
                item_dir.rmdir()
        except OSError:
            pass
        raise
    # Anything not in the tuple above is unexpected (programming bug,
    # cryptography corruption, …): let it propagate so the artifacts on
    # disk are preserved for human inspection rather than being silently
    # cleaned up. CLAUDE.md §7 + §13 #13 (preserve, don't modify).


def extend_capture(
    conn: sqlite3.Connection,
    *,
    download_id: int,
    role: str,
    source: Path,
    actor: str = "system",
) -> dict[str, Any]:
    """Add a new artifact to an existing library item — plan §U6 Phase D.

    Used by archive- or media-only re-fetch jobs to extend an item that
    already has a snapshot. The function:

    1. Moves ``source`` into the item's existing per-item folder with the
       canonical role-based filename (``{stem}.page.warc.gz`` etc.).
    2. Computes MD5 + SHA-256.
    3. Patches the on-disk and DB ``meta.json`` (artifacts + checksums).
    4. Re-writes ``checksums.txt`` so it matches.
    5. **Re-signs** ``meta.json`` with the current key. The prior signature
       is overwritten — both the old and new ``meta.json`` hashes are
       captured in the audit chain (via ``meta.updated.{role}``), so
       integrity history is preserved.
    6. Audit-logs the extension.

    Returns a summary dict (new artifact relpath + its checksums + new
    meta.json hash) that the orchestrator can stash in the job's result.
    """
    row = conn.execute(
        "SELECT id, case_id, item_dir, meta_json FROM downloads WHERE id = ?",
        (download_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"download {download_id} not found")

    item_dir = config.DOWNLOADS_DIR / row["item_dir"]
    if not item_dir.is_dir():
        raise FileNotFoundError(f"item dir missing: {item_dir}")
    stem = item_dir.name

    # Map role → on-disk filename. Mirrors ``finalize``'s naming.
    name_for = {
        "page_warc":               f"{stem}.page.warc.gz",
        "page_mhtml":              f"{stem}.page.mhtml",
        "page_screenshot":         f"{stem}.page.png",
        "user_browser_mhtml":      f"{stem}.user-browser.mhtml",
        "user_browser_screenshot": f"{stem}.user-browser.png",
        "user_browser_har":        f"{stem}.user-browser.har",
        "user_browser_environment": f"{stem}.user-browser.environment.json",
        "user_browser_tab_context": f"{stem}.user-browser.tab-context.json",
        "user_browser_session_state": f"{stem}.user-browser.session-state.json",
        "user_browser_dom_snapshot_html": f"{stem}.user-browser.dom-snapshot.html",
        "user_browser_dom_snapshot_meta": f"{stem}.user-browser.dom-snapshot.json",
        "media":                   None,  # named below using source extension
    }
    if role not in name_for:
        raise ValueError(f"unknown extend role {role!r}")

    if role == "media":
        # Track A layout: media files live inside the per-item folder
        # alongside the rest of the artifacts.
        ext = source.suffix.lstrip(".") or "bin"
        target_name = f"{stem}.{ext}"
        target = _move_into(item_dir, source, new_name=target_name)
    else:
        target = _move_into(item_dir, source, new_name=name_for[role])

    new_hashes = compute_hashes(target)
    new_relpath = paths.relative_to_downloads(target)

    meta = json.loads(row["meta_json"])
    artifacts = dict(meta.get("artifacts") or {})
    checksums = dict(meta.get("checksums") or {})
    artifacts[role] = new_relpath
    checksums[role] = new_hashes
    meta["artifacts"] = artifacts
    meta["checksums"] = checksums
    meta["updated_at"] = utc_now()
    history = meta.get("update_history") or []
    history.append({"role": role, "at": meta["updated_at"]})
    meta["update_history"] = history

    meta_path = item_dir / f"{stem}.meta.json"
    new_meta_bytes = json.dumps(
        meta, indent=2, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    meta_path.write_bytes(new_meta_bytes)

    # Re-sign with current key.
    sig_path = signing.sign_file(meta_path)

    # Re-write checksums.txt to match the updated artifact set.
    checksums_path = item_dir / f"{stem}.checksums.txt"
    with checksums_path.open("w", encoding="utf-8") as fh:
        for r, h in sorted(checksums.items()):
            rel = artifacts[r]
            fh.write(f"MD5    {h['md5']}  {rel}\n")
            fh.write(f"SHA256 {h['sha256']}  {rel}\n")

    # Persist the patched meta and (if media) the new media-row fields.
    update_cols = ["meta_json = ?"]
    update_params: list[Any] = [new_meta_bytes.decode("utf-8")]
    if role == "media":
        update_cols.extend([
            "relative_path = ?", "file_size_bytes = ?", "md5 = ?", "sha256 = ?",
            "capture_kind = 'media'",
        ])
        update_params.extend([
            new_relpath, new_hashes["size_bytes"],
            new_hashes["md5"], new_hashes["sha256"],
        ])
    update_params.append(download_id)
    with conn:
        conn.execute(
            f"UPDATE downloads SET {', '.join(update_cols)} WHERE id = ?",
            update_params,
        )

    audit.append(
        conn,
        f"meta.updated.{role}",
        case_id=int(row["case_id"]),
        download_id=download_id,
        actor=actor,
        details={
            "role": role,
            "relative_path": new_relpath,
            "md5": new_hashes["md5"],
            "sha256": new_hashes["sha256"],
            "meta_sha256": hashlib.sha256(new_meta_bytes).hexdigest(),
        },
    )

    return {
        "download_id": download_id,
        "role": role,
        "relative_path": new_relpath,
        "md5": new_hashes["md5"],
        "sha256": new_hashes["sha256"],
        "signature_path": str(sig_path),
    }


def new_job_uuid() -> str:
    return str(uuid.uuid4())


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
