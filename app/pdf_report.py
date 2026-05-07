"""Locale-aware PDF case report (CLAUDE.md §10).

WeasyPrint renders an HTML document to PDF — RTL-capable, supports
Noto Sans Arabic, embeds raster + vector assets. The template lives in
``app/templates/case_report.html``; rendering is a one-liner.

The report is intentionally austere: the PDF will be read by editors,
opposing counsel, judges' clerks. No ornament, lots of whitespace, the
integrity story up front.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import sqlite3
from pathlib import Path
from typing import Any

from . import __version__, cases, config, signing

__all__ = ["render_case_report"]


_TEMPLATE_PATH = config.APP_DIR / "templates" / "case_report.html"


def _format_bytes(n: int | None) -> str:
    if not n:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n = n / 1024
    return f"{n:.1f} PB"


def _format_dt(s: str | None) -> str:
    if not s:
        return "—"
    return s.replace("T", " ").split("+")[0]


def _item_html(row: sqlite3.Row) -> str:
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
        <dt>Source URL</dt>      <dd><bdi>{url}</bdi></dd>
        <dt>Captured (UTC)</dt>  <dd>{capture_date}</dd>
        <dt>Upload date</dt>     <dd>{upload_date}</dd>
        <dt>Size</dt>            <dd>{size}</dd>
        <dt>SHA-256</dt>         <dd><code>{sha}</code></dd>
        <dt>MD5</dt>             <dd><code>{md5}</code></dd>
        <dt>Signing key</dt>     <dd><code>{fp}</code></dd>
      </dl>
      <div class="artifacts">
        <h3>Artifacts</h3>
        <ul>{artifact_lis}</ul>
      </div>
    </article>
    """


def _render_html(case: cases.Case, items: list[sqlite3.Row]) -> str:
    fp = signing.fingerprint(signing.ensure_keypair().public)
    when = html.escape(_dt.datetime.now(_dt.timezone.utc).isoformat())
    name = html.escape(case.name)
    slug = html.escape(case.slug)
    desc = html.escape(case.description or "")
    items_html = "\n".join(_item_html(row) for row in items)
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("{{case_name}}", name)
        .replace("{{case_slug}}", slug)
        .replace("{{case_description}}", desc)
        .replace("{{case_status}}", html.escape(case.status))
        .replace("{{exported_at}}", when)
        .replace("{{fingerprint}}", html.escape(fp))
        .replace("{{app_version}}", html.escape(__version__))
        .replace("{{item_count}}", str(len(items)))
        .replace("{{items}}", items_html)
    )


def render_case_report(*, case: cases.Case, items: list[sqlite3.Row]) -> bytes:
    """Render the per-case PDF and return its bytes."""
    from weasyprint import HTML  # heavy import deferred

    html_doc = _render_html(case, items)
    pdf = HTML(string=html_doc, base_url=str(config.APP_DIR)).write_pdf()
    return pdf
