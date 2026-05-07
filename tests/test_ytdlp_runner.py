"""yt-dlp subprocess wrapper — CLAUDE.md §5, §13.13.

Most tests are hermetic: they shim ``yt-dlp`` with a fake script that emits
the same progress-template JSON shape as the real binary. One opt-in test
(gated by ``CAPSULE_E2E=1``) calls the real binary against a tiny CC clip.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from app import ytdlp_runner


@pytest.fixture
def fake_ytdlp(tmp_path: Path) -> Path:
    """Write a python shim that mimics yt-dlp's argv + progress output."""
    script = tmp_path / "fake-ytdlp"
    script.write_text(
        """#!/usr/bin/env python3
import json, os, sys, time

argv = sys.argv[1:]
if argv == ["--version"]:
    print("9999.99.99")
    sys.exit(0)

# Find --paths home:DIR
paths_dir = None
for i, a in enumerate(argv):
    if a == "--paths" and i + 1 < len(argv):
        token = argv[i + 1]
        if token.startswith("home:"):
            paths_dir = token[len("home:"):]
url = argv[-1]

# Emit a couple of progress lines.
for i in (50, 100):
    print(json.dumps({
        "status": "downloading" if i < 100 else "finished",
        "downloaded_bytes": i * 1024,
        "total_bytes": 102400,
        "speed": 12345.6,
        "eta": 0 if i == 100 else 5,
        "filename": "abc.mp4",
    }), flush=True)

# Banner-like non-JSON line, must be tolerated.
print("[fake-ytdlp] all done", flush=True)

# Pretend to produce a media file + sidecars.
if paths_dir:
    os.makedirs(paths_dir, exist_ok=True)
    open(os.path.join(paths_dir, "abc.mp4"), "wb").write(b"FAKEDATA")
    info = {"id": "abc", "title": "Hello", "ext": "mp4",
            "extractor_key": "Youtube", "uploader": "test"}
    open(os.path.join(paths_dir, "abc.info.json"), "w").write(json.dumps(info))
    open(os.path.join(paths_dir, "abc.description"), "w").write("desc")

sys.exit(0)
""",
        encoding="utf-8",
    )
    os.chmod(script, stat.S_IRWXU)
    return script


def test_parse_progress_line_valid():
    line = json.dumps(
        {
            "status": "downloading",
            "downloaded_bytes": 1024,
            "total_bytes": 4096,
            "speed": 256.0,
            "eta": 12,
            "filename": "x.mp4",
        }
    )
    p = ytdlp_runner._parse_progress_line(line)
    assert p is not None
    assert p.status == "downloading"
    assert p.downloaded_bytes == 1024
    assert p.total_bytes == 4096
    assert p.speed == 256.0
    assert p.eta == 12
    assert p.filename == "x.mp4"


def test_parse_progress_line_falls_back_to_estimate():
    line = json.dumps(
        {"status": "downloading", "total_bytes_estimate": 8192}
    )
    p = ytdlp_runner._parse_progress_line(line)
    assert p is not None
    assert p.total_bytes == 8192


def test_parse_progress_line_skips_non_json():
    assert ytdlp_runner._parse_progress_line("[banner] hi") is None
    assert ytdlp_runner._parse_progress_line("") is None
    assert ytdlp_runner._parse_progress_line("{not json") is None


def test_parse_progress_line_skips_non_progress_dict():
    line = json.dumps({"unrelated": "thing"})
    assert ytdlp_runner._parse_progress_line(line) is None


# --- sub_status classifier ----------------------------------------------------
# yt-dlp downloads several files per capture (video stream, audio stream,
# thumbnail, ...). Each fires its own 0→100% sequence; without per-file
# labels the UI bar appears to "loop." These tests cover the labelling
# rules so the live frontend can confidently render forward motion.


def test_classify_substatus_video_only():
    raw = {"info_dict": {"vcodec": "vp9", "acodec": "none"}}
    assert ytdlp_runner._classify_substatus(raw, "abc.f137.mp4") == "video"


