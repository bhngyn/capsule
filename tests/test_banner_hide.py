"""Tests for the cookie/consent banner CSS hide layer.

Forensic invariant: CSS only, never DOM mutation. The `display: none` rule
hides banners visually so the screenshot is unobstructed; the underlying
elements remain in the captured MHTML and WARC source so a forensic
reviewer can answer "did the page show a consent banner?" by inspecting
the archive.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


def test_load_returns_css_with_version_and_hash():
    from app import banner_hide
    rules = banner_hide.load()
    assert rules.css
    assert rules.version  # extracted from the file header
    assert rules.sha256
    assert len(rules.sha256) == 64  # sha-256 hex


def test_version_is_iso_dateish():
    """Version comes from ``Version: <token>`` in the CSS header."""
    from app import banner_hide
    rules = banner_hide.load()
    # Don't pin the value — just ensure it's the curated token format.
    assert rules.version != "unknown"
    assert "-" in rules.version  # YYYY-MM-DD shape


def test_css_uses_display_none_important():
    """Banner-hide rules must use !important so they win against
    bundled site CSS without us tweaking specificity for every site."""
    from app import banner_hide
    css = banner_hide.load().css
    assert "display: none !important" in css


def test_css_does_not_mutate_dom():
    """The CSS file must not contain any JS or DOM-mutating constructs.
    A naive scan: no `<script>`, no `eval(`, no document.* references."""
    from app import banner_hide
    css = banner_hide.load().css
    assert "<script" not in css.lower()
    assert "eval(" not in css.lower()
    assert "document." not in css.lower()


def test_load_with_explicit_path(tmp_path: Path):
    fixture = tmp_path / "tiny.css"
    fixture.write_text("/* Version: foo-1.0 */\n.banner { display: none !important; }\n")
    from app import banner_hide
    rules = banner_hide.load(fixture)
    assert rules.version == "foo-1.0"
    assert rules.sha256 == hashlib.sha256(fixture.read_bytes()).hexdigest()


def test_default_rules_caches():
    from app import banner_hide
    a = banner_hide.default_rules()
    b = banner_hide.default_rules()
    assert a is b
    banner_hide.reset_cache()
    c = banner_hide.default_rules()
    assert c is not a
