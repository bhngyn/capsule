"""URL classification — CLAUDE.md §5, §11."""

from __future__ import annotations

import httpx
import pytest


def _mock_client(routes: dict[str, httpx.Response]) -> httpx.AsyncClient:
    """Build an AsyncClient backed by a MockTransport.

    ``routes`` maps URL → Response. Unmatched URLs return 404.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if u in routes:
            return routes[u]
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, follow_redirects=False)


@pytest.mark.asyncio
async def test_no_redirect_yields_single_chain(capsule_dirs):
    import importlib

    from app import classify as classify_mod

    importlib.reload(classify_mod)

    client = _mock_client(
        {
            "https://www.youtube.com/watch?v=abc": httpx.Response(200),
        }
    )
    async with client:
        c = await classify_mod.classify(
            "https://www.youtube.com/watch?v=abc", client=client
        )
    assert c.redirect_chain == ["https://www.youtube.com/watch?v=abc"]
    assert c.url_final == c.url_submitted
    assert c.platform == "youtube"
    assert c.error is None
    assert len(c.url_hash) == 12


@pytest.mark.asyncio
async def test_redirect_chain_followed(capsule_dirs):
    import importlib

    from app import classify as classify_mod

    importlib.reload(classify_mod)

    client = _mock_client(
        {
            "https://t.co/abc": httpx.Response(
                301, headers={"location": "https://x.com/user/status/1"}
            ),
            "https://x.com/user/status/1": httpx.Response(200),
        }
    )
    async with client:
        c = await classify_mod.classify("https://t.co/abc", client=client)
    assert c.redirect_chain == [
        "https://t.co/abc",
        "https://x.com/user/status/1",
    ]
    assert c.url_final == "https://x.com/user/status/1"
    assert c.platform == "twitter"
    assert c.error is None


@pytest.mark.asyncio
async def test_redirect_loop_detected(capsule_dirs):
    import importlib

    from app import classify as classify_mod

    importlib.reload(classify_mod)

    client = _mock_client(
        {
            "https://a.test/": httpx.Response(
                302, headers={"location": "https://b.test/"}
            ),
            "https://b.test/": httpx.Response(
                302, headers={"location": "https://a.test/"}
            ),
        }
    )
    async with client:
        c = await classify_mod.classify("https://a.test/", client=client)
    assert c.error == "redirect_loop"


@pytest.mark.asyncio
async def test_too_many_hops(capsule_dirs):
    import importlib

    from app import classify as classify_mod

    importlib.reload(classify_mod)

    routes = {}
    for i in range(classify_mod.MAX_HOPS + 2):
        routes[f"https://hop{i}.test/"] = httpx.Response(
            302, headers={"location": f"https://hop{i + 1}.test/"}
        )
    client = _mock_client(routes)
    async with client:
        c = await classify_mod.classify("https://hop0.test/", client=client)
    assert c.error == "too_many_hops"


@pytest.mark.asyncio
async def test_url_hash_stable(capsule_dirs):
    import importlib

    from app import classify as classify_mod

    importlib.reload(classify_mod)

    routes = {"https://example.com/foo": httpx.Response(200)}
    async with _mock_client(routes) as client:
        a = await classify_mod.classify(
            "https://example.com/foo", client=client
        )
        b = await classify_mod.classify(
            "https://example.com/foo", client=client
        )
    assert a.url_hash == b.url_hash


@pytest.mark.asyncio
async def test_authenticated_domains_intersection(capsule_dirs):
    import importlib

    from app import classify as classify_mod
    from app import cookies as cookies_mod

    importlib.reload(cookies_mod)
    importlib.reload(classify_mod)

    sample = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t9999999999\tSID\tval\n"
        ".example.com\tTRUE\t/\tTRUE\t9999999999\tSID\tval\n"
    )
    cookies_mod.save("ops", sample.encode("utf-8"))

    routes = {"https://music.youtube.com/watch?v=abc": httpx.Response(200)}
    async with _mock_client(routes) as client:
        c = await classify_mod.classify(
            "https://music.youtube.com/watch?v=abc",
            case_slug="ops",
            client=client,
        )

    # youtube.com is social and matches via subdomain;
    # example.com isn't social, so it's not surfaced.
    assert c.authenticated_domains == ["youtube.com"]


@pytest.mark.asyncio
async def test_authenticated_domains_empty_without_case(capsule_dirs):
    import importlib

    from app import classify as classify_mod

    importlib.reload(classify_mod)
    routes = {"https://www.youtube.com/x": httpx.Response(200)}
    async with _mock_client(routes) as client:
        c = await classify_mod.classify(
            "https://www.youtube.com/x", client=client
        )
    assert c.authenticated_domains == []


@pytest.mark.asyncio
async def test_empty_url_rejected(capsule_dirs):
    import importlib

    from app import classify as classify_mod

    importlib.reload(classify_mod)
    with pytest.raises(ValueError):
        await classify_mod.classify("   ")


@pytest.mark.asyncio
async def test_classification_to_dict(capsule_dirs):
    import importlib

    from app import classify as classify_mod

    importlib.reload(classify_mod)
    routes = {"https://example.com/": httpx.Response(200)}
    async with _mock_client(routes) as client:
        c = await classify_mod.classify("https://example.com/", client=client)
    d = c.to_dict()
    assert d["url_final"] == "https://example.com/"
    assert "error" not in d  # only present on failure
