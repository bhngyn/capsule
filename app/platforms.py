"""Platform classification (CLAUDE.md §5, §11).

Two jobs:

1. Map yt-dlp's ``extractor_key`` to a friendly slug used in canonical
   filenames and platform icons (``youtube``, ``twitter``, ``tiktok`` …).
2. Decide whether a domain is one of the social-media platforms whose
   cookies should be auto-attached to a capture (``is_social``).

Both are pure data — adding a new platform is one line in the table plus a
matching SVG in ``static/icons/platforms/``.
"""

from __future__ import annotations

from urllib.parse import urlsplit

__all__ = [
    "EXTRACTOR_TO_PLATFORM",
    "GALLERY_EXTRACTOR_TO_PLATFORM",
    "SOCIAL_DOMAINS",
    "friendly_name",
    "gallery_friendly_name",
    "is_social",
    "platform_for_url",
]

# yt-dlp ``extractor_key`` (case-sensitive) → friendly slug. The keys are the
# same strings yt-dlp returns in its ``info.json`` ``extractor_key`` field.
# Keep this list short and intentional; ``generic`` is the catch-all.
EXTRACTOR_TO_PLATFORM: dict[str, str] = {
    "Youtube": "youtube",
    "YoutubeTab": "youtube",
    "Twitter": "twitter",
    "TwitterBroadcast": "twitter",
    "TikTok": "tiktok",
    "Instagram": "instagram",
    "InstagramStory": "instagram",
    "Facebook": "facebook",
    "LinkedIn": "linkedin",
    "Reddit": "reddit",
    "Vimeo": "vimeo",
    "SoundCloud": "soundcloud",
    "Bandcamp": "bandcamp",
    "BiliBili": "bilibili",
    "Threads": "threads",
}

# gallery-dl ``category`` (lower-case) → friendly slug. gallery-dl reports its
# extractor in ``info.json["category"]`` (e.g. ``"pixiv"``, ``"deviantart"``).
# Where the same site is reachable via both yt-dlp and gallery-dl (twitter,
# reddit, instagram, …) the slug matches the yt-dlp ``EXTRACTOR_TO_PLATFORM``
# entry so the on-disk filename ``platform`` token stays stable across kinds.
# Adding a new image-platform is one line here plus a matching SVG in
# ``static/icons/platforms/`` (a generic "images" mark suffices for now).
GALLERY_EXTRACTOR_TO_PLATFORM: dict[str, str] = {
    "twitter": "twitter",
    "reddit": "reddit",
    "instagram": "instagram",
    "tiktok": "tiktok",
    "facebook": "facebook",
    "tumblr": "tumblr",
    "pixiv": "pixiv",
    "deviantart": "deviantart",
    "imgur": "imgur",
    "flickr": "flickr",
    "artstation": "artstation",
    "patreon": "patreon",
    "mangadex": "mangadex",
    "danbooru": "danbooru",
    "gelbooru": "gelbooru",
    "e621": "e621",
    "rule34": "rule34",
    "newgrounds": "newgrounds",
    "fanbox": "fanbox",
    "weibo": "weibo",
    "vk": "vk",
    "twibooru": "twibooru",
    "subscribestar": "subscribestar",
    "kemonoparty": "kemono",
    "directlink": "generic",
}

# Domains where authenticated capture is the norm. Subdomain matching is the
# caller's job — see ``is_social``. Each entry is the registrable domain
# (effective TLD+1) we expect to encounter.
#
# The set covers two overlapping uses:
#   1. yt-dlp social-media auto-cookie attachment (CLAUDE.md §11).
#   2. gallery-dl image-site auto-cookie attachment — Pixiv, DeviantArt, etc.
#      gate adult / member-only galleries behind login. Investigators expect
#      the same cookies file to feed both runners (CLAUDE.md §11 — same
#      Netscape cookies.txt path is consumed by yt-dlp, Playwright,
#      browsertrix, and gallery-dl).
SOCIAL_DOMAINS: frozenset[str] = frozenset(
    {
        # yt-dlp / video-first sites
        "twitter.com",
        "x.com",
        "facebook.com",
        "instagram.com",
        "tiktok.com",
        "linkedin.com",
        "reddit.com",
        "youtube.com",
        "youtu.be",
        "threads.net",
        # gallery-dl / image-first sites where authenticated content is common
        "pixiv.net",
        "deviantart.com",
        "tumblr.com",
        "flickr.com",
        "imgur.com",
        "patreon.com",
        "artstation.com",
        "fanbox.cc",
    }
)

