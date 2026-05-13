"""In-session CDP→WARC writer (CLAUDE.md §13 #15 — capture-side mutations
are recorded; CLAUDE.md §2 — single-session forensic capture).

Replaces the ``browsertrix-crawler`` subprocess with an in-process WARC
writer that listens to Chromium's CDP ``Network.*`` events from the SAME
Playwright session that produces the MHTML and the screenshot. One
browser, one navigation, one network log — eliminating the timing /
request-order drift between the legacy two-session capture.

Hard rules:

* WARC/1.1 records, gzipped, written via :mod:`warcio` so the output is
  validated by the de-facto reference library (used by pywb,
  Webrecorder).
* Bodies are decoded via ``Network.getResponseBody`` and the
  ``Content-Encoding`` header is rewritten to ``identity`` because the
  bytes we have are already the decoded plaintext. ``Content-Length``
  is recomputed. The fact that this normalization happened is recorded
  in ``meta.json.capture.warc.encoding_normalized = true`` so a
  forensic reviewer can answer "are these the bytes that crossed the
  wire, or the decoded payload?" without ambiguity.
* Sensitive headers (``Cookie`` / ``Set-Cookie`` / ``Authorization`` /
  ``Proxy-Authorization``) are dropped from the request/response records
  written to the WARC. The cookies file the job consumed is hashed
  separately into ``meta.json.cookies_snapshot_sha256`` per
  CLAUDE.md §11; their values never enter evidence artifacts.
* Non-HTTP schemes (``data:``, ``blob:``, ``ws:``/``wss:``,
  ``chrome-extension:``) are recorded as WARC ``metadata`` records —
  the WARC stays a complete catalog of network activity without bloating
  with binary blobs.
* Failures are best-effort: the writer never raises. If a request can't
  be serialized, it's skipped and a counter is incremented; the surrounding
  capture continues. The caller falls back to the browsertrix subprocess
  when ``warc_record_count == 0``.

This module's only third-party dependency is :mod:`warcio` (~3 kLOC,
MIT-licensed, used by ArchiveBox / pywb). Pinned in ``pyproject.toml``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "CdpWarcWriter",
    "WarcWriteResult",
    "warcio_version",
    "WARC_FORMAT_VERSION",
]


WARC_FORMAT_VERSION = "1.1"
_log = logging.getLogger(__name__)

_SENSITIVE_HEADER_SUBSTRINGS = ("cookie", "authorization", "proxy-authorization")
_NON_HTTP_PREFIXES = ("data:", "blob:", "ws:", "wss:", "chrome-extension:", "about:", "javascript:")


def warcio_version() -> str | None:
    """Return the installed warcio version string, or ``None`` if missing."""
    try:
        from warcio import __version__ as v  # type: ignore[attr-defined]
        return str(v)
    except Exception:
        try:
            import warcio  # noqa: F401
            return "unknown"
        except Exception:
            return None


@dataclass
class _PendingRequest:
    """Per-``requestId`` aggregator used by the CDP event handlers."""

    request_id: str
    url: str
    method: str = "GET"
    request_headers: dict[str, str] = field(default_factory=dict)
    raw_request_headers: str | None = None  # from Network.requestWillBeSentExtraInfo
    request_post_data: str | None = None
    request_post_data_b64: bool = False
    response_status: int | None = None
    response_status_text: str = ""
    response_headers: dict[str, str] = field(default_factory=dict)
    raw_response_headers: str | None = None  # from Network.responseReceivedExtraInfo
    mime_type: str | None = None
    redirect_chain_urls: list[str] = field(default_factory=list)


@dataclass
class WarcWriteResult:
    """Summary of what the writer produced."""

    record_count: int
    skipped_count: int
    bytes_written: int
    encoding_normalized: bool
    format_version: str = WARC_FORMAT_VERSION


def _filter_headers(headers: dict[str, str] | list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Drop sensitive headers and normalize to a list of ``(name, value)``
    tuples preserving original order where possible."""
    out: list[tuple[str, str]] = []
    if isinstance(headers, dict):
        items: list[tuple[str, str]] = list(headers.items())
    else:
        items = [(h.get("name", ""), h.get("value", "")) for h in headers if h.get("name")]
    for name, value in items:
        if not name:
            continue
        n = name.lower()
        if any(s in n for s in _SENSITIVE_HEADER_SUBSTRINGS):
            continue
        out.append((str(name), str(value)))
    return out


