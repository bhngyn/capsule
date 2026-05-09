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


# Per-item folder layout (CLAUDE.md §6 v0.8):
# the two human-readable PDFs sit at the item root; everything else is
# routed into one of three subfolders so the per-item folder reads at a
# glance even before the recipient opens any file. Empty string means
# "item root" — used only for the report and manifest PDFs.
SUBDIR_CAPTURES = "Captures"   # page snapshots: mhtml, png, warc.gz, har, console, context.png, user-browser.*
SUBDIR_METADATA = "Metadata"   # meta.json + sig, checksums.txt, info.json, description, gallery_info, *_meta
SUBDIR_MEDIA = "Media"         # the media file(s), gallery images, thumbnail, subtitles
SUBDIR_ROOT = ""               # report.pdf, manifest.pdf


def _subdir_for_sidecar(filename: str) -> str:
    """Route a yt-dlp sidecar by its filename.

    Visual or playable assets ride with the media file (Media/); textual
    metadata sits in Metadata/. Defaults to Metadata/ for unknowns so a
    new sidecar role lands somewhere sensible without forcing a code
    change downstream of yt-dlp.
    """
    lower = filename.lower()
    if lower.endswith((".vtt", ".srt", ".ass", ".ttml", ".sbv")):
        return SUBDIR_MEDIA
    if ".thumbnail." in lower or lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return SUBDIR_MEDIA
    return SUBDIR_METADATA


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
    # v7 additions: in-session forensic sidecars (HAR + console events)
    # and the media-context viewport screenshot. Each rides through the
    # same hash-and-sign path as the canonical page artifacts.
    page_har: Path | None = None
    page_console: Path | None = None
    page_context_screenshot: Path | None = None
    extra_sidecars: list[Path] = field(default_factory=list)  # description, thumbnail, subs
    # Gallery pass v0.5: gallery-dl producer outputs. Used when yt-dlp
    # returned no media but gallery-dl pulled images from the URL — see
    # CLAUDE.md §15. ``gallery_files`` are the images themselves;
    # ``gallery_metadata_files`` are gallery-dl's per-image JSON
    # sidecars + the gallery-level info.json. ``gallery_extractor`` is
    # gallery-dl's ``category`` (e.g. ``"pixiv"``, ``"twitter"``); used
    # to pick the friendly platform slug.
    gallery_files: list[Path] = field(default_factory=list)
    gallery_metadata_files: list[Path] = field(default_factory=list)
    gallery_extractor: str | None = None
    gallery_dl_version: str | None = None
    authenticated_domains: list[str] = field(default_factory=list)
    chromium_version: str = "0"  # Phase 2 sets this
    browsertrix_version: str = "0"  # Phase 2 sets this
    warcio_version: str | None = None  # v7: set when in-session WARC writer ran
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
    # CLAUDE.md §15 v0.7: investigator-facing download options + reliability
    # counters as a plain dict (the orchestrator owns the dataclass).
    # Recorded into ``meta.json.download_options`` so a recipient can see
    # what was in effect for this capture (audio_only, quality_cap,
    # subtitle_langs, restart_count). The signature on meta.json binds
    # these values transitively.
    download_options: dict[str, Any] | None = None


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
    """Return ``(stem, ext)``. ``ext`` is None for page-only and gallery captures."""
    if capture_kind == "gallery":
        # gallery-dl publishes its extractor as ``category`` (lower-case);
        # fall back to the URL hint when category is missing.
        platform = (
            platforms.gallery_friendly_name(capture_input.gallery_extractor)
            if capture_input.gallery_extractor
            else platforms.platform_for_url(capture_input.url_final)
        )
    else:
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
    # page_only OR gallery — both reuse the page-only stem pattern. Gallery
    # captures don't have a single video_id but do have a page title (from
    # the gallery's info.json) and a URL.
    if capture_kind == "gallery" and capture_input.gallery_extractor:
        gi = info or {}
        page_title = (
            gi.get("title")
            or gi.get("subcategory")
            or capture_input.url_final
        )
    else:
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

    # Step 1: capture_kind. Three-way decision (CLAUDE.md §15 Gallery
    # pass v0.5): ``media`` wins (yt-dlp produced a video/audio file);
    # else ``gallery`` if gallery-dl produced any images; else
    # ``page_only`` (the page snapshot is still preserved). The
    # orchestrator only invokes gallery-dl in the no-media branch, so the
    # ``media`` and ``gallery`` cases here are mutually exclusive.
    if capture_input.media_files:
        capture_kind = "media"
    elif capture_input.gallery_files:
        capture_kind = "gallery"
    else:
        capture_kind = "page_only"

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
        # Step 4: move media + sidecars into the per-item folder. v0.8
        # routes everything into Captures/ (page snapshots), Metadata/
        # (meta + signed sidecars), or Media/ (the media file, gallery
        # images, thumbnail, subtitles); the two PDFs (rendered later in
        # this function) stay at the item root.
        media_dir = item_dir / SUBDIR_MEDIA
        metadata_dir = item_dir / SUBDIR_METADATA
        captures_dir = item_dir / SUBDIR_CAPTURES

        relative_media_path: str | None = None
        if capture_kind == "media":
            primary = capture_input.media_files[0]
            ext_clean = ext or primary.suffix.lstrip(".")
            media_target_name = f"{stem}.{ext_clean}" if ext_clean else stem
            media_target = _move_into(media_dir, primary, new_name=media_target_name)
            moved.append(media_target)
            relative_media_path = paths.relative_to_downloads(media_target)
            artifacts["media"] = relative_media_path

            # Any extra media files (e.g. multi-format) stay alongside the
            # primary inside Media/.
            for i, extra in enumerate(capture_input.media_files[1:], start=2):
                tail = f"{stem}.{i}.{extra.suffix.lstrip('.') or 'bin'}"
                m = _move_into(media_dir, extra, new_name=tail)
                moved.append(m)
                artifacts[f"media_{i}"] = paths.relative_to_downloads(m)

        # yt-dlp sidecars: rename to share the new stem, then split by
        # filename into Media/ (visual/playable: thumbnail, subtitles) or
        # Metadata/ (textual: info.json, description, live_chat).
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
            dest = _move_into(item_dir / _subdir_for_sidecar(rel_name), src, new_name=rel_name)
            moved.append(dest)
            artifacts[f"sidecar_{rel_name}"] = paths.relative_to_downloads(dest)

        # Gallery pass v0.5: gallery-dl outputs.
        #
        # Each image becomes one ``gallery_NNN`` artifact (NNN is a 1-based
        # 3-digit zero-padded index). gallery-dl's per-image metadata
        # sidecar (``<image>.json`` next to each image) becomes
        # ``gallery_NNN_meta``. The gallery-level ``info.json`` becomes
        # ``gallery_info``. Any other metadata file (rare; e.g. ``--write-tags``
        # output) lands as ``gallery_extra_NNN_meta``.
        #
        # We sort the image list before indexing so that NNN order is
        # deterministic across capture / re-verify, regardless of
        # filesystem walk order.
        if capture_kind == "gallery":
            sorted_galleries = sorted(
                (p for p in capture_input.gallery_files if p.exists()),
                key=lambda p: (p.name, str(p)),
            )
            # Map the gallery-dl per-image JSON sidecars (named
            # ``<image-name>.<image-ext>.json``) by their *image* path so we
            # can pair them up after the move. Path equality is post-move
            # because we move images first.
            metadata_by_image: dict[str, Path] = {}
            other_metadata: list[Path] = []
            gallery_info_src: Path | None = None
            for m in capture_input.gallery_metadata_files:
                if not m.exists():
                    continue
                if m.name == "info.json":
                    gallery_info_src = m
                    continue
                # gallery-dl writes ``<image>.json`` — strip the trailing
                # ``.json`` to recover the image name.
                if m.name.endswith(".json"):
                    image_name = m.name[: -len(".json")]
                    metadata_by_image[image_name] = m
                    continue
                other_metadata.append(m)

            for idx, src in enumerate(sorted_galleries, start=1):
                role = f"gallery_{idx:03d}"
                ext_clean = src.suffix.lstrip(".") or "bin"
                tail = f"{stem}.{idx:03d}.{ext_clean}"
                dest = _move_into(media_dir, src, new_name=tail)
                moved.append(dest)
                artifacts[role] = paths.relative_to_downloads(dest)
                meta_src = metadata_by_image.pop(src.name, None)
                if meta_src is not None:
                    meta_tail = f"{stem}.{idx:03d}.json"
                    meta_dest = _move_into(metadata_dir, meta_src, new_name=meta_tail)
                    moved.append(meta_dest)
                    artifacts[f"{role}_meta"] = paths.relative_to_downloads(meta_dest)

            if gallery_info_src is not None:
                info_dest = _move_into(
                    metadata_dir, gallery_info_src, new_name=f"{stem}.gallery_info.json"
                )
                moved.append(info_dest)
                artifacts["gallery_info"] = paths.relative_to_downloads(info_dest)

            # Orphan per-image metadata (image was filtered out by suffix
            # check, e.g. unrecognized extension) and other metadata
            # outputs (``--write-tags``, etc.). Preserve them under
            # ``gallery_extra_NNN_meta`` roles so they get hashed too.
            extras = list(metadata_by_image.values()) + other_metadata
            for j, src in enumerate(extras, start=1):
                if not src.exists():
                    continue
                tail = f"{stem}.gallery_extra_{j:03d}{src.suffix}"
                dest = _move_into(metadata_dir, src, new_name=tail)
                moved.append(dest)
                artifacts[f"gallery_extra_{j:03d}_meta"] = paths.relative_to_downloads(dest)

        # Page-snapshot artifacts (Phase 2 fills these in; Phase 1 stubs allowed).
        # Extension-supplied user-browser artifacts ride the same loop —
        # additive supplementary evidence; canonical capture is unchanged.
        for role, src in (
            ("page_mhtml", capture_input.page_mhtml),
            ("page_screenshot", capture_input.page_screenshot),
            ("page_warc", capture_input.page_warc),
            ("page_har", capture_input.page_har),
            ("page_console", capture_input.page_console),
            ("page_context_screenshot", capture_input.page_context_screenshot),
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
                "page_har": f"{stem}.page.har",
                "page_console": f"{stem}.page.console.json",
                "page_context_screenshot": f"{stem}.page.context.png",
                "user_browser_mhtml": f"{stem}.user-browser.mhtml",
                "user_browser_screenshot": f"{stem}.user-browser.png",
                "user_browser_har": f"{stem}.user-browser.har",
                "user_browser_environment": f"{stem}.user-browser.environment.json",
                "user_browser_tab_context": f"{stem}.user-browser.tab-context.json",
                "user_browser_session_state": f"{stem}.user-browser.session-state.json",
                "user_browser_dom_snapshot_html": f"{stem}.user-browser.dom-snapshot.html",
                "user_browser_dom_snapshot_meta": f"{stem}.user-browser.dom-snapshot.json",
            }[role]
            dest = _move_into(captures_dir, src, new_name=named)
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
        # Platform slug for the report PDF mirrors meta.platform: gallery
        # captures use gallery-dl's category, others use yt-dlp's
        # extractor_key (or the URL hint when info is missing).
        if capture_kind == "gallery" and capture_input.gallery_extractor:
            report_platform = platforms.gallery_friendly_name(capture_input.gallery_extractor)
        else:
            report_platform = (
                platforms.friendly_name(info.get("extractor_key", ""))
                if info
                else platforms.platform_for_url(capture_input.url_final)
            )

        # Gallery thumbnail strip — the report PDF renders an <img>-strip
        # of the first N images so a court reviewer can see the gallery's
        # contents at a glance. We pass the relative paths only; the
        # template resolves them against ``DOWNLOADS_DIR``.
        gallery_thumbnails = (
            [
                artifacts[k]
                for k in sorted(artifacts)
                if len(k) == 11 and k.startswith("gallery_") and k[8:11].isdigit()
            ]
            if capture_kind == "gallery"
            else []
        )

        report_view = {
            "title": title_sanitized_for_pdf,
            "source_url": capture_input.url_submitted,
            "final_url": capture_input.url_final,
            "redirect_chain": list(capture_input.redirect_chain or []),
            "captured_utc": capture_input.capture_date,
            "signing_key_fp": fp,
            "platform": report_platform,
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
                "warcio_version": capture_input.warcio_version,
                "gallery_dl_version": capture_input.gallery_dl_version,
            },
            "capture": capture_block_for_report,
            "manifest_filename": f"{stem}.manifest.pdf",
            # v7: forward the artifacts map so the report PDF can embed
            # the page_context_screenshot (when present) inline.
            "artifacts": dict(artifacts),
            # Gallery pass v0.5
            "capture_kind": capture_kind,
            "gallery_count": len(gallery_thumbnails),
            "gallery_extractor": capture_input.gallery_extractor if capture_kind == "gallery" else None,
            "gallery_thumbnails": gallery_thumbnails,
            # CLAUDE.md §15 v0.7: forward investigator-facing knobs so the
            # report PDF renders the Download options section. Empty/default
            # values render nothing — the section disappears entirely.
            "download_options": dict(capture_input.download_options or {}),
        }
        report_pdf_bytes = pdf_report.render_item_report(
            case=case, item_view=report_view, lang=capture_input.lang,
        )
        # v0.8 layout: the two human-readable PDFs sit at the item root
        # (Captures/, Metadata/, Media/ live one level below). They are
        # the first thing a recipient sees when they open the folder; the
        # rest of the artifacts are grouped by role beneath. Their
        # relpaths still flow through ``artifacts``, so checksums.txt,
        # the manifest's file table, and the evidence-export ZIP all pick
        # them up without further wiring.
        report_pdf_path = item_dir / f"{stem}.report.pdf"
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
        manifest_pdf_path = item_dir / f"{stem}.manifest.pdf"
        manifest_pdf_path.write_bytes(manifest_pdf_bytes)
        moved.append(manifest_pdf_path)

        # Step 5c: hash the manifest PDF and record it under the
        # ``manifest_pdf`` role so it gets the same chain-of-custody
        # treatment as every other artifact.
        manifest_pdf_relpath = paths.relative_to_downloads(manifest_pdf_path)
        artifacts["manifest_pdf"] = manifest_pdf_relpath
        checksums["manifest_pdf"] = compute_hashes(manifest_pdf_path)

        # Step 5d: write checksums.txt now that the manifest PDF is hashed.
        # checksums.txt lives in Metadata/ so the item root stays at "two
        # PDFs + three folders" even at a glance.
        metadata_dir.mkdir(parents=True, exist_ok=True)
        checksums_path = metadata_dir / f"{stem}.checksums.txt"
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

        # ``platform`` slug: gallery captures derive theirs from gallery-dl's
        # extractor (``category``); media + page-only use yt-dlp's
        # ``extractor_key`` or the URL hint.
        if capture_kind == "gallery" and capture_input.gallery_extractor:
            meta_platform = platforms.gallery_friendly_name(capture_input.gallery_extractor)
        else:
            meta_platform = (
                platforms.friendly_name(info.get("extractor_key", ""))
                if info
                else platforms.platform_for_url(capture_input.url_final)
            )

        # Count only the image artifact roles (``gallery_NNN``), not their
        # per-image ``gallery_NNN_meta`` siblings nor ``gallery_info`` /
        # ``gallery_extra_*``. The role pattern is exactly
        # ``gallery_<3-digit>``: 11 chars, last 3 are digits.
        gallery_count = (
            sum(
                1
                for k in artifacts
                if len(k) == 11 and k.startswith("gallery_") and k[8:11].isdigit()
            )
            if capture_kind == "gallery"
            else None
        )

        # CLAUDE.md §15 v0.7: surface investigator-facing download knobs
        # so a recipient can see what was in effect (audio_only, quality_cap,
        # subtitle_langs, restart_count, stalled_count). Always emit the
        # block so absence-vs-default is unambiguous; empty values are fine.
        download_options_block = dict(capture_input.download_options or {})
        download_options_block.setdefault("audio_only", False)
        download_options_block.setdefault("quality_cap", None)
        download_options_block.setdefault("subtitle_langs", [])
        download_options_block.setdefault("restart_count", 0)
        # capture.stalled_count: pull from the same dict so the audit
        # trail matches what was on the orchestrator's Job at sign time.
        stalled_count = int(download_options_block.pop("stalled_count", 0) or 0)
        # Mirror onto the capture block so a reviewer reading the report
        # PDF sees stall events alongside other capture-side counters.
        capture_block.setdefault("stalled_count", stalled_count)

        meta = {
            "schema_version": 8,
            "job_uuid": capture_input.job_uuid,
            "capture_kind": capture_kind,
            "case": {"id": case.id, "slug": case.slug, "name": case.name},
            "url_submitted": capture_input.url_submitted,
            "url_final": capture_input.url_final,
            # Canonical form used for dedup (CLAUDE.md §15). Originals
            # ``url_submitted`` and ``url_final`` stay verbatim.
            "url_canonical": canonical_url,
            "url_redirect_chain": capture_input.redirect_chain,
            "platform": meta_platform,
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
                # Schema-required for back-compat with v2–v6: this stays
                # populated even when the in-session WARC writer made
                # browsertrix moot (it'll be "0" / null in that case).
                "browsertrix_version": capture_input.browsertrix_version,
                "warcio_version": capture_input.warcio_version,
                "gallery_dl_version": capture_input.gallery_dl_version,
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
            # Gallery pass v0.5: count and extractor surfaced at the root
            # so callers can discriminate without parsing artifact keys.
            # ``null`` for non-gallery kinds.
            "gallery_count": gallery_count,
            "gallery_extractor": capture_input.gallery_extractor if capture_kind == "gallery" else None,
            # CLAUDE.md §15 v0.7: per-job download knobs + reliability
            # counters. Always emitted (defaults included) so absence-vs-
            # default is never ambiguous for downstream verifiers.
            "download_options": download_options_block,
            "audit_log_entry_id": None,  # filled below
            "signing_key_fp": fp,
        }
        meta_path = metadata_dir / f"{stem}.meta.json"
        meta_bytes = json.dumps(meta, indent=2, ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
        meta_path.write_bytes(meta_bytes)

        # Step 7: detached signature — lands next to meta.json (so also in
        # Metadata/) via signing.sign_file's path.with_name + ".sig".
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

        download_details: dict[str, Any] = {
            "stem": stem,
            "capture_kind": capture_kind,
            "url_hash": url_hash,
            "platform": meta["platform"],
            "authenticated_domains": list(capture_input.authenticated_domains),
        }
        if capture_kind == "gallery":
            # Pinning the count + extractor on the audit row gives an
            # evidence reviewer a quick "what does this row preserve?"
            # answer without having to read the meta.json — the same role
            # that ``video_id`` plays for media kinds.
            download_details["gallery_count"] = gallery_count
            download_details["gallery_extractor"] = capture_input.gallery_extractor
        audit_id = audit.append(
            conn,
            "download.created",
            case_id=case.id,
            download_id=download_id,
            actor="system",
            details=download_details,
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

        # v7 capture-side audit rows. Each is conditional — emitted only
        # when the corresponding capture step actually fired — so audit
        # traffic stays proportional to evidence content, not to checklist.
        capture_block = capture_block_for_report  # already merged above
        warc_block = capture_block.get("warc") if isinstance(capture_block, dict) else None
        if isinstance(warc_block, dict) and warc_block.get("captured_in_session"):
            audit.append(
                conn, "capture.warc_session_in_process",
                case_id=case.id, download_id=download_id, actor="system",
                details={
                    "record_count": int(warc_block.get("record_count") or 0),
                    "encoding_normalized": bool(warc_block.get("encoding_normalized")),
                    "format_version": warc_block.get("format_version"),
                    "warcio_version": capture_input.warcio_version,
                },
            )
        elif isinstance(warc_block, dict) and warc_block.get("captured_in_session") is False and "page_warc" in artifacts:
            audit.append(
                conn, "capture.warc_session_subprocess",
                case_id=case.id, download_id=download_id, actor="system",
                details={
                    "browsertrix_version": capture_input.browsertrix_version,
                    "reason": "in_session_unavailable_or_failed",
                },
            )
        if isinstance(capture_block, dict) and capture_block.get("animations_frozen"):
            audit.append(
                conn, "capture.animations_frozen",
                case_id=case.id, download_id=download_id, actor="system",
                details={"version": capture_block.get("animations_frozen_version")},
            )
        if isinstance(capture_block, dict) and capture_block.get("media_context_captured"):
            audit.append(
                conn, "capture.media_context_captured",
                case_id=case.id, download_id=download_id, actor="system",
                details={
                    "selector": capture_block.get("media_context_selector"),
                    "sha256": (checksums.get("page_context_screenshot") or {}).get("sha256"),
                },
            )
        if isinstance(capture_block, dict) and capture_block.get("console_message_count", 0) > 0:
            audit.append(
                conn, "capture.console_messages_recorded",
                case_id=case.id, download_id=download_id, actor="system",
                details={
                    "count": int(capture_block.get("console_message_count") or 0),
                    "error_count": int(capture_block.get("console_error_count") or 0),
                },
            )
        if isinstance(capture_block, dict) and capture_block.get("screenshot_truncated_at_px"):
            audit.append(
                conn, "capture.screenshot_truncated",
                case_id=case.id, download_id=download_id, actor="system",
                details={
                    "truncated_at_px": int(capture_block.get("screenshot_truncated_at_px") or 0),
                    "full_height_px": int(capture_block.get("lazy_load_max_height_px") or 0),
                },
            )
        if isinstance(capture_block, dict) and capture_block.get("readiness_timed_out"):
            # Distinct from `capture.readiness_timed_out` (already emitted
            # by jobs.py for individual gate timeouts): this fires only when
            # the outer 60s render-wait budget was exceeded and remaining
            # gates were skipped. Same evidence story, more precise signal.
            audit.append(
                conn, "capture.readiness_budget_exceeded",
                case_id=case.id, download_id=download_id, actor="system",
                details={
                    "render_waits": [
                        {"name": w.get("name"), "ok": w.get("ok"), "timed_out": w.get("timed_out")}
                        for w in (capture_block.get("render_waits") or [])
                    ],
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

    # Map role → (subdir, on-disk filename). Subdir mirrors the v0.8
    # routing in ``finalize``: page snapshots into Captures/, the media
    # file into Media/, and meta + checksums into Metadata/.
    name_for: dict[str, tuple[str, str | None]] = {
        "page_warc":                     (SUBDIR_CAPTURES, f"{stem}.page.warc.gz"),
        "page_mhtml":                    (SUBDIR_CAPTURES, f"{stem}.page.mhtml"),
        "page_screenshot":               (SUBDIR_CAPTURES, f"{stem}.page.png"),
        "page_har":                      (SUBDIR_CAPTURES, f"{stem}.page.har"),
        "page_console":                  (SUBDIR_CAPTURES, f"{stem}.page.console.json"),
        "page_context_screenshot":       (SUBDIR_CAPTURES, f"{stem}.page.context.png"),
        "user_browser_mhtml":            (SUBDIR_CAPTURES, f"{stem}.user-browser.mhtml"),
        "user_browser_screenshot":       (SUBDIR_CAPTURES, f"{stem}.user-browser.png"),
        "user_browser_har":              (SUBDIR_CAPTURES, f"{stem}.user-browser.har"),
        "user_browser_environment":      (SUBDIR_CAPTURES, f"{stem}.user-browser.environment.json"),
        "user_browser_tab_context":      (SUBDIR_CAPTURES, f"{stem}.user-browser.tab-context.json"),
        "user_browser_session_state":    (SUBDIR_CAPTURES, f"{stem}.user-browser.session-state.json"),
        "user_browser_dom_snapshot_html": (SUBDIR_CAPTURES, f"{stem}.user-browser.dom-snapshot.html"),
        "user_browser_dom_snapshot_meta": (SUBDIR_CAPTURES, f"{stem}.user-browser.dom-snapshot.json"),
        "media":                         (SUBDIR_MEDIA, None),  # named below using source extension
    }
    if role not in name_for:
        raise ValueError(f"unknown extend role {role!r}")

    subdir, target_name = name_for[role]
    target_dir = item_dir / subdir if subdir else item_dir
    if role == "media":
        ext = source.suffix.lstrip(".") or "bin"
        target = _move_into(target_dir, source, new_name=f"{stem}.{ext}")
    else:
        target = _move_into(target_dir, source, new_name=target_name)

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

    # meta.json + signature + checksums.txt all live in Metadata/. Older
    # items captured before v0.8 carry a meta.json at the item root —
    # honor that path so an extend on a legacy item updates the right
    # file rather than spawning a stranded sibling.
    legacy_meta = item_dir / f"{stem}.meta.json"
    if legacy_meta.is_file():
        meta_path = legacy_meta
        checksums_path = item_dir / f"{stem}.checksums.txt"
    else:
        meta_dir = item_dir / SUBDIR_METADATA
        meta_dir.mkdir(parents=True, exist_ok=True)
        meta_path = meta_dir / f"{stem}.meta.json"
        checksums_path = meta_dir / f"{stem}.checksums.txt"
    new_meta_bytes = json.dumps(
        meta, indent=2, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    meta_path.write_bytes(new_meta_bytes)

    # Re-sign with current key.
    sig_path = signing.sign_file(meta_path)

    # Re-write checksums.txt to match the updated artifact set.
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
