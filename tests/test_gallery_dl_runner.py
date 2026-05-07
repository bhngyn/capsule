"""gallery-dl subprocess wrapper — CLAUDE.md §15 Gallery pass v0.5.

Hermetic: a python shim mimics gallery-dl's argv + per-file stdout output.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from app import gallery_dl_runner


@pytest.fixture
def fake_gallerydl(tmp_path: Path) -> Path:
    """Write a python shim that mimics gallery-dl: prints completed file
    paths to stdout and creates the actual files in ``-d <dir>``."""
    script = tmp_path / "fake-gallerydl"
    script.write_text(
        """#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
if argv == ["--version"]:
    print("1.30.fake")
    sys.exit(0)

# Find -d <dir> and the URL (last arg).
work_dir = None
for i, a in enumerate(argv):
    if a == "-d" and i + 1 < len(argv):
        work_dir = argv[i + 1]
url = argv[-1]

# Synthesise a per-extractor subdir so we exercise the rglob walk.
gallery_subdir = os.path.join(work_dir, "fakecat", "user")
os.makedirs(gallery_subdir, exist_ok=True)

# Three images + per-image metadata + a gallery-level info.json.
images = [("01.jpg", b"FAKEJPG1"), ("02.png", b"FAKEPNG2"), ("03.webp", b"FAKEWEBP3")]
for name, blob in images:
    p = os.path.join(gallery_subdir, name)
    open(p, "wb").write(blob)
    open(p + ".json", "w").write(json.dumps({"filename": name, "category": "fakecat"}))
    print(p, flush=True)

# Banner + gallery-level info.json (sibling of the first image).
print("[fake-gallerydl] complete", flush=True)
open(os.path.join(gallery_subdir, "info.json"), "w").write(
    json.dumps({"category": "fakecat", "subcategory": "user", "url": url})
)

sys.exit(0)
""",
        encoding="utf-8",
    )
    os.chmod(script, stat.S_IRWXU)
    return script


def test_parse_progress_line_image_path(tmp_path: Path):
    work = tmp_path / "w"
    work.mkdir()
    (work / "imgur").mkdir()
    line = str(work / "imgur" / "01.jpg")
    p = gallery_dl_runner._parse_progress_line(line, work)
    assert p is not None
    assert p.filename == "01.jpg"
    assert p.sub_status == "gallery_image"


def test_parse_progress_line_relative_path(tmp_path: Path):
    work = tmp_path / "w"
    work.mkdir()
    line = "imgur/02.png"
    p = gallery_dl_runner._parse_progress_line(line, work)
    assert p is not None
    assert p.filename == "02.png"


def test_parse_progress_line_skips_banner(tmp_path: Path):
    work = tmp_path / "w"
    work.mkdir()
    assert gallery_dl_runner._parse_progress_line("", work) is None
    assert gallery_dl_runner._parse_progress_line("[gallery-dl] hi", work) is None
    assert gallery_dl_runner._parse_progress_line("# comment", work) is None


def test_parse_progress_line_skips_outside_work_dir(tmp_path: Path):
    work = tmp_path / "w"
    work.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    line = str(other / "evil.jpg")
    assert gallery_dl_runner._parse_progress_line(line, work) is None


def test_parse_progress_line_skips_non_image(tmp_path: Path):
    work = tmp_path / "w"
    work.mkdir()
    line = str(work / "log.txt")
    assert gallery_dl_runner._parse_progress_line(line, work) is None


def test_build_argv_default():
    argv = gallery_dl_runner._build_argv(
        "https://example.com/g",
        work_dir=Path("/tmp/w"),
        cookies_file=None,
        max_items=200,
        extra_args=None,
        executable="/usr/bin/fake",
    )
    assert argv[0] == "/usr/bin/fake"
    assert "--write-metadata" in argv
    assert "--write-info-json" in argv
    assert "--no-mtime" in argv
    assert "-d" in argv
    assert "/tmp/w" in argv
    assert "--range" in argv
    i = argv.index("--range")
    assert argv[i + 1] == "1-200"
    # cookies omitted when not given
    assert "--cookies" not in argv
    # URL last
    assert argv[-1] == "https://example.com/g"


def test_build_argv_with_cookies_and_proxy(tmp_path: Path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape\n", encoding="utf-8")
    argv = gallery_dl_runner._build_argv(
        "https://example.com/g",
        work_dir=tmp_path,
        cookies_file=cookies,
        max_items=50,
        extra_args=None,
        proxy_url="http://localhost:9999",
        executable="/usr/bin/fake",
    )
    assert "--cookies" in argv
    i = argv.index("--cookies")
    assert argv[i + 1] == str(cookies)
    assert "--proxy" in argv
    j = argv.index("--proxy")
    assert argv[j + 1] == "http://localhost:9999"


@pytest.mark.asyncio
async def test_run_happy_path(fake_gallerydl: Path, tmp_path: Path):
    work = tmp_path / "work"
    queue: asyncio.Queue = asyncio.Queue()

    result = await gallery_dl_runner.run(
        "https://example.com/g",
        work_dir=work,
        progress_queue=queue,
        executable=str(fake_gallerydl),
    )

    assert result.ok
    assert result.returncode == 0
    # 3 images + 3 per-image metadata JSONs + 1 gallery info.json = 7 files.
    assert len(result.produced_files) == 7
    assert len(result.image_files) == 3
    assert {p.name for p in result.image_files} == {"01.jpg", "02.png", "03.webp"}
    # info.json + 3 per-image .json sidecars.
    assert len(result.metadata_files) == 4
    assert result.info is not None
    assert result.info["category"] == "fakecat"
    assert result.extractor == "fakecat"

    # Three progress events + sentinel None.
    items: list = []
    while True:
        v = await queue.get()
        if v is None:
            break
        items.append(v)
    assert len(items) == 3
    assert [it.downloaded_count for it in items] == [1, 2, 3]
    assert all(it.sub_status == "gallery_image" for it in items)


@pytest.mark.asyncio
async def test_run_no_results(tmp_path: Path):
    """gallery-dl returns 64 (NoExtractorError) on unsupported URL."""
    script = tmp_path / "fake-empty"
    script.write_text(
        """#!/usr/bin/env python3
import sys
sys.stderr.write("Unsupported URL\\n")
sys.exit(64)
""",
        encoding="utf-8",
    )
    os.chmod(script, stat.S_IRWXU)

    work = tmp_path / "work"
    result = await gallery_dl_runner.run(
        "https://example.com/x",
        work_dir=work,
        executable=str(script),
    )
    assert not result.ok
    assert result.returncode == 64
    assert result.image_files == []
    assert result.metadata_files == []
    assert "Unsupported URL" in result.stderr
    assert result.extractor is None


@pytest.mark.asyncio
async def test_run_proc_holder_registered(fake_gallerydl: Path, tmp_path: Path):
    work = tmp_path / "work"
    holder: list = []
    await gallery_dl_runner.run(
        "https://example.com/g",
        work_dir=work,
        executable=str(fake_gallerydl),
        proc_holder=holder,
    )
    # The orchestrator depends on this for pause/cancel.
    assert len(holder) == 1
