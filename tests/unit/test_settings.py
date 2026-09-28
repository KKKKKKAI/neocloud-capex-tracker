"""Runtime settings registry backed by the `settings` table."""
from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from capex import paths, settings
from capex.db.schema import Database, migrate


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPEX_HOME", str(tmp_path))
    database = Database()
    migrate(database)
    return database


def test_defaults_without_a_db_create_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPEX_HOME", str(tmp_path))
    assert settings.get("llm.model") == "claude-opus-4-8"
    assert settings.get("llm.max_calls_per_day") == 150
    assert not paths.db_path().exists()


def test_set_get_round_trip_is_audited(db):
    settings.set("llm.max_calls_per_day", 40, db=db, actor="tester")
    settings.set("llm.max_calls_per_day", "60", db=db, actor="tester")  # CLI string
    assert settings.get("llm.max_calls_per_day", db) == 60
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT actor, entity, entity_key, old_json, new_json FROM settings_audit "
            "ORDER BY id"
        ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("tester", "setting", "llm.max_calls_per_day", None, "40"),
        ("tester", "setting", "llm.max_calls_per_day", "40", "60"),
    ]


@pytest.mark.parametrize("key, raw, expected", [
    ("scheduler.paused", "true", True),
    ("scheduler.paused", "off", False),
    ("alerts.operator_emails", "a@x.com, b@y.com", ["a@x.com", "b@y.com"]),
    ("alerts.operator_emails", '["a@x.com"]', ["a@x.com"]),
    ("watcher.stale_after_days", '{"10-Q": 30}', {"10-Q": 30}),
    ("llm.token_created_at", "2026-09-28", "2026-09-28"),
])
def test_cli_strings_are_coerced(key, raw, expected):
    assert settings.coerce(key, raw) == expected


@pytest.mark.parametrize("key, raw", [
    ("no.such.key", "1"),
    ("llm.max_calls_per_day", "lots"),
    ("llm.max_calls_per_day", "999999"),       # out of range
    ("llm.timeout_s", True),                   # bool is not an int here
    ("scheduler.paused", "maybe"),
    ("alerts.operator_emails", "not-an-email"),
    ("llm.token_created_at", "28/09/2026"),
    ("llm.paused_until", "soon"),
    ("watcher.stale_after_days", '{"10-Q": 0}'),
])
def test_bad_values_are_rejected(key, raw):
    with pytest.raises(settings.SettingError):
        settings.coerce(key, raw)


def test_env_supplies_defaults_for_stack_values(db, monkeypatch):
    monkeypatch.setenv("CAPEX_SITE_BUCKET", "capex-sitebucket-abc")
    assert settings.get("publish.site_bucket", db) == "capex-sitebucket-abc"
    settings.set("publish.site_bucket", "override", db=db)
    assert settings.get("publish.site_bucket", db) == "override"


def test_reset_restores_the_default(db):
    settings.set("llm.model", "claude-other", db=db)
    settings.reset("llm.model", db=db)
    assert settings.get("llm.model", db) == "claude-opus-4-8"
    listing = {s.key: (value, is_default) for s, value, is_default in settings.all_settings(db)}
    assert listing["llm.model"] == ("claude-opus-4-8", True)


def test_every_default_passes_its_own_validation():
    for key, s in settings.REGISTRY.items():
        assert settings.coerce(key, s.default) == s.default


def test_migration_0011_on_a_copy_of_the_real_db(tmp_path):
    real = paths.CODE_ROOT / "data" / "db" / "capex.db"
    if not real.exists():
        pytest.skip("no local production DB copy")
    copy = tmp_path / "capex.db"
    shutil.copy2(real, copy)
    before = {
        t: n for t, n in _counts(copy).items()
        if t in ("companies", "source_documents", "extractions", "fiscal_calendar")
    }
    db = Database(path=copy, dump_path=tmp_path / "dump.sql")
    assert migrate(db) >= 11
    after = _counts(copy)
    assert {t: after[t] for t in before} == before  # additive only
    for table in ("settings", "settings_audit", "watchlist", "job_schedules",
                  "job_requests", "runs", "subscribers", "alerts_sent", "llm_calls"):
        assert table in after
    settings.set("llm.model", "claude-x", db=db)
    assert json.loads(_value(copy, "llm.model")) == "claude-x"


def _counts(path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()


def _value(path, key) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()[0]
    finally:
        conn.close()
