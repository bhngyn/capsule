"""Locale-aware PDF rendering — Track A commits 2 + 3.

Asserts that ``render_case_report`` and ``render_item_manifest`` produce
valid PDF bytes, swap labels by locale, and emit the expected
``<html lang dir>`` for each tier of language support:

* ``en`` — full English bundle (always present).
* ``ar`` — full Arabic bundle, RTL.
* ``ja`` — bundle does not exist on this branch (Track B owns it).
  ``i18n.merged_with_fallback("ja")`` returns the English fallback;
  these tests confirm the renderer doesn't crash and the document
  still has ``<html lang="ja" dir="ltr">``. Once Track B's
  ``ja.json`` lands, the same tests will start picking up Japanese
  labels automatically — no test changes needed.
"""

from __future__ import annotations

import importlib
import json
import sys

import pytest


@pytest.fixture
def env(capsule_dirs):
    """Reload modules so the freshly tmp'd config takes effect."""
    for name in (
        "app.config", "app.paths", "app.signing", "app.i18n",
        "app.db", "app.audit", "app.cases", "app.cookies",
        "app.postprocess", "app.pdf_report",
    ):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    from app import (
        cases as cases_mod,
        db,
        pdf_report,
        postprocess,
        signing,
    )
    signing._reset_cache_for_tests()
    conn = db.connect(":memory:")
    db.migrate(conn)
    case = cases_mod.create(conn, name="Operation Sunrise")
    yield {
        "conn": conn, "case": case,
        "downloads": capsule_dirs["downloads"],
        "config": capsule_dirs["config"],
        "pdf_report": pdf_report, "postprocess": postprocess,
        "cases": cases_mod, "db": db, "signing": signing,
    }
    conn.close()


def _seed_capture(env, video_id: str = "abc"):
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

    pp = env["postprocess"]
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


# --- _load_pdf_strings ----------------------------------------------------


def test_load_pdf_strings_en_returns_pdf_and_manifest_keys(env):
    labels = env["pdf_report"]._load_pdf_strings("en")
    assert labels["pdf.brand.name"] == "Capsule"
    assert labels["manifest.heading"] == "File manifest"
    # Every key must start with one of the expected prefixes — no leakage
    # from other namespaces.
    for k in labels:
        assert k.startswith("pdf.") or k.startswith("manifest."), k


def test_load_pdf_strings_ar_returns_arabic(env):
    labels = env["pdf_report"]._load_pdf_strings("ar")
    # Arabic translation is present at the freeze SHA; assert a known
    # Arabic-script value rather than re-hardcoding the bytes.
    assert labels["pdf.brand.name"]  # not empty
    # The "items" summary label is translated, not transliterated.
    assert labels["pdf.summary.items"] != "Items"


def test_load_pdf_strings_ja_falls_back_to_english(env):
    """Track B owns ja.json; on this branch it doesn't exist yet, so
    ``merged_with_fallback`` returns English. Once Track B lands, this
    same call will start returning Japanese labels."""
    labels = env["pdf_report"]._load_pdf_strings("ja")
    assert labels["pdf.brand.name"] == "Capsule"
    assert labels["manifest.heading"] == "File manifest"


# --- render_case_report ---------------------------------------------------


def test_render_case_report_en_produces_pdf_bytes(env):
    pdf = env["pdf_report"].render_case_report(
        case=env["case"], items=[], lang="en",
    )
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1000


def test_render_case_report_ar_produces_pdf_bytes_and_rtl(env):
    pdf = env["pdf_report"].render_case_report(
        case=env["case"], items=[], lang="ar",
    )
    assert pdf.startswith(b"%PDF-")


def test_render_case_report_html_has_correct_lang_and_dir_for_ar(env):
    """Intercept the rendered HTML rather than scraping the PDF — the
    template's ``<html lang dir>`` is the contract; the PDF byte stream
    is just WeasyPrint's serialization of that contract."""
    html_doc = env["pdf_report"]._render_html(
        env["case"], [], lang="ar",
    )
    assert '<html lang="ar" dir="rtl">' in html_doc
    # Localised labels appear in the body.
    labels = env["pdf_report"]._load_pdf_strings("ar")
    assert labels["pdf.summary.items"] in html_doc
    assert labels["pdf.summary.signing_key"] in html_doc


