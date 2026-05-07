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

    rows = [
        (labels["pdf.report.field.render_wait"], waits_html),
        (labels["pdf.report.field.blocked_requests"], blocked_html),
        (labels["pdf.report.field.banner_hide"], banner_html),
        (labels["pdf.report.field.tab_context"], _bool(cap.get("tab_context_used"))),
        (labels["pdf.report.field.report_lang"], _scalar(cap.get("report_lang"))),
    ]
    items = "\n".join(
        f"<dt>{html.escape(label)}</dt><dd>{value}</dd>"
        for label, value in rows
    )
    return f"<dl>{items}</dl>"


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
        "{{tool_browsertrix_version}}": html.escape(str(tools.get("browsertrix_version") or dash)),
        "{{capture_block}}": capture_block,
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
        "{{label.browsertrix_version}}": html.escape(labels["pdf.report.field.browsertrix_version"]),
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
