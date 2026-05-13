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


def test_iter_entries_filters_by_download(conn):
    audit.append(conn, "x", case_id=1, download_id=10, details={"a": 1})
    audit.append(conn, "x", case_id=1, download_id=20, details={"a": 2})
    audit.append(conn, "x", case_id=1, details={"case_only": True})
    rows = list(audit.iter_entries(conn, download_id=10))
    assert [r["download_id"] for r in rows] == [10]
    assert rows[0]["details"] == {"a": 1}


def test_write_item_sidecar_writes_only_matching_rows(conn, tmp_path):
    audit.append(conn, "download.created", case_id=1, download_id=42, details={"a": 1})
    audit.append(conn, "noise", case_id=1, download_id=99, details={"x": 1})
    audit.append(conn, "item.manifest_rendered", case_id=1, download_id=42, details={"b": 2})

    item_dir = tmp_path / "case" / "stem"
    item_dir.mkdir(parents=True)
    out = audit.write_item_sidecar(
        conn, download_id=42, item_dir=item_dir, stem="stem",
    )
    assert out == item_dir / "Metadata" / "stem.audit.json"
    assert out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["download_id"] == 42
    assert payload["stem"] == "stem"
    actions = [e["action"] for e in payload["entries"]]
    assert actions == ["download.created", "item.manifest_rendered"]
    # details_json must round-trip back to row_hash via canonical_encode.
    for entry in payload["entries"]:
        rebuilt = {
            k: entry[k]
            for k in (
                "timestamp", "action", "case_id", "download_id",
                "actor", "details_json", "prev_hash",
            )
        }
        assert audit.row_hash_for(rebuilt) == entry["row_hash"]


def test_write_item_sidecar_uses_legacy_root_when_meta_lives_there(conn, tmp_path):
    audit.append(conn, "download.created", case_id=1, download_id=7, details={})

    item_dir = tmp_path / "case" / "legacy"
    item_dir.mkdir(parents=True)
    # Pre-v0.8 layout: meta.json sits at the item root, no Metadata/ dir.
    (item_dir / "legacy.meta.json").write_text("{}", encoding="utf-8")

    out = audit.write_item_sidecar(
        conn, download_id=7, item_dir=item_dir, stem="legacy",
    )
    assert out == item_dir / "legacy.audit.json"
    assert not (item_dir / "Metadata").exists()


def test_write_item_sidecar_is_idempotent(conn, tmp_path):
    audit.append(conn, "download.created", case_id=1, download_id=3, details={"k": "v"})
    item_dir = tmp_path / "case" / "stem"
    item_dir.mkdir(parents=True)
    a = audit.write_item_sidecar(conn, download_id=3, item_dir=item_dir, stem="stem")
    first = json.loads(a.read_text(encoding="utf-8"))
    b = audit.write_item_sidecar(conn, download_id=3, item_dir=item_dir, stem="stem")
    second = json.loads(b.read_text(encoding="utf-8"))
    # generated_at_utc may differ across writes, but the payload entries
    # are deterministic — they're the same DB rows in the same order.
    assert first["entries"] == second["entries"]
    assert first["download_id"] == second["download_id"]
