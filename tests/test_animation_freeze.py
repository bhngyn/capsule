"""Tests for the animation/transition freeze layer.

Forensic invariant: CSS only, applied immediately before the screenshot
and removed immediately after. The MHTML and WARC are captured before the
freeze and so retain the page's source-of-record CSS — a reviewer can
still answer "what animations did the site originally play?" from the
archive.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def test_load_returns_css_with_version_and_hash():
    from app import animation_freeze
    rules = animation_freeze.load()
    assert rules.css
    assert rules.version
    assert rules.sha256
    assert len(rules.sha256) == 64


def test_freeze_css_pauses_animations_and_transitions():
    from app import animation_freeze
    css = animation_freeze.load().css
    assert "animation-play-state: paused !important" in css
    assert "transition: none !important" in css
    assert "caret-color: transparent !important" in css


def test_css_does_not_mutate_dom():
    from app import animation_freeze
    css = animation_freeze.load().css
    assert "<script" not in css.lower()
    assert "eval(" not in css.lower()
    assert "document." not in css.lower()


def test_load_with_explicit_path(tmp_path: Path):
    fixture = tmp_path / "tiny.css"
    fixture.write_text("/* Version: anim-1.0 */\n* { animation: none !important; }\n")
    from app import animation_freeze
    rules = animation_freeze.load(fixture)
    assert rules.version == "anim-1.0"
    assert rules.sha256 == hashlib.sha256(fixture.read_bytes()).hexdigest()


def test_default_rules_caches():
    from app import animation_freeze
    a = animation_freeze.default_rules()
    b = animation_freeze.default_rules()
    assert a is b
    animation_freeze.reset_cache()
    c = animation_freeze.default_rules()
    assert c is not a
