# Capsule — case study

How this project was built. Three deliverables, one folder.

| File                                                | What                                                                                                |
|-----------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| **[transcript.md](transcript.md)**                  | Twelve real exchanges from the Claude Code session logs, May 6–8 2026. Editor's notes per excerpt. |
| **[transcript.pdf](transcript.pdf)**                | PDF of `transcript.md`. A4 portrait, 19 pages, ~410 KB.                                             |
| **[methodology.md](methodology.md)**                | Twelve patterns generalized into a best-practices guide for non-technical decision-makers.          |
| **[methodology.pdf](methodology.pdf)**              | Editorial PDF of `methodology.md` with inline vector diagrams. A4 portrait, 19 pages, ~480 KB.      |
| [diagrams/](diagrams/)                              | The seven SVGs embedded throughout the document. Editable as plain text.                            |

For email distribution, send the two PDFs together — they share the same cover/byline format and reference each other.

## Reproducibility

Both the transcript and the PDF are regenerable from on-disk source.

```bash
# 1. Re-build the chronological session index from the JSONL logs at
#    ~/.claude/projects/-Users-brian-Documents-ytdlp/
.venv/bin/python3 scripts/extract_session_excerpts.py
# Outputs: scripts/_session-index.csv, scripts/_session-excerpts.json

# 2. Re-render both PDFs from the markdown sources + the SVGs.
.venv/bin/python3 scripts/render_case_study_pdf.py
# Outputs: docs/case-study/methodology.pdf, docs/case-study/transcript.pdf
```

The session-extraction script is **read-only** against the JSONL files — it
won't mutate your conversation history.

## Audience

- **transcript.md** is for engineers who want to see the actual prompt-and-response shape of the build.
- **methodology.md** and **methodology.pdf** are for project leads, ops, legal advisors, and product owners — anyone deciding whether to adopt this style of AI-assisted development for their own work.

## Caveats and redaction policy

Documented at the top of [transcript.md](transcript.md). In short:

- All quoted user prompts are verbatim.
- Claude responses are excerpted to first 15–30 lines of substantive text.
- Tool-use blocks are summarized.
- No cookie values, auth tokens, or API keys appear (the audit-log spec
  prohibits logging them; spot-grep confirmed).
- One known gap: the session in which CLAUDE.md was authored predates this
  project's log directory. Treated as itself instructive — see
  methodology.md §3.
