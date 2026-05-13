"""Helper used during transcript curation: dump human-readable user/assistant
turns from a chosen session JSONL. Not shipped — exists so the transcript can
be regenerated/audited from the same source data.

Usage:
  python3 scripts/_session_dump.py <session_id> [--max-turns N] [--first-only]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LOGS_DIR = Path.home() / ".claude/projects/-Users-brian-Documents-ytdlp"


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("type")
                if t == "text":
                    parts.append(block.get("text", ""))
                elif t == "tool_use":
                    name = block.get("name", "tool")
                    inp = block.get("input", {})
                    if name == "TodoWrite":
                        parts.append(f"<{name}>")
                    elif name in {"Bash", "Edit", "Write", "Read"}:
                        cmd = inp.get("command") or inp.get("file_path") or inp.get("description") or ""
                        parts.append(f"<{name}: {str(cmd)[:80]}>")
                    else:
                        parts.append(f"<{name}>")
                elif t == "tool_result":
                    out = block.get("content", "")
                    if isinstance(out, list):
                        out = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in out)
                    parts.append(f"<result: {str(out)[:120].splitlines()[0] if out else ''}>")
        return "\n".join(p for p in parts if p)
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id")
    ap.add_argument("--max-turns", type=int, default=20)
    ap.add_argument("--first-only", action="store_true")
    ap.add_argument("--full", action="store_true", help="don't truncate any text")
    args = ap.parse_args()

    path = LOGS_DIR / f"{args.session_id}.jsonl"
    if not path.exists():
        print(f"not found: {path}", file=sys.stderr)
        return 1

    turns = []
    with path.open("r", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("type") not in {"user", "assistant"}:
                continue
            msg = rec.get("message", {})
            role = msg.get("role")
            text = _content_to_text(msg.get("content", "")).strip()
            if not text:
                continue
            ts = rec.get("timestamp", "")
            turns.append((ts, role, text))

    out_count = 0
    for ts, role, text in turns:
        if args.first_only and role != "user":
            if out_count >= 1:
                continue
        # Skip system-injected user turns (tool results, request-interrupted)
        if role == "user" and (text.startswith("<") or text.startswith("[Request interrupted")):
            continue
        if not args.full:
            text = text[:1200]
        sep = "=" * 80
        print(f"\n{sep}\n[{ts}] {role.upper()}\n{sep}")
        print(text)
        out_count += 1
        if out_count >= args.max_turns:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
