"""Tests for the extension-token store (CLAUDE.md §11; plan §"backend changes")."""

from __future__ import annotations

import importlib
import os
import stat

import pytest


@pytest.fixture
def tokens_module(capsule_dirs):
    from app import extension_tokens as t

    importlib.reload(t)
    return t


def test_issue_returns_record_and_raw_token(tokens_module):
    record, raw = tokens_module.issue("My laptop")
    assert isinstance(raw, str) and len(raw) >= 32
    assert record.label == "My laptop"
    assert record.id and len(record.id) == 12
    assert record.created_at
    assert record.last_used_at is None


def test_issued_token_is_stored_hashed_not_raw(tokens_module):
    _, raw = tokens_module.issue("X")
    path = tokens_module.tokens_path()
    text = path.read_text(encoding="utf-8")
    assert raw not in text  # raw token must never hit disk


def test_tokens_file_is_0600_on_posix(tokens_module):
    tokens_module.issue("X")
    path = tokens_module.tokens_path()
    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600


def test_verify_accepts_correct_token(tokens_module):
    _, raw = tokens_module.issue("X")
    record = tokens_module.verify(raw)
    assert record is not None
    assert record.label == "X"


def test_verify_rejects_wrong_token(tokens_module):
    tokens_module.issue("X")
    assert tokens_module.verify("not-a-real-token") is None
    assert tokens_module.verify("") is None


def test_verify_constant_time_check_iterates_all_rows(tokens_module):
    """Verifying when there are multiple tokens still resolves correctly.

    The implementation deliberately iterates the full list every call to
    keep the timing data-independent — this just proves the behaviour
    is still correct in that mode.
    """
    tokens_module.issue("a")
    _, raw_b = tokens_module.issue("b")
    tokens_module.issue("c")
    record = tokens_module.verify(raw_b)
    assert record.label == "b"


def test_touch_updates_last_used_at(tokens_module):
    record, raw = tokens_module.issue("X")
    assert record.last_used_at is None
    tokens_module.touch(record.id)
    refreshed = next(t for t in tokens_module.list_tokens() if t.id == record.id)
    assert refreshed.last_used_at is not None


def test_revoke_removes_token(tokens_module):
    record, raw = tokens_module.issue("X")
    removed = tokens_module.revoke(record.id)
    assert removed is not None
    assert removed.id == record.id
    # And the token is no longer accepted.
    assert tokens_module.verify(raw) is None
    assert tokens_module.list_tokens() == []


def test_revoke_unknown_returns_none(tokens_module):
    assert tokens_module.revoke("does-not-exist") is None


def test_issue_rejects_empty_label(tokens_module):
    with pytest.raises(ValueError):
        tokens_module.issue("")
    with pytest.raises(ValueError):
        tokens_module.issue("   ")


def test_list_tokens_empty_when_no_file(tokens_module):
    assert tokens_module.list_tokens() == []


# --- Hardening pass: extension_id binding + rotation ----------------------


def test_verify_accepts_token_with_no_extension_binding_when_id_missing(tokens_module):
    """Tokens issued without an ``extension_id`` (legacy / pre-hardening)
    are grandfathered: any caller can verify without supplying an id."""
    _, raw = tokens_module.issue("legacy")
    assert tokens_module.verify(raw) is not None
    assert tokens_module.verify(raw, extension_id="anything") is not None


def test_verify_rejects_when_bound_id_mismatches(tokens_module):
    _, raw = tokens_module.issue("bound", extension_id="abc123")
    # Right id passes.
    record = tokens_module.verify(raw, extension_id="abc123")
    assert record is not None
    # Missing id raises.
    with pytest.raises(tokens_module.ExtensionIdMismatch):
        tokens_module.verify(raw, extension_id=None)
    # Wrong id raises.
    with pytest.raises(tokens_module.ExtensionIdMismatch):
        tokens_module.verify(raw, extension_id="def456")


def test_rotate_revokes_old_and_issues_new(tokens_module):
    record, raw_old = tokens_module.issue("toRotate", extension_id="abc")
    result = tokens_module.rotate(record.id)
    assert result is not None
    new_record, raw_new = result
    assert raw_new != raw_old
    assert new_record.id != record.id
    # Label and binding carry over.
    assert new_record.label == "toRotate"
    assert new_record.extension_id == "abc"
    # Old token no longer verifies.
    assert tokens_module.verify(raw_old, extension_id="abc") is None
    # New token does.
    assert tokens_module.verify(raw_new, extension_id="abc") is not None


def test_rotate_unknown_returns_none(tokens_module):
    assert tokens_module.rotate("does-not-exist") is None


def test_rotate_keeps_other_tokens_intact(tokens_module):
    a_record, a_raw = tokens_module.issue("a")
    b_record, b_raw = tokens_module.issue("b")
    tokens_module.rotate(a_record.id)
    # The other token still verifies.
    assert tokens_module.verify(b_raw) is not None
    # And there are still exactly two tokens persisted.
    assert len(tokens_module.list_tokens()) == 2
