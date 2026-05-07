"""Case CRUD + filesystem layout — CLAUDE.md §9, §11."""

from __future__ import annotations

import pytest

from app import audit, cases, db


@pytest.fixture
def conn(capsule_dirs):
    # Reload cases module so it picks up the freshly-reloaded config.
    import importlib

    from app import cases as cases_mod

    importlib.reload(cases_mod)
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def test_create_persists_row(conn, capsule_dirs):
    from app import cases as cases_mod

    case = cases_mod.create(conn, name="Operation Sunrise")
    assert case.id > 0
    assert case.slug == "operation-sunrise"
    assert case.name == "Operation Sunrise"
    assert case.status == "open"

    fetched = cases_mod.get(conn, case.id)
    assert fetched == case


def test_create_provisions_directories(conn, capsule_dirs):
    from app import cases as cases_mod

    cases_mod.create(conn, name="Ops")
    assert (capsule_dirs["downloads"] / "ops").is_dir()
    # Track A: per-item folders live directly under the case dir; the old
    # ``sidecars/`` intermediate is gone (CLAUDE.md §5/§6).
    assert (capsule_dirs["config"] / "cases" / "ops").is_dir()


def test_create_audits(conn, capsule_dirs):
    from app import cases as cases_mod

    cases_mod.create(conn, name="Ops")
    rows = list(audit.iter_entries(conn))
    assert len(rows) == 1
    assert rows[0]["action"] == "case.created"
    assert rows[0]["details"]["slug"] == "ops"


def test_slug_collision_appends_suffix(conn, capsule_dirs):
    from app import cases as cases_mod

    a = cases_mod.create(conn, name="Op")
    b = cases_mod.create(conn, name="Op")
    c = cases_mod.create(conn, name="Op")
    assert a.slug == "op"
    assert b.slug == "op-2"
    assert c.slug == "op-3"


def test_create_rejects_blank_name(conn, capsule_dirs):
    from app import cases as cases_mod

    with pytest.raises(ValueError):
        cases_mod.create(conn, name="   ")


def test_arabic_name_falls_back_to_case_n(conn, capsule_dirs):
    from app import cases as cases_mod

    case = cases_mod.create(conn, name="مرحبا")
    assert case.slug.startswith("case-")
    assert case.name == "مرحبا"  # original preserved


def test_rename_updates_row_and_audits(conn, capsule_dirs):
    from app import cases as cases_mod

    case = cases_mod.create(conn, name="Old")
    cases_mod.rename(conn, case.id, "New")
    refreshed = cases_mod.get(conn, case.id)
    assert refreshed.name == "New"
    actions = [r["action"] for r in audit.iter_entries(conn)]
    assert actions == ["case.created", "case.renamed"]


def test_update_status_transitions(conn, capsule_dirs):
    from app import cases as cases_mod

    case = cases_mod.create(conn, name="X")
    cases_mod.update_status(conn, case.id, "closed")
    cases_mod.update_status(conn, case.id, "archived")
    assert cases_mod.get(conn, case.id).status == "archived"
    actions = [r["action"] for r in audit.iter_entries(conn)]
    assert actions.count("case.status_changed") == 2


def test_update_status_no_op_when_unchanged(conn, capsule_dirs):
    from app import cases as cases_mod

    case = cases_mod.create(conn, name="X")
    cases_mod.update_status(conn, case.id, "open")
    actions = [r["action"] for r in audit.iter_entries(conn)]
    assert actions == ["case.created"]


def test_update_status_rejects_invalid(conn, capsule_dirs):
    from app import cases as cases_mod

    case = cases_mod.create(conn, name="X")
    with pytest.raises(ValueError):
        cases_mod.update_status(conn, case.id, "deleted")


def test_list_open_filters_archived(conn, capsule_dirs):
    from app import cases as cases_mod

    a = cases_mod.create(conn, name="A")
    b = cases_mod.create(conn, name="B")
    cases_mod.update_status(conn, b.id, "archived")
    open_cases = cases_mod.list_open(conn)
    assert [c.id for c in open_cases] == [a.id]
    all_cases = cases_mod.list_all(conn)
    assert {c.id for c in all_cases} == {a.id, b.id}


def test_ensure_default_case_creates_pinned_slug(conn, capsule_dirs):
    from app import cases as cases_mod

    case = cases_mod.ensure_default_case(conn)
    assert case.slug == cases_mod.DEFAULT_CASE_SLUG == "downloads"
    assert case.name == cases_mod.DEFAULT_CASE_NAME == "Downloads"
    assert case.status == "open"
    assert case.settings.get("auto_managed") is True
    assert (capsule_dirs["downloads"] / "downloads").is_dir()
    # Track A: per-item folders live directly under the case dir.


def test_ensure_default_case_is_idempotent(conn, capsule_dirs):
    from app import cases as cases_mod

    a = cases_mod.ensure_default_case(conn)
    b = cases_mod.ensure_default_case(conn)
    assert a.id == b.id
    rows = [r for r in audit.iter_entries(conn) if r["action"] == "case.created"]
    assert len(rows) == 1
    assert rows[0]["details"]["kind"] == "quick"
    assert rows[0]["actor"] == "system"


def test_ensure_default_case_coexists_with_user_named_collision(conn, capsule_dirs):
    """A user case named 'Downloads' must not steal the pinned slug."""
    from app import cases as cases_mod

    default = cases_mod.ensure_default_case(conn)
    user_case = cases_mod.create(conn, name="Downloads")
    assert default.slug == "downloads"
    assert user_case.slug != "downloads"  # got '-2' suffix
    assert user_case.slug.startswith("downloads-")


def test_ensure_default_case_uses_legacy_quick_captures_when_present(
    conn, capsule_dirs,
):
    """Forward-only fallback: legacy ``quick-captures`` rows stay put.

    Existing installs created the auto-managed case under the old slug. We
    must NOT migrate, rename, or shadow them — they continue to land on
    ``quick-captures`` and no ``downloads`` row is created.
    """
    from app import cases as cases_mod

    # Seed the DB the way an older install would have left it: a row with
    # slug ``quick-captures`` and no ``downloads`` row.
    import datetime as _dt
    import json

    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    settings_json = json.dumps({"auto_managed": True}, sort_keys=True)
    with conn:
        conn.execute(
            """
            INSERT INTO cases(slug, name, description, status,
                              created_at, updated_at, settings_json)
            VALUES (?, ?, ?, 'open', ?, ?, ?)
            """,
            (
                "quick-captures",
                "Quick captures",
                "Auto-managed case for the simple downloader.",
                now,
                now,
                settings_json,
            ),
        )

    case = cases_mod.ensure_default_case(conn)
    assert case.slug == "quick-captures"
    assert cases_mod.get_by_slug(conn, "downloads") is None


def test_ensure_default_case_creates_downloads_when_neither_exists(
    conn, capsule_dirs,
):
    """Fresh installs (no legacy row) get the new ``downloads`` slug."""
    from app import cases as cases_mod

    assert cases_mod.get_by_slug(conn, "quick-captures") is None
    assert cases_mod.get_by_slug(conn, "downloads") is None

    case = cases_mod.ensure_default_case(conn)
    assert case.slug == "downloads"
    assert cases_mod.get_by_slug(conn, "downloads") is not None


def test_audit_chain_holds_after_case_lifecycle(conn, capsule_dirs):
    from app import cases as cases_mod

    case = cases_mod.create(conn, name="Op")
    cases_mod.rename(conn, case.id, "Op2")
    cases_mod.update_status(conn, case.id, "closed")
    ok, broken = audit.verify_chain(conn)
    assert ok is True
    assert broken is None