def test_classify_substatus_audio_only():
    raw = {"info_dict": {"vcodec": "none", "acodec": "opus"}}
    assert ytdlp_runner._classify_substatus(raw, "abc.f140.webm") == "audio"


def test_classify_substatus_combined():
    raw = {"info_dict": {"vcodec": "h264", "acodec": "aac"}}
    assert ytdlp_runner._classify_substatus(raw, "abc.mp4") == "combined"


def test_classify_substatus_thumbnail_by_extension():
    # No codec context — extension alone identifies the thumbnail.
    assert ytdlp_runner._classify_substatus({}, "abc.webp") == "thumbnail"
    assert ytdlp_runner._classify_substatus({}, "abc.jpg") == "thumbnail"


def test_classify_substatus_subtitles():
    assert ytdlp_runner._classify_substatus({}, "abc.en.vtt") == "subtitles"
    assert ytdlp_runner._classify_substatus({}, "abc.srt") == "subtitles"


def test_classify_substatus_info_json():
    assert ytdlp_runner._classify_substatus({}, "abc.info.json") == "info_json"
    assert ytdlp_runner._classify_substatus({}, "abc.description") == "info_json"


def test_classify_substatus_strips_part_suffix():
    raw = {"info_dict": {"vcodec": "vp9", "acodec": "none"}}
    assert ytdlp_runner._classify_substatus(raw, "abc.f137.mp4.part") == "video"


def test_classify_substatus_unknown():
    assert ytdlp_runner._classify_substatus({}, None) == "unknown"
    assert ytdlp_runner._classify_substatus({}, "abc.bin") == "unknown"


def test_parse_progress_line_carries_substatus():
    line = json.dumps(
        {
            "status": "downloading",
            "downloaded_bytes": 1024,
            "total_bytes": 4096,
            "filename": "abc.f140.webm",
            "info_dict": {"vcodec": "none", "acodec": "opus"},
        }
    )
    p = ytdlp_runner._parse_progress_line(line)
    assert p is not None
    assert p.sub_status == "audio"


def test_detect_postprocess_substatus_merging():
    assert ytdlp_runner._detect_postprocess_substatus("[Merger] Merging formats into \"x.mp4\"") == "merging"
    assert ytdlp_runner._detect_postprocess_substatus("[ffmpeg] Merging formats into \"x.mkv\"") == "merging"


def test_detect_postprocess_substatus_extract_audio():
    assert ytdlp_runner._detect_postprocess_substatus("[ExtractAudio] Destination: x.mp3") == "extract_audio"


def test_detect_postprocess_substatus_ignores_other():
    assert ytdlp_runner._detect_postprocess_substatus("[generic] Extracting URL: ...") is None
    assert ytdlp_runner._detect_postprocess_substatus("") is None


def test_build_argv_pins_preservation_flags():
    argv = ytdlp_runner._build_argv(
        "https://example.com/v",
        case_dir=Path("/tmp/x"),
        cookies_file=None,
        format_spec=None,
        extra_args=None,
    )
    for required in (
        "--no-embed-metadata",
        "--no-embed-thumbnail",
        "--no-embed-subs",
        "--write-info-json",
        "--write-description",
        "--write-thumbnail",
        "--no-mtime",
        "--newline",
    ):
        assert required in argv
    assert argv[-1] == "https://example.com/v"


def test_build_argv_includes_cookies_when_set(tmp_path):
    cf = tmp_path / "c.txt"
    cf.write_text("# Netscape\n")
    argv = ytdlp_runner._build_argv(
        "https://x/y",
        case_dir=tmp_path,
        cookies_file=cf,
        format_spec="bestvideo+bestaudio",
        extra_args=None,
    )
    assert "--cookies" in argv
    assert str(cf) in argv
    assert "--format" in argv
    assert "bestvideo+bestaudio" in argv


