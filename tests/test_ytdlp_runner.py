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


# --- CLAUDE.md §15 v0.7: download options + restart + stall watchdog --------


def test_build_format_spec_audio_only():
    # Audio-only suppresses --format entirely (yt-dlp's -x picks the audio
    # stream); the helper returns None so the argv builder skips --format.
    assert ytdlp_runner.build_format_spec(
        audio_only=True, quality_cap=None, fallback="best",
    ) is None


def test_build_format_spec_quality_cap_audio_alias():
    # quality_cap="audio" is equivalent to audio_only=True per the v0.7
    # contract — the segmented pill in the UI maps both to the same thing.
    assert ytdlp_runner.build_format_spec(
        audio_only=False, quality_cap="audio", fallback="best",
    ) is None


def test_build_format_spec_height_caps():
    for cap in ("480", "720", "1080"):
        spec = ytdlp_runner.build_format_spec(
            audio_only=False, quality_cap=cap, fallback="best",
        )
        assert spec == f"bestvideo[height<={cap}]+bestaudio/best[height<={cap}]"


def test_build_format_spec_best_overrides_fallback():
    # When the user explicitly picks "Best" they want any profile-imposed
    # cap lifted, not the slow profile's [height<=480] selector.
    assert (
        ytdlp_runner.build_format_spec(
            audio_only=False, quality_cap="best",
            fallback="bestvideo[height<=480]+bestaudio/best[height<=480]",
        )
        == "best"
    )


def test_build_format_spec_no_overrides_returns_fallback():
    assert (
        ytdlp_runner.build_format_spec(
            audio_only=False, quality_cap=None, fallback="best",
        )
        == "best"
    )
    assert (
        ytdlp_runner.build_format_spec(
            audio_only=False, quality_cap=None, fallback=None,
        )
        is None
    )


def test_build_subtitle_argv_empty():
    assert ytdlp_runner.build_subtitle_argv(None) == []
    assert ytdlp_runner.build_subtitle_argv([]) == []
    assert ytdlp_runner.build_subtitle_argv(["", "  "]) == []


def test_build_subtitle_argv_csv():
    out = ytdlp_runner.build_subtitle_argv(["en", "ar"])
    assert "--write-subs" in out
    assert "--sub-langs" in out
    assert "en,ar" in out
    assert "--sub-format" in out


def test_build_subtitle_argv_all_sentinel():
    out = ytdlp_runner.build_subtitle_argv(["all"])
    # 'all' maps to all,-live_chat — exclude live-chat tracks.
    idx = out.index("--sub-langs")
    assert out[idx + 1] == "all,-live_chat"


def test_build_argv_audio_only_emits_extract_flags(tmp_path):
    argv = ytdlp_runner._build_argv(
        "https://example.com/x",
        case_dir=tmp_path,
        cookies_file=None,
        format_spec="best",  # should be ignored when audio_only is set
        extra_args=None,
        audio_only=True,
    )
    assert "-x" in argv
    assert argv[argv.index("-x") + 1] == "--audio-format"
    assert "mp3" in argv
    assert "--audio-quality" in argv
    # audio_only beats format_spec — no --format token survives.
    assert "--format" not in argv


def test_build_argv_quality_cap_height_overrides_format_spec(tmp_path):
    argv = ytdlp_runner._build_argv(
        "https://example.com/x",
        case_dir=tmp_path,
        cookies_file=None,
        format_spec="bestvideo[height<=480]+bestaudio/best[height<=480]",
        extra_args=None,
        quality_cap="720",
    )
    fmt_idx = argv.index("--format")
    assert argv[fmt_idx + 1] == (
        "bestvideo[height<=720]+bestaudio/best[height<=720]"
    )
    # No -x leaked in.
    assert "-x" not in argv


def test_build_argv_subtitles_emits_flags(tmp_path):
    argv = ytdlp_runner._build_argv(
        "https://example.com/x",
        case_dir=tmp_path,
        cookies_file=None,
        format_spec=None,
        extra_args=None,
        subtitle_langs=["en", "ja"],
    )
    assert "--write-subs" in argv
    sub_idx = argv.index("--sub-langs")
    assert argv[sub_idx + 1] == "en,ja"


# --- CLAUDE.md §15 v0.9: container picker ----------------------------------


def test_build_format_spec_video_container_mp4_prefers_ext():
    # video_container=mp4 makes the format spec prefer ext-matched streams
    # before falling back to the generic bestvideo+bestaudio selector. The
    # m4a audio pair is the conventional yt-dlp idiom for mp4-friendly audio.
    spec = ytdlp_runner.build_format_spec(
        audio_only=False, quality_cap=None, fallback="best",
        video_container="mp4",
    )
    assert spec is not None
    assert "[ext=mp4]" in spec
    assert "[ext=m4a]" in spec
    # First branch is the cleanest "no remux at all" pair.
    assert spec.startswith("bestvideo[ext=mp4]+bestaudio[ext=m4a]/")
    # And it falls all the way to a generic ``best`` so a site that doesn't
    # offer mp4 still captures rather than failing.
    assert spec.endswith("/best")


