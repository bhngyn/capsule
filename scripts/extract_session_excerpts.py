"""Walk the Claude Code session-log directory for this project and emit a
chronological CSV index of every session, plus a per-session JSON dump of the
first user prompt + first assistant text response. Used to build the
docs/case-study/transcript.md curated case study.

Outputs:
  scripts/_session-index.csv     — sortable index (gitignored)
  scripts/_session-excerpts.json — first-prompt + first-response per session

Reproducibility note: this reads from ~/.claude/projects/<project-key>/.
The project key for this repo is `-Users-brian-Documents-ytdlp` (the working-
directory path with `/` replaced by `-`).
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path

LOGS_DIR = Path.home() / ".claude/projects/-Users-brian-Documents-ytdlp"
OUT_CSV = Path(__file__).parent / "_session-index.csv"
OUT_JSON = Path(__file__).parent / "_session-excerpts.json"

# Heuristic markers (used downstream when curating; we just label here).
REDIRECT_MARKERS = re.compile(r"^\s*(no|wait|stop|don'?t|actually|hold on|hmm)\b", re.I)
APPROVE_MARKERS = re.compile(r"\b(yes|perfect|exactly|great|nice|works|looks good|ship it)\b", re.I)
BUG_MARKERS = re.compile(r"\b(broken|doesn'?t work|crash|error|fails|bug|regression|wrong)\b", re.I)


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    name = block.get("name", "tool")
                    parts.append(f"<tool_use:{name}>")
                elif block.get("type") == "tool_result":
                    parts.append("<tool_result>")
        return "\n".join(p for p in parts if p)
    return ""


def _parse_session(path: Path) -> dict | None:
    first_user = None
    first_assistant = None
    pivots: list[dict] = []
    turn_count = 0
    user_count = 0
    assistant_count = 0
    timestamps: list[str] = []
    git_branches: set[str] = set()

    with path.open("r", errors="replace") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("type") not in {"user", "assistant"}:
                continue

            ts = rec.get("timestamp")
            if ts:
                timestamps.append(ts)
            gb = rec.get("gitBranch")
            if gb:
                git_branches.add(gb)

            msg = rec.get("message", {})
            role = msg.get("role")
            text = _content_to_text(msg.get("content", "")).strip()
            if not text:
                continue

            turn_count += 1
            if role == "user":
                user_count += 1
                if first_user is None and not text.startswith("<") and not text.startswith("[Request"):
                    # Skip system-injected user messages (tool results, request-interrupted, etc).
                    first_user = {"timestamp": ts, "text": text}
                # Pivot detection on substantive user messages only
                if not text.startswith("<") and len(text) >= 10:
                    flags = []
                    if REDIRECT_MARKERS.search(text):
                        flags.append("redirect")
                    if APPROVE_MARKERS.search(text):
                        flags.append("approve")
                    if BUG_MARKERS.search(text):
                        flags.append("bug")
                    if flags:
                        pivots.append({
                            "timestamp": ts,
                            "flags": flags,
                            "text": text[:400],
                        })
            elif role == "assistant":
                assistant_count += 1
                if first_assistant is None and len(text) > 30:
                    first_assistant = {"timestamp": ts, "text": text}

    if not first_user and not first_assistant:
        return None

    timestamps.sort()
    return {
        "session_id": path.stem,
        "path": str(path),
        "turns": turn_count,
        "user_turns": user_count,
        "assistant_turns": assistant_count,
        "started_at": timestamps[0] if timestamps else None,
        "ended_at": timestamps[-1] if timestamps else None,
        "git_branches": sorted(git_branches),
        "first_user": first_user,
        "first_assistant": first_assistant,
        "pivots": pivots[:30],
    }


def main() -> int:
    if not LOGS_DIR.exists():
        print(f"logs dir not found: {LOGS_DIR}")
        return 1

    sessions: list[dict] = []
    for path in sorted(LOGS_DIR.glob("*.jsonl")):
        rec = _parse_session(path)
        if rec is None:
            continue
        sessions.append(rec)

    sessions.sort(key=lambda r: r.get("started_at") or "")

    with OUT_CSV.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "started_at", "ended_at", "session_id", "turns",
            "user_turns", "branches", "redirect_count", "approve_count",
            "bug_count", "first_user_120",
        ])
        for rec in sessions:
            redirects = sum(1 for p in rec["pivots"] if "redirect" in p["flags"])
            approves = sum(1 for p in rec["pivots"] if "approve" in p["flags"])
            bugs = sum(1 for p in rec["pivots"] if "bug" in p["flags"])
            first = rec.get("first_user") or {}
            preview = (first.get("text") or "").replace("\n", " ")[:120]
            writer.writerow([
                rec.get("started_at") or "",
                rec.get("ended_at") or "",
                rec["session_id"],
                rec["turns"],
                rec["user_turns"],
                "|".join(rec["git_branches"]),
                redirects, approves, bugs,
                preview,
            ])

    with OUT_JSON.open("w") as fh:
        json.dump(sessions, fh, indent=2, ensure_ascii=False)

    print(f"wrote {OUT_CSV}  ({len(sessions)} sessions)")
    print(f"wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
