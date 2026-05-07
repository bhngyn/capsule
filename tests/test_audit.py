"""Hash-chained audit log — CLAUDE.md §8."""

from __future__ import annotations

import json

import pytest

from app import audit, db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def test_first_row_uses_zero_prev_hash(conn):
    row_id = audit.append(conn, "system.boot", details={"v": "0.1.0"})
    row = conn.execute("SELECT * FROM audit_log WHERE id = ?", (row_id,)).fetchone()
    assert row["prev_hash"] == audit.ZERO_HASH
    assert len(row["row_hash"]) == 64


def test_chain_links_each_row(conn):
    a = audit.append(conn, "case.created", details={"name": "A"})
    b = audit.append(conn, "case.renamed", details={"to": "B"})
    rows = list(conn.execute("SELECT * FROM audit_log ORDER BY id ASC"))
    assert rows[0]["row_hash"] == rows[1]["prev_hash"]
    assert rows[0]["id"] == a
    assert rows[1]["id"] == b


def test_verify_empty_chain(conn):
    ok, broken = audit.verify_chain(conn)
    assert ok is True
    assert broken is None


def test_verify_long_chain(conn):
    for i in range(100):
        audit.append(conn, f"action.{i}", details={"i": i})
    ok, broken = audit.verify_chain(conn)
    assert ok is True
    assert broken is None


def test_tampered_details_breaks_chain(conn):
    for i in range(5):
        audit.append(conn, "x", details={"i": i})
    # Flip a byte in row 3's details. Use ``with conn:`` to avoid
    # interfering with the audit module's own transaction handling.
    with conn:
        conn.execute(
            "UPDATE audit_log SET details_json = ? WHERE id = 3",
            (json.dumps({"i": 999}, sort_keys=True, separators=(",", ":")),),
        )
    ok, broken = audit.verify_chain(conn)
    assert ok is False
    assert broken == 3


def test_tampered_prev_hash_breaks_chain(conn):
    for i in range(3):
        audit.append(conn, "x", details={"i": i})
    with conn:
        conn.execute(
            "UPDATE audit_log SET prev_hash = ? WHERE id = 2",
            ("a" * 64,),
        )
    ok, broken = audit.verify_chain(conn)
    assert ok is False
    assert broken == 2


def test_forbidden_detail_keys_rejected(conn):
    with pytest.raises(audit.DetailLeakError):
        audit.append(conn, "x", details={"cookies": "abc"})
    with pytest.raises(audit.DetailLeakError):
        audit.append(conn, "x", details={"Cookie": "abc"})
    with pytest.raises(audit.DetailLeakError):
        audit.append(conn, "x", details={"set_cookie": "abc"})


def test_forbidden_keys_caught_at_depth(conn):
    with pytest.raises(audit.DetailLeakError):
        audit.append(conn, "x", details={"meta": {"nested": {"cookies": "v"}}})


def test_iter_entries_filters_by_case(conn):
    audit.append(conn, "x", case_id=1, details={"a": 1})
    audit.append(conn, "x", case_id=2, details={"a": 2})
    audit.append(conn, "x", case_id=1, details={"a": 3})
    rows = list(audit.iter_entries(conn, case_id=1))
    assert [r["case_id"] for r in rows] == [1, 1]
    assert all("details" in r for r in rows)


def test_iter_entries_returns_parsed_details(conn):
    audit.append(conn, "x", details={"k": "v"})
    rows = list(audit.iter_entries(conn))
    assert rows[0]["details"] == {"k": "v"}
    assert "details_json" not in rows[0]


def test_canonical_encode_excludes_row_hash_and_id():
    payload = {
        "id": 7,
        "timestamp": "2026-05-06T00:00:00+00:00",
        "action": "x",
        "details_json": "{}",
        "prev_hash": audit.ZERO_HASH,
        "row_hash": "abc",
    }
    encoded = audit.canonical_encode(payload)
    assert b"row_hash" not in encoded
    assert b'"id":7' not in encoded
