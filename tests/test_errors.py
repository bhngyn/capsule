"""yt-dlp error classification — CLAUDE.md §4.7."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import errors


@pytest.mark.parametrize(
    "stderr, expected_key, expected_action, expected_severity",
    [
        (
            "ERROR: unable to download video data: HTTP Error 429: Too Many Requests",
            "errors.rate_limited",
            errors.ACTION_TRY_AGAIN,
            "transient",
        ),
        (
            "ERROR: HTTP Error 503: Service Unavailable",
            "errors.network",
            errors.ACTION_TRY_AGAIN,
            "transient",
        ),
        (
            "ERROR: HTTP Error 403: Forbidden",
            "errors.blocked",
            errors.ACTION_ADD_COOKIES,
            "permanent",
        ),
        (
            "ERROR: Sign in to confirm you're not a bot",
            "errors.blocked",
            errors.ACTION_ADD_COOKIES,
            "permanent",
        ),
        (
            "ERROR: [twitter] 12345: Error(s) while querying API: Bad guest token; "
            "please report this issue on https://github.com/yt-dlp/yt-dlp/issues",
            "errors.blocked",
            errors.ACTION_ADD_COOKIES,
            "permanent",
        ),
        (
            "ERROR: Video unavailable",
            "errors.unavailable",
            None,
            "permanent",
        ),
        (
            "ERROR: Private video. Sign in if you've been granted access.",
            "errors.unavailable",
            None,
            "permanent",
        ),
        (
            "ERROR: Unsupported URL: https://example.com/foo",
            "errors.no_media",
            None,
            "permanent",
        ),
        (
            "ERROR: No video formats found",
            "errors.no_media",
            None,
            "permanent",
        ),
        (
            "ERROR: ffmpeg not found. Please install or provide the path.",
            "errors.internal",
            errors.ACTION_OPEN_LOGS,
            "internal",
        ),
        (
            "ERROR: <urlopen error [Errno -2] getaddrinfo failed>",
            "errors.network",
            errors.ACTION_TRY_AGAIN,
            "transient",
        ),
        (
            "ERROR: Could not resolve host: youtu.be",
            "errors.network",
            errors.ACTION_TRY_AGAIN,
            "transient",
        ),
        (
            "ERROR: Connection reset by peer",
            "errors.network",
            errors.ACTION_TRY_AGAIN,
            "transient",
        ),
        (
            "ERROR: Read timed out.",
            "errors.network",
            errors.ACTION_TRY_AGAIN,
            "transient",
        ),
        (
            "ERROR: This extractor is outdated. Please update yt-dlp.",
            "errors.extractor_outdated",
            errors.ACTION_CHECK_UPDATE,
            "permanent",
        ),
    ],
)
def test_classification_table(stderr, expected_key, expected_action, expected_severity):
    out = errors.classify(stderr)
    assert out.i18n_key == expected_key
    assert out.suggested_action == expected_action
    assert out.severity == expected_severity


def test_unknown_falls_through():
    out = errors.classify("ERROR: something we have never seen")
    assert out.i18n_key == "errors.unknown"
    assert out.suggested_action == errors.ACTION_OPEN_LOGS
    assert out.severity == "internal"


def test_empty_stderr():
    out = errors.classify("")
    assert out.i18n_key == "errors.unknown"


def test_first_match_wins_for_429_vs_403():
    """Make sure HTTP Error 429 is not caught by the 403 rule."""
    out = errors.classify("HTTP Error 429: Too Many Requests")
    assert out.i18n_key == "errors.rate_limited"


def test_every_pattern_has_an_i18n_key():
    """Each error key referenced by ``errors.py`` must exist in en.json."""
    en = json.loads(
        Path("app/i18n/en.json").read_text(encoding="utf-8")
    )
    keys = {k for _, k, _, _ in errors.ERROR_PATTERNS} | {"errors.unknown"}
    missing = keys - set(en)
    assert missing == set(), f"missing i18n keys: {missing}"


def test_arabic_bundle_has_same_keys_as_english():
    en = json.loads(Path("app/i18n/en.json").read_text(encoding="utf-8"))
    ar = json.loads(Path("app/i18n/ar.json").read_text(encoding="utf-8"))
    en_errors = {k for k in en if k.startswith("errors.")}
    ar_errors = {k for k in ar if k.startswith("errors.")}
    assert en_errors == ar_errors
