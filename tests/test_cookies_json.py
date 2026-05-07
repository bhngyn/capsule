"""Tests for the browser-extension cookie path (CLAUDE.md §11; plan)."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def cookies_module(capsule_dirs):
    from app import cookies as c

    importlib.reload(c)
    return c


# --- _to_netscape_line ----------------------------------------------------


def test_basic_cookie_renders_to_netscape_line(cookies_module):
    line = cookies_module._to_netscape_line(
        {
            "name": "SID",
            "value": "abc123",
            "domain": "example.com",
            "path": "/",
            "expirationDate": 1764547200,
            "secure": True,
            "httpOnly": False,
            "hostOnly": False,
        }
    )
    parts = line.split("\t")
    assert parts == [".example.com", "TRUE", "/", "TRUE", "1764547200", "SID", "abc123"]


def test_host_only_cookie_omits_leading_dot(cookies_module):
    line = cookies_module._to_netscape_line(
        {
            "name": "X",
            "value": "y",
            "domain": "example.com",
            "path": "/",
            "expirationDate": 1764547200,
            "secure": False,
            "httpOnly": False,
            "hostOnly": True,
        }
    )
    parts = line.split("\t")
    assert parts[0] == "example.com"
    assert parts[1] == "FALSE"


def test_http_only_cookie_emits_prefix(cookies_module):
    line = cookies_module._to_netscape_line(
        {
            "name": "HSID",
            "value": "secret",
            "domain": "example.com",
            "path": "/",
            "expirationDate": 1764547200,
            "secure": True,
            "httpOnly": True,
            "hostOnly": False,
        }
    )
    assert line.startswith("#HttpOnly_")
    # And the rest of the line still parses as a valid Netscape line.
    summary = cookies_module.parse(line + "\n")
    assert summary.total_cookies == 1


def test_session_cookie_uses_zero_expiry(cookies_module):
    line = cookies_module._to_netscape_line(
        {
            "name": "tmp",
            "value": "abc",
            "domain": "example.com",
            "path": "/",
            "expirationDate": None,
            "secure": False,
            "httpOnly": False,
            "hostOnly": False,
        }
    )
    parts = line.split("\t")
    assert parts[4] == "0"


def test_negative_expiry_is_treated_as_session(cookies_module):
    line = cookies_module._to_netscape_line(
        {
            "name": "x",
            "value": "y",
            "domain": "example.com",
            "path": "/",
            "expirationDate": -1,
            "secure": False,
            "httpOnly": False,
            "hostOnly": False,
        }
    )
    parts = line.split("\t")
    assert parts[4] == "0"


def test_float_expiry_is_truncated(cookies_module):
    """Chrome's cookies API returns expirationDate as a float."""
    line = cookies_module._to_netscape_line(
        {
            "name": "x",
            "value": "y",
            "domain": "example.com",
            "path": "/",
            "expirationDate": 1764547200.499,
            "secure": False,
            "httpOnly": False,
            "hostOnly": False,
        }
    )
    parts = line.split("\t")
    assert parts[4] == "1764547200"


def test_path_defaults_to_slash(cookies_module):
    line = cookies_module._to_netscape_line(
        {
            "name": "x",
            "value": "y",
            "domain": "example.com",
        }
    )
    parts = line.split("\t")
    assert parts[2] == "/"


# --- error cases ----------------------------------------------------------


def test_missing_name_rejected(cookies_module):
    with pytest.raises(ValueError):
        cookies_module._to_netscape_line({"value": "y", "domain": "example.com"})


def test_missing_value_rejected(cookies_module):
    with pytest.raises(ValueError):
        cookies_module._to_netscape_line({"name": "x", "domain": "example.com"})


def test_missing_domain_rejected(cookies_module):
    with pytest.raises(ValueError):
        cookies_module._to_netscape_line({"name": "x", "value": "y"})


def test_tab_in_name_rejected(cookies_module):
    with pytest.raises(ValueError):
        cookies_module._to_netscape_line(
            {"name": "x\ty", "value": "v", "domain": "example.com"}
        )


def test_newline_in_value_rejected(cookies_module):
    with pytest.raises(ValueError):
        cookies_module._to_netscape_line(
            {"name": "x", "value": "v\ny", "domain": "example.com"}
        )


def test_non_numeric_expiry_rejected(cookies_module):
    with pytest.raises(ValueError):
        cookies_module._to_netscape_line(
            {
                "name": "x",
                "value": "y",
                "domain": "example.com",
                "expirationDate": "not-a-number",
            }
        )


# --- to_netscape ----------------------------------------------------------


def test_to_netscape_emits_header(cookies_module):
    text = cookies_module.to_netscape(
        [{"name": "X", "value": "y", "domain": "example.com"}]
    )
    assert text.splitlines()[0] == "# Netscape HTTP Cookie File"


def test_to_netscape_round_trips_through_parser(cookies_module):
    cookies = [
        {
            "name": "SID",
            "value": "v",
            "domain": "youtube.com",
            "path": "/",
            "expirationDate": 9999999999,
            "secure": True,
            "httpOnly": False,
            "hostOnly": False,
        },
        {
            "name": "HSID",
            "value": "v2",
            "domain": "youtube.com",
            "path": "/",
            "expirationDate": 9999999999,
            "secure": True,
            "httpOnly": True,
            "hostOnly": False,
        },
        {
            "name": "tmp",
            "value": "v3",
            "domain": "x.com",
            "path": "/",
            "expirationDate": None,
            "secure": False,
            "httpOnly": False,
            "hostOnly": False,
        },
    ]
    text = cookies_module.to_netscape(cookies)
    summary = cookies_module.parse(text)
    assert summary.total_cookies == 3
    assert {d.domain for d in summary.domains} == {"youtube.com", "x.com"}


def test_to_netscape_rejects_non_list(cookies_module):
    with pytest.raises(ValueError):
        cookies_module.to_netscape("not a list")  # type: ignore[arg-type]


def test_to_netscape_rejects_non_dict_entries(cookies_module):
    with pytest.raises(ValueError):
        cookies_module.to_netscape([42])  # type: ignore[list-item]


# --- write_json ------------------------------------------------------------


def test_write_json_persists_to_case_path(cookies_module):
    summary = cookies_module.write_json(
        "ops",
        [
            {"name": "x", "value": "y", "domain": "example.com",
             "expirationDate": 9999999999, "secure": True, "httpOnly": False,
             "hostOnly": False},
        ],
    )
    assert summary.total_cookies == 1
    target = cookies_module.path_for("ops")
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Netscape HTTP Cookie File")


def test_write_json_supports_http_only_for_yt_dlp(cookies_module):
    """The whole point of the JSON path is to deliver HttpOnly cookies that
    document.cookie cannot see. The persisted file must carry the prefix
    so yt-dlp / browsertrix recognise the flag."""
    cookies_module.write_json(
        "ops",
        [
            {"name": "session", "value": "secret", "domain": "youtube.com",
             "expirationDate": 9999999999, "secure": True, "httpOnly": True,
             "hostOnly": False},
        ],
    )
    text = cookies_module.path_for("ops").read_text(encoding="utf-8")
    assert "#HttpOnly_" in text


def test_write_json_does_not_leak_value_to_summary(cookies_module):
    summary = cookies_module.write_json(
        "ops",
        [{"name": "session", "value": "topsecret",
          "domain": "youtube.com", "expirationDate": 9999999999,
          "secure": True, "httpOnly": True, "hostOnly": False}],
    )
    text = repr(summary)
    assert "topsecret" not in text
