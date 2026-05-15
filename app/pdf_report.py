"""Locale-aware PDF case report (CLAUDE.md §10).

WeasyPrint renders an HTML document to PDF — RTL-capable, supports
Noto Sans Arabic / Noto Sans JP, embeds raster + vector assets. The
template lives in ``app/templates/case_report.html``; rendering is a
one-liner.

The report is intentionally austere: PDFs will be read by editors,
opposing counsel, judges' clerks. No ornament, lots of whitespace, the
integrity story up front. Strings come from the i18n bundle so the
report follows the active UI locale at export time — labels, page
direction, and font stack all flip on ``lang``.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import __version__, cases, config, i18n, signing

__all__ = [
    "FileEntry",
    "render_case_report",
    "render_item_manifest",
    "render_item_report",
]


_TEMPLATE_PATH = config.APP_DIR / "templates" / "case_report.html"
_ITEM_TEMPLATE_PATH = config.APP_DIR / "templates" / "item_manifest.html"
_ITEM_REPORT_TEMPLATE_PATH = config.APP_DIR / "templates" / "item_report.html"


# Subset of i18n keys consumed by the PDF templates. Centralised so the
# tests can assert against the exact set without re-reading the bundle.
_PDF_KEY_PREFIXES = ("pdf.", "manifest.")


def is_real_version(v: Any) -> bool:
    """True iff ``v`` looks like a meaningful version string.

    Used by the per-item PDF tools-table renderer to decide whether to
    emit a row for an optional tool. ``None``, the empty string, ``"0"``,
    ``"unknown"``, and ``"none"`` (case-insensitive) all read as "tool
    didn't really run" — a v0.6 in-session WARC capture writes
    ``browsertrix_version="0"`` because the browsertrix subprocess wasn't
    invoked, and we want that row hidden rather than rendered as
    ``browsertrix version: 0`` which would mislead a reviewer.
    """
    if v is None:
        return False
    s = str(v).strip().lower()
    if not s:
        return False
    if s in ("0", "unknown", "none"):
        return False
    return True


def _load_pdf_strings(lang: str) -> dict[str, str]:
    """Return the ``pdf.*`` + ``manifest.*`` slice of the locale bundle.

    Falls back to English for any missing key via
    ``i18n.merged_with_fallback``. The PDF templates can rely on every
    key being present even when the requested locale is partial (or
    absent — e.g. ``ja`` while Track B is still in flight).
    """
    bundle = i18n.merged_with_fallback(lang)
    return {
        k: v for k, v in bundle.items()
        if any(k.startswith(prefix) for prefix in _PDF_KEY_PREFIXES)
    }


def _format_bytes(n: int | None) -> str:
    if not n:
        return "—"
    size: float = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size = size / 1024
    return f"{size:.1f} PB"


def _format_dt(s: str | None) -> str:
    if not s:
        return "—"
    return s.replace("T", " ").split("+")[0]


def _footer_prefix_for(footer_template: str, context: str) -> str:
    """Resolve the localised footer minus the page-counter portion.

    The i18n value uses ``{page}`` and ``{pages}`` placeholders for the
    page numbers; we keep them as literal CSS counters in the template
    and only substitute the leading text + context here.
    """
    prefix = footer_template.replace("{context}", context)
    sentinel = prefix.find("{page}")
    if sentinel != -1:
        prefix = prefix[:sentinel].rstrip()
    return html.escape(prefix)


def _item_html(row: sqlite3.Row, labels: Mapping[str, str]) -> str:
    """Render one library row into an HTML <article>."""
    meta = json.loads(row["meta_json"])
    title = html.escape(row["title_original"] or "untitled")
    url = html.escape(row["final_url"] or row["source_url"] or "")
    platform = html.escape(row["platform"] or "")
    capture_kind = html.escape(row["capture_kind"])
    capture_date = html.escape(_format_dt(row["capture_date"]))
    upload_date = html.escape(row["upload_date"] or "—")
    sha = html.escape((row["sha256"] or "—")[:16] + ("…" if row["sha256"] else ""))
    md5 = html.escape((row["md5"] or "—")[:16] + ("…" if row["md5"] else ""))
    size = html.escape(_format_bytes(row["file_size_bytes"]))
    fp = html.escape(row["signing_key_fp"])
    artifacts = meta.get("artifacts", {})
    artifact_lis = "\n".join(
        f"<li><span class='role'>{html.escape(role)}</span> "
        f"<bdi>{html.escape(rel)}</bdi></li>"
        for role, rel in sorted(artifacts.items())
    )
    return f"""
    <article class="item">
      <header>
        <h2><bdi>{title}</bdi></h2>
        <div class="badges">
          <span class="badge platform">{platform}</span>
          <span class="badge kind">{capture_kind}</span>
        </div>
      </header>
      <dl>
        <dt>{html.escape(labels['pdf.item.source_url'])}</dt>      <dd><bdi>{url}</bdi></dd>
        <dt>{html.escape(labels['pdf.item.captured_utc'])}</dt>    <dd>{capture_date}</dd>
        <dt>{html.escape(labels['pdf.item.upload_date'])}</dt>     <dd>{upload_date}</dd>
        <dt>{html.escape(labels['pdf.item.size'])}</dt>            <dd>{size}</dd>
        <dt>{html.escape(labels['pdf.item.sha256'])}</dt>          <dd><code>{sha}</code></dd>
        <dt>{html.escape(labels['pdf.item.md5'])}</dt>             <dd><code>{md5}</code></dd>
        <dt>{html.escape(labels['pdf.item.signing_key'])}</dt>     <dd><code>{fp}</code></dd>
      </dl>
      <div class="artifacts">
        <h3>{html.escape(labels['pdf.item.artifacts'])}</h3>
        <ul>{artifact_lis}</ul>
      </div>
    </article>
    """


def _render_html(
    case: cases.Case,
    items: list[sqlite3.Row],
    *,
    lang: str,
) -> str:
    labels = _load_pdf_strings(lang)
    direction = "rtl" if config.is_rtl(lang) else "ltr"
    fp = signing.fingerprint(signing.ensure_keypair().public)
    when = html.escape(_dt.datetime.now(_dt.timezone.utc).isoformat())
    name = html.escape(case.name)
    slug = html.escape(case.slug)
    desc = html.escape(case.description or "")
    items_html = "\n".join(_item_html(row, labels) for row in items)
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")

    footer_template = labels.get(
        "pdf.footer",
        "Capsule evidence export · {context} · page {page} of {pages}",
    )

    rendered = template
    for needle, replacement in {
        "{{lang}}": html.escape(lang),
        "{{dir}}": direction,
        "{{case_name}}": name,
        "{{case_slug}}": slug,
        "{{case_description}}": desc,
        "{{case_status}}": html.escape(case.status),
        "{{exported_at}}": when,
        "{{fingerprint}}": html.escape(fp),
        "{{app_version}}": html.escape(__version__),
        "{{item_count}}": str(len(items)),
        "{{items}}": items_html,
        "{{label.brand_name}}": html.escape(labels["pdf.brand.name"]),
        "{{label.brand_tagline}}": html.escape(labels["pdf.brand.tagline.case"]),
        "{{label.field_slug}}": html.escape(labels["pdf.field.slug"]),
        "{{label.field_status}}": html.escape(labels["pdf.field.status"]),
        "{{label.summary_items}}": html.escape(labels["pdf.summary.items"]),
        "{{label.summary_exported_utc}}": html.escape(labels["pdf.summary.exported_utc"]),
        "{{label.summary_app_version}}": html.escape(labels["pdf.summary.app_version"]),
        "{{label.summary_signing_key}}": html.escape(labels["pdf.summary.signing_key"]),
        "{{label.footer_prefix}}": _footer_prefix_for(footer_template, name),
    }.items():
        rendered = rendered.replace(needle, replacement)
    return rendered


def render_case_report(
    *,
    case: cases.Case,
    items: list[sqlite3.Row],
    lang: str = "en",
) -> bytes:
    """Render the per-case PDF and return its bytes.

    ``lang`` selects the locale bundle (``i18n.merged_with_fallback``)
    and flips RTL/LTR. Defaults to English so old call sites keep
    working; callers that want the active UI locale should pass it.
    """
    from weasyprint import HTML  # heavy import deferred

    html_doc = _render_html(case, items, lang=lang)
    pdf = HTML(string=html_doc, base_url=str(config.APP_DIR)).write_pdf()
    return pdf


# --- Per-item manifest PDF -------------------------------------------------


@dataclass(frozen=True)
class FileEntry:
    """One row in the per-item manifest table.

    ``relpath`` is the file's path relative to ``/downloads`` so the
    manifest stays portable across machines (CLAUDE.md §3 — never
    write absolute host paths into evidence).
    """

    relpath: str
    size: int
    md5: str
    sha256: str


def _file_row_html(entry: FileEntry) -> str:
    # Forensic re-verification requires the full hex digests — truncated
    # values can't be recomputed by ``md5sum``/``sha256sum``. The template
    # uses ``word-break: break-all`` on ``td.hash`` so the long strings
    # wrap inside the column rather than overflow the page.
    return (
        "<tr>"
        f"<td class='path'><bdi>{html.escape(entry.relpath)}</bdi></td>"
        f"<td class='size'>{html.escape(_format_bytes(entry.size))}</td>"
        f"<td class='hash'><code>{html.escape(entry.md5)}</code></td>"
        f"<td class='hash'><code>{html.escape(entry.sha256)}</code></td>"
        "</tr>"
    )


def _render_item_manifest_html(
    *,
    case: cases.Case,
    item_view: Mapping[str, Any],
    files: list[FileEntry],
    lang: str,
) -> str:
    labels = _load_pdf_strings(lang)
    direction = "rtl" if config.is_rtl(lang) else "ltr"
    template = _ITEM_TEMPLATE_PATH.read_text(encoding="utf-8")

    title = html.escape(str(item_view.get("title") or "untitled"))
    source_url = html.escape(str(item_view.get("source_url") or ""))
    captured_utc = html.escape(_format_dt(str(item_view.get("captured_utc") or "")))
    fp = html.escape(str(item_view.get("signing_key_fp") or ""))
    case_name = html.escape(case.name)

    if files:
        rows = "\n".join(_file_row_html(f) for f in files)
        rows_block = (
            "<table class='manifest-table'>"
            "<thead><tr>"
            f"<th>{html.escape(labels['manifest.col.path'])}</th>"
            f"<th>{html.escape(labels['manifest.col.size'])}</th>"
            f"<th>{html.escape(labels['manifest.col.md5'])}</th>"
            f"<th>{html.escape(labels['manifest.col.sha256'])}</th>"
            "</tr></thead>"
            f"<tbody>{rows}</tbody>"
            "</table>"
        )
    else:
        rows_block = (
            f"<p class='empty'>{html.escape(labels['manifest.empty'])}</p>"
        )

    footer_template = labels.get(
        "pdf.footer",
        "Capsule evidence export · {context} · page {page} of {pages}",
    )

    rendered = template
    for needle, replacement in {
        "{{lang}}": html.escape(lang),
        "{{dir}}": direction,
        "{{case_name}}": case_name,
        "{{title}}": title,
        "{{source_url}}": source_url,
        "{{captured_utc}}": captured_utc,
        "{{fingerprint}}": fp,
        "{{rows_block}}": rows_block,
        "{{label.brand_name}}": html.escape(labels["pdf.brand.name"]),
        "{{label.brand_tagline}}": html.escape(labels["pdf.brand.tagline.item"]),
        "{{label.heading}}": html.escape(labels["manifest.heading"]),
        "{{label.source_url}}": html.escape(labels["pdf.item.source_url"]),
        "{{label.captured_utc}}": html.escape(labels["pdf.item.captured_utc"]),
        "{{label.signing_key}}": html.escape(labels["pdf.item.signing_key"]),
        "{{label.footer_prefix}}": _footer_prefix_for(footer_template, case_name),
    }.items():
        rendered = rendered.replace(needle, replacement)
    return rendered


def render_item_manifest(
    *,
    case: cases.Case,
    item_view: Mapping[str, Any],
    item_dir: Path,
    files: list[FileEntry],
    lang: str = "en",
) -> bytes:
    """Render the per-item manifest PDF and return its bytes.

    ``item_view`` is a small mapping with the fields the template needs
    (``title``, ``source_url``, ``captured_utc``, ``signing_key_fp``).
    The library row hasn't been inserted into ``downloads`` at the
    call site — the postprocess hook builds the dict from the
    ``CaptureInput`` it has on hand. ``item_dir`` is accepted for
    symmetry with the postprocess code path; the renderer doesn't
    consume it.
    """
    from weasyprint import HTML  # heavy import deferred

    del item_dir  # symmetry with the postprocess call site
    html_doc = _render_item_manifest_html(
        case=case, item_view=item_view, files=files, lang=lang,
    )
    pdf = HTML(string=html_doc, base_url=str(config.APP_DIR)).write_pdf()
    return pdf


# --- Per-item report PDF ---------------------------------------------------


def _format_duration(seconds: int | float | None, dash: str) -> str:
    """Render a duration as ``mm:ss`` or ``hh:mm:ss``.

    ``dash`` is the localised "unknown" placeholder for ``None`` /
    non-positive values.
    """
    if seconds is None:
        return dash
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return dash
    if s <= 0:
        return dash
    hours, rem = divmod(s, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def _format_redirect_chain(
    chain: list[str], labels: Mapping[str, str]
) -> str:
    """Render the redirect chain as an ``<ol>`` or the empty placeholder."""
    if not chain:
        return (
            "<em class='muted'>"
            f"{html.escape(labels['pdf.report.field.redirect_chain_empty'])}"
            "</em>"
        )
    items = "\n".join(
        f"<li><bdi>{html.escape(url)}</bdi></li>" for url in chain
    )
    return f"<ol>{items}</ol>"


def _format_capture_report(
    capture: Mapping[str, Any] | None,
    labels: Mapping[str, str],
) -> str:
    """Render a definition list of selected capture-report fields.

    Mirrors the keys recorded in ``meta.json.capture.*`` per CLAUDE.md
    §6 (hardening pass): render-wait outcomes, blocked-request count,
    banner-hide flags, tab-context flag, report locale.
    """
    cap = dict(capture or {})
    dash = labels["pdf.report.field.unknown"]
    yes = labels["pdf.report.field.yes"]
    no = labels["pdf.report.field.no"]

    def _bool(v: Any) -> str:
        if v is True:
            return yes
        if v is False:
            return no
        return dash

    def _scalar(v: Any) -> str:
        if v is None:
            return dash
        if isinstance(v, bool):
            return _bool(v)
        return html.escape(str(v))

    # Render-wait outcomes — collapse the structured array into a
    # single-line summary keyed by stage name. Each stage is one of
    # ``load|fonts|images|video|lazy_load|networkidle``.
    waits = cap.get("render_waits") or []
    wait_bits: list[str] = []
    for w in waits:
        if not isinstance(w, dict):
            continue
        name = w.get("name") or "?"
        ok = w.get("ok")
        if ok is True:
            mark = "✓"
        elif ok is False:
            mark = "✗"
        else:
            mark = "·"
        wait_bits.append(f"{html.escape(str(name))} {mark}")
    waits_html = (
        ", ".join(wait_bits) if wait_bits else dash
    )

    blocked_count = cap.get("blocked_request_count")
    blocked_html = (
        _scalar(blocked_count)
        if blocked_count is not None
        else dash
    )

    banner_applied = _bool(cap.get("banner_hide_applied"))
    banner_version = cap.get("banner_hide_version")
    banner_html = banner_applied
    if cap.get("banner_hide_applied") and banner_version:
        banner_html = f"{banner_applied} (<code>{html.escape(str(banner_version))}</code>)"

    # WARC session provenance (v7) — in-session vs. subprocess fallback
    # is forensically meaningful, so it gets a visible row.
    warc_meta = cap.get("warc") or {}
    if isinstance(warc_meta, dict) and warc_meta:
        if warc_meta.get("captured_in_session"):
            warc_html = (
                labels["pdf.report.field.capture.warc_session.in_process"]
                + f" — <code>{int(warc_meta.get('record_count') or 0)}</code> "
                + labels["pdf.report.field.capture.warc_session.records"]
            )
        else:
            warc_html = labels["pdf.report.field.capture.warc_session.subprocess"]
    else:
        warc_html = dash

    # Animation freeze — applied just before screenshot. MHTML/WARC are
    # captured before the freeze and unaffected.
    if cap.get("animations_frozen"):
        v = cap.get("animations_frozen_version")
        anim_html = labels["pdf.report.field.capture.animations_frozen"]
        if v:
            anim_html = f"{anim_html} (<code>{html.escape(str(v))}</code>)"
    else:
        anim_html = _bool(cap.get("animations_frozen"))

    # Console messages — single-line summary; the JSON sidecar holds the
    # full event list under the ``page_console`` artifact role.
    msg_count = cap.get("console_message_count") or 0
    err_count = cap.get("console_error_count") or 0
    console_html = labels["pdf.report.field.capture.console_messages"].format(
        count=int(msg_count), errors=int(err_count),
    )

    # Media-context screenshot — surface the matched selector for
    # forensic clarity.
    if cap.get("media_context_captured"):
        sel = cap.get("media_context_selector")
        ctx_html = labels["pdf.report.field.capture.media_context.captured"]
        if sel:
            ctx_html = f"{ctx_html} (<code>{html.escape(str(sel))}</code>)"
    else:
        ctx_html = labels["pdf.report.field.capture.media_context.absent"]

    # Page height + truncation flag.
    h_px = int(cap.get("lazy_load_max_height_px") or 0)
    trunc = cap.get("screenshot_truncated_at_px")
    if trunc:
        height_html = labels["pdf.report.field.capture.screenshot_truncated"].format(
            px=int(trunc), full_px=h_px,
        )
    elif h_px:
        height_html = f"{h_px}px"
    else:
        height_html = dash

    # CLAUDE.md §15 v0.6 drift fix (v0.12): three forensic counters that
    # land in meta.json.capture today but were never surfaced to the
    # per-item PDF. Always rendered — consistent with the existing
    # "always show the row, render dash/0 when no signal" pattern — so
    # a recipient can see Capsule looked for these signals even when the
    # value is zero.
    videos_paused_html = _scalar(cap.get("videos_paused") or 0)
    lazy_promoted_html = _scalar(cap.get("lazy_promoted_count") or 0)
    readiness_html = _bool(bool(cap.get("readiness_timed_out")))

    # CLAUDE.md §15 v0.12: frozen single-file HTML view. The block is
    # always emitted to meta.json so absence-vs-default is unambiguous;
    # the PDF row reads "generated, tier=full, N inlined, M external"
    # on success and "skipped" / "size_budget_exceeded" on failure.
    # When the block is missing entirely (legacy pre-v11 records) the
    # row falls through to a dash so an old item still renders.
    fh = cap.get("frozen_html") or {}
    if isinstance(fh, dict) and fh.get("generated"):
        tier = fh.get("tier") or "?"
        tier_key = f"pdf.report.field.capture.frozen_html.tier.{tier}"
        tier_label = labels.get(tier_key, tier)
        inlined = int(fh.get("inlined_image_count") or 0)
        external = int(fh.get("external_image_count") or 0)
        shadow = int(fh.get("shadow_root_omitted_count") or 0)
        # Compose: tier + "N inlined, M external" + optional shadow note.
        body = labels["pdf.report.field.capture.frozen_html.summary"].format(
            tier=html.escape(tier_label),
            inlined=inlined,
            external=external,
        )
        if shadow:
            body = body + " · " + labels[
                "pdf.report.field.capture.frozen_html.shadow_omitted"
            ].format(count=shadow)
        frozen_html_html = body
    elif isinstance(fh, dict) and fh.get("error"):
        err_key = f"pdf.report.field.capture.frozen_html.error.{fh['error'].split(':', 1)[0]}"
        frozen_html_html = html.escape(
            labels.get(err_key, fh.get("error") or labels["pdf.report.field.unknown"])
        )
    else:
        frozen_html_html = dash

    rows = [
        (labels["pdf.report.field.render_wait"], waits_html),
        (labels["pdf.report.field.blocked_requests"], blocked_html),
        (labels["pdf.report.field.banner_hide"], banner_html),
        (labels["pdf.report.field.capture.animations_frozen.label"], anim_html),
        (labels["pdf.report.field.capture.videos_paused.label"], videos_paused_html),
        (labels["pdf.report.field.capture.lazy_promoted.label"], lazy_promoted_html),
        (labels["pdf.report.field.capture.readiness_timed_out.label"], readiness_html),
        (labels["pdf.report.field.capture.console.label"], console_html),
        (labels["pdf.report.field.capture.media_context.label"], ctx_html),
        (labels["pdf.report.field.capture.page_height.label"], height_html),
        (labels["pdf.report.field.capture.warc_session.label"], warc_html),
        (labels["pdf.report.field.capture.frozen_html.label"], frozen_html_html),
        (labels["pdf.report.field.tab_context"], _bool(cap.get("tab_context_used"))),
        (labels["pdf.report.field.report_lang"], _scalar(cap.get("report_lang"))),
    ]
    items = "\n".join(
        f"<dt>{html.escape(label)}</dt><dd>{value}</dd>"
        for label, value in rows
    )
    return f"<dl>{items}</dl>"


def _format_download_options_section(
    download_options: Mapping[str, Any] | None,
    capture: Mapping[str, Any] | None,
    labels: Mapping[str, str],
) -> str:
    """Render the Download options section for the per-item report PDF.

    Returns an empty string when every knob is at its default — the
    section then disappears from the PDF entirely so the layout stays
    tight for plain captures. Forensically meaningful values (audio_only,
    quality cap, format/container, subtitles, restart count, stall count)
    get one row each.
    CLAUDE.md §15 v0.7/v0.9.
    """
    opts = dict(download_options or {})
    cap = dict(capture or {})
    audio_only = bool(opts.get("audio_only"))
    quality_cap = opts.get("quality_cap")
    subs = list(opts.get("subtitle_langs") or [])
    video_container = opts.get("video_container") or None
    audio_container = opts.get("audio_container") or None
    force_gallery_run = bool(opts.get("force_gallery_run"))
    # CLAUDE.md §15 v0.11 drift fix (v0.12): the capture_mode i18n keys
    # already exist in all four bundles but the rendering code was never
    # written. ``None`` = default fallback routing (yt-dlp then gallery-dl),
    # which renders nothing — the row is only meaningful when the
    # investigator picked a non-default mode.
    capture_mode = opts.get("capture_mode") or None
    restart_count = int(opts.get("restart_count") or 0)
    stalled_count = int(cap.get("stalled_count") or 0)

    if not (
        audio_only
        or quality_cap
        or subs
        or video_container
        or audio_container
        or force_gallery_run
        or capture_mode
        or restart_count
        or stalled_count
    ):
        return ""

    rows: list[tuple[str, str]] = []

    if audio_only:
        rows.append((
            labels["pdf.report.field.download_options.audio_only.label"],
            html.escape(labels["pdf.report.field.download_options.audio_only.value"]),
        ))

    if quality_cap and quality_cap != "audio":
        if quality_cap == "best":
            value = labels["pdf.report.field.download_options.quality_cap.best"]
        else:
            template = labels["pdf.report.field.download_options.quality_cap.height"]
            value = template.replace("{height}", str(quality_cap))
        rows.append((
            labels["pdf.report.field.download_options.quality_cap.label"],
            html.escape(value),
        ))

    # v0.9: container picker. Mux-only on the video path (no re-encode);
    # extraction-format choice on the audio path. Render whichever side
    # the user actually picked, gated by the audio_only state so a stale
    # opposite-side value doesn't appear on the wrong report. The
    # container choice is part of meta.json.download_options and
    # transitively bound by meta.json.sig.
    if not audio_only and video_container in {"mp4", "webm", "mkv"}:
        template = labels["pdf.report.field.download_options.format.video"]
        rows.append((
            labels["pdf.report.field.download_options.format.label"],
            html.escape(template.replace("{fmt}", video_container.upper())),
        ))
    elif audio_only and audio_container in {"mp3", "m4a", "opus", "wav", "flac"}:
        template = labels["pdf.report.field.download_options.format.audio"]
        rows.append((
            labels["pdf.report.field.download_options.format.label"],
            html.escape(template.replace("{fmt}", audio_container.upper())),
        ))

    if subs:
        rows.append((
            labels["pdf.report.field.download_options.subs.label"],
            html.escape(", ".join(subs)),
        ))

    if force_gallery_run:
        rows.append((
            labels["pdf.report.field.download_options.force_gallery_run.label"],
            html.escape(labels["pdf.report.field.download_options.force_gallery_run.value"]),
        ))

    # v0.11 drift fix: localise the mode via the dedicated bundle key,
    # fall back to the raw enum value if a future mode is added before
    # the bundle is updated (so a missing key never crashes the PDF).
    if capture_mode in {"webpage", "media", "gallery"}:
        mode_key = f"pdf.report.field.download_options.capture_mode.{capture_mode}"
        rows.append((
            labels["pdf.report.field.download_options.capture_mode"],
            html.escape(labels.get(mode_key, capture_mode)),
        ))

    if restart_count:
        template = labels["pdf.report.field.download_options.restart_count"]
        rows.append((
            labels["pdf.report.field.download_options.restart_count.label"],
            html.escape(template.replace("{count}", str(restart_count))),
        ))

    if stalled_count:
        template = labels["pdf.report.field.download_options.stalled_count"]
        rows.append((
            labels["pdf.report.field.download_options.stalled_count.label"],
            html.escape(template.replace("{count}", str(stalled_count))),
        ))

    items = "\n".join(
        f"<dt>{html.escape(label)}</dt><dd>{value}</dd>"
        for label, value in rows
    )
    heading = html.escape(labels["pdf.report.heading.download_options"])
    return (
        f"<section class='download-options-section'>"
        f"<h2>{heading}</h2>"
        f"<dl>{items}</dl>"
        f"</section>"
    )


def _format_audit_trail_section(
    stem: str,
    labels: Mapping[str, str],
) -> str:
    """Render the Audit trail pointer for the per-item report PDF.

    The per-item audit-log sidecar (``Metadata/{stem}.audit.json``) is
    written after this PDF is signed and grows as post-finalize events
    extend the chain (verify, extend_capture, recapture). The PDF
    therefore can't carry a live entry count — instead we point the
    recipient at the file and remind them the case-level
    ``audit_log.json`` is authoritative.
    """
    template = labels["pdf.report.audit_trail.body"]
    body_text = template.replace("{path}", f"Metadata/{stem}.audit.json")
    heading = html.escape(labels["pdf.report.heading.audit_trail"])
    return (
        f"<section class='audit-trail-section'>"
        f"<h2>{heading}</h2>"
        f"<p class='audit-trail-pointer'>{html.escape(body_text)}</p>"
        f"</section>"
    )


def _render_item_report_html(
    *,
    case: cases.Case,
    item_view: Mapping[str, Any],
    lang: str,
) -> str:
    labels = _load_pdf_strings(lang)
    direction = "rtl" if config.is_rtl(lang) else "ltr"
    template = _ITEM_REPORT_TEMPLATE_PATH.read_text(encoding="utf-8")

    dash = labels["pdf.report.field.unknown"]
    title = html.escape(str(item_view.get("title") or "untitled"))
    source_url = html.escape(str(item_view.get("source_url") or ""))
    final_url_raw = item_view.get("final_url") or ""
    captured_utc = html.escape(_format_dt(str(item_view.get("captured_utc") or "")))
    fp = html.escape(str(item_view.get("signing_key_fp") or ""))
    case_name = html.escape(case.name)

    # Final URL block in the header is suppressed when it equals the
    # source URL — duplicating the row would just be noise.
    if final_url_raw and final_url_raw != item_view.get("source_url"):
        final_url_block = (
            f"<dt>{html.escape(labels['pdf.item.final_url'])}</dt>"
            f"<dd><bdi>{html.escape(str(final_url_raw))}</bdi></dd>"
        )
    else:
        final_url_block = ""

    redirect_chain = list(item_view.get("redirect_chain") or [])
    redirect_chain_block = _format_redirect_chain(redirect_chain, labels)

    auth_domains = list(item_view.get("authenticated_domains") or [])
    if auth_domains:
        authenticated_domains = html.escape(", ".join(auth_domains))
    else:
        authenticated_domains = html.escape(
            labels["pdf.report.field.authenticated_domains_empty"]
        )

    description = item_view.get("description")
    if description:
        description_block = (
            f"<pre class='description'>{html.escape(str(description))}</pre>"
        )
    else:
        description_block = (
            f"<p class='description description-empty'>"
            f"{html.escape(labels['pdf.report.field.description_empty'])}"
            f"</p>"
        )

    tools = dict(item_view.get("tools") or {})

    capture = dict(item_view.get("capture") or {})
    capture_block = _format_capture_report(capture, labels)
    download_options_section = _format_download_options_section(
        item_view.get("download_options"), capture, labels,
    )

    # Audit-trail pointer (CLAUDE.md §6, §8). The per-item audit sidecar
    # is mutable and is written after this PDF is signed, so the section
    # carries a static pointer rather than a live entry count.
    stem_for_audit = str(
        item_view.get("stem")
        or item_view.get("manifest_filename", "").removesuffix(".manifest.pdf")
        or ""
    )
    audit_trail_section = (
        _format_audit_trail_section(stem_for_audit, labels)
        if stem_for_audit
        else ""
    )

    # Gallery section — only emitted for capture_kind == "gallery"
    # (CLAUDE.md §15 v0.5). Renders a grid of thumbnails (the actual
    # files in the per-item folder, referenced by relative path) plus a
    # short caption with the image count and gallery-dl extractor.
    capture_kind = item_view.get("capture_kind")
    gallery_thumbnails = list(item_view.get("gallery_thumbnails") or [])
    if capture_kind == "gallery" and gallery_thumbnails:
        gallery_count = int(item_view.get("gallery_count") or len(gallery_thumbnails))
        extractor = item_view.get("gallery_extractor")
        meta_label = labels["pdf.report.heading.gallery"]
        meta_caption_template = labels["pdf.report.field.gallery_meta"]
        meta_caption = (
            meta_caption_template
            .replace("{count}", str(gallery_count))
            .replace("{extractor}", str(extractor or labels["pdf.report.field.unknown"]))
        )
        # Resolve thumbnail paths under DOWNLOADS_DIR and feed WeasyPrint
        # ``file://`` URIs so it can read the bytes off disk. We cap the
        # rendered thumbnails at 20 so a 200-image gallery doesn't blow
        # up the report PDF — the manifest PDF lists every image
        # regardless, so no evidence is lost.
        thumb_cap = 20
        thumb_uris: list[str] = []
        for rel in gallery_thumbnails[:thumb_cap]:
            abs_path = (config.DOWNLOADS_DIR / rel).resolve()
            try:
                abs_path.relative_to(config.DOWNLOADS_DIR.resolve())
            except ValueError:
                continue  # path escapes DOWNLOADS_DIR; skip defensively
            thumb_uris.append(abs_path.as_uri())
        items_html = "".join(
            f"<li><img src=\"{html.escape(uri)}\" alt=\"\"></li>"
            for uri in thumb_uris
        )
        gallery_section = (
            f"<section class='gallery-section'>"
            f"<h2>{html.escape(meta_label)}</h2>"
            f"<p class='gallery-meta'>{html.escape(meta_caption)}</p>"
            f"<ul class='gallery-strip'>{items_html}</ul>"
            f"</section>"
        )
    else:
        gallery_section = ""

    # Media-context section (v7) — embeds the page_context_screenshot if
    # one was captured, framed on the most prominent video element so a
    # reviewer can see where the captured media lived on the page. Skipped
    # when the capture pipeline didn't find a candidate element.
    artifacts_map = dict(item_view.get("artifacts") or {})
    context_rel = artifacts_map.get("page_context_screenshot")
    if context_rel:
        try:
            ctx_abs = (config.DOWNLOADS_DIR / context_rel).resolve()
            ctx_abs.relative_to(config.DOWNLOADS_DIR.resolve())
            ctx_uri = ctx_abs.as_uri()
            ctx_caption = capture.get("media_context_selector") or ""
            media_context_section = (
                f"<section class='media-context-section'>"
                f"<h2>{html.escape(labels['pdf.report.heading.media_context'])}</h2>"
                f"<figure class='media-context-figure'>"
                f"<img src=\"{html.escape(ctx_uri)}\" alt=\"\">"
                f"<figcaption><code>{html.escape(str(ctx_caption))}</code></figcaption>"
                f"</figure>"
                f"</section>"
            )
        except (ValueError, OSError):
            media_context_section = ""
    else:
        media_context_section = ""

    # gallery-dl version row in the tools table — only when gallery-dl
    # ran or was attempted; otherwise the row is suppressed so video
    # captures keep the same compact 4-row table they had pre-v0.5.
    gallery_dl_version = tools.get("gallery_dl_version")
    if gallery_dl_version:
        tool_gallery_dl_row = (
            f"<tr><td class='label'>{html.escape(labels['pdf.report.field.gallery_dl_version'])}</td>"
            f"<td><code>{html.escape(str(gallery_dl_version))}</code></td></tr>"
        )
    else:
        tool_gallery_dl_row = ""

    # WARC-engine version rows (v0.6). Two complementary tools produce
    # the page.warc.gz: warcio (in-session CDP→WARC writer) and
    # browsertrix-crawler (subprocess fallback). Each row is only emitted
    # when its tool actually ran — a reviewer can tell which engine wrote
    # the WARC by which row appears (with the "WARC session" row in the
    # capture-report dl confirming).
    warcio_v = tools.get("warcio_version")
    if is_real_version(warcio_v):
        tool_warcio_row = (
            f"<tr><td class='label'>{html.escape(labels['pdf.report.field.warcio_version'])}</td>"
            f"<td><code>{html.escape(str(warcio_v))}</code></td></tr>"
        )
    else:
        tool_warcio_row = ""

    btx_v = tools.get("browsertrix_version")
    if is_real_version(btx_v):
        tool_browsertrix_row = (
            f"<tr><td class='label'>{html.escape(labels['pdf.report.field.browsertrix_version'])}</td>"
            f"<td><code>{html.escape(str(btx_v))}</code></td></tr>"
        )
    else:
        tool_browsertrix_row = ""

    footer_template = labels.get(
        "pdf.footer",
        "Capsule evidence export · {context} · page {page} of {pages}",
    )
    # Footer note cross-references the manifest PDF so a reviewer
    # can find the verifiable hash list.
    manifest_filename = str(item_view.get("manifest_filename") or "")
    footer_note = (
        labels["pdf.report.footer"].replace(
            "{filename}", manifest_filename or "manifest.pdf"
        )
    )

    rendered = template
    upload_date_raw = item_view.get("upload_date")
    upload_date = _format_iso_date(upload_date_raw) if upload_date_raw else dash
    uploader = item_view.get("uploader") or dash
    platform = item_view.get("platform") or dash

    duration_text = _format_duration(item_view.get("duration_seconds"), dash)

    for needle, replacement in {
        "{{lang}}": html.escape(lang),
        "{{dir}}": direction,
        "{{title}}": title,
        "{{source_url}}": source_url,
        "{{final_url_block}}": final_url_block,
        "{{captured_utc}}": captured_utc,
        "{{fingerprint}}": fp,
        "{{url_submitted}}": source_url,
        "{{url_final}}": html.escape(str(final_url_raw or item_view.get("source_url") or "")),
        "{{redirect_chain_block}}": redirect_chain_block,
        "{{platform}}": html.escape(str(platform)),
        "{{uploader}}": html.escape(str(uploader)),
        "{{upload_date}}": html.escape(str(upload_date)),
        "{{duration}}": html.escape(duration_text),
        "{{authenticated_domains}}": authenticated_domains,
        "{{description_block}}": description_block,
        "{{tool_app_version}}": html.escape(str(tools.get("app_version") or dash)),
        "{{tool_ytdlp_version}}": html.escape(str(tools.get("ytdlp_version") or dash)),
        "{{tool_chromium_version}}": html.escape(str(tools.get("chromium_version") or dash)),
        "{{tool_browsertrix_row}}": tool_browsertrix_row,
        "{{tool_warcio_row}}": tool_warcio_row,
        "{{capture_block}}": capture_block,
        "{{media_context_section}}": media_context_section,
        "{{download_options_section}}": download_options_section,
        "{{audit_trail_section}}": audit_trail_section,
        "{{gallery_section}}": gallery_section,
        "{{tool_gallery_dl_row}}": tool_gallery_dl_row,
        "{{footer_note}}": html.escape(footer_note),
        "{{label.brand_name}}": html.escape(labels["pdf.brand.name"]),
        "{{label.brand_tagline}}": html.escape(labels["pdf.brand.tagline.report"]),
        "{{label.source_url}}": html.escape(labels["pdf.item.source_url"]),
        "{{label.captured_utc}}": html.escape(labels["pdf.item.captured_utc"]),
        "{{label.signing_key}}": html.escape(labels["pdf.item.signing_key"]),
        "{{label.heading_provenance}}": html.escape(labels["pdf.report.heading.provenance"]),
        "{{label.heading_description}}": html.escape(labels["pdf.report.heading.description"]),
        "{{label.heading_tools}}": html.escape(labels["pdf.report.heading.tools"]),
        "{{label.heading_capture}}": html.escape(labels["pdf.report.heading.capture"]),
        "{{label.url_submitted}}": html.escape(labels["pdf.report.field.url_submitted"]),
        "{{label.url_final}}": html.escape(labels["pdf.report.field.url_final"]),
        "{{label.redirect_chain}}": html.escape(labels["pdf.report.field.redirect_chain"]),
        "{{label.platform}}": html.escape(labels["pdf.report.field.platform"]),
        "{{label.uploader}}": html.escape(labels["pdf.report.field.uploader"]),
        "{{label.upload_date}}": html.escape(labels["pdf.report.field.upload_date"]),
        "{{label.duration}}": html.escape(labels["pdf.report.field.duration"]),
        "{{label.authenticated_domains}}": html.escape(labels["pdf.report.field.authenticated_domains"]),
        "{{label.app_version}}": html.escape(labels["pdf.report.field.app_version"]),
        "{{label.ytdlp_version}}": html.escape(labels["pdf.report.field.ytdlp_version"]),
        "{{label.chromium_version}}": html.escape(labels["pdf.report.field.chromium_version"]),
        "{{label.footer_prefix}}": _footer_prefix_for(footer_template, case_name),
    }.items():
        rendered = rendered.replace(needle, replacement)
    return rendered


def _format_iso_date(raw: str | None) -> str:
    """Render yt-dlp's ``YYYYMMDD`` upload_date as ``YYYY-MM-DD``.

    Falls through unchanged for already-hyphenated input or anything
    else; the caller passes the raw value either way.
    """
    if not raw:
        return ""
    s = str(raw)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def render_item_report(
    *,
    case: cases.Case,
    item_view: Mapping[str, Any],
    lang: str = "en",
) -> bytes:
    """Render the per-item report PDF and return its bytes.

    Companion to ``render_item_manifest`` — the manifest PDF carries
    the file table with full forensic hashes; the report PDF carries
    the human-readable provenance, full untruncated description,
    tools/versions, and the capture report. Both PDFs are hashed and
    bound to ``meta.json`` via the same Ed25519 signature, so neither
    can be silently swapped after the fact (CLAUDE.md §7).

    ``item_view`` is the same shape as the dict the postprocess hook
    builds from ``CaptureInput`` + ``info_json``: it must include
    ``title``, ``source_url``, ``final_url``, ``redirect_chain``,
    ``captured_utc``, ``signing_key_fp``, ``platform``, ``uploader``,
    ``upload_date``, ``duration_seconds``, ``authenticated_domains``,
    ``description``, ``tools`` (dict of versions), ``capture`` (the
    capture-report dict), and optionally ``manifest_filename`` (for
    the cross-reference footer).
    """
    from weasyprint import HTML  # heavy import deferred

    html_doc = _render_item_report_html(
        case=case, item_view=item_view, lang=lang,
    )
    pdf = HTML(string=html_doc, base_url=str(config.APP_DIR)).write_pdf()
    return pdf