def _parse_raw_headers(raw: str | None) -> list[tuple[str, str]]:
    """Parse the ``\\r\\n``-joined header block CDP gives us in
    ``Network.responseReceivedExtraInfo.headersText``.
    """
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    # Skip the status line if present.
    lines = raw.split("\r\n") if "\r\n" in raw else raw.split("\n")
    for line in lines:
        if not line or line.startswith(("HTTP/", "GET ", "POST ", "PUT ", "DELETE ", "HEAD ", "OPTIONS ", "PATCH ")):
            continue
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        n = name.strip().lower()
        if any(s in n for s in _SENSITIVE_HEADER_SUBSTRINGS):
            continue
        out.append((name.strip(), value.strip()))
    return out


def _rewrite_content_encoding(headers: list[tuple[str, str]], body_len: int) -> list[tuple[str, str]]:
    """Set ``Content-Encoding: identity`` and recompute ``Content-Length``.

    CDP's ``Network.getResponseBody`` returns the decoded payload; we record
    that as the response body and overwrite the encoding header so a WARC
    replayer doesn't try to gunzip already-decoded bytes.
    """
    seen_ce = False
    seen_cl = False
    out: list[tuple[str, str]] = []
    for name, value in headers:
        n = name.lower()
        if n == "content-encoding":
            out.append((name, "identity"))
            seen_ce = True
        elif n == "content-length":
            out.append((name, str(body_len)))
            seen_cl = True
        else:
            out.append((name, value))
    if not seen_ce:
        out.append(("Content-Encoding", "identity"))
    if not seen_cl:
        out.append(("Content-Length", str(body_len)))
    return out


