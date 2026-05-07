"""Page-snapshot capture — CLAUDE.md §2, §6."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture
def capture_module(capsule_dirs):
    from app import capture as c
    from app import cookies as cookies_mod

    importlib.reload(cookies_mod)
    importlib.reload(c)
    return c


def test_netscape_to_playwright_basic(capture_module):
    sample = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t9999999999\tSID\tabc\n"
        "#HttpOnly_youtube.com\tTRUE\t/\tTRUE\t9999999999\tHSID\tdef\n"
        ".x.com\tTRUE\t/\tFALSE\t0\tsess\txyz\n"
    )
    cookies = capture_module._netscape_to_playwright_cookies(sample)
    assert len(cookies) == 3
    by_name = {c["name"]: c for c in cookies}
    assert by_name["SID"]["domain"] == "youtube.com"
    assert by_name["SID"]["secure"] is True
    assert by_name["SID"]["expires"] == 9999999999
    assert by_name["HSID"]["httpOnly"] is True
    # Session cookie has no ``expires`` field.
    assert "expires" not in by_name["sess"]
    assert by_name["sess"]["secure"] is False


def test_netscape_to_playwright_skips_comments_and_blanks(capture_module):
    sample = "\n# only comment\n\n\n"
    assert capture_module._netscape_to_playwright_cookies(sample) == []


def test_netscape_to_playwright_skips_malformed(capture_module):
    """Malformed lines are silently dropped — cookies.py is the
    validating boundary; capture.py just translates whatever it gets."""
    sample = "junk_line_with_no_tabs\n"
    assert capture_module._netscape_to_playwright_cookies(sample) == []


def test_load_cookies_for_returns_empty_without_case(capture_module):
    assert capture_module._load_cookies_for(None) == []
    assert capture_module._load_cookies_for("missing") == []


def test_browsertrix_available_reflects_path(capture_module, monkeypatch):
    monkeypatch.setattr("app.capture.shutil.which", lambda name: None)
    assert capture_module.browsertrix_available() is False
    monkeypatch.setattr(
        "app.capture.shutil.which",
        lambda name: "/usr/local/bin/browsertrix-crawler",
    )
    assert capture_module.browsertrix_available() is True


@pytest.mark.asyncio
async def test_browsertrix_warc_returns_none_when_missing(
    capture_module, monkeypatch, tmp_path
):
    monkeypatch.setattr("app.capture.shutil.which", lambda name: None)
    warc, version = await capture_module._browsertrix_warc(
        url="https://example.com/", out_dir=tmp_path, case_cookies_path=None,
    )
    assert warc is None
    assert version == "0"


@pytest.mark.asyncio
async def test_browsertrix_warc_retries_until_success(
    capture_module, monkeypatch, tmp_path
):
    """The outer retry envelope (plan §U3) should retry a transient failure."""
    monkeypatch.setattr(
        "app.capture.shutil.which", lambda name: "/fake/browsertrix-crawler",
    )

    calls = {"n": 0}

    async def fake_attempt(*, attempt_idx, out_dir, **_kw):
        calls["n"] += 1
        if attempt_idx < 3:
            return None
        target = out_dir / "page.warc.gz"
        target.write_bytes(b"PK")
        return target

    async def fake_version(*_a, **_k):
        class _P:
            returncode = 0
            async def communicate(self):
                return (b"1.2.3", b"")
        return _P()

    monkeypatch.setattr(capture_module, "_browsertrix_attempt", fake_attempt)
    monkeypatch.setattr(
        "app.capture.asyncio.create_subprocess_exec", fake_version,
    )
    # No real backoff sleep needed in tests.
    monkeypatch.setattr("app.capture.asyncio.sleep", lambda *_a, **_k: _noop())

    warc, version = await capture_module._browsertrix_warc(
        url="https://example.com/",
        out_dir=tmp_path,
        case_cookies_path=None,
        attempts=3,
        timeout_s=5,
    )
    assert calls["n"] == 3
    assert warc is not None
    assert version == "1.2.3"


@pytest.mark.asyncio
async def test_browsertrix_warc_gives_up_after_attempts_cap(
    capture_module, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "app.capture.shutil.which", lambda name: "/fake/browsertrix-crawler",
    )

    calls = {"n": 0}

    async def fake_attempt(**_kw):
        calls["n"] += 1
        return None

    async def fake_version(*_a, **_k):
        class _P:
            returncode = 0
            async def communicate(self):
                return (b"1.2.3", b"")
        return _P()

    monkeypatch.setattr(capture_module, "_browsertrix_attempt", fake_attempt)
    monkeypatch.setattr(
        "app.capture.asyncio.create_subprocess_exec", fake_version,
    )
    monkeypatch.setattr("app.capture.asyncio.sleep", lambda *_a, **_k: _noop())

    warc, _ = await capture_module._browsertrix_warc(
        url="https://example.com/",
        out_dir=tmp_path,
        case_cookies_path=None,
        attempts=3,
        timeout_s=5,
    )
    assert warc is None
    assert calls["n"] == 3


async def _noop():
    return None


@pytest.mark.asyncio
async def test_browsertrix_attempt_includes_proxy_when_set(
    capture_module, monkeypatch, tmp_path
):
    """Plan §U8: WARC traffic must traverse the per-case proxy when set."""
    captured: dict[str, list[str]] = {}

    async def fake_exec(*argv, **_kw):
        captured["argv"] = list(argv)
        class _P:
            returncode = 0
            async def communicate(self):
                return (b"", b"")
            async def wait(self):
                return 0
            def kill(self):
                return None
        return _P()

    monkeypatch.setattr(
        "app.capture.asyncio.create_subprocess_exec", fake_exec,
    )

    await capture_module._browsertrix_attempt(
        binary="/fake/browsertrix-crawler",
        url="https://example.com/",
        out_dir=tmp_path,
        case_cookies_path=None,
        timeout_s=5,
        attempt_idx=1,
        proxy_url="socks5h://127.0.0.1:1080",
    )
    argv = captured["argv"]
    i = argv.index("--proxyServer")
    assert argv[i + 1] == "socks5h://127.0.0.1:1080"


@pytest.mark.asyncio
async def test_browsertrix_attempt_omits_proxy_when_unset(
    capture_module, monkeypatch, tmp_path
):
    captured: dict[str, list[str]] = {}

    async def fake_exec(*argv, **_kw):
        captured["argv"] = list(argv)
        class _P:
            returncode = 0
            async def communicate(self):
                return (b"", b"")
            async def wait(self):
                return 0
            def kill(self):
                return None
        return _P()

    monkeypatch.setattr(
        "app.capture.asyncio.create_subprocess_exec", fake_exec,
    )

    await capture_module._browsertrix_attempt(
        binary="/fake/browsertrix-crawler",
        url="https://example.com/",
        out_dir=tmp_path,
        case_cookies_path=None,
        timeout_s=5,
        attempt_idx=1,
    )
    assert "--proxyServer" not in captured["argv"]


# --- Real Playwright test (gated; ~3s when run) -----------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("CAPSULE_SKIP_E2E") == "1",
    reason="set CAPSULE_SKIP_E2E=1 to skip Playwright e2e",
)
async def test_capture_data_url_end_to_end(capture_module, tmp_path):
    """Drive Playwright against a data: URL — no network, but exercises the
    real CDP capture code path. Skipped if Chromium isn't installed."""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            await browser.close()
    except Exception:
        pytest.skip("Chromium not available in this environment")

    url = "data:text/html,<html><head><title>Hi</title></head><body><h1>Hello</h1></body></html>"
    bundle = await capture_module.capture_page(
        url=url, case_slug=None, work_dir=tmp_path,
    )
    assert bundle.mhtml is not None and bundle.mhtml.is_file()
    assert bundle.screenshot is not None and bundle.screenshot.is_file()
    assert bundle.page_title == "Hi"
    # WARC missing is fine — browsertrix is optional.
    assert bundle.chromium_version != "0"


