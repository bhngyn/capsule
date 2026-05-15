"""Regression tests for the PDF report drift fixes (v0.6 + v0.11).

These tests lock in the rows that ``_format_capture_report`` and
``_format_download_options_section`` MUST emit when the corresponding
``meta.json`` fields are present. The fields land in ``meta.json``
today; the per-item PDF report rendered them only partially before
v0.12 closed the drift. If a future refactor drops one of these rows
the test fails immediately — a recipient won't silently lose forensic
signal in the only human-readable artifact at the top of the per-item
folder.

The tests operate on the HTML the PDF templates substitute into, not
on the rendered PDF bytes — WeasyPrint is heavy and adds nothing here
since the templating layer is what actually drops the rows.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def labels():
    """Load the English ``pdf.*`` / ``manifest.*`` label set."""
    from app import pdf_report

    importlib.reload(pdf_report)
    return pdf_report._load_pdf_strings("en")


# --- v0.6 capture-report counters --------------------------------------------


def test_capture_report_renders_videos_paused_label(labels):
    """videos_paused row exists, value visible, label is the localised key."""
    from app import pdf_report

    html_out = pdf_report._format_capture_report(
        {"videos_paused": 3},
        labels,
    )
    assert labels["pdf.report.field.capture.videos_paused.label"] in html_out
    # Value rendered as plain integer between <dd>...</dd>.
    assert ">3<" in html_out


def test_capture_report_renders_videos_paused_zero(labels):
    """Always-show pattern — row appears with value 0 when nothing paused."""
    from app import pdf_report

    html_out = pdf_report._format_capture_report({}, labels)
    assert labels["pdf.report.field.capture.videos_paused.label"] in html_out
    assert ">0<" in html_out


def test_capture_report_renders_lazy_promoted(labels):
    """lazy_promoted_count surfaced — was silent before v0.12."""
    from app import pdf_report

    html_out = pdf_report._format_capture_report(
        {"lazy_promoted_count": 17},
        labels,
    )
    assert labels["pdf.report.field.capture.lazy_promoted.label"] in html_out
    assert ">17<" in html_out


def test_capture_report_renders_readiness_timed_out_true(labels):
    """readiness_timed_out=true renders 'Yes' (forensically meaningful)."""
    from app import pdf_report

    html_out = pdf_report._format_capture_report(
        {"readiness_timed_out": True},
        labels,
    )
    assert labels["pdf.report.field.capture.readiness_timed_out.label"] in html_out
    assert labels["pdf.report.field.yes"] in html_out


def test_capture_report_renders_readiness_timed_out_false(labels):
    """readiness_timed_out=false renders 'No' (common case, still shown)."""
    from app import pdf_report

    html_out = pdf_report._format_capture_report(
        {"readiness_timed_out": False},
        labels,
    )
    assert labels["pdf.report.field.capture.readiness_timed_out.label"] in html_out
    # 'No' string from labels; the existing yes/no labels never overlap.
    assert labels["pdf.report.field.no"] in html_out


# --- v0.11 capture_mode in download-options section --------------------------


def test_download_options_renders_capture_mode_webpage(labels):
    """Webpage mode row uses the localised 'Webpage' enum value."""
    from app import pdf_report

    html_out = pdf_report._format_download_options_section(
        {"capture_mode": "webpage"},
        {},
        labels,
    )
    assert labels["pdf.report.field.download_options.capture_mode"] in html_out
    assert labels["pdf.report.field.download_options.capture_mode.webpage"] in html_out


def test_download_options_renders_capture_mode_media(labels):
    from app import pdf_report

    html_out = pdf_report._format_download_options_section(
        {"capture_mode": "media"},
        {},
        labels,
    )
    assert labels["pdf.report.field.download_options.capture_mode.media"] in html_out


def test_download_options_renders_capture_mode_gallery(labels):
    from app import pdf_report

    html_out = pdf_report._format_download_options_section(
        {"capture_mode": "gallery"},
        {},
        labels,
    )
    assert labels["pdf.report.field.download_options.capture_mode.gallery"] in html_out


def test_download_options_section_appears_when_only_capture_mode_set(labels):
    """The section's render gate must include capture_mode — earlier code
    would return '' if no other knob was non-default, silently swallowing
    the mode pick."""
    from app import pdf_report

    html_out = pdf_report._format_download_options_section(
        {"capture_mode": "webpage"},
        {},
        labels,
    )
    assert html_out != ""
    assert labels["pdf.report.heading.download_options"] in html_out


def test_download_options_omits_capture_mode_when_null(labels):
    """``None`` (default fallback routing) renders no row — the row is
    only meaningful when the investigator overrode the default."""
    from app import pdf_report

    html_out = pdf_report._format_download_options_section(
        {"capture_mode": None, "audio_only": True},
        {},
        labels,
    )
    assert labels["pdf.report.field.download_options.capture_mode"] not in html_out


def test_download_options_omits_capture_mode_for_unknown_value(labels):
    """A future enum value not yet in the bundle never reaches a label
    KeyError — the guard at the call site skips unknown values."""
    from app import pdf_report

    html_out = pdf_report._format_download_options_section(
        {"capture_mode": "experimental_v2", "audio_only": True},
        {},
        labels,
    )
    assert labels["pdf.report.field.download_options.capture_mode"] not in html_out


# --- i18n bundle parity ------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "ja", "ar", "es"])
def test_v06_counter_keys_present_in_every_bundle(lang):
    """All four locales must carry the new v0.6 counter labels — a CI
    check would fail the build if a translator missed one."""
    from app import pdf_report

    labels = pdf_report._load_pdf_strings(lang)
    assert labels["pdf.report.field.capture.videos_paused.label"]
    assert labels["pdf.report.field.capture.lazy_promoted.label"]
    assert labels["pdf.report.field.capture.readiness_timed_out.label"]


# --- v0.12 frozen-html row in the capture report ---------------------------


def test_capture_report_renders_frozen_html_generated(labels):
    """Generated frozen.html: row shows tier + counts."""
    from app import pdf_report

    html_out = pdf_report._format_capture_report(
        {"frozen_html": {
            "generated": True, "tier": "full",
            "inlined_image_count": 12, "external_image_count": 3,
            "shadow_root_omitted_count": 0,
        }},
        labels,
    )
    assert labels["pdf.report.field.capture.frozen_html.label"] in html_out
    # Localised tier word + the counts substituted in.
    assert labels["pdf.report.field.capture.frozen_html.tier.full"] in html_out
    assert "12" in html_out
    assert "3" in html_out


def test_capture_report_renders_frozen_html_size_budget_exceeded(labels):
    """Hard-cap omission: row shows the localised error explanation."""
    from app import pdf_report

    html_out = pdf_report._format_capture_report(
        {"frozen_html": {"generated": False, "error": "size_budget_exceeded"}},
        labels,
    )
    expected = labels["pdf.report.field.capture.frozen_html.error.size_budget_exceeded"]
    assert expected in html_out


def test_capture_report_renders_frozen_html_skipped(labels):
    """Default 'skipped' outcome: localised explanation."""
    from app import pdf_report

    html_out = pdf_report._format_capture_report(
        {"frozen_html": {"generated": False, "error": "skipped"}},
        labels,
    )
    assert labels["pdf.report.field.capture.frozen_html.error.skipped"] in html_out


def test_capture_report_legacy_record_without_frozen_html_block(labels):
    """A pre-v11 meta.json that omits the frozen_html block entirely
    must still render — the row falls through to the localised dash."""
    from app import pdf_report

    html_out = pdf_report._format_capture_report({}, labels)
    assert labels["pdf.report.field.capture.frozen_html.label"] in html_out


@pytest.mark.parametrize("lang", ["en", "ja", "ar", "es"])
def test_v12_frozen_html_keys_present_in_every_bundle(lang):
    from app import pdf_report

    labels = pdf_report._load_pdf_strings(lang)
    assert labels["pdf.report.field.capture.frozen_html.label"]
    assert labels["pdf.report.field.capture.frozen_html.summary"]
    assert labels["pdf.report.field.capture.frozen_html.tier.full"]
    assert labels["pdf.report.field.capture.frozen_html.error.size_budget_exceeded"]