def test_build_format_spec_video_container_webm_uses_webm_audio_pair():
    # WebM pairs with WebM audio (Opus / Vorbis), not m4a.
    spec = ytdlp_runner.build_format_spec(
        audio_only=False, quality_cap=None, fallback=None,
        video_container="webm",
    )
    assert spec is not None
    assert "bestvideo[ext=webm]+bestaudio[ext=webm]" in spec
    assert "[ext=m4a]" not in spec


def test_build_format_spec_video_container_with_height_cap_combines_clauses():
    # When both clauses are set, the [height<=N] clause attaches inline to
    # every bestvideo selector in the cascade.
    spec = ytdlp_runner.build_format_spec(
        audio_only=False, quality_cap="720", fallback=None,
        video_container="mp4",
    )
    assert spec is not None
    assert "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/" in spec
    # Height clause survives the fall-through to the generic best at end.
    assert "best[height<=720]" in spec


def test_build_format_spec_audio_only_ignores_video_container():
    # Orthogonality check — audio-only short-circuits before the container
    # branch so a stale video_container in the payload never leaks.
    assert ytdlp_runner.build_format_spec(
        audio_only=True, quality_cap=None, fallback="best",
        video_container="mp4",
    ) is None
    assert ytdlp_runner.build_format_spec(
        audio_only=False, quality_cap="audio", fallback="best",
        video_container="mp4",
    ) is None


def test_build_format_spec_unknown_container_falls_through_to_legacy():
    # Defensive: an enum value that didn't match the allowed set must not
    # leak ``[ext=<garbage>]`` into the spec — the legacy branch wins.
    assert ytdlp_runner.build_format_spec(
        audio_only=False, quality_cap=None, fallback="best",
        video_container="mov",  # not in {mp4, webm, mkv}
    ) == "best"


def test_build_container_argv_emits_merge_output_format():
    out = ytdlp_runner.build_container_argv(
        audio_only=False, quality_cap=None, video_container="mp4",
    )
    assert out == ["--merge-output-format", "mp4"]
    out = ytdlp_runner.build_container_argv(
        audio_only=False, quality_cap=None, video_container="mkv",
    )
    assert out == ["--merge-output-format", "mkv"]


def test_build_container_argv_returns_empty_on_audio_path():
    # Audio-only path uses --audio-format instead — the muxer flag would
    # be a no-op and clutter the argv. Same for the quality_cap='audio' alias.
    assert ytdlp_runner.build_container_argv(
        audio_only=True, quality_cap=None, video_container="mp4",
    ) == []
    assert ytdlp_runner.build_container_argv(
        audio_only=False, quality_cap="audio", video_container="mp4",
    ) == []


def test_build_container_argv_empty_when_no_container_picked():
    assert ytdlp_runner.build_container_argv(
        audio_only=False, quality_cap=None, video_container=None,
    ) == []


def test_build_argv_video_container_emits_merge_output_format(tmp_path):
    argv = ytdlp_runner._build_argv(
        "https://example.com/x",
        case_dir=tmp_path,
        cookies_file=None, format_spec=None, extra_args=None,
        video_container="mp4",
    )
    # --merge-output-format lands after --format, before subtitle flags.
    assert "--merge-output-format" in argv
    mof_idx = argv.index("--merge-output-format")
    assert argv[mof_idx + 1] == "mp4"
    fmt_idx = argv.index("--format")
    assert fmt_idx < mof_idx
    # The format spec carries the ext clause so yt-dlp prefers compatible
    # streams in the first place — no transcode-fallback needed.
    assert "[ext=mp4]" in argv[fmt_idx + 1]


def test_build_argv_audio_container_overrides_default_mp3(tmp_path):
    # Picking a non-default audio container swaps mp3 for m4a in the
    # extraction args. The page snapshot still preserves the original video.
    argv = ytdlp_runner._build_argv(
        "https://example.com/x",
        case_dir=tmp_path,
        cookies_file=None, format_spec=None, extra_args=None,
        audio_only=True,
        audio_container="m4a",
    )
    fmt_idx = argv.index("--audio-format")
    assert argv[fmt_idx + 1] == "m4a"
    assert "mp3" not in argv  # mp3 default isn't accidentally appended too


def test_build_argv_audio_container_unknown_falls_back_to_default(tmp_path):
    # Defensive: an unknown audio_container must not leak as --audio-format
    # — fall back to the mp3 default rather than passing garbage to yt-dlp.
    argv = ytdlp_runner._build_argv(
        "https://example.com/x",
        case_dir=tmp_path,
        cookies_file=None, format_spec=None, extra_args=None,
        audio_only=True,
        audio_container="aiff",  # not in AUDIO_CONTAINERS
    )
    fmt_idx = argv.index("--audio-format")
    assert argv[fmt_idx + 1] == "mp3"


