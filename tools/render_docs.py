"""Render docs/*.{en,ja,ar,es}.md into matching PDFs via WeasyPrint.

Usage:
    .venv/bin/python tools/render_docs.py            # render quickstart + user-guide
    .venv/bin/python tools/render_docs.py docs/foo.en.md  # render specific files

Output is written next to each input as ``{stem}.pdf`` (e.g. ``quickstart.en.md``
becomes ``quickstart.en.pdf``). Locale is detected from the ``.{en,ja,ar,es}.md``
suffix; Arabic pages render right-to-left with appropriate fallback fonts,
Japanese pages pick up Noto Sans CJK JP for full glyph coverage, and Spanish
pages use the same Latin font stack as English.

Requires ``markdown-it-py`` (``.venv/bin/pip install markdown-it-py``) and
``weasyprint`` (already in ``[evidence]``).
"""
from __future__ import annotations

import sys
from html import escape
from pathlib import Path

from markdown_it import MarkdownIt
from weasyprint import HTML

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGETS = [
    ROOT / "docs" / "quickstart.en.md",
    ROOT / "docs" / "quickstart.ar.md",
    ROOT / "docs" / "quickstart.ja.md",
    ROOT / "docs" / "quickstart.es.md",
    ROOT / "docs" / "user-guide.en.md",
    ROOT / "docs" / "user-guide.ar.md",
    # docs/dist-only/ holds Markdown sources that are only rendered into the
    # dist/Capsule/ bundle (install-{mac,windows}, launchers, verifying-
    # evidence). Each is rendered to a sibling .pdf and is then renamed by
    # tools/build-bundle.sh into the bundle layout.
    *sorted((ROOT / "docs" / "dist-only").glob("*.md")),
]


PRINT_CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm; }

html { font-size: 10.5pt; }
body {
  font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, "Noto Sans CJK JP", "Noto Sans Arabic", sans-serif;
  color: #18181b;
  line-height: 1.55;
}
html[dir="rtl"] body {
  font-family: "Geeza Pro", "Noto Sans Arabic", -apple-system, "Segoe UI", Arial, sans-serif;
  line-height: 1.75;
}

h1 { font-size: 22pt; margin: 0 0 0.4em; color: #0f766e; }
h2 { font-size: 14pt; margin-top: 1.4em; margin-bottom: 0.4em; border-bottom: 1px solid #e4e4e7; padding-bottom: 0.15em; }
h3 { font-size: 11.5pt; margin-top: 1.0em; margin-bottom: 0.25em; }
em { color: #52525b; font-style: italic; }
hr { border: 0; border-top: 1px solid #e4e4e7; margin: 1.4em 0; }

p, ul, ol { margin: 0.4em 0 0.6em; }
li { margin: 0.15em 0; }

a { color: #0f766e; text-decoration: none; word-break: break-word; }

code {
  font-family: "Menlo", "Consolas", "Courier New", monospace;
  font-size: 9.5pt;
  background: #f4f4f5;
  padding: 0.05em 0.3em;
  border-radius: 3px;
}
pre {
  font-family: "Menlo", "Consolas", "Courier New", monospace;
  background: #f4f4f5;
  padding: 0.7em 0.9em;
  border-radius: 5px;
  font-size: 9.5pt;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}
pre code { background: transparent; padding: 0; }

blockquote {
  margin: 0.6em 0;
  padding: 0.5em 0.9em;
  border-inline-start: 3px solid #0f766e;
  background: #f4f4f5;
  color: #3f3f46;
}

img { max-width: 100%; height: auto; border: 1px solid #e4e4e7; border-radius: 4px; margin: 0.6em 0; }

table { border-collapse: collapse; width: 100%; margin: 0.5em 0; font-size: 9.5pt; }
th, td { border: 1px solid #e4e4e7; padding: 0.35em 0.55em; text-align: start; vertical-align: top; }
th { background: #f4f4f5; }
"""


def render_one(src: Path) -> Path:
    """Render ``src`` (e.g. quickstart.en.md) to ``{stem}.pdf`` next to it."""
    stem = src.name
    if stem.endswith(".en.md"):
        locale = "en"
    elif stem.endswith(".ja.md"):
        locale = "ja"
    elif stem.endswith(".ar.md"):
        locale = "ar"
    elif stem.endswith(".es.md"):
        locale = "es"
    else:
        locale = "en"
    direction = "rtl" if locale == "ar" else "ltr"

    md = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("table")
    body_html = md.render(src.read_text(encoding="utf-8"))

    title = escape(src.stem)
    full_html = (
        f'<!doctype html><html lang="{locale}" dir="{direction}"><head>'
        f'<meta charset="utf-8"><title>{title}</title>'
        f"<style>{PRINT_CSS}</style></head><body>{body_html}</body></html>"
    )

    out = src.with_suffix(".pdf")
    HTML(string=full_html, base_url=str(src.parent)).write_pdf(str(out))
    return out


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        targets = [Path(p).resolve() for p in argv[1:]]
    else:
        targets = DEFAULT_TARGETS
    for src in targets:
        if not src.exists():
            print(f"  skip (missing): {src}", file=sys.stderr)
            continue
        out = render_one(src)
        print(f"  wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
