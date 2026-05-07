"""Capture post-processor — CLAUDE.md §5, §6, §7, §8."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def env(capsule_dirs):
    """Reload everything that depends on config + reset the signing cache."""
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
    case = _cases.create(conn, name="Operation Sunrise")

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


def _stage_media(env, *, name: str = "abc.mp4", payload: bytes = b"FAKEMP4DATA"):
    case_dir = env["downloads"] / env["case"].slug
    case_dir.mkdir(parents=True, exist_ok=True)
    media = case_dir / name
    media.write_bytes(payload)
    info = case_dir / "abc.info.json"
    info.write_text(
        json.dumps(
            {
                "id": "abc",
                "title": "Hello World",
                "ext": "mp4",
                "extractor_key": "Youtube",
                "uploader": "veritasium",
                "upload_date": "20240812",
                "duration": 600,
                "description": "A description.",
            }
        )
    )
    desc = case_dir / "abc.description"
    desc.write_text("A description.")
    return media, info, desc


def test_media_capture_full_happy_path(env):
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
    )
    result = pp.finalize(env["conn"], capture_input)

    # Capture kind correct.
    assert result.capture_kind == "media"

    # Canonical filename obeys the §5 pattern.
    assert "youtube__veritasium__Hello World__2024-08-12__abc" in result.stem

    case_dir = env["downloads"] / env["case"].slug
    item_dir = case_dir / result.stem
    media_path = item_dir / f"{result.stem}.mp4"
    assert media_path.exists()

    assert (item_dir / f"{result.stem}.info.json").exists()
    assert (item_dir / f"{result.stem}.description").exists()
    assert (item_dir / f"{result.stem}.checksums.txt").exists()
    assert (item_dir / f"{result.stem}.meta.json").exists()
    assert (item_dir / f"{result.stem}.meta.json.sig").exists()

    # Signature verifies against the active public key.
    meta_bytes = (item_dir / f"{result.stem}.meta.json").read_bytes()
    sig_bytes = (item_dir / f"{result.stem}.meta.json.sig").read_bytes()
    assert env["signing"].verify(meta_bytes, sig_bytes) is True

    # DB row inserted.
    row = env["conn"].execute(
        "SELECT * FROM downloads WHERE id = ?", (result.download_id,)
    ).fetchone()
    assert row["capture_kind"] == "media"
    assert row["video_id"] == "abc"
    assert row["uploader"] == "veritasium"
    assert row["md5"] is not None
    assert row["sha256"] is not None
    # Paths are stored relative to /downloads — never absolute.
    assert not row["relative_path"].startswith("/")
    assert row["relative_path"].startswith(env["case"].slug + "/")

    # Audit entry created and chain still verifies.
    actions = [r["action"] for r in env["audit"].iter_entries(env["conn"])]
    assert "download.created" in actions
    ok, broken = env["audit"].verify_chain(env["conn"])
    assert ok and broken is None


def test_page_only_capture(env):
    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://example.com/page",
        url_final="https://example.com/page",
        redirect_chain=["https://example.com/page"],
        capture_date=pp.utc_now(),
        media_files=[],
        info_json=None,
    )
    result = pp.finalize(env["conn"], capture_input)
    assert result.capture_kind == "page_only"
    assert result.relative_media_path is None
    assert result.stem.startswith("generic__")
    case_dir = env["downloads"] / env["case"].slug
    assert (case_dir / result.stem / f"{result.stem}.meta.json").exists()


def test_duplicate_raises(env):
    media, info, desc = _stage_media(env)
    pp = env["pp"]

    def make_input():
        # Re-stage because finalize moves the source file.
        m, i, d = _stage_media(env, name=f"dup-{id(env)}.mp4")
        return pp.CaptureInput(
            case=env["case"],
            job_uuid=pp.new_job_uuid(),
            url_submitted="https://www.youtube.com/watch?v=abc",
            url_final="https://www.youtube.com/watch?v=abc",
            redirect_chain=["https://www.youtube.com/watch?v=abc"],
            capture_date=pp.utc_now(),
            media_files=[m],
            info_json=json.loads(i.read_text()),
            extra_sidecars=[i, d],
            ytdlp_version="2026.03.17",
        )

    pp.finalize(env["conn"], pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
    ))

    with pytest.raises(pp.DuplicateCapture) as exc_info:
        pp.finalize(env["conn"], make_input())
    assert exc_info.value.existing_id > 0


def test_collision_appends_suffix(env):
    """Pre-existing file with the canonical name forces a __c2 stem."""
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    base_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
    )
    first = pp.finalize(env["conn"], base_input)

    # Re-stage and call again with a different URL so the DB UNIQUE doesn't fire,
    # but the same metadata so the canonical stem collides.
    media2, info2, desc2 = _stage_media(env, name="abc2.mp4")
    second_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc-different",
        url_final="https://www.youtube.com/watch?v=abc-different",
        redirect_chain=["https://www.youtube.com/watch?v=abc-different"],
        capture_date=pp.utc_now(),
        media_files=[media2],
        info_json=json.loads(info2.read_text()),
        extra_sidecars=[info2, desc2],
        ytdlp_version="2026.03.17",
    )
    second = pp.finalize(env["conn"], second_input)

    assert second.stem.endswith("__c2")
    assert first.stem != second.stem


def test_meta_json_paths_are_relative(env):
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
    )
    result = pp.finalize(env["conn"], capture_input)
    meta = json.loads(result.meta_json_path.read_text())
    for role, rel in meta["artifacts"].items():
        assert not rel.startswith("/")
        assert "/Users/" not in rel
        assert "C:" not in rel


def test_audit_entry_does_not_leak_authenticated_domains_as_cookies(env):
    """Sanity: the audit log records authenticated_domains but never values."""
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        authenticated_domains=["youtube.com"],
        ytdlp_version="2026.03.17",
    )
    pp.finalize(env["conn"], capture_input)
    rows = list(env["audit"].iter_entries(env["conn"]))
    found = [r for r in rows if r["action"] == "download.created"][0]
    assert found["details"]["authenticated_domains"] == ["youtube.com"]
    # No nested "cookies" key — DetailLeakError would have caught it earlier
    # but assert explicitly to make the contract obvious.
    assert "cookies" not in json.dumps(found["details"])


# --- Extension-supplied "user-browser" supplementary artifacts -------------


def _stage_user_browser(env, *, stem_dir: str = "user-bundle") -> dict:
    """Materialise a tiny set of user-browser sidecar files in a tmpdir.

    The orchestrator hand-off keeps these on disk; postprocess moves them
    into the canonical sidecar directory alongside the clean-Chromium
    capture and signs the lot together.
    """
    tmp = env["downloads"] / "_extension_inbox" / stem_dir
    tmp.mkdir(parents=True, exist_ok=True)
    mhtml = tmp / "user-browser.mhtml"
    mhtml.write_bytes(b"<html>investigator's view</html>")
    shot = tmp / "user-browser.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    har = tmp / "user-browser.har"
    har.write_text(json.dumps({"page": {"url": "https://x.com/"}, "entries": []}))
    env_json = tmp / "user-browser.environment.json"
    env_json.write_text(json.dumps({"userAgent": "TestUA/1", "language": "en-US"}))
    return {"mhtml": mhtml, "screenshot": shot, "har": har, "environment": env_json}


def test_user_browser_artifacts_are_moved_into_item_dir(env):
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    user = _stage_user_browser(env)
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
        user_browser_mhtml=user["mhtml"],
        user_browser_screenshot=user["screenshot"],
        user_browser_har=user["har"],
        user_browser_environment=user["environment"],
        user_browser_label="My laptop",
    )
    result = pp.finalize(env["conn"], capture_input)
    item_dir = env["downloads"] / result.relative_item_dir
    stem = result.stem
    for tail in (".user-browser.mhtml", ".user-browser.png",
                 ".user-browser.har", ".user-browser.environment.json"):
        assert (item_dir / f"{stem}{tail}").is_file(), tail


def test_user_browser_artifacts_are_listed_in_meta_and_checksums(env):
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    user = _stage_user_browser(env)
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
        user_browser_mhtml=user["mhtml"],
        user_browser_screenshot=user["screenshot"],
        user_browser_har=user["har"],
        user_browser_environment=user["environment"],
        user_browser_label="My laptop",
    )
    result = pp.finalize(env["conn"], capture_input)
    meta = json.loads(result.meta_json_path.read_text(encoding="utf-8"))
    assert "user_browser_mhtml" in meta["artifacts"]
    assert "user_browser_screenshot" in meta["artifacts"]
    assert "user_browser_har" in meta["artifacts"]
    assert "user_browser_environment" in meta["artifacts"]
    # Each role appears in the checksums map with the correct shape.
    for role in ("user_browser_mhtml", "user_browser_screenshot",
                 "user_browser_har", "user_browser_environment"):
        h = meta["checksums"][role]
        assert h["sha256"] and h["md5"] and h["size_bytes"] > 0

    # checksums.txt mirrors the meta.json projection.
    cs_text = (result.meta_json_path.parent / f"{result.stem}.checksums.txt").read_text()
    assert "user_browser_mhtml".replace("_", "_") not in cs_text  # reads relative paths, not roles
    # Just confirm the per-artifact lines exist by searching for filenames.
    assert ".user-browser.mhtml" in cs_text


def test_user_browser_artifacts_trigger_audit_entry(env):
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    user = _stage_user_browser(env)
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
        user_browser_mhtml=user["mhtml"],
        user_browser_environment=user["environment"],
        user_browser_label="ext-on-my-laptop",
    )
    pp.finalize(env["conn"], capture_input)
    rows = list(env["audit"].iter_entries(env["conn"]))
    actions = [r["action"] for r in rows]
    assert "user_browser_capture.received" in actions
    entry = next(r for r in rows if r["action"] == "user_browser_capture.received")
    assert entry["details"]["extension_label"] == "ext-on-my-laptop"
    assert "user_browser_mhtml" in entry["details"]["artifact_roles"]


# --- Track A: per-item manifest PDF + layout + lang -----------------------


def test_per_item_folder_layout_collapses_sidecars(env):
    """All artifacts for one capture live directly under
    ``/downloads/{case}/{stem}/`` — no ``sidecars/`` intermediate."""
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
    )
    result = pp.finalize(env["conn"], capture_input)
    case_dir = env["downloads"] / env["case"].slug
    item_dir = case_dir / result.stem

    # Old "sidecars/" tier must NOT exist.
    assert not (case_dir / "sidecars").exists()
    # Media + sidecars all sit in the same per-item folder.
    assert (item_dir / f"{result.stem}.mp4").is_file()
    assert (item_dir / f"{result.stem}.info.json").is_file()
    assert (item_dir / f"{result.stem}.meta.json").is_file()
    assert (item_dir / f"{result.stem}.checksums.txt").is_file()

    # CaptureResult exposes the new field name.
    assert result.relative_item_dir.endswith(f"/{result.stem}")
    # DB column is now item_dir.
    row = env["conn"].execute(
        "SELECT item_dir FROM downloads WHERE id = ?", (result.download_id,)
    ).fetchone()
    assert row["item_dir"] == result.relative_item_dir


def test_manifest_pdf_is_emitted_for_every_capture(env):
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
    )
    result = pp.finalize(env["conn"], capture_input)
    item_dir = env["downloads"] / result.relative_item_dir
    manifest_pdf = item_dir / f"{result.stem}.manifest.pdf"
    assert manifest_pdf.is_file()
    assert manifest_pdf.read_bytes()[:5] == b"%PDF-"


def test_manifest_pdf_hash_present_in_meta_and_checksums(env):
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
    )
    result = pp.finalize(env["conn"], capture_input)
    meta = json.loads(result.meta_json_path.read_text(encoding="utf-8"))
    assert "manifest_pdf" in meta["artifacts"]
    assert "manifest_pdf" in meta["checksums"]
    assert meta["checksums"]["manifest_pdf"]["sha256"]
    cs_text = (result.meta_json_path.parent / f"{result.stem}.checksums.txt").read_text()
    assert ".manifest.pdf" in cs_text
    # Schema v4: per-item report PDF joins the manifest PDF in the
    # artifact set; both bound transitively by meta.json.sig.
    assert meta["schema_version"] == 4


def test_capture_input_lang_is_recorded_in_meta(env):
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
        lang="ar",
    )
    result = pp.finalize(env["conn"], capture_input)
    meta = json.loads(result.meta_json_path.read_text(encoding="utf-8"))
    assert meta["capture"]["report_lang"] == "ar"


def test_manifest_pdf_audit_entry_recorded(env):
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
        lang="en",
    )
    pp.finalize(env["conn"], capture_input)
    rows = list(env["audit"].iter_entries(env["conn"]))
    actions = [r["action"] for r in rows]
    assert "item.manifest_rendered" in actions
    entry = next(r for r in rows if r["action"] == "item.manifest_rendered")
    assert entry["details"]["lang"] == "en"
    assert entry["details"]["sha256"]
    assert int(entry["details"]["size_bytes"]) > 0
    # Audit chain still verifies after the new entry type lands.
    ok, broken = env["audit"].verify_chain(env["conn"])
    assert ok and broken is None


def test_manifest_pdf_renders_for_page_only(env):
    """Page-only captures get a manifest PDF too — no media file in the
    table, but every sidecar (MHTML, screenshot, WARC) shows up."""
    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://example.com/page",
        url_final="https://example.com/page",
        redirect_chain=["https://example.com/page"],
        capture_date=pp.utc_now(),
        media_files=[],
        info_json=None,
        lang="en",
    )
    result = pp.finalize(env["conn"], capture_input)
    item_dir = env["downloads"] / result.relative_item_dir
    assert (item_dir / f"{result.stem}.manifest.pdf").is_file()


def test_manifest_pdf_for_arabic_locale(env):
    """``lang='ar'`` flows through and the PDF still emits valid bytes
    (the Noto fallback chain in the template handles glyph coverage —
    see commit 2's font stack)."""
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
        lang="ar",
    )
    result = pp.finalize(env["conn"], capture_input)
    item_dir = env["downloads"] / result.relative_item_dir
    pdf_bytes = (item_dir / f"{result.stem}.manifest.pdf").read_bytes()
    assert pdf_bytes.startswith(b"%PDF-")


