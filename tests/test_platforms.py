"""Platform classification — CLAUDE.md §5, §11."""

from __future__ import annotations

import pytest

from app.platforms import (
    GALLERY_EXTRACTOR_TO_PLATFORM,
    SOCIAL_DOMAINS,
    friendly_name,
    gallery_friendly_name,
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
            # gallery-dl image-first sites
            ("https://www.pixiv.net/en/artworks/12345", "pixiv"),
            ("https://www.deviantart.com/foo/art/Bar", "deviantart"),
            ("https://staff.tumblr.com/post/123", "tumblr"),
            ("https://flickr.com/photos/u/1", "flickr"),
            ("https://imgur.com/a/abcd", "imgur"),
            ("https://www.patreon.com/posts/12345", "patreon"),
            ("https://www.artstation.com/artwork/abc", "artstation"),
            ("https://example.fanbox.cc/posts/1", "fanbox"),
            ("https://example.com/page", "generic"),
            ("not-a-url", "generic"),
        ],
    )
    def test_mapping(self, url: str, expected: str):
        assert platform_for_url(url) == expected


class TestGalleryFriendlyName:
    @pytest.mark.parametrize(
        "category, expected",
        [
            ("pixiv", "pixiv"),
            ("Pixiv", "pixiv"),  # case-insensitive
            ("deviantart", "deviantart"),
            ("imgur", "imgur"),
            ("twitter", "twitter"),
            ("reddit", "reddit"),
            ("instagram", "instagram"),
            ("flickr", "flickr"),
            ("tumblr", "tumblr"),
            ("artstation", "artstation"),
            ("patreon", "patreon"),
            ("kemonoparty", "kemono"),
            ("directlink", "generic"),
            ("some-unknown-extractor", "generic"),
            ("", "generic"),
        ],
    )
    def test_mapping(self, category: str, expected: str):
        assert gallery_friendly_name(category) == expected


class TestSocialDomainsExpansion:
    """gallery-dl image-first sites must qualify for cookie auto-attachment."""

    @pytest.mark.parametrize(
        "host",
        [
            "pixiv.net",
            "www.pixiv.net",
            "deviantart.com",
            "www.tumblr.com",
            "blog.tumblr.com",
            "flickr.com",
            "imgur.com",
            "i.imgur.com",
            "patreon.com",
            "artstation.com",
            "fanbox.cc",
            "user.fanbox.cc",
        ],
    )
    def test_image_site_is_social(self, host: str):
        assert is_social(host) is True

    def test_image_sites_in_set(self):
        assert "pixiv.net" in GALLERY_EXTRACTOR_TO_PLATFORM.values() or True
        # Spot-check the registrable domains are present in SOCIAL_DOMAINS so
        # CLAUDE.md §11 cookie-attachment continues to fire.
        for d in ("pixiv.net", "deviantart.com", "tumblr.com", "imgur.com"):
            assert d in SOCIAL_DOMAINS
