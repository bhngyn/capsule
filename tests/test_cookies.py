"""Per-case cookies — CLAUDE.md §11."""

from __future__ import annotations

import datetime as _dt
import os
import stat

import pytest


SAMPLE = (
    "# Netscape HTTP Cookie File\n"
    "# Comment line\n"
    "\n"
    ".youtube.com\tTRUE\t/\tTRUE\t9999999999\tCONSENT\tYES+abc\n"
    "youtube.com\tTRUE\t/\tTRUE\t9999999999\tSID\tsecretvalue\n"
    "#HttpOnly_youtube.com\tTRUE\t/\tTRUE\t9999999999\tHSID\thttponlyvalue\n"
    ".x.com\tTRUE\t/\tTRUE\t1\tauth_token\tabcd\n"  # already expired
    ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsv\n"  # session cookie (exp=0)
)


@pytest.fixture
def cookies_module(capsule_dirs):
    import importlib

    from app import cookies as c

    importlib.reload(c)
    return c


def test_save_creates_file_with_correct_mode(cookies_module, capsule_dirs):
    summary = cookies_module.save("ops", SAMPLE.encode("utf-8"))
    target = cookies_module.path_for("ops")
    assert target.is_file()
    if os.name != "nt":
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600
    assert summary.total_cookies == 5


def test_summary_lists_domains(cookies_module):
    cookies_module.save("ops", SAMPLE.encode("utf-8"))
    summary = cookies_module.summary("ops")
    domain_names = {d.domain for d in summary.domains}
    assert domain_names == {"youtube.com", "x.com", "instagram.com"}


def test_summary_counts_per_domain(cookies_module):
    cookies_module.save("ops", SAMPLE.encode("utf-8"))
    summary = cookies_module.summary("ops")
    by = {d.domain: d for d in summary.domains}
    # .youtube.com (1) + youtube.com (1) + #HttpOnly_youtube.com (1) → 3
    assert by["youtube.com"].count == 3
    assert by["x.com"].count == 1
    assert by["instagram.com"].count == 1


def test_session_cookie_has_no_earliest_expiry(cookies_module):
    cookies_module.save("ops", SAMPLE.encode("utf-8"))
    summary = cookies_module.summary("ops")
    by = {d.domain: d for d in summary.domains}
    assert by["instagram.com"].earliest_expiry is None


def test_expired_cookie_flagged(cookies_module):
    cookies_module.save("ops", SAMPLE.encode("utf-8"))
    summary = cookies_module.summary("ops")
    by = {d.domain: d for d in summary.domains}
    assert by["x.com"].has_expired is True
    assert by["youtube.com"].has_expired is False


def test_summary_returns_none_when_missing(cookies_module):
    assert cookies_module.summary("not-here") is None


def test_domains_for_returns_set(cookies_module):
    cookies_module.save("ops", SAMPLE.encode("utf-8"))
    domains = cookies_module.domains_for("ops")
    assert domains == {"youtube.com", "x.com", "instagram.com"}


def test_domains_for_empty_when_missing(cookies_module):
    assert cookies_module.domains_for("missing") == set()


def test_malformed_rejected(cookies_module):
    bad = b"not a cookie file\nrandom garbage\n"
    with pytest.raises(ValueError):
        cookies_module.save("ops", bad)
    # No file should have been written.
    assert not cookies_module.path_for("ops").exists()


def test_malformed_error_message_does_not_leak_values(cookies_module):
    """A regression for §11: parse-error strings must not surface cookie
    values, even when the offending line is "almost valid". A future
    well-meaning "include the offending line in the error" change must not
    quietly leak credentials into logs or error responses.
    """
    # 6 fields instead of 7 — domain + the would-be value as the trailing field.
    bad_short = b"example.com\tFALSE\t/\tFALSE\t0\tSECRET_TOKEN_VALUE\n"
    # 7 fields but a non-integer expiry — the parser raises in a different code path.
    bad_expiry = b"example.com\tFALSE\t/\tFALSE\tnotanumber\tname\tSECRET_TOKEN_VALUE\n"
    for blob in (bad_short, bad_expiry):
        with pytest.raises(ValueError) as excinfo:
            cookies_module.save("ops", blob)
        assert "SECRET_TOKEN_VALUE" not in str(excinfo.value)


def test_no_cookie_values_in_summary_repr(cookies_module):
    """The dataclasses we return must never carry the value field."""
    cookies_module.save("ops", SAMPLE.encode("utf-8"))
    summary = cookies_module.summary("ops")
    text = repr(summary)
    for forbidden in ("YES+abc", "secretvalue", "httponlyvalue", "abcd", "sv"):
        assert forbidden not in text


