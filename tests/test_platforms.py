"""Platform classification — CLAUDE.md §5, §11."""

from __future__ import annotations

import pytest

from app.platforms import (
    SOCIAL_DOMAINS,
    friendly_name,
    is_social,
    platform_for_url,
)


class TestFriendlyName:
    @pytest.mark.parametrize(
        "key, expected",
        [
            ("Youtube", "youtube"),
            ("YoutubeTab", "youtube"),
            ("Twitter", "twitter"),
            ("TikTok", "tiktok"),
            ("Instagram", "instagram"),
            ("Facebook", "facebook"),
            ("LinkedIn", "linkedin"),
            ("Reddit", "reddit"),
            ("Vimeo", "vimeo"),
            ("SoundCloud", "soundcloud"),
            ("Bandcamp", "bandcamp"),
            ("BiliBili", "bilibili"),
            ("Threads", "threads"),
            ("Some-Random-Extractor", "generic"),
            ("", "generic"),
        ],
    )
    def test_mapping(self, key: str, expected: str):
        assert friendly_name(key) == expected


class TestIsSocial:
    @pytest.mark.parametrize("d", sorted(SOCIAL_DOMAINS))
    def test_exact_domain(self, d: str):
        assert is_social(d) is True

    @pytest.mark.parametrize(
        "host",
        [
            "m.youtube.com",
            "music.youtube.com",
            "www.x.com",
            "old.reddit.com",
            "vm.tiktok.com",
            "www.facebook.com",
        ],
    )
    def test_subdomain(self, host: str):
        assert is_social(host) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=abc",
            "https://m.youtube.com/watch?v=abc",
            "https://x.com/user/status/1",
            "https://www.tiktok.com/@user/video/1",
        ],
    )
    def test_full_url(self, url: str):
        assert is_social(url) is True

    @pytest.mark.parametrize(
        "host",
        [
            "example.com",
            "fakeyoutube.com",  # not a subdomain of youtube.com
            "youtube.com.evil.example",  # nope
            "",
        ],
    )
    def test_negative(self, host: str):
        assert is_social(host) is False

    def test_uppercase_normalised(self):
        assert is_social("YouTube.com") is True
        assert is_social("HTTPS://X.COM/foo") is True


class TestPlatformForUrl:
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://www.youtube.com/watch?v=abc", "youtube"),
            ("https://m.youtube.com/watch?v=abc", "youtube"),
            ("https://youtu.be/abc", "youtube"),
            ("https://twitter.com/user/status/1", "twitter"),
            ("https://x.com/user/status/1", "twitter"),
            ("https://www.tiktok.com/@u/video/1", "tiktok"),
            ("https://instagram.com/p/abc", "instagram"),
            ("https://www.facebook.com/u/posts/1", "facebook"),
            ("https://fb.watch/abc", "facebook"),
            ("https://linkedin.com/in/foo", "linkedin"),
            ("https://old.reddit.com/r/foo", "reddit"),
            ("https://vimeo.com/12345", "vimeo"),
            ("https://soundcloud.com/u/track", "soundcloud"),
            ("https://artist.bandcamp.com/track/foo", "bandcamp"),
            ("https://www.bilibili.com/video/BV1", "bilibili"),
            ("https://threads.net/@u/post/1", "threads"),
            ("https://example.com/page", "generic"),
            ("not-a-url", "generic"),
        ],
    )
    def test_mapping(self, url: str, expected: str):
        assert platform_for_url(url) == expected
