"""Tests for the response-header sanitizer and HAR redactor used by the
Playwright capture path. Both exist to keep cookie / authorization values
out of meta.json and the page_har sidecar — defense in depth on top of
audit.py's substring guard."""

from __future__ import annotations

import json
from pathlib import Path


def test_sanitize_response_headers_drops_cookie_and_authorization():
    from app.capture import _sanitize_response_headers
    out = _sanitize_response_headers({
        "Set-Cookie": "id=abc",
        "Cookie": "session=secret",
        "Authorization": "Bearer x",
        "Proxy-Authorization": "Basic y",
        "Content-Type": "application/json",
        "X-Foo": "bar",
    })
    assert "Set-Cookie" not in out
    assert "Cookie" not in out
    assert "Authorization" not in out
    assert "Proxy-Authorization" not in out
    assert out["Content-Type"] == "application/json"
    assert out["X-Foo"] == "bar"


def test_sanitize_response_headers_handles_none_and_empty():
    from app.capture import _sanitize_response_headers
    assert _sanitize_response_headers(None) == {}
    assert _sanitize_response_headers({}) == {}


def test_sanitize_response_headers_lower_case_match():
    """Substring match is case-insensitive — 'set-cookie', 'COOKIE',
    'cookie2' must all be dropped."""
    from app.capture import _sanitize_response_headers
    out = _sanitize_response_headers({
        "set-cookie": "v",
        "COOKIE": "v",
        "Cookie2": "v",
        "Stay": "y",
    })
    assert list(out.keys()) == ["Stay"]


def test_redact_har_strips_sensitive_headers_and_cookies(tmp_path: Path):
    from app.capture import _redact_har_in_place
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "headers": [
                            {"name": "Cookie", "value": "sid=secret"},
                            {"name": "User-Agent", "value": "Mozilla/5.0"},
                        ],
                        "cookies": [{"name": "sid", "value": "secret"}],
                    },
                    "response": {
                        "headers": [
                            {"name": "Set-Cookie", "value": "leak=1"},
                            {"name": "Content-Type", "value": "text/html"},
                            {"name": "Authorization", "value": "Bearer y"},
                        ],
                        "cookies": [{"name": "leak", "value": "1"}],
                    },
                }
            ]
        }
    }
    p = tmp_path / "page.har"
    p.write_text(json.dumps(har), encoding="utf-8")
    _redact_har_in_place(p)
    redacted = json.loads(p.read_text(encoding="utf-8"))
    entries = redacted["log"]["entries"]
    req = entries[0]["request"]
    resp = entries[0]["response"]
    # cookies[] arrays are wiped on both sides.
    assert req["cookies"] == []
    assert resp["cookies"] == []
    # Sensitive headers are dropped on both sides.
    req_names = [h["name"].lower() for h in req["headers"]]
    resp_names = [h["name"].lower() for h in resp["headers"]]
    assert "cookie" not in req_names
    assert "set-cookie" not in resp_names
    assert "authorization" not in resp_names
    # Benign headers survive.
    assert any(h["name"] == "User-Agent" for h in req["headers"])
    assert any(h["name"] == "Content-Type" for h in resp["headers"])
    # Counter is stamped on the log so reviewers can see redaction happened.
    assert redacted["log"]["_capsule_redacted_header_count"] >= 3


def test_redact_har_silently_skips_missing_file(tmp_path: Path):
    from app.capture import _redact_har_in_place
    # Should not raise on a non-existent path.
    _redact_har_in_place(tmp_path / "absent.har")


def test_redact_har_tolerates_malformed_json(tmp_path: Path):
    from app.capture import _redact_har_in_place
    p = tmp_path / "broken.har"
    p.write_text("not json", encoding="utf-8")
    # Must not raise — the redactor is best-effort by design (the HAR is
    # an additive forensic artifact; if it's broken we surface that with
    # the file's checksum, not by crashing the capture).
    _redact_har_in_place(p)


def test_capture_report_includes_v7_fields():
    """CaptureReport.to_dict must surface every v7 field so postprocess
    can serialize them into meta.json.capture without further work."""
    from app.capture import CaptureReport
    d = CaptureReport().to_dict()
    expected = {
        "render_waits", "blocked_request_count", "blocked_requests_sample",
        "blocklist_version", "banner_hide_applied", "banner_hide_version",
        "tab_context_used",
        # v7
        "lazy_promoted_count", "lazy_load_max_height_px", "videos_paused",
        "animations_frozen", "animations_frozen_version",
        "shadow_dom_walked", "iframes_seen",
        "screenshot_truncated_at_px", "readiness_timed_out",
        "console_message_count", "console_error_count", "response",
        "media_context_captured", "media_context_selector",
        "warc",
    }
    assert expected.issubset(set(d.keys()))
    # warc sub-block has the four expected keys.
    warc = d["warc"]
    assert isinstance(warc, dict)
    assert {"captured_in_session", "record_count", "encoding_normalized", "format_version"}.issubset(warc.keys())