def test_no_cookie_values_when_listing_domains(cookies_module, capsys):
    """Sanity: even if a caller prints the result of domains_for, no values."""
    cookies_module.save("ops", SAMPLE.encode("utf-8"))
    domains = cookies_module.domains_for("ops")
    print(domains)
    captured = capsys.readouterr().out
    for forbidden in ("YES+abc", "secretvalue", "httponlyvalue"):
        assert forbidden not in captured


def test_exists(cookies_module):
    assert cookies_module.exists("nope") is False
    cookies_module.save("ops", SAMPLE.encode("utf-8"))
    assert cookies_module.exists("ops") is True


# --- parse() — wizard preview path -----------------------------------------


def test_parse_accepts_str_and_bytes(cookies_module):
    s_bytes = cookies_module.parse(SAMPLE.encode("utf-8"))
    s_str = cookies_module.parse(SAMPLE)
    assert s_bytes.total_cookies == s_str.total_cookies == 5
    assert {d.domain for d in s_bytes.domains} == {d.domain for d in s_str.domains}


def test_parse_does_not_write_disk(cookies_module):
    cookies_module.parse(SAMPLE)
    assert not cookies_module.path_for("ops").exists()


def test_parse_raises_on_malformed(cookies_module):
    with pytest.raises(ValueError):
        cookies_module.parse(b"not a cookies file\n")


# --- target_coverage() ------------------------------------------------------


def test_target_coverage_exact_domain_match(cookies_module):
    s = cookies_module.parse(SAMPLE)
    cov = cookies_module.target_coverage(s, "https://x.com/anyone/status/1")
    assert cov["target_domain"] == "x.com"
    assert cov["covered"] is True
    assert "x.com" in cov["matched_domains"]


def test_target_coverage_subdomain_match(cookies_module):
    s = cookies_module.parse(SAMPLE)
    # Cookies set on youtube.com should cover the m. subdomain.
    cov = cookies_module.target_coverage(s, "https://m.youtube.com/watch?v=abc")
    assert cov["covered"] is True
    assert "youtube.com" in cov["matched_domains"]


def test_target_coverage_unrelated(cookies_module):
    s = cookies_module.parse(SAMPLE)
    cov = cookies_module.target_coverage(s, "https://example.org/foo")
    assert cov["target_domain"] == "example.org"
    assert cov["covered"] is False
    assert cov["matched_domains"] == []


def test_target_coverage_returns_none_for_empty(cookies_module):
    s = cookies_module.parse(SAMPLE)
    assert cookies_module.target_coverage(s, None) is None
    assert cookies_module.target_coverage(s, "") is None


def test_target_coverage_accepts_bare_host(cookies_module):
    s = cookies_module.parse(SAMPLE)
    cov = cookies_module.target_coverage(s, "x.com")
    assert cov["target_domain"] == "x.com"
    assert cov["covered"] is True


def test_target_coverage_child_does_not_cover_parent(cookies_module):
    only_subdomain = (
        "# Netscape HTTP Cookie File\n"
        "m.youtube.com\tTRUE\t/\tTRUE\t9999999999\tA\tval\n"
    )
    s = cookies_module.parse(only_subdomain)
    cov = cookies_module.target_coverage(s, "https://youtube.com/watch?v=abc")
    assert cov["covered"] is False


# --- merge() ---------------------------------------------------------------


def test_merge_into_empty_existing(cookies_module):
    incoming = (
        "# Netscape HTTP Cookie File\n"
        ".x.com\tTRUE\t/\tTRUE\t9999999999\tauth\tA\n"
    )
    merged, stats = cookies_module.merge("", incoming)
    assert stats.added == 1
    assert stats.replaced == 0
    assert stats.kept == 0
    # Re-parses cleanly.
    summary = cookies_module.parse(merged)
    assert summary.total_cookies == 1
    assert summary.domains[0].domain == "x.com"


def test_merge_non_overlapping_appends(cookies_module):
    existing = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t9999999999\tSID\tA\n"
    )
    incoming = (
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tB\n"
    )
    merged, stats = cookies_module.merge(existing, incoming)
    assert stats.added == 1
    assert stats.replaced == 0
    assert stats.kept == 1
    summary = cookies_module.parse(merged)
    domains = {d.domain for d in summary.domains}
    assert domains == {"youtube.com", "instagram.com"}