def test_build_argv_video_container_skipped_on_audio_only_path(tmp_path):
    # The audio-only path must never emit --merge-output-format, even when
    # a video_container is set in the options (stale UI state, ephemeral
    # toggling, etc.).
    argv = ytdlp_runner._build_argv(
        "https://example.com/x",
        case_dir=tmp_path,
        cookies_file=None, format_spec=None, extra_args=None,
        audio_only=True,
        video_container="mp4",
    )
    assert "--merge-output-format" not in argv


def test_build_argv_restart_swaps_continue_for_no_continue(tmp_path):
    argv_resume = ytdlp_runner._build_argv(
        "https://example.com/x",
        case_dir=tmp_path,
        cookies_file=None, format_spec=None, extra_args=None,
        restart=False,
    )
    argv_restart = ytdlp_runner._build_argv(
        "https://example.com/x",
        case_dir=tmp_path,
        cookies_file=None, format_spec=None, extra_args=None,
        restart=True,
    )
    assert "--continue" in argv_resume
    assert "--no-continue" not in argv_resume
    assert "--no-continue" in argv_restart
    assert "--continue" not in argv_restart


def test_wipe_partial_files_removes_part_and_ytdl(tmp_path):
    (tmp_path / "abc.mp4.part").write_bytes(b"partial")
    (tmp_path / "abc.f137.mp4.part").write_bytes(b"partial")
    (tmp_path / "abc.ytdl").write_bytes(b"state")
    (tmp_path / "abc.info.json").write_bytes(b"{}")  # NOT wiped
    (tmp_path / "abc.mp4").write_bytes(b"complete")  # NOT wiped
    n = ytdlp_runner._wipe_partial_files(tmp_path)
    assert n == 3
    # Whitelist preserved.
    assert (tmp_path / "abc.info.json").exists()
    assert (tmp_path / "abc.mp4").exists()
    # Blacklist gone.
    assert not (tmp_path / "abc.mp4.part").exists()
    assert not (tmp_path / "abc.f137.mp4.part").exists()
    assert not (tmp_path / "abc.ytdl").exists()


def test_wipe_partial_files_handles_missing_dir(tmp_path):
    assert ytdlp_runner._wipe_partial_files(tmp_path / "does-not-exist") == 0


@pytest.mark.asyncio
async def test_run_restart_pre_deletes_part_files(fake_ytdlp, tmp_path):
    # Stage a stale .part the prior (cancelled) run left behind.
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    stale = case_dir / "abc.mp4.part"
    stale.write_bytes(b"corrupted-bytes")
    res = await ytdlp_runner.run(
        url="https://example.com/x",
        case_dir=case_dir,
        executable=str(fake_ytdlp),
        restart=True,
    )
    assert res.ok
    # The pre-wipe deleted the staged .part before yt-dlp started; the fake
    # script never recreates one (it writes the final mp4 directly).
    assert not stale.exists()


@pytest.mark.asyncio
async def test_stall_watchdog_emits_stalled_then_clears(tmp_path):
    """Drive the runner against a fake yt-dlp that goes silent for the
    stall threshold, then resumes. Verify exactly one ``stalled`` event
    and one ``running``/``downloading`` clear, with no SIGTERM."""
    script = tmp_path / "slow-ytdlp"
    script.write_text(
        """#!/usr/bin/env python3
import json, sys, time
argv = sys.argv[1:]
if argv == ["--version"]:
    print("9999.99.99"); sys.exit(0)
# First progress event.
print(json.dumps({
    "status": "downloading", "downloaded_bytes": 100, "total_bytes": 1000,
    "speed": 1.0, "eta": 1, "filename": "x.mp4",
}), flush=True)
# Sleep past the test threshold (2s). Watchdog must fire here.
time.sleep(3.0)
# Second progress event clears the stall.
print(json.dumps({
    "status": "downloading", "downloaded_bytes": 500, "total_bytes": 1000,
    "speed": 1.0, "eta": 1, "filename": "x.mp4",
}), flush=True)
sys.exit(0)
""",
        encoding="utf-8",
    )
    os.chmod(script, stat.S_IRWXU)
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    queue: asyncio.Queue = asyncio.Queue()
    res = await ytdlp_runner.run(
        url="https://example.com/x",
        case_dir=case_dir,
        executable=str(script),
        progress_queue=queue,
        stall_threshold_s=2,
    )
    assert res.returncode == 0  # No SIGTERM — stall is a UI signal only.

    # Drain the queue.
    items: list = []
    while True:
        item = queue.get_nowait()
        items.append(item)
        if item is None:
            break
    statuses = [
        getattr(it, "status", None) for it in items if it is not None
    ]
    # Exactly one synthetic "stalled" event surfaced.
    assert statuses.count("stalled") == 1
    # The second progress event surfaces with status "downloading"
    # (the runner clears stall_active so the next stretch can re-fire).
    assert "downloading" in statuses
    # Order: a downloading came BEFORE the stalled, and another came
    # AFTER it.
    stalled_idx = statuses.index("stalled")
    assert any(s == "downloading" for s in statuses[:stalled_idx])
    assert any(s == "downloading" for s in statuses[stalled_idx + 1:])