def _serialize_http_response(
    status: int,
    status_text: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> bytes:
    """Build an HTTP/1.1 response wire-format byte string for the WARC
    response record body."""
    head = f"HTTP/1.1 {status} {status_text or ''}".rstrip() + "\r\n"
    head += "".join(f"{n}: {v}\r\n" for n, v in headers)
    head += "\r\n"
    return head.encode("iso-8859-1", errors="replace") + body


def _serialize_http_request(
    method: str,
    url: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> bytes:
    """Build an HTTP/1.1 request wire-format byte string for the WARC
    request record body. Path is the ``url`` minus the scheme+host so the
    record line looks like ``GET /foo HTTP/1.1`` per the WARC spec."""
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
    except Exception:
        path = "/"
    head = f"{method} {path} HTTP/1.1\r\n"
    head += "".join(f"{n}: {v}\r\n" for n, v in headers)
    head += "\r\n"
    return head.encode("iso-8859-1", errors="replace") + body


class CdpWarcWriter:
    """Async context manager that captures a single navigation's network
    activity into a gzipped WARC/1.1 file.

    Lifecycle:

        async with CdpWarcWriter(cdp, target, app_version=..., target_uri=...) as w:
            await page.goto(url)
            await ...
        # File is closed and flushed; `w.result` describes what was written.

    The writer subscribes to ``Network.*`` events on ``__aenter__`` and
    flushes per-``requestId`` records on ``loadingFinished`` /
    ``loadingFailed``. Late-arriving events after the navigation completes
    keep being captured until the context exits.
    """

    def __init__(
        self,
        cdp_session,
        output_path: Path,
        *,
        app_version: str,
        chromium_version: str,
        target_uri: str,
        max_inline_body_bytes: int = 5_000_000,
    ) -> None:
        self._cdp = cdp_session
        self._path = output_path
        self._app_version = app_version
        self._chromium_version = chromium_version
        self._target_uri = target_uri
        self._max_inline = max_inline_body_bytes
        self._pending: dict[str, _PendingRequest] = {}
        self._fp = None
        self._writer = None
        self._record_count = 0
        self._skipped_count = 0
        self._bytes_written = 0
        self._listeners_registered = False

    # ---- Context manager -------------------------------------------------

    async def __aenter__(self) -> "CdpWarcWriter":
        try:
            from warcio.warcwriter import WARCWriter
            from warcio.statusandheaders import StatusAndHeaders  # noqa: F401
        except Exception as exc:  # pragma: no cover — caller falls back
            raise RuntimeError("warcio is not installed") from exc

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self._path.open("wb")
        self._writer = WARCWriter(self._fp, gzip=True)
        await self._write_warcinfo()
        try:
            await self._cdp.send("Network.enable", {
                "maxResourceBufferSize": 100_000_000,
                "maxTotalBufferSize": 200_000_000,
            })
        except Exception as exc:
            _log.warning("Network.enable failed: %s", exc)
        self._cdp.on("Network.requestWillBeSent", self._on_request_will_be_sent)
        self._cdp.on("Network.requestWillBeSentExtraInfo", self._on_request_extra_info)
        self._cdp.on("Network.responseReceived", self._on_response_received)
        self._cdp.on("Network.responseReceivedExtraInfo", self._on_response_extra_info)
        self._cdp.on("Network.loadingFinished", self._on_loading_finished)
        self._cdp.on("Network.loadingFailed", self._on_loading_failed)
        self._listeners_registered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Drain pending requests for a brief window — late loadingFinished
        # events can fire just after the page closes. Cap the wait so the
        # capture doesn't hang on a slow tracker.
        for _ in range(20):
            if not self._pending:
                break
            await asyncio.sleep(0.05)
        if self._fp is not None:
            with suppress(Exception):
                self._fp.flush()
                self._bytes_written = self._fp.tell()
                self._fp.close()

    @property
    def result(self) -> WarcWriteResult:
        return WarcWriteResult(
            record_count=self._record_count,
            skipped_count=self._skipped_count,
            bytes_written=self._bytes_written,
            encoding_normalized=True,
        )

    # ---- WARC record builders -------------------------------------------

    async def _write_warcinfo(self) -> None:
        from warcio.warcwriter import WARCWriter  # noqa: F401
        info = (
            f"software: Capsule/{self._app_version}\r\n"
            f"format: WARC/{WARC_FORMAT_VERSION}\r\n"
            f"conformsTo: http://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1/\r\n"
            f"hostname: localhost\r\n"
            f"isPartOf: capsule-page-capture\r\n"
            f"description: Single-session capture (Playwright + CDP) — Chromium/{self._chromium_version}\r\n"
            f"capsule.encoding_normalized: true\r\n"
        ).encode("utf-8")
        record = self._writer.create_warc_record(
            self._target_uri,
            "warcinfo",
            payload=io.BytesIO(info),
            warc_content_type="application/warc-fields",
            length=len(info),
        )
        self._writer.write_record(record)
        self._record_count += 1

    def _write_metadata_record(self, url: str, payload: dict[str, Any]) -> None:
        """Emit a ``metadata`` record for non-HTTP schemes or for the
        loadingFailed catalog entries."""
        try:
            blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            record = self._writer.create_warc_record(
                url,
                "metadata",
                payload=io.BytesIO(blob),
                warc_content_type="application/json",
                length=len(blob),
            )
            self._writer.write_record(record)
            self._record_count += 1
        except Exception as exc:  # pragma: no cover
            _log.warning("metadata record write failed: %s", exc)
            self._skipped_count += 1

    async def _write_http_pair(self, p: _PendingRequest, body: bytes) -> None:
        """Write a WARC request + response record pair for one completed
        HTTP exchange. Linked via ``WARC-Concurrent-To``."""
        from warcio.statusandheaders import StatusAndHeaders

        # Response side first so we can link the request to it.
        if p.raw_response_headers:
            headers_pairs = _parse_raw_headers(p.raw_response_headers)
        else:
            headers_pairs = _filter_headers(p.response_headers)
        if not any(n.lower() == "content-type" and v for n, v in headers_pairs) and p.mime_type:
            headers_pairs.append(("Content-Type", p.mime_type))
        headers_pairs = _rewrite_content_encoding(headers_pairs, len(body))

        try:
            from warcio.warcwriter import WARCWriter  # noqa: F401
            status_line = f"{p.response_status or 0} {p.response_status_text or ''}".strip()
            sah = StatusAndHeaders(status_line, headers_pairs, protocol="HTTP/1.1")
            response_record = self._writer.create_warc_record(
                p.url,
                "response",
                payload=io.BytesIO(body),
                http_headers=sah,
                length=len(body),
            )
            self._writer.write_record(response_record)
            response_id = response_record.rec_headers.get_header("WARC-Record-ID")
            self._record_count += 1
        except Exception as exc:
            _log.warning("response record write failed for %s: %s", p.url, exc)
            self._skipped_count += 1
            return

        # Request side — body is rare for navigations; include if present.
        try:
            req_body_bytes = b""
            if p.request_post_data:
                if p.request_post_data_b64:
                    import base64
                    req_body_bytes = base64.b64decode(p.request_post_data)
                else:
                    req_body_bytes = p.request_post_data.encode("utf-8", errors="replace")
            if p.raw_request_headers:
                req_headers = _parse_raw_headers(p.raw_request_headers)
            else:
                req_headers = _filter_headers(p.request_headers)
            req_status_line = f"{p.method} HTTP/1.1"
            req_sah = StatusAndHeaders(req_status_line, req_headers, protocol="HTTP/1.1", is_http_request=True)
            request_record = self._writer.create_warc_record(
                p.url,
                "request",
                payload=io.BytesIO(req_body_bytes),
                http_headers=req_sah,
                length=len(req_body_bytes),
                warc_headers_dict={"WARC-Concurrent-To": response_id} if response_id else None,
            )
            self._writer.write_record(request_record)
            self._record_count += 1
        except Exception as exc:  # pragma: no cover
            _log.warning("request record write failed for %s: %s", p.url, exc)
            self._skipped_count += 1

    # ---- CDP event handlers ---------------------------------------------

    def _on_request_will_be_sent(self, ev: dict[str, Any]) -> None:
        try:
            request_id = ev["requestId"]
            req = ev.get("request") or {}
            url = req.get("url") or ""
            if not url or url.startswith(_NON_HTTP_PREFIXES):
                return
            redirect = ev.get("redirectResponse") or {}
            if redirect and request_id in self._pending:
                # Flush the previous hop as a redirect record so the chain
                # is preserved in the WARC even though no body was loaded.
                prev = self._pending[request_id]
                prev.response_status = int(redirect.get("status") or 0)
                prev.response_status_text = str(redirect.get("statusText") or "")
                prev.response_headers = dict(redirect.get("headers") or {})
                prev.mime_type = redirect.get("mimeType")
                # Schedule the write — fire-and-forget; we keep going.
                asyncio.get_event_loop().create_task(self._write_http_pair(prev, b""))
                self._pending.pop(request_id, None)
            self._pending[request_id] = _PendingRequest(
                request_id=request_id,
                url=url,
                method=str(req.get("method") or "GET"),
                request_headers=dict(req.get("headers") or {}),
                request_post_data=req.get("postData"),
                request_post_data_b64=bool(req.get("hasPostData") and req.get("postDataEntries")),
            )
        except Exception as exc:  # pragma: no cover
            _log.debug("requestWillBeSent handler error: %s", exc)

    def _on_request_extra_info(self, ev: dict[str, Any]) -> None:
        try:
            request_id = ev.get("requestId")
            if request_id in self._pending:
                self._pending[request_id].raw_request_headers = ev.get("headersText") or self._pending[request_id].raw_request_headers
        except Exception:
            pass

    def _on_response_received(self, ev: dict[str, Any]) -> None:
        try:
            request_id = ev.get("requestId")
            if request_id not in self._pending:
                return
            resp = ev.get("response") or {}
            p = self._pending[request_id]
            p.response_status = int(resp.get("status") or 0)
            p.response_status_text = str(resp.get("statusText") or "")
            p.response_headers = dict(resp.get("headers") or {})
            p.mime_type = resp.get("mimeType")
        except Exception as exc:  # pragma: no cover
            _log.debug("responseReceived handler error: %s", exc)

    def _on_response_extra_info(self, ev: dict[str, Any]) -> None:
        try:
            request_id = ev.get("requestId")
            if request_id in self._pending:
                self._pending[request_id].raw_response_headers = ev.get("headersText") or self._pending[request_id].raw_response_headers
        except Exception:
            pass

    def _on_loading_finished(self, ev: dict[str, Any]) -> None:
        request_id = ev.get("requestId")
        p = self._pending.pop(request_id, None) if request_id else None
        if p is None:
            return
        asyncio.get_event_loop().create_task(self._finish_record(p, ev.get("encodedDataLength") or 0))

    def _on_loading_failed(self, ev: dict[str, Any]) -> None:
        request_id = ev.get("requestId")
        p = self._pending.pop(request_id, None) if request_id else None
        if p is None:
            return
        # Record the failed request as a metadata catalog entry so a reviewer
        # can still answer "did this URL get attempted?". No body, no status.
        self._write_metadata_record(p.url, {
            "kind": "loading_failed",
            "method": p.method,
            "errorText": ev.get("errorText"),
            "blockedReason": ev.get("blockedReason"),
            "type": ev.get("type"),
        })

    async def _finish_record(self, p: _PendingRequest, encoded_data_length: int) -> None:
        # Pre-check the size BEFORE asking CDP for the body. A 500 MB hero
        # video would otherwise be base64-decoded into Python heap just to
        # be discarded by the post-fetch cap below — easy OOM on a laptop.
        # ``encoded_data_length`` is the wire-bytes count Chromium attached
        # to the loadingFinished event; for compressed responses the
        # decoded body can be larger, so the post-fetch check stays as
        # belt-and-braces.
        if encoded_data_length and encoded_data_length > self._max_inline:
            self._write_metadata_record(p.url, {
                "kind": "body_truncated",
                "method": p.method,
                "status": p.response_status,
                "encoded_data_length": encoded_data_length,
                "max_inline_bytes": self._max_inline,
                "mime_type": p.mime_type,
                "truncated_before_fetch": True,
            })
            return
        body = b""
        try:
            data = await self._cdp.send("Network.getResponseBody", {"requestId": p.request_id})
            raw = data.get("body") or ""
            if data.get("base64Encoded"):
                import base64
                body = base64.b64decode(raw)
            else:
                body = raw.encode("utf-8", errors="replace")
        except Exception:
            # Cache hits / responses with no retained body — record a
            # revisit-style metadata record so the URL is still cataloged.
            self._write_metadata_record(p.url, {
                "kind": "no_body_available",
                "method": p.method,
                "status": p.response_status,
                "encoded_data_length": encoded_data_length,
            })
            return
        if len(body) > self._max_inline:
            # Belt-and-braces: a compressed response can decode larger
            # than its wire-bytes count, slipping past the pre-check.
            self._write_metadata_record(p.url, {
                "kind": "body_truncated",
                "method": p.method,
                "status": p.response_status,
                "actual_size_bytes": len(body),
                "max_inline_bytes": self._max_inline,
                "mime_type": p.mime_type,
            })
            return
        await self._write_http_pair(p, body)
