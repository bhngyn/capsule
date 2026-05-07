"""Sanitisation rules — covers CLAUDE.md §3 and §5."""

from __future__ import annotations

import pytest

from app.sanitize import (
    SEPARATOR,
    TITLE_MAX,
    canonical_filename,
    canonical_page_only_stem,
    next_collision_suffix,
    sanitize_component,
    slugify_case,
    url_hash,
)


class TestSanitizeComponent:
    def test_ascii_passthrough(self):
        assert sanitize_component("hello world") == "hello world"

    def test_empty_returns_untitled(self):
        assert sanitize_component("") == "untitled"
        assert sanitize_component("   ") == "untitled"
        assert sanitize_component("...") == "untitled"

    def test_strips_illegal_chars(self):
        assert sanitize_component('a<b>c:d"e/f\\g|h?i*j') == "a-b-c-d-e-f-g-h-i-j"

    def test_strips_control_chars(self):
        assert sanitize_component("a\x00b\x1fc") == "a-b-c"

    def test_collapses_whitespace(self):
        assert sanitize_component("a   b\t\nc") == "a b c"

    def test_strips_trailing_dot_and_space(self):
        assert sanitize_component("hello.") == "hello"
        assert sanitize_component("hello   ") == "hello"
        assert sanitize_component(" hello ") == "hello"
        assert sanitize_component(".hello.") == "hello"

    def test_truncates_at_max_len(self):
        s = "a" * 200
        assert len(sanitize_component(s, max_len=80)) == 80
        assert len(sanitize_component(s, max_len=40)) == 40

    def test_truncate_does_not_leave_trailing_space(self):
        s = "a" * 79 + " " + "b" * 50
        out = sanitize_component(s, max_len=80)
        assert out == "a" * 79
        assert not out.endswith(" ")

    @pytest.mark.parametrize(
        "name", ["CON", "con", "Con", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9"]
    )
    def test_windows_reserved_names_are_escaped(self, name: str):
        out = sanitize_component(name)
        assert out.upper() != name.upper()
        assert out.endswith("_")

    @pytest.mark.parametrize(
        "name", ["CON.txt", "PRN.mp4"]
    )
    def test_windows_reserved_with_extension_is_escaped(self, name: str):
        out = sanitize_component(name)
        assert out.endswith("_")

    def test_nfkc_ligature_normalisation(self):
        # ﬁ (U+FB01) decomposes to "fi" under NFKC.
        assert sanitize_component("ﬁle") == "file"

    def test_nfkc_fullwidth_normalisation(self):
        assert sanitize_component("Ｈｅｌｌｏ") == "Hello"

    def test_arabic_passthrough(self):
        assert sanitize_component("مرحبا بالعالم") == "مرحبا بالعالم"

    def test_hebrew_passthrough(self):
        assert sanitize_component("שלום עולם") == "שלום עולם"

    def test_cjk_passthrough(self):
        assert sanitize_component("你好世界") == "你好世界"

    def test_emoji_passthrough(self):
        # Emoji are not in the illegal set; they survive.
        assert sanitize_component("hi 👋") == "hi 👋"

    def test_codepoint_truncation_does_not_split_grapheme_cluster_for_bmp_chars(self):
        # Codepoint-level truncation; we don't promise grapheme-cluster safety,
        # only that we don't produce invalid UTF-8 / invalid Python strings.
        out = sanitize_component("你" * 200, max_len=80)
        assert len(out) == 80
        assert out == "你" * 80


class TestCanonicalFilename:
    def test_full_pattern(self):
        out = canonical_filename(
            platform="youtube",
            uploader="veritasium",
            title="The Most Stubbornly Misunderstood Concept in Math",
            upload_date="2024-08-12",
            video_id="abc123XYZ",
            ext="mp4",
        )
        assert out == (
            "youtube__veritasium__The Most Stubbornly Misunderstood Concept in Math"
            "__2024-08-12__abc123XYZ.mp4"
        )

    def test_separator_present_four_times(self):
        out = canonical_filename(
            platform="x",
            uploader="y",
            title="z",
            upload_date="2024-01-01",
            video_id="id",
            ext="mp4",
        )
        assert out.count(SEPARATOR) == 4

    def test_extension_normalised(self):
        a = canonical_filename(
            platform="x", uploader="y", title="z",
            upload_date="d", video_id="i", ext=".mp4",
        )
        b = canonical_filename(
            platform="x", uploader="y", title="z",
            upload_date="d", video_id="i", ext="mp4",
        )
        assert a == b
        assert a.endswith(".mp4")

    def test_arabic_title(self):
        out = canonical_filename(
            platform="youtube",
            uploader="قناة",
            title="فيديو مهم",
            upload_date="2024-08-12",
            video_id="abc",
            ext="mp4",
        )
        assert "فيديو مهم" in out
        assert "قناة" in out


class TestCanonicalPageOnlyStem:
    def test_pattern(self):
        out = canonical_page_only_stem(
            platform="twitter",
            page_title="Some Important Tweet Title",
            capture_date="dl-2026-05-06",
            url_final="https://twitter.com/x/status/12345",
        )
        parts = out.split(SEPARATOR)
        assert len(parts) == 4
        assert parts[0] == "twitter"
        assert parts[1] == "Some Important Tweet Title"
        assert parts[2] == "dl-2026-05-06"
        assert len(parts[3]) == 12
        assert all(c in "0123456789abcdef" for c in parts[3])

    def test_url_hash_is_stable(self):
        url = "https://example.com/foo"
        a = canonical_page_only_stem(
            platform="generic", page_title="t", capture_date="d", url_final=url,
        )
        b = canonical_page_only_stem(
            platform="generic", page_title="t", capture_date="d", url_final=url,
        )
        assert a == b
        assert a.endswith(url_hash(url))


class TestSlugifyCase:
    def test_simple(self):
        assert slugify_case("Operation Sunrise") == "operation-sunrise"

    def test_diacritics_stripped(self):
        assert slugify_case("Café 2026") == "cafe-2026"

    def test_arabic_falls_back_to_index(self):
        # Arabic decomposes to combining marks that ascii-strip removes;
        # we accept that and fall back to the case-N pattern.
        assert slugify_case("مرحبا", fallback_index=7) == "case-7"

    def test_empty_falls_back(self):
        assert slugify_case("", fallback_index=3) == "case-3"
        assert slugify_case("   ", fallback_index=3) == "case-3"

    def test_collapses_punctuation(self):
        assert slugify_case("Foo --- Bar !!!") == "foo-bar"

    def test_truncated_at_64(self):
        out = slugify_case("a" * 200)
        assert len(out) <= 64


class TestNextCollisionSuffix:
    def test_returns_stem_when_unused(self):
        assert next_collision_suffix(set(), "foo") == "foo"

    def test_progression(self):
        existing = {"foo"}
        s = next_collision_suffix(existing, "foo")
        assert s == "foo__c2"
        existing.add(s)
        assert next_collision_suffix(existing, "foo") == "foo__c3"

    def test_progression_to_c10(self):
        taken = {"foo"} | {f"foo{SEPARATOR}c{n}" for n in range(2, 10)}
        assert next_collision_suffix(taken, "foo") == "foo__c10"

    def test_skips_holes_correctly(self):
        # If c2 is free but stem is taken, we use c2.
        assert next_collision_suffix({"foo"}, "foo") == "foo__c2"
