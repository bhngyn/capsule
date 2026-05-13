"""Render the docs/case-study/ markdown documents to print-quality PDFs with
inline SVG diagrams.

Two PDFs are produced from the same template:

  docs/case-study/methodology.pdf   ← from methodology.md (12 §-numbered sections)
  docs/case-study/transcript.pdf    ← from transcript.md  (14 §-numbered sections)

Usage:
  /Users/brian/Documents/ytdlp/.venv/bin/python3 scripts/render_case_study_pdf.py
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import markdown as md_lib
from weasyprint import HTML

ROOT = Path(__file__).resolve().parent.parent
CASE = ROOT / "docs" / "case-study"
TEMPLATE = CASE / "_pdf-template.html"
DIAGRAMS = CASE / "diagrams"

BYLINE = "Brian"  # Edit here to change the byline on both PDF covers.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _inline_svg(name: str) -> str:
    path = DIAGRAMS / name
    if not path.exists():
        raise FileNotFoundError(path)
    text = path.read_text()
    return re.sub(r"<\?xml[^?]*\?>", "", text).strip()


def _meta_grid(pairs: list[tuple[str, str]]) -> str:
    rows = []
    for k, v in pairs:
        rows.append(f"      <dt>{k}</dt><dd>{v}</dd>")
    return "\n".join(rows)


def _toc_items(items: list[tuple[str, str]]) -> str:
    rows = []
    for num, title in items:
        rows.append(
            f'      <li><span class="num">{num}</span>'
            f'<span class="title">{title}</span></li>'
        )
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Markdown -> HTML body
# ---------------------------------------------------------------------------

def _render_markdown(md_text: str, *, drop_first_h1_re: str | None = None) -> str:
    """Convert one of the case-study markdown files to HTML and post-process so
    that:

    - The leading top-level H1 (the doc title) is removed (the cover repeats it).
    - SVG <img> references become inline <figure><svg>…</svg></figure> blocks.
    - Section H2s prefixed with "§N" are promoted to H1 so each section starts
      on a new page in the PDF.
    """
    html = md_lib.markdown(
        md_text,
        extensions=["extra", "tables", "sane_lists", "toc", "fenced_code"],
        extension_configs={"toc": {"toc_depth": "2-3"}},
    )

    if drop_first_h1_re:
        html = re.sub(drop_first_h1_re, "", html, count=1)

    # ![alt](diagrams/foo.svg) — already <img>; replace with inline-SVG figure.
    def _img_to_figure(match: re.Match) -> str:
        src = match.group("src")
        if not src.startswith("diagrams/"):
            return match.group(0)
        name = src.split("/", 1)[1]
        try:
            svg = _inline_svg(name)
        except FileNotFoundError:
            return match.group(0)
        alt = match.group("alt") or ""
        caption = f"<figcaption>{alt}</figcaption>" if alt else ""
        return f'<figure class="figure-block">{svg}{caption}</figure>'

    html = re.sub(
        r'<img alt="(?P<alt>[^"]*)" src="(?P<src>[^"]+)"\s*/?>',
        _img_to_figure,
        html,
    )
    html = re.sub(
        r'<p>(\s*<figure[^>]*>.*?</figure>\s*)</p>',
        r"\1",
        html,
        flags=re.S,
    )

    # Promote §-prefixed H2 to H1 so each section starts a new page.
    html = re.sub(
        r'<h2(?P<attrs>[^>]*)>(?P<inner>§\d+\b[^<]*)</h2>',
        r"<h1\g<attrs>>\g<inner></h1>",
        html,
    )

    # The methodology doc has a "Closing" section we wrap into a callout box.
    html = re.sub(
        r'<h2[^>]*>Closing</h2>(?P<body>(?:.|\n)*?)(?=<hr ?/?>|$)',
        r'<div class="closing"><h2>Closing</h2>\g<body></div>',
        html,
    )

    return html


# ---------------------------------------------------------------------------
# Document configurations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DocConfig:
    source_md: Path
    output_pdf: Path
    eyebrow: str
    title_headline: str
    title_subhead: str
    subtitle: str
    cover_diagram: str            # filename in diagrams/
    meta_pairs: list[tuple[str, str]]
    toc_items: list[tuple[str, str]]
    cover_footer: str
    running_header: str
    drop_first_h1_re: str | None
    footer_path: str


METHODOLOGY = DocConfig(
    source_md=CASE / "methodology.md",
    output_pdf=CASE / "methodology.pdf",
    eyebrow="Case study · vibe coding",
    title_headline="How Capsule got built",
    title_subhead="A methodology for spec-first AI-assisted development",
    subtitle=(
        "A real, shipping forensic-grade web-evidence capture tool, built in "
        "roughly 36 hours by one person collaborating with Claude Code. This "
        "document distils the patterns that made it possible — for the "
        "decision-makers in your organization who don't write the code."
    ),
    cover_diagram="capture-pipeline.svg",
    meta_pairs=[
        ("Project", "Capsule"),
        ("Tagline", "Capture the web, with proof."),
        ("Build window", "2026-05-06 → 2026-05-08 (≈ 36 hours wall clock)"),
        ("Versions shipped", "v0.1 → v0.7"),
        ("Audience", "Investigators (researchers, journalists, lawyers, "
                     "legal-discovery practitioners)"),
        ("Companion", "transcript.pdf — twelve real exchanges from the build"),
    ],
    toc_items=[
        ("§1", "What &quot;vibe coding&quot; actually means here"),
        ("§2", "The CLAUDE.md constitution"),
        ("§3", "Spec-first, then iterate"),
        ("§4", "Docker as the install story"),
        ("§5", "Self-healing launchers"),
        ("§6", "Reproducible builds"),
        ("§7", "Versioning as a practice"),
        ("§8", "Tamper-evident audit trail by default"),
        ("§9", "Schema versioning, additive-only"),
        ("§10", "Iteration loops with AI: four moves"),
        ("§11", "What gets pruned, comes back"),
        ("§12", "Best-practices checklist"),
    ],
    cover_footer="Capsule case study · methodology · A4 portrait",
    running_header="Capsule — case study · methodology",
    drop_first_h1_re=r'^<h1[^>]*>How Capsule got built[^<]*</h1>\s*',
    footer_path="docs/case-study/methodology.md",
)

TRANSCRIPT = DocConfig(
    source_md=CASE / "transcript.md",
    output_pdf=CASE / "transcript.pdf",
    eyebrow="Case study · transcript",
    title_headline="Capsule — development transcript",
    title_subhead="Twelve real exchanges from the build",
    subtitle=(
        "Curated excerpts from the Claude Code session logs that produced "
        "Capsule between 2026-05-06 14:31 UTC and 2026-05-08 03:03 UTC. Every "
        "user prompt is verbatim. Every Claude response is excerpted from a "
        "real session. Companion to methodology.pdf."
    ),
    cover_diagram="version-timeline.svg",
    meta_pairs=[
        ("Project", "Capsule"),
        ("Source",  "~/.claude/projects/-Users-brian-Documents-ytdlp/"),
        ("Sessions on disk", "35 (selection: 12)"),
        ("Build window", "2026-05-06 → 2026-05-08 (≈ 36 hours)"),
        ("Reproducibility", "scripts/extract_session_excerpts.py"),
        ("Companion", "methodology.pdf — twelve generalized patterns"),
    ],
    toc_items=[
        ("§1", "The &quot;investigators&quot; pivot"),
        ("§2", "Plan from HANDOFF.md"),
        ("§3", "&quot;Strip away everything but downloading&quot;"),
        ("§4", "&quot;the logo looks like a button&quot;"),
        ("§5", "First real-world bug"),
        ("§6", "End-of-day-1 code review"),
        ("§7", "&quot;remove the court setting&quot;"),
        ("§8", "Storage cleanup → Japanese → Spanish"),
        ("§9", "Smallest possible distribution"),
        ("§10", "&quot;handle duplicate videos better&quot;"),
        ("§11", "&quot;handle images as much as possible&quot;"),
        ("§12", "When the launcher failed on Apple Silicon"),
        ("§13", "&quot;harden the page preservation&quot;"),
        ("§14", "&quot;ways to modify the download… restart…&quot;"),
    ],
    cover_footer="Capsule case study · transcript · A4 portrait",
    running_header="Capsule — case study · transcript",
    drop_first_h1_re=r'^<h1[^>]*>Capsule\s*&mdash;\s*development transcript</h1>\s*',
    footer_path="docs/case-study/transcript.md",
)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(cfg: DocConfig) -> None:
    if not cfg.source_md.exists():
        raise FileNotFoundError(cfg.source_md)

    md_text = cfg.source_md.read_text()
    body = _render_markdown(md_text, drop_first_h1_re=cfg.drop_first_h1_re)

    template = TEMPLATE.read_text()
    substitutions: dict[str, str] = {
        "__TITLE__": cfg.title_headline,
        "__EYEBROW__": cfg.eyebrow,
        "__TITLE_HEADLINE__": cfg.title_headline,
        "__TITLE_SUBHEAD__": cfg.title_subhead,
        "__SUBTITLE__": cfg.subtitle,
        "__BYLINE__": f" {BYLINE}",
        "__META_GRID__": _meta_grid(cfg.meta_pairs),
        "__COVER_DIAGRAM__": _inline_svg(cfg.cover_diagram),
        "__COVER_FOOTER__": cfg.cover_footer,
        "__RUNNING_HEADER__": cfg.running_header,
        "__FOOTER_PATH__": cfg.footer_path,
        "__TOC_ITEMS__": _toc_items(cfg.toc_items),
        "__BODY__": body,
    }
    full_html = template
    for key, value in substitutions.items():
        full_html = full_html.replace(key, value)

    debug_html = cfg.output_pdf.with_suffix(".rendered.html")
    debug_html.write_text(full_html)

    HTML(string=full_html, base_url=str(CASE)).write_pdf(str(cfg.output_pdf))
    size_kb = cfg.output_pdf.stat().st_size // 1024
    print(f"  wrote {cfg.output_pdf.relative_to(ROOT)}  ({size_kb} KB)")


def main() -> int:
    print("rendering case-study PDFs:")
    render(METHODOLOGY)
    render(TRANSCRIPT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
