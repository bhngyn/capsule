"""Unit tests for app.frozen_html — the v0.12 frozen single-file HTML view.

These exercise the pure-Python pieces (renderer ladder + image-body
substitution + result dataclass shape). The in-browser DOM walker
(``_FROZEN_HTML_JS``) is exercised end-to-end by the integration suite;
unit-testing it would require booting Chromium which dwarfs the value.

Each test pinpoints one of the design-review-mandated behaviors:

* CDP-cached image bodies → data: URIs at the full tier
* oversized images skipped, accounted as ``external``
* deduplication: same URL fetched once
* tier downgrade ladder: full → small_only → external
* hard-cap omission with ``error="size_budget_exceeded"``
* failure paths (evaluate raises, IO write fails) return None paths +
  populate ``error`` so the capture pipeline keeps moving
* generated output preserves <bdi>/dir attrs (CLAUDE.md §4.5 bidi)
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reload_frozen_html():
    """Force a fresh import so module-level constants reflect any edit
    by a previous test in this run."""
    import importlib

    if "app.frozen_html" in sys.modules:
        importlib.reload(sys.modules["app.frozen_html"])
    yield


def _stub_page(eval_return: dict, fetch_results: dict[str, tuple[int, dict, bytes]]):
    """Build a Playwright-shaped page stub.

    ``eval_return`` is what ``page.evaluate`` resolves to (mirrors the
    in-browser script's return shape).

    ``fetch_results`` maps URL → (status, headers, body_bytes). Lookup
    failure raises (simulating a network error).
    """
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=eval_return)

    async def _fetch(url, **_kw):
        if url not in fetch_results:
            raise RuntimeError(f"network error for {url}")
        status, headers, body = fetch_results[url]
        resp = MagicMock()
        resp.ok = 200 <= status < 300
        resp.status = status
        resp.headers = headers
        resp.body = AsyncMock(return_value=body)
        return resp

    request_ctx = MagicMock()
    request_ctx.fetch = _fetch
    page.request = request_ctx
    return page


def _skeleton_with_one_image() -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"en\"><head></head>"
        "<body><img src=\"__CAPSULE_IMG_0__\" alt=\"\"></body></html>"
    )


def _png_bytes(size: int) -> bytes:
    """Produce a deterministic byte blob of ``size`` bytes (not a real PNG —
    the tests never decode the body, only check that it round-trips)."""
    return (b"\x89PNG\r\n\x1a\n" + b"x" * (size - 8))[:size]


# --- Tier laddering ---------------------------------------------------------


def test_full_tier_inlines_small_image_as_data_uri(tmp_path: Path):
    from app import frozen_html

    body = _png_bytes(10_000)  # under 256KB → full tier
    page = _stub_page(
        eval_return={
            "html": _skeleton_with_one_image(),
            "image_refs": [{"idx": 0, "url": "https://cdn.example/logo.png"}],
        },
        fetch_results={
            "https://cdn.example/logo.png": (
                200, {"content-type": "image/png"}, body,
            ),
        },
    )
    result = asyncio.run(frozen_html.generate(page=page, work_dir=tmp_path))

    assert result.path is not None
    assert result.tier == "full"
    assert result.inlined_image_count == 1
    assert result.external_image_count == 0
    assert result.error is None

    rendered = result.path.read_text(encoding="utf-8")
    expected_b64 = base64.b64encode(body).decode("ascii")
    assert f"data:image/png;base64,{expected_b64}" in rendered


def test_oversized_image_falls_back_to_external_url(tmp_path: Path):
    """An image larger than TIER_FULL_LIMIT and TIER_SMALL_LIMIT but
    smaller than the page budget is left as an absolute URL — the
    image is too large to inline at any tier but the document itself
    fits under the budget."""
    from app import frozen_html

    body = _png_bytes(frozen_html.TIER_FULL_LIMIT + 1)  # 256KB + 1 → over both caps
    url = "https://cdn.example/hero.png"
    page = _stub_page(
        eval_return={
            "html": _skeleton_with_one_image(),
            "image_refs": [{"idx": 0, "url": url}],
        },
        fetch_results={url: (200, {"content-type": "image/png"}, body)},
    )
    result = asyncio.run(frozen_html.generate(page=page, work_dir=tmp_path))

    # Full tier: the image is over the per-image cap so it falls through
    # to an external src. The document itself is tiny, so the full tier
    # succeeds with one image inlined=0 / external=1.
    assert result.path is not None
    assert result.tier == "full"
    assert result.inlined_image_count == 0
    assert result.external_image_count == 1
    rendered = result.path.read_text(encoding="utf-8")
    assert url in rendered
    assert "data:image/png" not in rendered


def test_network_failure_renders_as_external(tmp_path: Path):
    """A 404 / network error on an image URL must not abort the
    generation — leave the absolute URL in place."""
    from app import frozen_html

    page = _stub_page(
        eval_return={
            "html": _skeleton_with_one_image(),
            "image_refs": [{"idx": 0, "url": "https://cdn.example/dead.png"}],
        },
        fetch_results={},  # any lookup raises
    )
    result = asyncio.run(frozen_html.generate(page=page, work_dir=tmp_path))

    assert result.path is not None
    assert result.inlined_image_count == 0
    assert result.external_image_count == 1
    assert "https://cdn.example/dead.png" in result.path.read_text(encoding="utf-8")


def test_duplicate_urls_fetched_once(tmp_path: Path):
    """Pages that reuse the same logo image N times trigger one
    fetch — the cache inside _fetch_image_bodies dedupes."""
    from app import frozen_html

    body = _png_bytes(5_000)
    url = "https://cdn.example/spacer.png"
    skeleton = (
        "<!DOCTYPE html>\n<html><head></head><body>"
        + "".join(
            f'<img src="__CAPSULE_IMG_{i}__" alt="">' for i in range(3)
        )
        + "</body></html>"
    )
    fetch_calls = []

    async def _track(url_, **_kw):
        fetch_calls.append(url_)
        resp = MagicMock()
        resp.ok = True
        resp.status = 200
        resp.headers = {"content-type": "image/png"}
        resp.body = AsyncMock(return_value=body)
        return resp

    page = MagicMock()
    page.evaluate = AsyncMock(return_value={
        "html": skeleton,
        "image_refs": [
            {"idx": 0, "url": url}, {"idx": 1, "url": url}, {"idx": 2, "url": url},
        ],
    })
    page.request = MagicMock()
    page.request.fetch = _track
    result = asyncio.run(frozen_html.generate(page=page, work_dir=tmp_path))

    assert result.path is not None
    assert result.inlined_image_count == 3
    assert len(fetch_calls) == 1  # dedup: one fetch despite three references


def test_data_url_image_kept_as_is(tmp_path: Path):
    """data: URLs already carry their own bytes — round-trip them."""
    from app import frozen_html

    data_uri = "data:image/svg+xml;base64,PHN2Zy8+"
    skeleton = (
        "<!DOCTYPE html>\n<html><head></head>"
        f'<body><img src="__CAPSULE_IMG_0__" alt=""></body></html>'
    )
    page = MagicMock()
    page.evaluate = AsyncMock(return_value={
        "html": skeleton,
        "image_refs": [{"idx": 0, "url": data_uri}],
    })
    page.request = MagicMock()
    page.request.fetch = AsyncMock(side_effect=AssertionError("data: URLs must not hit network"))
    result = asyncio.run(frozen_html.generate(page=page, work_dir=tmp_path))

    assert result.path is not None
    rendered = result.path.read_text(encoding="utf-8")
    assert data_uri in rendered


# --- Hard cap + omission ----------------------------------------------------


def test_pathological_page_busts_hard_cap_and_omits_artifact(tmp_path: Path):
    """A skeleton that already exceeds HARD_CAP_BYTES gets no artifact —
    the document is too large to be a useful evidence file."""
    from app import frozen_html

    # 26 MB skeleton (> 25 MB hard cap). The renderer walks the tier
    # ladder and aborts at "external" since even that tier is over cap.
    skeleton = "<!DOCTYPE html>\n<html><body>" + ("x" * (26 * 1024 * 1024)) + "</body></html>"
    page = MagicMock()
    page.evaluate = AsyncMock(return_value={"html": skeleton, "image_refs": []})
    page.request = MagicMock()

    result = asyncio.run(frozen_html.generate(page=page, work_dir=tmp_path))

    assert result.path is None
    assert result.error == "size_budget_exceeded"
    assert result.byte_count is not None and result.byte_count > frozen_html.HARD_CAP_BYTES


# --- Failure paths ----------------------------------------------------------


def test_evaluate_raised_returns_well_formed_error(tmp_path: Path):
    """page.evaluate raising must not crash the capture pipeline."""
    from app import frozen_html

    page = MagicMock()
    page.evaluate = AsyncMock(side_effect=RuntimeError("CDP died mid-walk"))

    result = asyncio.run(frozen_html.generate(page=page, work_dir=tmp_path))

    assert result.path is None
    assert result.error == "evaluate_raised:RuntimeError"
    assert result.tier is None
    assert result.byte_count is None


def test_evaluate_unexpected_shape_returns_error(tmp_path: Path):
    """A defensive guard against the in-browser script returning the
    wrong shape (string instead of dict, missing 'html' key)."""
    from app import frozen_html

    page = MagicMock()
    page.evaluate = AsyncMock(return_value="something weird")

    result = asyncio.run(frozen_html.generate(page=page, work_dir=tmp_path))

    assert result.path is None
    assert result.error == "evaluate_raised:UnexpectedReturnShape"


# --- Counter propagation ---------------------------------------------------


def test_counters_round_trip_to_result(tmp_path: Path):
    """The strip / shadow-root / iframe / font-face counters from the
    in-browser script land on the FrozenHtmlResult so meta.json can
    surface them."""
    from app import frozen_html

    page = MagicMock()
    page.evaluate = AsyncMock(return_value={
        "html": "<!DOCTYPE html>\n<html><head></head><body></body></html>",
        "image_refs": [],
        "stripped_script_count": 7,
        "stripped_iframe_count": 2,
        "stripped_font_face_count": 4,
        "shadow_root_omitted_count": 1,
    })
    page.request = MagicMock()

    result = asyncio.run(frozen_html.generate(page=page, work_dir=tmp_path))

    assert result.path is not None
    assert result.stripped_script_count == 7
    assert result.stripped_iframe_count == 2
    assert result.stripped_font_face_count == 4
    assert result.shadow_root_omitted_count == 1
    assert result.version == frozen_html.FROZEN_HTML_VERSION


# --- Renderer pure-function tests ------------------------------------------


def test_render_with_tier_substitutes_placeholders():
    from app import frozen_html

    skeleton = "<img src='__CAPSULE_IMG_0__'><img src='__CAPSULE_IMG_1__'>"
    refs = [
        {"idx": 0, "url": "https://example.com/a.png"},
        {"idx": 1, "url": "https://example.com/b.png"},
    ]
    bodies = {
        0: ("image/png", b"AAAA"),
        # idx=1 has no body → external URL
    }
    rendered, inlined, external = frozen_html._render_with_tier(
        skeleton=skeleton, image_refs=refs, bodies=bodies,
        per_image_cap=frozen_html.TIER_FULL_LIMIT,
    )
    assert inlined == 1
    assert external == 1
    assert "data:image/png;base64,QUFBQQ==" in rendered  # base64("AAAA")
    assert "https://example.com/b.png" in rendered


def test_external_tier_keeps_every_url_absolute():
    from app import frozen_html

    skeleton = "<img src='__CAPSULE_IMG_0__'>"
    refs = [{"idx": 0, "url": "https://example.com/x.png"}]
    bodies = {0: ("image/png", b"AAAA")}  # body present, but per_image_cap=0 forces external
    rendered, inlined, external = frozen_html._render_with_tier(
        skeleton=skeleton, image_refs=refs, bodies=bodies, per_image_cap=0,
    )
    assert inlined == 0
    assert external == 1
    assert "data:" not in rendered
    assert "https://example.com/x.png" in rendered
