"""Evidence export — CLAUDE.md §10."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def env(capsule_dirs):
    for name in (
        "app.config", "app.paths", "app.signing",
        "app.db", "app.audit", "app.cases", "app.cookies",
        "app.classify", "app.postprocess", "app.evidence_export",
        "app.pdf_report", "app.jobs",
    ):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    from app import (
        cases as cases_mod,
        db,
        evidence_export as ee,
        postprocess as pp,
        signing,
    )
    signing._reset_cache_for_tests()
    conn = db.connect(":memory:")
    db.migrate(conn)
    case = cases_mod.create(conn, name="Operation Sunrise")

    yield {
        "conn": conn, "case": case, "downloads": capsule_dirs["downloads"],
        "config": capsule_dirs["config"],
        "ee": ee, "pp": pp, "cases": cases_mod, "signing": signing,
    }
    conn.close()


def _seed_capture(env, video_id: str = "abc"):
    """Stage a media file + info.json + describing sidecars and finalize."""
    case_dir = env["downloads"] / env["case"].slug
    case_dir.mkdir(parents=True, exist_ok=True)
    media = case_dir / f"{video_id}.mp4"
    media.write_bytes(b"FAKEMP4" + video_id.encode())
    info_path = case_dir / f"{video_id}.info.json"
    info = {
        "id": video_id, "title": f"Title {video_id}", "ext": "mp4",
        "extractor_key": "Youtube", "uploader": "veritasium",
        "upload_date": "20240812",
    }
    info_path.write_text(json.dumps(info))
    desc_path = case_dir / f"{video_id}.description"
    desc_path.write_text("desc")

    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted=f"https://www.youtube.com/watch?v={video_id}",
        url_final=f"https://www.youtube.com/watch?v={video_id}",
        redirect_chain=[f"https://www.youtube.com/watch?v={video_id}"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=info,
        extra_sidecars=[info_path, desc_path],
        ytdlp_version="9999.0.0",
    )
    return pp.finalize(env["conn"], capture_input)


def test_export_produces_signed_zip(env):
    _seed_capture(env, "abc")
    _seed_capture(env, "def")
    result = env["ee"].build_bundle(env["conn"], case_id=env["case"].id)
    assert result.zip_path.is_file()

    with zipfile.ZipFile(result.zip_path) as zf:
        names = set(zf.namelist())
    expected = {"manifest.json", "manifest.sig", "public_key.pem",
                "audit_log.json", "verify.py", "README.txt", "case_report.pdf"}
    assert expected <= names

    # Two captures → at least 2 media files in downloads/
    media_in_zip = [n for n in names if n.endswith(".mp4")]
    assert len(media_in_zip) == 2


def test_manifest_signature_verifies(env):
    _seed_capture(env, "abc")
    result = env["ee"].build_bundle(env["conn"], case_id=env["case"].id)
    with zipfile.ZipFile(result.zip_path) as zf:
        manifest = zf.read("manifest.json")
        sig = zf.read("manifest.sig")
    assert env["signing"].verify(manifest, sig) is True


def test_manifest_records_every_artifact_hash(env):
    _seed_capture(env, "abc")
    result = env["ee"].build_bundle(env["conn"], case_id=env["case"].id)
    with zipfile.ZipFile(result.zip_path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["case"]["slug"] == env["case"].slug
    assert len(manifest["files"]) > 0
    for entry in manifest["files"]:
        assert entry["sha256"] and entry["md5"]
        assert entry["size_bytes"] >= 0
        # Paths in the manifest must be relative.
        assert not entry["path"].startswith("/")


def test_audit_log_records_export(env):
    _seed_capture(env, "abc")
    env["ee"].build_bundle(env["conn"], case_id=env["case"].id)
    actions = [
        r["action"] for r in env["conn"].execute(
            "SELECT action FROM audit_log WHERE case_id = ? ORDER BY id ASC",
            (env["case"].id,),
        )
    ]
    assert "case.exported" in actions


def test_verifier_script_runs_and_passes(env, tmp_path):
    """Extract bundle + run the bundled verify.py against it."""
    _seed_capture(env, "abc")
    result = env["ee"].build_bundle(env["conn"], case_id=env["case"].id)

    extract_dir = tmp_path / "ex"
    extract_dir.mkdir()
    with zipfile.ZipFile(result.zip_path) as zf:
        zf.extractall(extract_dir)

    proc = subprocess.run(
        [sys.executable, str(extract_dir / "verify.py"), str(extract_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout


def test_verifier_detects_tampered_artifact(env, tmp_path):
    _seed_capture(env, "abc")
    result = env["ee"].build_bundle(env["conn"], case_id=env["case"].id)
    extract_dir = tmp_path / "ex"
    extract_dir.mkdir()
    with zipfile.ZipFile(result.zip_path) as zf:
        zf.extractall(extract_dir)

    # Tamper: flip a byte in any media file.
    mp4 = next(extract_dir.rglob("*.mp4"))
    data = bytearray(mp4.read_bytes())
    data[0] ^= 0xFF
    mp4.write_bytes(bytes(data))

    proc = subprocess.run(
        [sys.executable, str(extract_dir / "verify.py"), str(extract_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "FAIL" in proc.stdout


def test_export_lookup_error(env):
    with pytest.raises(LookupError):
        env["ee"].build_bundle(env["conn"], case_id=9999)


def test_pdf_renders_for_empty_case(env):
    """Edge case: a case with zero items still produces a valid PDF."""
    result = env["ee"].build_bundle(env["conn"], case_id=env["case"].id)
    with zipfile.ZipFile(result.zip_path) as zf:
        pdf = zf.read("case_report.pdf")
    assert pdf.startswith(b"%PDF-")


def test_pdf_renders_for_populated_case(env):
    _seed_capture(env, "abc")
    _seed_capture(env, "def")
    result = env["ee"].build_bundle(env["conn"], case_id=env["case"].id)
    with zipfile.ZipFile(result.zip_path) as zf:
        pdf = zf.read("case_report.pdf")
    assert pdf.startswith(b"%PDF-")
    # Sanity: at least a few KB of content.
    assert len(pdf) > 2000


# --- Extension-supplied user-browser sidecars in evidence bundles ---------


def _seed_capture_with_user_browser(env, video_id: str = "ext"):
    """Stage a media capture that ALSO includes extension-supplied
    user_browser_* sidecars. Mirrors the orchestrator hand-off in jobs.py."""
    case_dir = env["downloads"] / env["case"].slug
    case_dir.mkdir(parents=True, exist_ok=True)
    media = case_dir / f"{video_id}.mp4"
    media.write_bytes(b"FAKEMP4" + video_id.encode())
    info_path = case_dir / f"{video_id}.info.json"
    info = {
        "id": video_id, "title": f"Title {video_id}", "ext": "mp4",
        "extractor_key": "Youtube", "uploader": "veritasium",
        "upload_date": "20240812",
    }
    info_path.write_text(json.dumps(info))
    desc_path = case_dir / f"{video_id}.description"
    desc_path.write_text("desc")

    # User-browser bundle on a tmp dir (mirrors /api/extension/capture).
    user_dir = env["config"] / "extension_inbox" / "ext-job"
    user_dir.mkdir(parents=True, exist_ok=True)
    user_mhtml = user_dir / "user-browser.mhtml"
    user_mhtml.write_bytes(b"<html>investigator's view</html>")
    user_shot = user_dir / "user-browser.png"
    user_shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    user_env = user_dir / "user-browser.environment.json"
    user_env.write_text(json.dumps({"userAgent": "TestUA/1"}))

    pp = env["pp"]
    capture_input = pp.CaptureInput(
        case=env["case"],
        job_uuid=pp.new_job_uuid(),
        url_submitted=f"https://www.youtube.com/watch?v={video_id}",
        url_final=f"https://www.youtube.com/watch?v={video_id}",
        redirect_chain=[f"https://www.youtube.com/watch?v={video_id}"],
        capture_date=pp.utc_now(),
        media_files=[media],
        info_json=info,
        extra_sidecars=[info_path, desc_path],
        ytdlp_version="9999.0.0",
        user_browser_mhtml=user_mhtml,
        user_browser_screenshot=user_shot,
        user_browser_environment=user_env,
        user_browser_label="My laptop",
    )
    return pp.finalize(env["conn"], capture_input)


def test_export_includes_user_browser_sidecars(env):
    _seed_capture_with_user_browser(env)
    result = env["ee"].build_bundle(env["conn"], case_id=env["case"].id)
    with zipfile.ZipFile(result.zip_path) as zf:
        names = zf.namelist()
    user_browser_files = [n for n in names if "user-browser" in n]
    assert any(n.endswith(".user-browser.mhtml") for n in user_browser_files)
    assert any(n.endswith(".user-browser.png") for n in user_browser_files)
    assert any(n.endswith(".user-browser.environment.json") for n in user_browser_files)


def test_verifier_passes_with_user_browser_sidecars(env, tmp_path):
    """Bundle includes both canonical and user-browser artifacts; verify.py
    must still PASS — both sets are hashed, signed, and listed in meta.json."""
    _seed_capture_with_user_browser(env)
    result = env["ee"].build_bundle(env["conn"], case_id=env["case"].id)
    extract_dir = tmp_path / "ex"
    extract_dir.mkdir()
    with zipfile.ZipFile(result.zip_path) as zf:
        zf.extractall(extract_dir)

    proc = subprocess.run(
        [sys.executable, str(extract_dir / "verify.py"), str(extract_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout


def test_verifier_detects_tampered_user_browser_artifact(env, tmp_path):
    """Tampering with a user_browser_* sidecar must FAIL the verifier."""
    _seed_capture_with_user_browser(env)
    result = env["ee"].build_bundle(env["conn"], case_id=env["case"].id)
    extract_dir = tmp_path / "ex"
    extract_dir.mkdir()
    with zipfile.ZipFile(result.zip_path) as zf:
        zf.extractall(extract_dir)

    target = next(extract_dir.rglob("*.user-browser.mhtml"))
    target.write_bytes(target.read_bytes() + b"TAMPER")

    proc = subprocess.run(
        [sys.executable, str(extract_dir / "verify.py"), str(extract_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "FAIL" in proc.stdout