def test_merge_overlapping_replaces(cookies_module):
    existing = (
        "# Netscape HTTP Cookie File\n"
        ".x.com\tTRUE\t/\tTRUE\t1\tauth_token\tOLD\n"  # expired
    )
    incoming = (
        "# Netscape HTTP Cookie File\n"
        ".x.com\tTRUE\t/\tTRUE\t9999999999\tauth_token\tNEW\n"  # refreshed
    )
    merged, stats = cookies_module.merge(existing, incoming)
    assert stats.added == 0
    assert stats.replaced == 1
    assert stats.kept == 0
    # The merged file holds the refreshed expiry, not the stale one.
    assert "9999999999" in merged
    # And no longer flags x.com as expired.
    summary = cookies_module.parse(merged)
    assert summary.domains[0].has_expired is False


def test_merge_mixed_case(cookies_module):
    existing = (
        "# Netscape HTTP Cookie File\n"
        ".x.com\tTRUE\t/\tTRUE\t1\tauth_token\tOLD\n"
        ".youtube.com\tTRUE\t/\tTRUE\t9999999999\tSID\tY\n"
    )
    incoming = (
        "# Netscape HTTP Cookie File\n"
        ".x.com\tTRUE\t/\tTRUE\t9999999999\tauth_token\tNEW\n"  # replace
        ".instagram.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tI\n"  # add
    )
    merged, stats = cookies_module.merge(existing, incoming)
    assert stats.added == 1
    assert stats.replaced == 1
    assert stats.kept == 1
    summary = cookies_module.parse(merged)
    assert {d.domain for d in summary.domains} == {"x.com", "youtube.com", "instagram.com"}


def test_merge_preserves_httponly_prefix(cookies_module):
    incoming = (
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_youtube.com\tTRUE\t/\tTRUE\t9999999999\tHSID\tval\n"
    )
    merged, _ = cookies_module.merge("", incoming)
    assert "#HttpOnly_youtube.com" in merged


def test_merge_preview_no_existing_file(cookies_module):
    incoming = SAMPLE
    summary, stats = cookies_module.merge_preview("notexist", incoming)
    assert summary.total_cookies == 5
    assert stats.added == 5
    assert stats.replaced == 0
    assert stats.kept == 0


def test_merge_preview_with_existing(cookies_module):
    cookies_module.save("ops", SAMPLE.encode("utf-8"))
    incoming = (
        "# Netscape HTTP Cookie File\n"
        ".x.com\tTRUE\t/\tTRUE\t9999999999\tauth_token\tFRESH\n"  # was expired in SAMPLE
        ".reddit.com\tTRUE\t/\tTRUE\t9999999999\treddit_session\tR\n"  # new
    )
    summary, stats = cookies_module.merge_preview("ops", incoming)
    # SAMPLE has 5 cookies across 3 domains; we replace the x.com cookie and
    # add a reddit.com cookie => 6 cookies across 4 domains.
    assert summary.total_cookies == 6
    assert {d.domain for d in summary.domains} == {"youtube.com", "x.com", "instagram.com", "reddit.com"}
    assert stats.added == 1
    assert stats.replaced == 1
    assert stats.kept == 4  # 3 youtube + 1 instagram (the x.com one was replaced)


# --- save_merged() ---------------------------------------------------------


def test_save_merged_with_no_existing_file_writes_input_verbatim(cookies_module):
    summary, stats = cookies_module.save_merged("ops", SAMPLE.encode("utf-8"))
    assert summary.total_cookies == 5
    assert stats.added == 5
    assert stats.replaced == 0
    assert stats.kept == 0
    assert cookies_module.path_for("ops").read_text(encoding="utf-8") == SAMPLE


def test_save_merged_overlapping(cookies_module):
    cookies_module.save("ops", SAMPLE.encode("utf-8"))
    incoming = (
        "# Netscape HTTP Cookie File\n"
        ".x.com\tTRUE\t/\tTRUE\t9999999999\tauth_token\tNEW\n"
    )
    summary, stats = cookies_module.save_merged("ops", incoming.encode("utf-8"))
    assert stats.replaced == 1
    assert stats.added == 0
    # Existing youtube/instagram cookies still present.
    by_domain = {d.domain: d for d in summary.domains}
    assert "youtube.com" in by_domain
    assert "instagram.com" in by_domain
    # x.com no longer expired.
    assert by_domain["x.com"].has_expired is False


def test_save_merged_preserves_0600_mode(cookies_module):
    cookies_module.save_merged("ops", SAMPLE.encode("utf-8"))
    if os.name != "nt":
        mode = stat.S_IMODE(cookies_module.path_for("ops").stat().st_mode)
        assert mode == 0o600


