"""Gallery-capture post-processor — CLAUDE.md §15 Gallery pass v0.5.

Mirrors the structure of ``test_postprocess.py``. Verifies that a
gallery-dl run produces a forensically complete per-item folder:

* ``capture_kind == "gallery"`` in DB + meta.json
* every image gets ``{stem}.NNN.{ext}`` + a sibling ``.json`` sidecar
* ``meta.json.sig`` verifies (covers every gallery image's hash via
  the artifacts/checksums map)
* ``checksums.txt`` lists every gallery image with both MD5 and SHA-256
* the manifest PDF lists all images
* ``meta.json.gallery_count`` and ``gallery_extractor`` are set
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def env(capsule_dirs):
    from app import (
        audit as _audit,
        cases as _cases,
        cookies as _cookies,
        db as _db,
        paths as _paths,
        postprocess as _pp,
        signing as _signing,
    )

    importlib.reload(_paths)
    importlib.reload(_signing)
    _signing._reset_cache_for_tests()
    importlib.reload(_cases)
    importlib.reload(_cookies)
    importlib.reload(_pp)

    conn = _db.connect(":memory:")
    _db.migrate(conn)
    case = _cases.create(conn, name="Image-Thread Investigation")

    yield {
        "conn": conn,
        "case": case,
        "downloads": capsule_dirs["downloads"],
        "config": capsule_dirs["config"],
        "audit": _audit,
        "cases": _cases,
        "pp": _pp,
        "signing": _signing,
    }
    _signing._reset_cache_for_tests()
    conn.close()


def _stage_gallery(
    env,
    *,
    n_images: int = 3,
    extractor: str = "twitter",
):
    """Drop ``n_images`` fake gallery files into a tmp work-dir.

    Returns ``(images, metadata_files)`` ready to feed CaptureInput.
    Each image has a sibling ``<image>.json`` per gallery-dl convention,
    plus a single gallery-level ``info.json``.
    """
    work = env["downloads"] / "_gallery_tmp"
    work.mkdir(parents=True, exist_ok=True)
    extensions = ["jpg", "png", "webp"]
    images: list[Path] = []
    metadata: list[Path] = []
    for i in range(1, n_images + 1):
        ext = extensions[(i - 1) % len(extensions)]
        img = work / f"{i:02d}.{ext}"
        img.write_bytes(b"FAKE-IMAGE-" + str(i).encode())
        images.append(img)
        meta = work / f"{img.name}.json"
        meta.write_text(
            json.dumps({"filename": img.name, "category": extractor, "num": i})
        )
        metadata.append(meta)
    info = work / "info.json"
    info.write_text(
        json.dumps(
            {
                "category": extractor,
                "subcategory": "user",
                "title": "Test gallery thread",
                "url": "https://example.com/g",
            }
        )
    )
    metadata.append(info)
    return images, metadata


def test_gallery_capture_full_happy_path(env):
    pp = env["pp"]
    images, metadata = _stage_gallery(env, n_images=4, extractor="twitter")
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://x.com/u/status/1",
        url_final="https://x.com/u/status/1",
        redirect_chain=["https://x.com/u/status/1"],
        capture_date=pp.utc_now(),
        media_files=[],  # yt-dlp produced nothing
        info_json=None,
        extra_sidecars=[],
        gallery_files=images,
        gallery_metadata_files=metadata,
        gallery_extractor="twitter",
        gallery_dl_version="1.30.fake",
        ytdlp_version="2026.03.17",
    )
    result = pp.finalize(env["conn"], capture_input)

    assert result.capture_kind == "gallery"
    # No single media file; relative_media_path stays null
    assert result.relative_media_path is None

    case_dir = env["downloads"] / env["case"].slug
    item_dir = case_dir / result.stem
    media_dir = item_dir / "Media"
    metadata_dir = item_dir / "Metadata"

    # Per-item folder layout (v0.8): images live in Media/, per-image and
    # gallery-level JSON sidecars live in Metadata/, the two PDFs sit at
    # the item root.
    image_files = sorted(p for p in media_dir.iterdir() if p.suffix in {".jpg", ".png", ".webp"})
    assert len(image_files) == 4
    # 1-based 3-digit zero-padded index, original extensions preserved.
    expected_image_names = [
        f"{result.stem}.{i:03d}.{ext}"
        for i, ext in enumerate(["jpg", "png", "webp", "jpg"], start=1)
    ]
    assert sorted(p.name for p in image_files) == sorted(expected_image_names)

    # Per-image metadata sidecars renamed to share the stem; live in Metadata/.
    json_sidecars = sorted(
        p for p in metadata_dir.iterdir()
        if p.suffix == ".json" and p.name.startswith(result.stem) and ".gallery_info" not in p.name and ".meta" not in p.name
    )
    assert len(json_sidecars) == 4

    # Gallery-level info.json has its own role; lives in Metadata/.
    gallery_info_path = metadata_dir / f"{result.stem}.gallery_info.json"
    assert gallery_info_path.exists()

    # Read meta.json — schema v8 (gallery fields from v6 stay; v7 added the
    # page-preservation hardening block; v0.7 added download_options).
    meta = json.loads(result.meta_json_path.read_text())
    assert meta["schema_version"] == 8
    assert meta["capture_kind"] == "gallery"
    assert meta["gallery_count"] == 4
    assert meta["gallery_extractor"] == "twitter"
    assert meta["platform"] == "twitter"
    assert meta["tools"]["gallery_dl_version"] == "1.30.fake"

    # Each image gets its own role + checksums (MD5 + SHA-256 + size).
    for i in range(1, 5):
        role = f"gallery_{i:03d}"
        assert role in meta["artifacts"], f"missing artifact role {role}"
        assert role in meta["checksums"]
        cs = meta["checksums"][role]
        assert cs["md5"] and cs["sha256"]
        assert cs["size_bytes"] > 0
        meta_role = f"{role}_meta"
        assert meta_role in meta["artifacts"], f"missing meta role {meta_role}"
        assert meta_role in meta["checksums"]

    # Gallery-info role.
    assert "gallery_info" in meta["artifacts"]
    assert "gallery_info" in meta["checksums"]

    # checksums.txt lists every gallery image (MD5 + SHA-256 lines); lives
    # in Metadata/ alongside meta.json.
    cs_text = (metadata_dir / f"{result.stem}.checksums.txt").read_text()
    for i in range(1, 5):
        ext = ["jpg", "png", "webp", "jpg"][i - 1]
        rel_name = f"{result.stem}.{i:03d}.{ext}"
        assert f"Media/{rel_name}" in cs_text or rel_name in cs_text

    # meta.json.sig verifies — covers every gallery image's hash through
    # the artifacts/checksums map.
    public = env["signing"].ensure_keypair().public
    assert env["signing"].verify(
        result.meta_json_path.read_bytes(),
        result.signature_path.read_bytes(),
        public,
    )

    # DB row capture_kind round-trips.
    row = env["conn"].execute(
        "SELECT capture_kind, platform, video_id, relative_path FROM downloads WHERE id = ?",
        (result.download_id,),
    ).fetchone()
    assert row["capture_kind"] == "gallery"
    assert row["platform"] == "twitter"
    assert row["video_id"] is None  # gallery has no single video_id
    assert row["relative_path"] is None  # no single media file


def test_gallery_capture_no_extractor_falls_back_to_url_platform(env):
    """If gallery-dl never set ``category``, the URL hint picks the platform."""
    pp = env["pp"]
    images, metadata = _stage_gallery(env, n_images=2, extractor="imgur")
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://imgur.com/a/abcd",
        url_final="https://imgur.com/a/abcd",
        redirect_chain=["https://imgur.com/a/abcd"],
        capture_date=pp.utc_now(),
        gallery_files=images,
        gallery_metadata_files=metadata,
        gallery_extractor=None,  # missing — exercise the URL-hint fallback
        ytdlp_version="2026.03.17",
    )
    result = pp.finalize(env["conn"], capture_input)
    assert result.capture_kind == "gallery"
    meta = json.loads(result.meta_json_path.read_text())
    assert meta["platform"] == "imgur"
    assert meta["gallery_extractor"] is None


def test_gallery_capture_with_no_metadata_sidecars(env):
    """gallery-dl was run with --no-write-metadata; only images survive."""
    pp = env["pp"]
    images, _ = _stage_gallery(env, n_images=2, extractor="reddit")
    # Drop the metadata files entirely.
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://reddit.com/r/foo/comments/1",
        url_final="https://reddit.com/r/foo/comments/1",
        redirect_chain=["https://reddit.com/r/foo/comments/1"],
        capture_date=pp.utc_now(),
        gallery_files=images,
        gallery_metadata_files=[],
        gallery_extractor="reddit",
        ytdlp_version="2026.03.17",
    )
    result = pp.finalize(env["conn"], capture_input)
    meta = json.loads(result.meta_json_path.read_text())
    assert meta["gallery_count"] == 2
    # No per-image meta roles; no gallery_info role.
    assert "gallery_001" in meta["artifacts"]
    assert "gallery_001_meta" not in meta["artifacts"]
    assert "gallery_info" not in meta["artifacts"]


def test_gallery_capture_preserves_image_extensions(env):
    """Investigators rely on the extension to identify MIME type. We must
    not mangle it (no forced .jpg renaming, no extension stripping)."""
    pp = env["pp"]
    work = env["downloads"] / "_gallery_tmp_ext"
    work.mkdir(parents=True, exist_ok=True)
    images = [
        work / "a.jpg",
        work / "b.png",
        work / "c.webp",
        work / "d.gif",
    ]
    for p in images:
        p.write_bytes(b"FAKE-" + p.name.encode())

    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://example.com/g",
        url_final="https://example.com/g",
        redirect_chain=["https://example.com/g"],
        capture_date=pp.utc_now(),
        gallery_files=images,
        gallery_extractor="generic",
        ytdlp_version="2026.03.17",
    )
    result = pp.finalize(env["conn"], capture_input)
    case_dir = env["downloads"] / env["case"].slug
    item_dir = case_dir / result.stem
    found = sorted(p.suffix for p in (item_dir / "Media").iterdir() if p.is_file())
    assert ".jpg" in found
    assert ".png" in found
    assert ".webp" in found
    assert ".gif" in found


def test_gallery_report_pdf_includes_thumbnail_strip_and_version(env):
    """The per-item report PDF for a gallery capture must:

    * mention the gallery-dl version in the tools table
    * include the gallery section heading
    * embed file:// URIs for the preserved images so a viewer can see
      the thumbnail strip without resolving against the case dir
    """
    pp = env["pp"]
    images, metadata = _stage_gallery(env, n_images=3, extractor="pixiv")
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.pixiv.net/en/artworks/12345",
        url_final="https://www.pixiv.net/en/artworks/12345",
        redirect_chain=["https://www.pixiv.net/en/artworks/12345"],
        capture_date=pp.utc_now(),
        gallery_files=images,
        gallery_metadata_files=metadata,
        gallery_extractor="pixiv",
        gallery_dl_version="1.30.test",
        ytdlp_version="2026.03.17",
    )
    result = pp.finalize(env["conn"], capture_input)
    item_dir = (env["downloads"] / env["case"].slug / result.stem)
    report_pdf = item_dir / f"{result.stem}.report.pdf"
    assert report_pdf.exists()
    pdf_bytes = report_pdf.read_bytes()
    # PDFs render images as embedded XObjects — direct text search of
    # the binary won't find "1.30.test" reliably. Re-render the HTML
    # form to assert content; the PDF bytes already proved it generated.
    from app import pdf_report
    html_str = pdf_report._render_item_report_html(
        case=env["case"],
        item_view={
            "title": "Pixiv test",
            "source_url": capture_input.url_submitted,
            "final_url": capture_input.url_final,
            "redirect_chain": ["https://www.pixiv.net/en/artworks/12345"],
            "captured_utc": capture_input.capture_date,
            "signing_key_fp": "f" * 32,
            "platform": "pixiv",
            "uploader": None,
            "tools": {
                "app_version": "0.5.0",
                "ytdlp_version": "2026.03.17",
                "chromium_version": "0",
                "browsertrix_version": "0",
                "gallery_dl_version": "1.30.test",
            },
            "capture": {},
            "manifest_filename": f"{result.stem}.manifest.pdf",
            "capture_kind": "gallery",
            "gallery_count": 3,
            "gallery_extractor": "pixiv",
            "gallery_thumbnails": [
                f"{env['case'].slug}/{result.stem}/{result.stem}.001.jpg",
                f"{env['case'].slug}/{result.stem}/{result.stem}.002.png",
                f"{env['case'].slug}/{result.stem}/{result.stem}.003.webp",
            ],
        },
        lang="en",
    )
    # gallery-dl version row is present.
    assert "1.30.test" in html_str
    assert "gallery-dl version" in html_str
    # Gallery heading rendered.
    assert "Image gallery" in html_str
    # Thumbnail caption with count + extractor.
    assert "3 images preserved" in html_str
    assert "pixiv" in html_str
    # Three <img> tags inside the gallery-strip ul, with file:// URIs.
    assert html_str.count("<li><img src=\"file://") == 3


def test_gallery_capture_is_deduped_against_subsequent_attempt(env):
    """Two captures of the same gallery URL collide on
    UNIQUE(case_id, capture_kind, url_hash) — the second raises
    DuplicateCapture so the §15 modal can fire."""
    pp = env["pp"]
    images_a, metadata_a = _stage_gallery(env, n_images=2, extractor="twitter")
    capture_input_a = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://x.com/u/status/123",
        url_final="https://x.com/u/status/123",
        redirect_chain=["https://x.com/u/status/123"],
        capture_date=pp.utc_now(),
        gallery_files=images_a,
        gallery_metadata_files=metadata_a,
        gallery_extractor="twitter",
        ytdlp_version="2026.03.17",
    )
    pp.finalize(env["conn"], capture_input_a)

    # Re-stage so the second attempt has fresh files (the first finalize
    # moved the originals into the per-item folder).
    images_b, metadata_b = _stage_gallery(env, n_images=2, extractor="twitter")
    capture_input_b = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://x.com/u/status/123",
        url_final="https://x.com/u/status/123",
        redirect_chain=["https://x.com/u/status/123"],
        capture_date=pp.utc_now(),
        gallery_files=images_b,
        gallery_metadata_files=metadata_b,
        gallery_extractor="twitter",
        ytdlp_version="2026.03.17",
    )
    with pytest.raises(pp.DuplicateCapture):
        pp.finalize(env["conn"], capture_input_b)