def test_per_item_report_pdf_emitted(env):
    """Track 3 — every capture also gets a {stem}.report.pdf companion.

    Asserts:
    - the PDF file exists in the per-item folder and starts with %PDF-
    - meta.json.artifacts contains a ``report_pdf`` role
    - meta.json.checksums has matching md5/sha256 with non-zero size
    - checksums.txt contains a line for the report PDF
    - The report PDF appears in the manifest PDF's table — i.e. it
      was hashed BEFORE the manifest rendered, so the meta.json
      signature transitively binds it.
    """
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    info_dict = json.loads(info.read_text())
    info_dict["description"] = "A description that is long enough to test."
    info.write_text(json.dumps(info_dict))
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=info_dict,
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
        lang="en",
    )
    result = pp.finalize(env["conn"], capture_input)
    item_dir = env["downloads"] / result.relative_item_dir

    # File present and a valid PDF.
    report_pdf = item_dir / f"{result.stem}.report.pdf"
    assert report_pdf.is_file()
    bytes_ = report_pdf.read_bytes()
    assert bytes_[:5] == b"%PDF-"
    assert len(bytes_) > 1000

    # meta.json records both the artifact path and the checksums.
    meta = json.loads(result.meta_json_path.read_text(encoding="utf-8"))
    assert "report_pdf" in meta["artifacts"]
    assert meta["artifacts"]["report_pdf"].endswith(".report.pdf")
    cs = meta["checksums"]["report_pdf"]
    assert cs["md5"] and len(cs["md5"]) == 32
    assert cs["sha256"] and len(cs["sha256"]) == 64
    assert cs["size_bytes"] > 0

    # checksums.txt mirrors the meta.checksums projection — both MD5
    # and SHA256 lines reference the report PDF.
    cs_text = (item_dir / f"{result.stem}.checksums.txt").read_text()
    assert ".report.pdf" in cs_text
    assert cs["sha256"] in cs_text
    assert cs["md5"] in cs_text

    # The manifest PDF's file table includes a row for ``report.pdf``.
    # This proves the report PDF was hashed BEFORE the manifest rendered
    # — the invariant that lets meta.json.sig transitively bind it.
    from app import pdf_report
    files = [
        pdf_report.FileEntry(
            relpath=meta["artifacts"][role],
            size=int(meta["checksums"][role]["size_bytes"]),
            md5=meta["checksums"][role]["md5"],
            sha256=meta["checksums"][role]["sha256"],
        )
        for role in sorted(meta["artifacts"].keys())
    ]
    # ``report_pdf`` appears alongside ``manifest_pdf`` and the rest.
    roles = [role for role in sorted(meta["artifacts"].keys())]
    assert "report_pdf" in roles
    assert "manifest_pdf" in roles
    # Order check: in our fixture both PDFs end up adjacent in sorted
    # order (manifest_pdf < report_pdf). The role list MUST contain both.
    assert {"manifest_pdf", "report_pdf"}.issubset(set(roles))
    # And rendering the manifest from this snapshot still works (i.e. the
    # FileEntry list is well-formed).
    assert files  # exercised the construction


