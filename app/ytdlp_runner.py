"""Async wrapper around the ``yt-dlp`` CLI (CLAUDE.md §5, §13.13).

Why a subprocess and not the Python module:

* yt-dlp ships frequent updates that the user can apply at runtime
  (CLAUDE.md §4.4). Running it as a subprocess keeps a stale Python import
  out of the way — ``pip install --upgrade yt-dlp`` becomes effective
  immediately.
* The progress JSON via ``--progress-template`` is a stable contract; the
  Python API's progress hooks change between releases.

Flags pinned by CLAUDE.md §5 (preservation rule):
* ``--no-embed-metadata --no-embed-thumbnail --no-embed-subs`` — keep the
  source media bytes intact.
* ``--write-info-json --write-description --write-thumbnail`` — preserve
  full original metadata as sidecars.
* ``--no-mtime`` — we control timestamps ourselves.
* ``--newline`` — line-buffered progress.

The runner does **not** write to the database or the audit log; that is
``postprocess``'s job. Keeping the runner pure means tests don't need a DB
just to exercise progress parsing.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "RunResult",
    "ProgressUpdate",
    "run",
    "version",
    "PROGRESS_TEMPLATE",
    "DEFAULT_SOCKET_TIMEOUT_S",
]


# Emit one JSON object per progress callback. ``%(progress)j`` is the
# documented public surface for this; other keys (``%(info)j``) change
# between releases.
PROGRESS_TEMPLATE = "%(progress)j"

# Universal default; profiles override (Slow=60, Fast=20). High enough that
# a high-latency VPN doesn't trip on TLS handshake; low enough that a truly
# stalled connection doesn't hold a slot for an hour.
DEFAULT_SOCKET_TIMEOUT_S = 30


@dataclass(frozen=True)
class ProgressUpdate:
    status: str  # 'downloading' | 'finished' | 'error' | 'postprocess'
    downloaded_bytes: int | None
    total_bytes: int | None
    speed: float | None
    eta: int | None
    filename: str | None
    raw: dict
    # UI-only label describing which file/step yt-dlp is on right now
    # (video stream, audio stream, thumbnail, merging, ...). Not persisted
    # — see CLAUDE.md §1: disk artifacts are identical across UI affordances.
    sub_status: str | None = None


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    info: dict | None  # parsed info.json, if one was written
    produced_files: list[Path] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _ytdlp_executable() -> str:
    found = shutil.which("yt-dlp")
    if not found:
        raise RuntimeError("yt-dlp executable not found on PATH")
    return found


async def version() -> str:
    """Return the installed ``yt-dlp`` version string."""
    proc = await asyncio.create_subprocess_exec(
        _ytdlp_executable(),
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return out.decode().strip()


_THUMBNAIL_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_SUBTITLE_EXTS = (".vtt", ".srt", ".ass", ".ssa", ".ttml")
_INFO_SUFFIXES = (".info.json", ".description")


def _classify_substatus(raw: dict, filename: str | None) -> str:
    """Classify a yt-dlp progress event into a user-readable sub-step.

    yt-dlp downloads several files per capture (video stream, audio
    stream, thumbnail, subtitles) and each fires its own 0→100% progress
    sequence. Without context, the UI bar appears to "loop." This helper
    inspects ``info_dict.vcodec``/``acodec`` and the filename to label
    each sequence so the user sees forward motion as text under the bar.

    Returns one of: ``"video"``, ``"audio"``, ``"combined"``,
    ``"thumbnail"``, ``"subtitles"``, ``"info_json"``, ``"unknown"``.
    """
    info = raw.get("info_dict") if isinstance(raw, dict) else None
    if not isinstance(info, dict):
        info = {}

    vcodec = info.get("vcodec")
    acodec = info.get("acodec")
    has_video = bool(vcodec) and vcodec != "none"
    has_audio = bool(acodec) and acodec != "none"

    name = (filename or "").lower()
    # Strip yt-dlp's transient suffix so 'foo.mp4.part' classifies like 'foo.mp4'.
    if name.endswith(".part"):
        name = name[: -len(".part")]
    if name.endswith(".ytdl"):
        name = name[: -len(".ytdl")]

    if name.endswith(_INFO_SUFFIXES):
        return "info_json"
    if name.endswith(_SUBTITLE_EXTS):
        return "subtitles"

    if has_video and not has_audio:
        return "video"
    if has_audio and not has_video:
        return "audio"
    if has_video and has_audio:
        return "combined"

    # No codec hint — fall back to extension. Thumbnail-format extensions
    # without video/audio codec context are reliably image sidecars.
    if name.endswith(_THUMBNAIL_EXTS):
        return "thumbnail"

    return "unknown"


def _parse_progress_line(line: str) -> ProgressUpdate | None:
    """Try to parse one stdout line as a progress JSON object.

    Returns ``None`` for non-progress output (e.g. yt-dlp's banner). The
    caller is responsible for keeping non-progress lines for diagnostics.
    """
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "status" not in obj:
        return None
    filename = obj.get("filename")
    return ProgressUpdate(
        status=str(obj.get("status", "")),
        downloaded_bytes=_int_or_none(obj.get("downloaded_bytes")),
        total_bytes=_int_or_none(
            obj.get("total_bytes") or obj.get("total_bytes_estimate")
        ),
        speed=_float_or_none(obj.get("speed")),
        eta=_int_or_none(obj.get("eta")),
        filename=filename,
        raw=obj,
        sub_status=_classify_substatus(obj, filename),
    )


# Stdout markers yt-dlp prints when it hands a file off to ffmpeg. These
# do not flow through ``--progress-template`` (postprocessor steps emit
# no JSON progress), so the runner synthesizes one ``ProgressUpdate`` per
# marker so the UI can flip its label from "Downloading audio" to
# "Merging video and audio" without staring at a stalled bar.
def _detect_postprocess_substatus(line: str) -> str | None:
    s = line.strip()
    if not s:
        return None
    # yt-dlp wraps postprocessor messages in [name] prefixes.
    if s.startswith("[Merger]") or "Merging formats into" in s:
        return "merging"
    if s.startswith("[ExtractAudio]"):
        return "extract_audio"
    if s.startswith("[ffmpeg]") and "Merging" in s:
        return "merging"
    return None


def _int_or_none(v: object) -> int | None:
    try:
        return int(v) if v is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _float_or_none(v: object) -> float | None:
    try:
        return float(v) if v is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _build_argv(
    url: str,
    *,
    case_dir: Path,
    cookies_file: Path | None,
    format_spec: str | None,
    extra_args: list[str] | None,
    socket_timeout_s: int = DEFAULT_SOCKET_TIMEOUT_S,
    limit_rate_kbps: int | None = None,
    proxy_url: str | None = None,
) -> list[str]:
    """Build the yt-dlp command line. Visible separately for tests.

    Resilience flags (CLAUDE.md plan §U2) are always passed:

    * ``--continue`` — resume partial ``.part`` files instead of restarting
    * ``--retries infinite`` / ``--fragment-retries infinite`` — keep retrying
      transient HTTP errors and individual HLS/DASH fragments
    * ``--file-access-retries 10`` — bounded retry for transient filesystem
      contention (Windows lock-on-write is the main offender)
    * ``--retry-sleep linear=1:30:5`` — start with 1 s, add 5 s per attempt,
      cap at 30 s; gentle on the source while still making progress
    * ``--socket-timeout`` — caller-tunable; profiles override (Slow=60, Fast=20)

    ``limit_rate_kbps`` and ``proxy_url`` are accepted here so the profile
    layer (still on its way) can plug into the same surface; today the
    orchestrator passes only the resilience flags.
    """
    argv: list[str] = [
        _ytdlp_executable(),
        "--no-embed-metadata",
        "--no-embed-thumbnail",
        "--no-embed-subs",
        "--write-info-json",
        "--write-description",
        "--write-thumbnail",
        "--no-mtime",
        "--newline",
        "--no-progress",  # silence the human-readable progress bar; we use the template
        "--progress",  # but keep the structured progress
        "--progress-template",
        PROGRESS_TEMPLATE,
        "--continue",
        "--retries", "infinite",
        "--fragment-retries", "infinite",
        "--file-access-retries", "10",
        "--retry-sleep", "linear=1:30:5",
        "--socket-timeout", str(socket_timeout_s),
        "--paths",
        f"home:{case_dir}",
        # Temp filenames; postprocess renames everything to canonical form.
        "--output",
        "%(id)s.%(ext)s",
    ]
    if limit_rate_kbps is not None and limit_rate_kbps > 0:
        argv += ["--limit-rate", f"{limit_rate_kbps}K"]
    if proxy_url:
        argv += ["--proxy", proxy_url]
    if cookies_file is not None:
        argv += ["--cookies", str(cookies_file)]
    if format_spec:
        argv += ["--format", format_spec]
    if extra_args:
        argv += list(extra_args)
    argv.append(url)
    return argv


async def run(
    url: str,
    *,
    case_dir: Path,
    cookies_file: Path | None = None,
    format_spec: str | None = None,
    progress_queue: asyncio.Queue | None = None,
    extra_args: list[str] | None = None,
    executable: str | None = None,
    env: Mapping[str, str] | None = None,
    socket_timeout_s: int = DEFAULT_SOCKET_TIMEOUT_S,
    limit_rate_kbps: int | None = None,
    proxy_url: str | None = None,
    proc_holder: list | None = None,
) -> RunResult:
    """Invoke yt-dlp and return a ``RunResult``.

    Progress JSON lines are pushed onto ``progress_queue`` if provided; one
    sentinel ``None`` is pushed at the end so consumers can drain. The
    function does not raise on a non-zero exit code — the caller inspects
    ``returncode`` and ``stderr`` (and feeds the latter to ``errors.classify``).
    """
    case_dir.mkdir(parents=True, exist_ok=True)

    before = {p.name for p in case_dir.iterdir() if p.is_file()}

    argv = _build_argv(
        url,
        case_dir=case_dir,
        cookies_file=cookies_file,
        format_spec=format_spec,
        extra_args=extra_args,
        socket_timeout_s=socket_timeout_s,
        limit_rate_kbps=limit_rate_kbps,
        proxy_url=proxy_url,
    )
    if executable:
        argv[0] = executable

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env) if env is not None else None,
    )
    if proc_holder is not None:
        # Plan §U4: surfaces the live subprocess so the orchestrator can
        # SIGTERM on user pause/cancel. yt-dlp catches SIGTERM and leaves
        # ``.part`` files in place so ``--continue`` can resume them.
        proc_holder.append(proc)

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    last_pp_substatus: str | None = None

    async def _drain_stdout() -> None:
        nonlocal last_pp_substatus
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                return
            line = raw.decode(errors="replace")
            stdout_chunks.append(line)
            update = _parse_progress_line(line)
            if update is not None:
                if progress_queue is not None:
                    await progress_queue.put(update)
                continue
            # Not JSON progress — sniff postprocessor markers so the UI
            # label transitions from the last download step to "Merging…"
            # / "Extracting audio…" instead of looking stalled.
            pp = _detect_postprocess_substatus(line)
            if pp is not None and pp != last_pp_substatus and progress_queue is not None:
                last_pp_substatus = pp
                await progress_queue.put(
                    ProgressUpdate(
                        status="postprocess",
                        downloaded_bytes=None,
                        total_bytes=None,
                        speed=None,
                        eta=None,
                        filename=None,
                        raw={"postprocess_marker": line.strip()},
                        sub_status=pp,
                    )
                )

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                return
            stderr_chunks.append(raw.decode(errors="replace"))

    await asyncio.gather(_drain_stdout(), _drain_stderr())
    rc = await proc.wait()

    if progress_queue is not None:
        await progress_queue.put(None)

    after = {p.name for p in case_dir.iterdir() if p.is_file()}
    produced = sorted(case_dir / name for name in (after - before))

    info = _read_info_json(produced)

    return RunResult(
        returncode=rc,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
        info=info,
        produced_files=produced,
    )


def _read_info_json(produced: list[Path]) -> dict | None:
    for p in produced:
        if p.name.endswith(".info.json"):
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
    return None