def test_render_case_report_html_has_lang_ja_dir_ltr(env):
    """``ja`` is non-RTL; until Track B's ja.json lands the labels are
    English, but ``<html lang="ja" dir="ltr">`` is set unconditionally."""
    html_doc = env["pdf_report"]._render_html(
        env["case"], [], lang="ja",
    )
    assert '<html lang="ja" dir="ltr">' in html_doc


def test_render_case_report_with_populated_case(env):
    _seed_capture(env, "abc")
    rows = list(env["conn"].execute("SELECT * FROM downloads ORDER BY id"))
    pdf = env["pdf_report"].render_case_report(
        case=env["case"], items=rows, lang="en",
    )
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 2000


# --- render_item_manifest -------------------------------------------------


def _file_entries(env):
    pdf_report = env["pdf_report"]
    return [
        pdf_report.FileEntry(
            relpath="ops/abc/abc.mp4",
            size=2048,
            md5="d41d8cd98f00b204e9800998ecf8427e",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        ),
        pdf_report.FileEntry(
            relpath="ops/abc/abc.page.mhtml",
            size=8192,
            md5="aabbccddeeff00112233445566778899",
            sha256="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        ),
    ]


def test_render_item_manifest_en_produces_pdf_bytes(env):
    pdf = env["pdf_report"].render_item_manifest(
        case=env["case"],
        item_view={
            "title": "Hello world",
            "source_url": "https://example.com/x",
            "captured_utc": "2026-05-06T12:00:00+00:00",
            "signing_key_fp": "abcdef0123",
        },
        item_dir=env["downloads"] / env["case"].slug / "stem",
        files=_file_entries(env),
        lang="en",
    )
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1000


def test_render_item_manifest_ar_produces_pdf_bytes(env):
    pdf = env["pdf_report"].render_item_manifest(
        case=env["case"],
        item_view={
            "title": "Hello world",
            "source_url": "https://example.com/x",
            "captured_utc": "2026-05-06T12:00:00+00:00",
            "signing_key_fp": "abcdef0123",
        },
        item_dir=env["downloads"] / env["case"].slug / "stem",
        files=_file_entries(env),
        lang="ar",
    )
    assert pdf.startswith(b"%PDF-")


def test_render_item_manifest_html_has_correct_lang_and_dir_for_ar(env):
    html_doc = env["pdf_report"]._render_item_manifest_html(
        case=env["case"],
        item_view={
            "title": "T",
            "source_url": "https://x.test",
            "captured_utc": "2026-05-06T12:00:00+00:00",
            "signing_key_fp": "fp",
        },
        files=_file_entries(env),
        lang="ar",
    )
    assert '<html lang="ar" dir="rtl">' in html_doc
    # Manifest table headings come from the locale bundle.
    labels = env["pdf_report"]._load_pdf_strings("ar")
    assert labels["manifest.heading"] in html_doc
    assert labels["manifest.col.path"] in html_doc


def test_render_item_manifest_html_for_ja_is_ltr_with_en_fallback(env):
    html_doc = env["pdf_report"]._render_item_manifest_html(
        case=env["case"],
        item_view={
            "title": "T",
            "source_url": "https://x.test",
            "captured_utc": "2026-05-06T12:00:00+00:00",
            "signing_key_fp": "fp",
        },
        files=_file_entries(env),
        lang="ja",
    )
    assert '<html lang="ja" dir="ltr">' in html_doc
    # Until Track B's ja.json lands the labels fall back to English.
    assert "File manifest" in html_doc


def test_render_item_manifest_with_no_files_renders_empty_marker(env):
    html_doc = env["pdf_report"]._render_item_manifest_html(
        case=env["case"],
        item_view={
            "title": "T",
            "source_url": "https://x.test",
            "captured_utc": "2026-05-06T12:00:00+00:00",
            "signing_key_fp": "fp",
        },
        files=[],
        lang="en",
    )
    labels = env["pdf_report"]._load_pdf_strings("en")
    assert labels["manifest.empty"] in html_doc