# --- Capture-in-context coverage --------------------------------------------


@pytest.mark.asyncio
async def test_browsertrix_argv_includes_autoscroll_behavior(
    capture_module, monkeypatch, tmp_path
):
    """The browsertrix invocation must request the autoscroll behavior so
    the WARC reflects lazy-loaded sub-resources, matching the Playwright
    MHTML/PNG. autoplay must remain off (yt-dlp owns media)."""
    captured: dict[str, list[str]] = {}

    async def fake_exec(*argv, **_kw):
        captured["argv"] = list(argv)

        class _P:
            returncode = 0
            async def communicate(self):
                return (b"", b"")
            async def wait(self):
                return 0
            def kill(self):
                return None
        return _P()

    monkeypatch.setattr(
        "app.capture.asyncio.create_subprocess_exec", fake_exec,
    )

    await capture_module._browsertrix_attempt(
        binary="/fake/browsertrix-crawler",
        url="https://example.com/",
        out_dir=tmp_path,
        case_cookies_path=None,
        timeout_s=5,
        attempt_idx=1,
    )

    argv = captured["argv"]
    assert "--behaviors" in argv
    behaviors_value = argv[argv.index("--behaviors") + 1]
    assert behaviors_value == "autoscroll"
    # autoplay would have browsertrix re-download the video bytes — yt-dlp
    # is the canonical media producer.
    assert "autoplay" not in behaviors_value
    assert "--behaviorTimeout" in argv
    timeout_value = int(argv[argv.index("--behaviorTimeout") + 1])
    assert 0 < timeout_value < capture_module.DEFAULT_BROWSERTRIX_TIMEOUT_S


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("CAPSULE_SKIP_E2E") == "1",
    reason="set CAPSULE_SKIP_E2E=1 to skip Playwright e2e",
)
async def test_capture_uses_desktop_viewport(capture_module, tmp_path):
    """Pages must render at the configured desktop viewport so responsive
    sites don't collapse to a narrow layout where embedded media dominates.
    The fixture writes ``window.innerWidth`` into the body so we can
    confirm via the captured MHTML."""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            await browser.close()
    except Exception:
        pytest.skip("Chromium not available in this environment")

    html = (
        "<html><head><title>vp</title></head><body>"
        "<div id='probe'></div>"
        "<script>"
        "document.getElementById('probe').textContent ="
        " 'WIDTH=' + window.innerWidth + ',HEIGHT=' + window.innerHeight;"
        "</script></body></html>"
    )
    url = "data:text/html;charset=utf-8," + html
    bundle = await capture_module.capture_page(
        url=url, case_slug=None, work_dir=tmp_path,
    )
    mhtml = bundle.mhtml.read_text(encoding="utf-8", errors="replace")
    assert f"WIDTH={capture_module.DEFAULT_VIEWPORT_WIDTH}" in mhtml
    assert f"HEIGHT={capture_module.DEFAULT_VIEWPORT_HEIGHT}" in mhtml


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("CAPSULE_SKIP_E2E") == "1",
    reason="set CAPSULE_SKIP_E2E=1 to skip Playwright e2e",
)
async def test_capture_warms_lazy_loaded_content(capture_module, tmp_path):
    """The bounded scroll warm-up must run before MHTML/PNG, so content
    that hydrates on scroll is captured. The fixture rewrites a div from
    'before' to 'after' the first scroll event — both MHTML and PNG should
    see the 'after' state."""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            await browser.close()
    except Exception:
        pytest.skip("Chromium not available in this environment")

    # Tall enough that scrolling actually changes scrollY.
    html = (
        "<html><head><title>lz</title></head><body style='margin:0'>"
        "<div id='hydrate' "
        "style='position:fixed;top:8px;left:8px;font-size:24px'>"
        "MARKER_BEFORE</div>"
        "<div style='height:5000px'></div>"
        "<script>"
        "window.addEventListener('scroll', function(){"
        " document.getElementById('hydrate').textContent = 'MARKER_AFTER';"
        "}, {once:true});"
        "</script></body></html>"
    )
    url = "data:text/html;charset=utf-8," + html
    bundle = await capture_module.capture_page(
        url=url, case_slug=None, work_dir=tmp_path,
    )
    mhtml = bundle.mhtml.read_text(encoding="utf-8", errors="replace")
    assert "MARKER_AFTER" in mhtml
    assert "MARKER_BEFORE" not in mhtml
