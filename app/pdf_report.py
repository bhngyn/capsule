"""Locale-aware PDF case report (CLAUDE.md §10).

WeasyPrint renders an HTML document to PDF — RTL-capable, supports
Noto Sans Arabic / Noto Sans CJK, embeds raster + vector assets. The
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
from pathlib import Path
from typing import Any, Mapping

from . import __version__, cases, config, i18n, signing

__all__ = ["render_case_report"]


_TEMPLATE_PATH = config.APP_DIR / "templates" / "case_report.html"


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
