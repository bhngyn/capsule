"""Tests for ``app/url_canonical.py`` (CLAUDE.md §15).

The canonical form is the input to ``url_hash``. Tests assert that
paste-variants of the same URL collapse to the same canonical string;
distinct URLs stay distinct; pathological / non-URL input is handled.
"""

from __future__ import annotations

from app.url_canonical import canonicalize as c


# ---------------------------------------------------------------------------
# Tracking-param stripping
# ---------------------------------------------------------------------------


def test_utm_params_stripped():
    base = "https://www.youtube.com/watch?v=abc"
    assert c(base + "&utm_source=email") == base + "/" if False else c(base) == c(
        base + "&utm_source=email"
    )


def test_all_utm_flavours_dropped():
    base = c("https://example.com/p?id=42")
    for k in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"):
        assert c(f"https://example.com/p?id=42&{k}=x") == base


def test_exact_tracking_params_dropped():
    base = c("https://example.com/p?id=42")
    for k in (
        "fbclid", "gclid", "igshid", "mc_eid", "mc_cid",
        "_ga", "_gl", "yclid", "msclkid",
        "ref", "ref_src", "ref_url", "share_id", "si", "feature",
        "mkt_tok", "_hsenc", "_hsmi", "spm", "scm",
    ):
        assert c(f"https://example.com/p?id=42&{k}=x") == base, k


def test_tracking_param_match_is_case_insensitive():
    base = c("https://example.com/p?id=42")
    assert c("https://example.com/p?id=42&UTM_Source=x") == base
    assert c("https://example.com/p?id=42&FBCLID=x") == base


def test_two_paste_variants_collapse():
    a = "https://www.youtube.com/watch?v=abc&utm_source=email"
    b = "https://www.youtube.com/watch?v=abc&utm_source=tweet"
    assert c(a) == c(b)


# ---------------------------------------------------------------------------
# Scheme + host normalization
# ---------------------------------------------------------------------------


def test_scheme_lowercased():
    assert c("HTTPS://example.com/p") == c("https://example.com/p")


def test_host_lowercased():
    assert c("https://Example.COM/p") == c("https://example.com/p")


def test_path_case_preserved():
    """Paths are server-defined; case matters."""
    assert c("https://example.com/Foo") != c("https://example.com/foo")


def test_userinfo_preserved_host_lowered():
    assert c("https://User:Pass@Example.COM/p") == "https://User:Pass@example.com/p"


def test_port_preserved():
    assert c("https://Example.COM:8443/p") == "https://example.com:8443/p"


# ---------------------------------------------------------------------------
# Path / fragment / trailing slash
# ---------------------------------------------------------------------------


def test_fragment_dropped():
    assert c("https://example.com/p#anchor") == c("https://example.com/p")


def test_trailing_slash_stripped_for_subpath():
    assert c("https://example.com/p/") == c("https://example.com/p")


def test_root_slash_kept():
    assert c("https://example.com") == c("https://example.com/")


# ---------------------------------------------------------------------------
# Query sorting
# ---------------------------------------------------------------------------


def test_query_keys_sorted():
    assert c("https://example.com/?b=2&a=1") == c("https://example.com/?a=1&b=2")


def test_query_value_preserved_verbatim():
    """Values aren't re-cased; only the parameter *name* is canonicalized."""
    out = c("https://example.com/?Q=Hello%20World")
    assert "Hello%20World" in out


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_non_url_input_returned_unchanged():
    assert c("not a url") == "not a url"
    assert c("") == ""


def test_idempotent():
    """Canonicalizing twice yields the same string as canonicalizing once."""
    inputs = [
        "HTTPS://Example.COM/Path/?b=2&a=1&utm_source=email#frag",
        "https://www.youtube.com/watch?v=abc&si=xyz&feature=share",
        "https://x.com/?b=&a=",
    ]
    for u in inputs:
        once = c(u)
        twice = c(once)
        assert once == twice, u