# Heuristic ``host substring → platform`` for ``platform_for_url``. Used
# before yt-dlp metadata is available (paste preview, classify step).
_DOMAIN_HINTS: tuple[tuple[str, str], ...] = (
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
    ("twitter.com", "twitter"),
    ("x.com", "twitter"),
    ("tiktok.com", "tiktok"),
    ("instagram.com", "instagram"),
    ("facebook.com", "facebook"),
    ("fb.watch", "facebook"),
    ("linkedin.com", "linkedin"),
    ("reddit.com", "reddit"),
    ("vimeo.com", "vimeo"),
    ("soundcloud.com", "soundcloud"),
    ("bandcamp.com", "bandcamp"),
    ("bilibili.com", "bilibili"),
    ("threads.net", "threads"),
    # gallery-dl image-first sites — used by ``platform_for_url`` so a paste
    # preview shows the right platform mark before any runner has resolved
    # the URL.
    ("pixiv.net", "pixiv"),
    ("deviantart.com", "deviantart"),
    ("tumblr.com", "tumblr"),
    ("flickr.com", "flickr"),
    ("imgur.com", "imgur"),
    ("patreon.com", "patreon"),
    ("artstation.com", "artstation"),
    ("fanbox.cc", "fanbox"),
    ("mangadex.org", "mangadex"),
    ("danbooru.donmai.us", "danbooru"),
    ("gelbooru.com", "gelbooru"),
    ("e621.net", "e621"),
    ("rule34.xxx", "rule34"),
    ("newgrounds.com", "newgrounds"),
)


def friendly_name(extractor_key: str) -> str:
    """Return a friendly slug for a yt-dlp extractor key. Unknown → ``generic``."""
    if not extractor_key:
        return "generic"
    return EXTRACTOR_TO_PLATFORM.get(extractor_key, "generic")


def gallery_friendly_name(category: str) -> str:
    """Return a friendly slug for a gallery-dl ``category``. Unknown → ``generic``.

    gallery-dl publishes its extractor identifier as ``category`` in the
    per-image info.json (lower-case, e.g. ``"pixiv"``, ``"deviantart"``).
    Where the slug matches one already used by yt-dlp (twitter / reddit /
    instagram), the on-disk ``{platform}`` token stays stable across
    capture kinds — useful for an investigator scanning a case dir.
    """
    if not category:
        return "generic"
    return GALLERY_EXTRACTOR_TO_PLATFORM.get(category.lower(), "generic")


def _hostname(url_or_domain: str) -> str:
    """Best-effort host extraction. Accepts a bare domain or a full URL."""
    if "://" in url_or_domain:
        host = urlsplit(url_or_domain).hostname or ""
    else:
        host = url_or_domain.split("/", 1)[0]
        host = host.split(":", 1)[0]
    return host.lower().lstrip(".")


def is_social(domain_or_url: str) -> bool:
    """Return True if ``domain_or_url`` is — or is a subdomain of — one of
    ``SOCIAL_DOMAINS``.

    >>> is_social("youtube.com")
    True
    >>> is_social("m.youtube.com")
    True
    >>> is_social("https://www.x.com/user/status/1")
    True
    >>> is_social("example.com")
    False
    """
    host = _hostname(domain_or_url)
    if not host:
        return False
    if host in SOCIAL_DOMAINS:
        return True
    return any(host.endswith("." + d) for d in SOCIAL_DOMAINS)


def platform_for_url(url: str) -> str:
    """Best-effort platform slug from a URL alone (no yt-dlp metadata).

    Falls back to ``generic`` if no hint matches.
    """
    host = _hostname(url)
    if not host:
        return "generic"
    for hint, slug in _DOMAIN_HINTS:
        if host == hint or host.endswith("." + hint):
            return slug
    return "generic"
