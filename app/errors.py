"""yt-dlp stderr → translatable error key (CLAUDE.md §4.7).

The frontend renders ``{headline, suggested_action, technical_details}``
where the headline is an ICU MessageFormat string sourced from the active
i18n bundle. The mapping table below is the seed; extend as new failure
modes show up in the wild.

Patterns are searched in order; the **first match wins**. Keep the most
specific patterns at the top so e.g. "HTTP Error 429" doesn't get caught by
a generic "HTTP Error" rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

__all__ = [
    "SuggestedAction",
    "Severity",
    "ErrorClassification",
    "classify",
    "ERROR_PATTERNS",
]


SuggestedAction = str  # one of the constants below, or None.

ACTION_CHECK_UPDATE: Final = "check_update"
ACTION_ADD_COOKIES: Final = "add_cookies"
ACTION_TRY_AGAIN: Final = "try_again"
ACTION_OPEN_LOGS: Final = "open_logs"

# Severity drives the orchestrator's retry decision (CLAUDE.md plan §U5):
#   transient — network/timeout/5xx/429 — retry with backoff
#   permanent — 404, removed, geo-block — surface and stop
#   internal  — bug or missing tooling — surface; user action required
Severity = Literal["transient", "permanent", "internal"]


@dataclass(frozen=True)
class ErrorClassification:
    i18n_key: str
    suggested_action: SuggestedAction | None
    severity: Severity = "internal"


# ``(pattern, i18n_key, suggested_action, severity)``. Patterns are case-insensitive.
ERROR_PATTERNS: list[tuple[re.Pattern[str], str, SuggestedAction | None, Severity]] = [
    # gallery-dl rate limiting — must come before the generic 429 catch
    # below so the gallery-specific i18n key (which suggests "wait until
    # the listed time") wins over the generic "try again in 5 minutes."
    # gallery-dl prints "Waiting until HH:MM:SS (rate limit)" and pauses
    # internally; if the orchestrator surfaces it that means it bubbled
    # up as a non-zero exit.
    (
        re.compile(r"Waiting until .* \(rate limit\)|rate ?limit", re.IGNORECASE),
        "errors.gallery_rate_limited",
        ACTION_TRY_AGAIN,
        "transient",
    ),
    # gallery-dl auth wall — Pixiv, Twitter, Instagram all surface
    # "AuthenticationError" or "Login rejected" here when cookies are
    # missing or expired. We tag this as ``permanent`` so the orchestrator
    # doesn't retry; the user action is "add cookies."
    (
        re.compile(
            r"AuthenticationError|Login (rejected|required)|Authorization required",
            re.IGNORECASE,
        ),
        "errors.gallery_auth_required",
        ACTION_ADD_COOKIES,
        "permanent",
    ),
    (
        re.compile(r"HTTP Error 429", re.IGNORECASE),
        "errors.rate_limited",
        ACTION_TRY_AGAIN,
        "transient",
    ),
    (
        re.compile(r"HTTP Error 5\d\d", re.IGNORECASE),
        "errors.network",
        ACTION_TRY_AGAIN,
        "transient",
    ),
    (
        re.compile(r"Bad guest token", re.IGNORECASE),
        "errors.blocked",
        ACTION_ADD_COOKIES,
        "permanent",
    ),
    (
        re.compile(r"HTTP Error 403|Sign in to confirm", re.IGNORECASE),
        "errors.blocked",
        ACTION_ADD_COOKIES,
        "permanent",
    ),
    (
        re.compile(r"Video unavailable|Private video", re.IGNORECASE),
        "errors.unavailable",
        None,
        "permanent",
    ),
    (
        re.compile(r"Unsupported URL|No video formats found", re.IGNORECASE),
        "errors.no_media",
        None,
        "permanent",
    ),
    (
        re.compile(r"ffmpeg not found|ffprobe not found", re.IGNORECASE),
        "errors.internal",
        ACTION_OPEN_LOGS,
        "internal",
    ),
    (
        re.compile(
            r"getaddrinfo"
            r"|Connection refused"
            r"|Could not resolve host"
            r"|Network is unreachable"
            r"|Connection reset"
            r"|Read timed out"
            r"|EOF occurred"
            r"|TLS.*timeout"
            r"|timed out",
            re.IGNORECASE,
        ),
        "errors.network",
        ACTION_TRY_AGAIN,
        "transient",
    ),
    (
        re.compile(r"extractor.*outdated|please update", re.IGNORECASE),
        "errors.extractor_outdated",
        ACTION_CHECK_UPDATE,
        "permanent",
    ),
    # gallery-dl "no extractor matched this URL" — the user pasted
    # something neither yt-dlp nor gallery-dl recognizes. Distinct from
    # ``errors.no_media`` (yt-dlp 'No video formats found' for a
    # supported site) so the headline can read "no images on this page"
    # rather than "no media."
    (
        re.compile(r"NoExtractorError|no extractor found", re.IGNORECASE),
        "errors.gallery_no_images",
        None,
        "permanent",
    ),
]


def classify(stderr: str) -> ErrorClassification:
    """Map yt-dlp stderr to an i18n key + suggested action + severity.

    Falls back to ``errors.unknown`` when nothing matches. ``stderr`` may
    be an empty string — that still maps to ``errors.unknown``.
    Unknown errors default to ``internal`` severity (no automatic retry):
    when we can't reason about what went wrong, surface it.
    """
    if stderr:
        for pattern, key, action, severity in ERROR_PATTERNS:
            if pattern.search(stderr):
                return ErrorClassification(
                    i18n_key=key,
                    suggested_action=action,
                    severity=severity,
                )
    return ErrorClassification(
        i18n_key="errors.unknown",
        suggested_action=ACTION_OPEN_LOGS,
        severity="internal",
    )
