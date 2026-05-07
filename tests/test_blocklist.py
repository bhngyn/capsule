"""Tests for the ad/tracker blocklist (CLAUDE.md §13 — capture-side mutations
recorded). Focus on (1) the source-of-truth byte-identity between backend
and extension, (2) the should_block predicate, (3) load + cache behaviour."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


def test_extension_copy_byte_identical_to_backend():
    """The extension's bundled blocklist must be byte-identical to the
    backend's source of truth — they're consumed at the same trust level
    and the audit log records a single version string for both."""
    backend = Path("app/static/blocklists/easylist-essentials.json").read_bytes()
    extension = Path("extension/blocklists/easylist-essentials.json").read_bytes()
    assert hashlib.sha256(backend).hexdigest() == hashlib.sha256(extension).hexdigest()


def test_extension_copy_parses_as_valid_json():
    raw = json.loads(
        Path("extension/blocklists/easylist-essentials.json").read_text(encoding="utf-8"),
    )
    assert "blocked_hosts" in raw
    assert isinstance(raw["blocked_hosts"], list)
    assert raw["blocked_hosts"], "blocklist must not be empty"
    assert "version" in raw


def test_load_returns_compiled_rules():
    from app import blocklist
    rules = blocklist.load()
    assert rules.version
    assert rules.blocked_hosts
    assert len(rules.blocked_hosts) > 100  # conservative sanity floor


def test_should_block_matches_known_hosts():
    from app import blocklist
    rules = blocklist.load()
    assert rules.should_block("https://www.googletagmanager.com/gtag/js")
    assert rules.should_block("https://ad.doubleclick.net/ddm/")
    assert rules.should_block("https://tr.outbrain.com/log")


def test_should_block_subdomain_match():
    """A rule for ``doubleclick.net`` must match ``ad.doubleclick.net``
    too — mirrors declarativeNetRequest ``requestDomains`` behaviour."""
    from app import blocklist
    rules = blocklist.load()
    assert rules.should_block("https://anything.doubleclick.net/x")
    assert rules.should_block("https://deep.sub.doubleclick.net/y")


def test_should_block_does_not_false_positive_first_party():
    from app import blocklist
    rules = blocklist.load()
    assert not rules.should_block("https://news.ycombinator.com/")
    assert not rules.should_block("https://example.com/article")
    # An exact-name lookalike must not match a different domain.
    assert not rules.should_block("https://notdoubleclick.net/")


def test_should_block_path_pattern():
    from app import blocklist
    rules = blocklist.load()
    # Path patterns from the bundled list catch common ad paths even on
    # first-party domains. Each rule's effect is recorded; nothing is
    # silently dropped.
    assert rules.should_block("https://some-news-site.com/ads/banner.png")


def test_should_block_handles_malformed_url():
    from app import blocklist
    rules = blocklist.load()
    assert not rules.should_block("not-a-url")
    assert not rules.should_block("")


def test_load_with_explicit_path(tmp_path: Path):
    """Allowing a path override means tests can inject a tiny fixture
    blocklist without touching the bundled file."""
    fixture = tmp_path / "tiny.json"
    fixture.write_text(json.dumps({
        "version": "test-1",
        "blocked_hosts": ["evil.example"],
        "blocked_path_patterns": [],
    }))
    from app import blocklist
    rules = blocklist.load(fixture)
    assert rules.version == "test-1"
    assert rules.should_block("https://evil.example/x")
    assert not rules.should_block("https://www.googletagmanager.com/gtag/js")


def test_default_rules_caches():
    from app import blocklist
    a = blocklist.default_rules()
    b = blocklist.default_rules()
    assert a is b
    blocklist.reset_cache()
    c = blocklist.default_rules()
    assert c is not a