def test_build_argv_pins_resilience_flags(tmp_path):
    """Plan §U2: every yt-dlp invocation gets resume + retry + timeout."""
    argv = ytdlp_runner._build_argv(
        "https://x/y",
        case_dir=tmp_path,
        cookies_file=None,
        format_spec=None,
        extra_args=None,
    )
    # Resume partial files.
    assert "--continue" in argv
    # Infinite top-level + fragment retries; bounded file-access retries.
    i = argv.index("--retries")
    assert argv[i + 1] == "infinite"
    i = argv.index("--fragment-retries")
    assert argv[i + 1] == "infinite"
    i = argv.index("--file-access-retries")
    assert argv[i + 1] == "10"
    # Retry sleep schedule must be linear with a cap so we don't hammer.
    i = argv.index("--retry-sleep")
    assert argv[i + 1] == "linear=1:30:5"
    # Socket timeout is the universal default.
    i = argv.index("--socket-timeout")
    assert argv[i + 1] == str(ytdlp_runner.DEFAULT_SOCKET_TIMEOUT_S)


def test_build_argv_passes_socket_timeout_override(tmp_path):
    argv = ytdlp_runner._build_argv(
        "https://x/y",
        case_dir=tmp_path,
        cookies_file=None,
        format_spec=None,
        extra_args=None,
        socket_timeout_s=60,
    )
    i = argv.index("--socket-timeout")
    assert argv[i + 1] == "60"


def test_build_argv_includes_limit_rate_when_set(tmp_path):
    argv = ytdlp_runner._build_argv(
        "https://x/y",
        case_dir=tmp_path,
        cookies_file=None,
        format_spec=None,
        extra_args=None,
        limit_rate_kbps=500,
    )
    assert "--limit-rate" in argv
    assert "500K" in argv


def test_build_argv_omits_limit_rate_when_unset(tmp_path):
    argv = ytdlp_runner._build_argv(
        "https://x/y",
        case_dir=tmp_path,
        cookies_file=None,
        format_spec=None,
        extra_args=None,
    )
    assert "--limit-rate" not in argv


def test_build_argv_includes_proxy_when_set(tmp_path):
    argv = ytdlp_runner._build_argv(
        "https://x/y",
        case_dir=tmp_path,
        cookies_file=None,
        format_spec=None,
        extra_args=None,
        proxy_url="socks5h://127.0.0.1:1080",
    )
    i = argv.index("--proxy")
    assert argv[i + 1] == "socks5h://127.0.0.1:1080"


@pytest.mark.asyncio
async def test_run_with_fake_ytdlp(fake_ytdlp, tmp_path):
    case_dir = tmp_path / "case"
    progress: asyncio.Queue = asyncio.Queue()

    result = await ytdlp_runner.run(
        "https://example.com/abc",
        case_dir=case_dir,
        progress_queue=progress,
        executable=str(fake_ytdlp),
    )

    assert result.ok
    assert result.returncode == 0
    assert result.info is not None
    assert result.info["id"] == "abc"

    produced_names = sorted(p.name for p in result.produced_files)
    assert produced_names == ["abc.description", "abc.info.json", "abc.mp4"]

    # Drain progress queue: 2 updates + sentinel None.
    updates = []
    while True:
        item = await progress.get()
        if item is None:
            break
        updates.append(item)
    assert [u.status for u in updates] == ["downloading", "finished"]
    assert updates[0].downloaded_bytes == 50 * 1024
    assert updates[1].downloaded_bytes == 100 * 1024


@pytest.mark.asyncio
async def test_run_propagates_nonzero_exit(tmp_path):
    failing = tmp_path / "fail"
    failing.write_text("#!/usr/bin/env python3\nimport sys; sys.stderr.write('boom\\n'); sys.exit(2)\n")
    os.chmod(failing, stat.S_IRWXU)
    result = await ytdlp_runner.run(
        "https://x/y",
        case_dir=tmp_path / "out",
        executable=str(failing),
    )
    assert result.ok is False
    assert result.returncode == 2
    assert "boom" in result.stderr


@pytest.mark.asyncio
async def test_version_against_real_binary():
    """Sanity-check: real yt-dlp answers ``--version``. Doesn't hit the network."""
    v = await ytdlp_runner.version()
    assert v
    # Versions look like 2026.03.17.
    assert v.count(".") >= 1