def test_save_merged_value_secrecy_invariant(cookies_module):
    cookies_module.save("ops", SAMPLE.encode("utf-8"))
    incoming = (
        "# Netscape HTTP Cookie File\n"
        ".x.com\tTRUE\t/\tTRUE\t9999999999\tauth_token\tSUPER_SECRET_NEW_TOKEN\n"
    )
    summary, stats = cookies_module.save_merged("ops", incoming.encode("utf-8"))
    text = repr((summary, stats))
    assert "SUPER_SECRET_NEW_TOKEN" not in text  # never in the returned objects' repr
    # Also: stats and the returned summary don't carry value strings.
    for d in summary.domains:
        assert "SUPER_SECRET_NEW_TOKEN" not in repr(d)


# --- Hardening pass: freshness, snapshot hash, ephemeral path -------------


def test_validate_freshness_returns_none_when_no_file(cookies_module):
    assert cookies_module.validate_freshness("no-such-case") is None


def test_validate_freshness_flags_expired_and_soon(cookies_module):
    sample = (
        "# Netscape HTTP Cookie File\n"
        # expired (epoch=1)
        ".old.com\tTRUE\t/\tTRUE\t1\told_session\tdoesntmatter\n"
        # expiring soon (1h from now)
        f".soon.com\tTRUE\t/\tTRUE\t{2_000_000_000}\tsoon_session\tdoesntmatter\n"
        # safely in the future
        ".far.com\tTRUE\t/\tTRUE\t9999999999\tlong\tdoesntmatter\n"
    )
    cookies_module.save("freshness-case", sample.encode("utf-8"))
    # Freeze "now" so the test is deterministic. 1_999_995_000 = soon-1h.
    fake_now = 1_999_995_000
    report = cookies_module.validate_freshness(
        "freshness-case", soon_window_s=24 * 60 * 60, now=fake_now,
    )
    assert report is not None
    expired_domains = {d.domain for d in report.expired}
    soon_domains = {d.domain for d in report.expiring_soon}
    assert "old.com" in expired_domains
    assert "soon.com" in soon_domains
    assert "far.com" not in expired_domains and "far.com" not in soon_domains


def test_validate_freshness_does_not_leak_values(cookies_module):
    sample = (
        "# Netscape HTTP Cookie File\n"
        ".x.com\tTRUE\t/\tTRUE\t1\tauth\tULTRA_SECRET_VALUE\n"
    )
    cookies_module.save("leak-case", sample.encode("utf-8"))
    report = cookies_module.validate_freshness("leak-case")
    assert report is not None
    assert "ULTRA_SECRET_VALUE" not in repr(report)


def test_snapshot_hash_returns_none_when_no_file(cookies_module):
    assert cookies_module.snapshot_hash("ghost") is None


def test_snapshot_hash_changes_when_cookies_change(cookies_module):
    sample_a = "# Netscape HTTP Cookie File\n.a.com\tTRUE\t/\tTRUE\t9999999999\ta\tv1\n"
    sample_b = "# Netscape HTTP Cookie File\n.a.com\tTRUE\t/\tTRUE\t9999999999\ta\tv2\n"
    cookies_module.save("snap-case", sample_a.encode("utf-8"))
    h1 = cookies_module.snapshot_hash("snap-case")
    cookies_module.save("snap-case", sample_b.encode("utf-8"))
    h2 = cookies_module.snapshot_hash("snap-case")
    assert h1 != h2
    assert len(h1) == 64 and len(h2) == 64  # sha-256 hex


def test_write_ephemeral_persists_outside_case_dir(cookies_module, capsule_dirs):
    cookie_objs = [{
        "name": "tok", "value": "ephemeral-val", "domain": "x.com",
        "path": "/", "expirationDate": 9999999999, "secure": True,
        "httpOnly": False, "hostOnly": False,
    }]
    path, summary = cookies_module.write_ephemeral("job-12345678", cookie_objs)
    assert path.is_file()
    # NOT in the case directory.
    assert "cases" not in str(path.parent)
    assert "cookies_ephemeral" in str(path.parent)
    # Summary present, but values never round-trip.
    assert summary.total_cookies == 1
    assert "ephemeral-val" not in repr(summary)


def test_discard_ephemeral_removes_file_and_dir(cookies_module):
    cookie_objs = [{
        "name": "k", "value": "v", "domain": "x.com",
        "path": "/", "expirationDate": 9999999999, "secure": True,
        "httpOnly": False, "hostOnly": False,
    }]
    path, _ = cookies_module.write_ephemeral("job-deleteme", cookie_objs)
    parent = path.parent
    cookies_module.discard_ephemeral(path)
    assert not path.exists()
    assert not parent.exists()


def test_discard_ephemeral_safe_on_already_removed(cookies_module, tmp_path):
    # Idempotent: callers should be able to call it from a finally block
    # without checking whether the path still exists.
    fake = tmp_path / "ghost-job" / "cookies.txt"
    cookies_module.discard_ephemeral(fake)  # must not raise
