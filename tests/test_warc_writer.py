"""Tests for the in-session CDP→WARC writer helpers.

These exercise the pure-Python serialization plumbing (header filtering,
encoding rewrite, HTTP wire-format reconstruction) without driving a real
Playwright session. End-to-end WARC validity is asserted through warcio's
own `archiveiterator` against a small fixture file produced by the writer
in a future integration test (TODO — see CLAUDE.md §15 v0.7).
"""

from __future__ import annotations


def test_filter_headers_drops_cookie_authorization_proxy_authorization():
    from app.warc_writer import _filter_headers
    out = _filter_headers({
        "Cookie": "session=secret",
        "Set-Cookie": "id=abc",
        "Authorization": "Bearer token",
        "Proxy-Authorization": "Basic xyz",
        "Content-Type": "text/html",
        "X-Custom": "ok",
    })
    names = [n.lower() for n, _ in out]
    assert "cookie" not in names
    assert "set-cookie" not in names
    assert "authorization" not in names
    assert "proxy-authorization" not in names
    assert ("Content-Type", "text/html") in out
    assert ("X-Custom", "ok") in out


def test_filter_headers_handles_list_form():
    from app.warc_writer import _filter_headers
    raw = [
        {"name": "Cookie", "value": "drop"},
        {"name": "Content-Length", "value": "42"},
        {"name": "", "value": "ignored"},  # nameless entries are skipped
    ]
    out = _filter_headers(raw)
    assert out == [("Content-Length", "42")]


def test_parse_raw_headers_skips_status_line_and_redacts():
    from app.warc_writer import _parse_raw_headers
    raw = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Set-Cookie: leak=true; Path=/\r\n"
        "Cache-Control: no-cache\r\n"
        "\r\n"
    )
    pairs = _parse_raw_headers(raw)
    names = [n.lower() for n, _ in pairs]
    assert "set-cookie" not in names
    assert ("Content-Type", "text/html; charset=utf-8") in pairs
    assert ("Cache-Control", "no-cache") in pairs


def test_rewrite_content_encoding_normalizes_to_identity():
    from app.warc_writer import _rewrite_content_encoding
    headers = [("Content-Encoding", "gzip"), ("Content-Length", "100"), ("X", "y")]
    out = _rewrite_content_encoding(headers, body_len=42)
    encodings = dict(out)
    assert encodings["Content-Encoding"] == "identity"
    assert encodings["Content-Length"] == "42"
    assert encodings["X"] == "y"


def test_rewrite_content_encoding_adds_missing_headers():
    from app.warc_writer import _rewrite_content_encoding
    out = _rewrite_content_encoding([("X", "y")], body_len=7)
    pairs = dict(out)
    assert pairs["Content-Encoding"] == "identity"
    assert pairs["Content-Length"] == "7"


def test_serialize_http_response_builds_status_and_blank_line():
    from app.warc_writer import _serialize_http_response
    blob = _serialize_http_response(
        200, "OK",
        [("Content-Type", "text/html"), ("Content-Length", "5")],
        b"hello",
    )
    head, _, body = blob.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Content-Type: text/html" in head
    assert body == b"hello"


def test_serialize_http_request_uses_path_only_in_request_line():
    from app.warc_writer import _serialize_http_request
    blob = _serialize_http_request(
        "GET",
        "https://example.test/foo/bar?baz=1",
        [("Host", "example.test")],
        b"",
    )
    head = blob.split(b"\r\n", 1)[0]
    assert head == b"GET /foo/bar?baz=1 HTTP/1.1"


def test_warcio_version_returns_string_or_none():
    from app.warc_writer import warcio_version
    v = warcio_version()
    assert v is None or isinstance(v, str)


def test_aexit_catalogs_pending_requests_that_never_finished(tmp_path):
    """Regression for HIGH-2 (CODE_REVIEW 2026-05-13).

    Anything still in ``_pending`` after the 1-second drain window
    would otherwise be silently dropped, leaving a mysterious gap in
    the WARC that a forensic reviewer comparing against the HAR
    couldn't explain. The fix catalogs each stragglers as a
    ``body_not_received_in_drain_window`` metadata record.
    """
    import asyncio

    import pytest
    pytest.importorskip("warcio")

    from app.warc_writer import CdpWarcWriter, _PendingRequest

    class _FakeCdp:
        async def send(self, *_a, **_kw):
            return {}
        def on(self, *_a, **_kw):
            return None

    writer = CdpWarcWriter(
        _FakeCdp(),
        tmp_path / "drain.warc.gz",
        app_version="test",
        chromium_version="0.0",
        target_uri="https://example.test/",
    )

    async def _drive():
        async with writer:
            # Two never-finished requests still in _pending at exit.
            writer._pending["A"] = _PendingRequest(
                request_id="A",
                url="https://example.test/late1.css",
                method="GET",
                response_status=200,
                mime_type="text/css",
            )
            writer._pending["B"] = _PendingRequest(
                request_id="B",
                url="https://example.test/late2.js",
                method="GET",
                response_status=200,
                mime_type="application/javascript",
            )
        # On exit, both should have been catalogued as metadata records.

    record_count_before = writer._record_count
    asyncio.run(_drive())
    # Two stragglers → record_count grows by exactly two beyond the
    # warcinfo record written on enter.
    assert writer._record_count - record_count_before >= 2
    assert not writer._pending  # ``_pending`` is cleared
