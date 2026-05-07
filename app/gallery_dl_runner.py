"""Async wrapper around the ``gallery-dl`` CLI (CLAUDE.md §15 "Gallery pass v0.5").

A second producer alongside :mod:`app.ytdlp_runner`. Used as a Phase-3
fallback when yt-dlp finds no media — e.g. Twitter image threads, Imgur
albums, Pixiv posts, DeviantArt pages, Reddit galleries.

Why a subprocess and not the Python module:

* gallery-dl ships frequent updates the user can apply at runtime
  (CLAUDE.md §4.4); a stale Python import would mask ``pip install
  --upgrade gallery-dl``.
* Keeps the runner contract identical to yt-dlp's: a thin process
  wrapper with no DB / audit-log side effects.

Flags pinned:

* ``-d <work_dir>`` — base directory; gallery-dl chooses its own
  per-extractor subdirs inside (postprocess walks the dir recursively
  and renames everything).
* ``--write-metadata`` — one JSON sidecar per image, the per-artifact
  provenance investigators need (CLAUDE.md §5 preservation rule).
* ``--write-info-json`` — gallery-level metadata (extractor, source URL,
  user, etc.) as a single sibling JSON.
* ``--no-mtime`` — we control timestamps ourselves.
* ``--range 1-{max_items}`` — bounded by case settings; default cap is
  enforced by the orchestrator (CLAUDE.md §15 Gallery pass v0.5).
* ``--cookies <path>`` — same Netscape file as yt-dlp / Playwright /
  browsertrix (CLAUDE.md §11).
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
    "DEFAULT_SOCKET_TIMEOUT_S",
    "DEFAULT_MAX_ITEMS",
    "IMAGE_EXTS",
]


# Mirrors :data:`app.ytdlp_runner.DEFAULT_SOCKET_TIMEOUT_S` so the same
# profile knob applies to both runners.
DEFAULT_SOCKET_TIMEOUT_S = 30

# Plan §"Image cap": investigators raise this per-case in
# ``case.settings_json.gallery_max_items`` for full-profile sweeps.
DEFAULT_MAX_ITEMS = 200

# Image extensions gallery-dl can produce. The set is conservative — anything
# else falls into the metadata-sidecar bucket so postprocess can still hash
# and preserve it. Keep in sync with the per-image JSON suffixes below.
IMAGE_EXTS: frozenset[str] = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
        ".avif",
        ".jfif",
        # Animated formats gallery-dl emits for some sources (Pixiv ugoira,
        # DeviantArt clips). Treated as images for forensic purposes — they
        # ARE the artifact.
        ".mp4",
        ".webm",
        ".mkv",
    }
)


@dataclass(frozen=True)
class ProgressUpdate:
    """A single gallery-dl per-file progress event.

    Distinct from yt-dlp's byte-level progress — gallery-dl reports one
    line per *completed* file, with no upfront count or per-file byte
    progress. The UI shows ``downloaded_count`` / "downloading image N…"
    instead of a percentage bar.
    """

    status: str  # 'downloading' | 'finished'
    filename: str | None
    downloaded_count: int
    raw: dict
    sub_status: str | None = None


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    # gallery-dl ``info.json`` parsed when ``--write-info-json`` is set.
    # Used by postprocess to derive ``platform`` (info["category"]) and the
    # gallery's title / author.
    info: dict | None
    produced_files: list[Path] = field(default_factory=list)
    # Convenience split — postprocess wants images and metadata separately.
    image_files: list[Path] = field(default_factory=list)
    metadata_files: list[Path] = field(default_factory=list)
    # gallery-dl's category (its extractor key, lower-case) when known.
    extractor: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _gallerydl_executable() -> str:
    found = shutil.which("gallery-dl")
    if not found:
        raise RuntimeError("gallery-dl executable not found on PATH")
    return found


async def version() -> str:
    """Return the installed ``gallery-dl`` version string."""
    proc = await asyncio.create_subprocess_exec(
        _gallerydl_executable(),
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return out.decode().strip()


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def _is_metadata(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(".json"):
        return True
    if name.endswith(".txt"):  # --write-tags
        return True
    return False


def _build_argv(
    url: str,
    *,
    work_dir: Path,
    cookies_file: Path | None,
    max_items: int,
    extra_args: list[str] | None,
    socket_timeout_s: int = DEFAULT_SOCKET_TIMEOUT_S,
    proxy_url: str | None = None,
    executable: str | None = None,
) -> list[str]:
    """Build the gallery-dl command line. Visible separately for tests.

    ``executable`` short-circuits the PATH lookup. The ``run`` wrapper
    threads the test override through here so ``_build_argv`` is
    independently exercisable without requiring gallery-dl on PATH.
    """
    argv: list[str] = [
        executable or _gallerydl_executable(),
        # gallery-dl logs file-by-file completion to stdout by default;
        # ``-v`` is too chatty (per-request HTTP), ``-q`` swallows the path
        # lines we use for progress. Default verbosity is right.
        "--write-metadata",
        "--write-info-json",
        "--no-mtime",
        "-d",
        str(work_dir),
        "--range",
        f"1-{max_items}",
        # gallery-dl's HTTP socket timeout knob is a config option, not a
        # CLI flag, so we plumb it via ``-o``.
        "-o",
        f"timeout={socket_timeout_s}",
    ]
    if proxy_url:
        argv += ["--proxy", proxy_url]
    if cookies_file is not None:
        argv += ["--cookies", str(cookies_file)]
    if extra_args:
        argv += list(extra_args)
    argv.append(url)
    return argv


def _parse_progress_line(line: str, work_dir: Path) -> ProgressUpdate | None:
    """Parse one gallery-dl stdout line as a per-file completion event.

    gallery-dl prints the absolute or relative path of each downloaded file
    on its own line, e.g.::

        /tmp/job/imgur/abcd/01.jpg
        /tmp/job/imgur/abcd/02.png

    Lines that don't look like image paths (banners, errors echoed to
    stdout) return ``None`` — the caller keeps them for diagnostics.
    """
    s = line.rstrip()
    if not s or s.startswith("#") or s.startswith("["):
        return None
    # Accept absolute or relative paths inside the work dir; ``-d`` makes
    # gallery-dl always emit something rooted at ``work_dir``.
    p = Path(s)
    if not p.is_absolute():
        p = work_dir / p
    try:
        in_work = p.is_relative_to(work_dir)
    except (AttributeError, ValueError):
        in_work = False
    if not in_work:
        return None
    if not _is_image(p):
        return None
    return ProgressUpdate(
        status="downloading",
        filename=p.name,
        downloaded_count=0,  # caller increments
        raw={"path": str(p)},
        sub_status="gallery_image",
    )


def _walk_produced(work_dir: Path) -> list[Path]:
    """Return every regular file under ``work_dir``, sorted.

    gallery-dl drops images into per-extractor subdirectories
    (``{category}/{user}/...``); postprocess walks the whole tree to pick
    them up. Mirrors how :mod:`app.ytdlp_runner` returns ``produced_files``,
    with the difference that gallery-dl's tree is genuinely nested.
    """
    if not work_dir.exists():
        return []
    return sorted(p for p in work_dir.rglob("*") if p.is_file())


def _read_info_json(produced: list[Path]) -> dict | None:
    """Read gallery-dl's ``info.json`` (gallery-level) if one exists.

    gallery-dl writes ``info.json`` next to each gallery's first file when
    ``--write-info-json`` is set. We pick the first one found; in practice
    a single gallery URL produces exactly one.
    """
    for p in produced:
        if p.name == "info.json":
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
    return None


def _extractor_from_info(info: dict | None) -> str | None:
    if not isinstance(info, dict):
        return None
    cat = info.get("category")
    if isinstance(cat, str) and cat:
        return cat.lower()
    return None


async def run(
    url: str,
    *,
    work_dir: Path,
    cookies_file: Path | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
    progress_queue: asyncio.Queue | None = None,
    extra_args: list[str] | None = None,
    executable: str | None = None,
    env: Mapping[str, str] | None = None,
    socket_timeout_s: int = DEFAULT_SOCKET_TIMEOUT_S,
    proxy_url: str | None = None,
    proc_holder: list | None = None,
) -> RunResult:
    """Invoke gallery-dl and return a :class:`RunResult`.

    Per-file progress events are pushed onto ``progress_queue`` if
    provided; one sentinel ``None`` is pushed at the end so consumers can
    drain. Like the yt-dlp runner, this function does not raise on
    non-zero exit — the caller inspects ``returncode`` + ``stderr`` and
    feeds the latter to :func:`app.errors.classify`.
    """
    work_dir.mkdir(parents=True, exist_ok=True)

    argv = _build_argv(
        url,
        work_dir=work_dir,
        cookies_file=cookies_file,
        max_items=max_items,
        extra_args=extra_args,
        socket_timeout_s=socket_timeout_s,
        proxy_url=proxy_url,
        executable=executable,
    )

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env) if env is not None else None,
    )
    if proc_holder is not None:
        # Mirrors the yt-dlp runner: surfaces the live process so the
        # orchestrator can SIGTERM on user pause/cancel.
        proc_holder.append(proc)

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    downloaded = 0

    async def _drain_stdout() -> None:
        nonlocal downloaded
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                return
            line = raw.decode(errors="replace")
            stdout_chunks.append(line)
            update = _parse_progress_line(line, work_dir)
            if update is None:
                continue
            downloaded += 1
            if progress_queue is not None:
                # Re-emit with the running count so the UI shows
                # "downloading image N".
                await progress_queue.put(
                    ProgressUpdate(
                        status=update.status,
                        filename=update.filename,
                        downloaded_count=downloaded,
                        raw=update.raw,
                        sub_status=update.sub_status,
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

    produced = _walk_produced(work_dir)
    info = _read_info_json(produced)
    extractor = _extractor_from_info(info)

    image_files = [p for p in produced if _is_image(p)]
    metadata_files = [p for p in produced if _is_metadata(p)]

    return RunResult(
        returncode=rc,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
        info=info,
        produced_files=produced,
        image_files=image_files,
        metadata_files=metadata_files,
        extractor=extractor,
    )