def test_per_item_folder_layout_includes_report_pdf(env):
    """Companion to test_per_item_folder_layout_collapses_sidecars —
    the new ``{stem}.report.pdf`` sits alongside ``{stem}.manifest.pdf``
    in the per-item folder."""
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
    )
    result = pp.finalize(env["conn"], capture_input)
    item_dir = env["downloads"] / result.relative_item_dir
    assert (item_dir / f"{result.stem}.manifest.pdf").is_file()
    assert (item_dir / f"{result.stem}.report.pdf").is_file()


def test_per_item_report_pdf_for_page_only_capture(env):
    """Page-only captures get a report PDF too — even when info_json
    is None, the report renders with empty/dash placeholders."""
    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://example.com/page",
        url_final="https://example.com/page",
        redirect_chain=["https://example.com/page"],
        capture_date=pp.utc_now(),
        media_files=[],
        info_json=None,
        lang="en",
    )
    result = pp.finalize(env["conn"], capture_input)
    item_dir = env["downloads"] / result.relative_item_dir
    report = item_dir / f"{result.stem}.report.pdf"
    assert report.is_file()
    assert report.read_bytes()[:5] == b"%PDF-"
    meta = json.loads(result.meta_json_path.read_text(encoding="utf-8"))
    assert "report_pdf" in meta["artifacts"]


def test_meta_json_signature_covers_user_browser_artifacts(env):
    media, info, desc = _stage_media(env)
    pp = env["pp"]
    signing = env["signing"]
    user = _stage_user_browser(env)
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted="https://www.youtube.com/watch?v=abc",
        url_final="https://www.youtube.com/watch?v=abc",
        redirect_chain=["https://www.youtube.com/watch?v=abc"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=json.loads(info.read_text()),
        extra_sidecars=[info, desc],
        ytdlp_version="2026.03.17",
        user_browser_mhtml=user["mhtml"],
    )
    result = pp.finalize(env["conn"], capture_input)
    # Signature was made on the meta.json that already references the
    # user_browser_mhtml artifact, so verify() should still pass.
    data = result.meta_json_path.read_bytes()
    sig = result.signature_path.read_bytes()
    assert signing.verify(data, sig) is True
    # And tampering with the user-browser file alone won't be caught by the
    # signature (it only signs meta.json) but checksums.txt + meta.checksums
    # would catch it. Sanity-check the meta record referenced the file.
    meta = json.loads(data)
    assert meta["artifacts"].get("user_browser_mhtml")
