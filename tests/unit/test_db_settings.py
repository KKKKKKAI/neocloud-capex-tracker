"""Database connection settings: journal mode, busy timeout, read-only
connections and when dump.sql is regenerated."""
from __future__ import annotations

import sqlite3

import pytest

from capex import paths
from capex.db.schema import Database, migrate


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("CAPEX_HOME", "CAPEX_DB_PATH", "CAPEX_DB_JOURNAL_MODE", "CAPEX_DUMP_SQL"):
        monkeypatch.delenv(var, raising=False)


def _journal_mode(db: Database) -> str:
    with db.connect() as conn:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]


def test_journal_mode_untouched_by_default(tmp_path):
    db = Database(path=tmp_path / "a.db")
    assert _journal_mode(db) == "delete"


def test_wal_when_requested(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPEX_DB_JOURNAL_MODE", "wal")
    db = Database(path=tmp_path / "a.db")
    assert _journal_mode(db) == "wal"


def test_rejects_unknown_journal_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPEX_DB_JOURNAL_MODE", "WAL; DROP TABLE x")
    db = Database(path=tmp_path / "a.db")
    with pytest.raises(ValueError):
        with db.connect():
            pass


def test_busy_timeout_is_set(tmp_path):
    db = Database(path=tmp_path / "a.db")
    with db.connect() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000


def test_connect_ro_cannot_write(tmp_path):
    db = Database(path=tmp_path / "a.db")
    migrate(db)
    with db.connect_ro() as conn:
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM companies")


def test_default_db_follows_capex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPEX_HOME", str(tmp_path))
    db = Database()
    assert db.path == tmp_path / "data" / "db" / "capex.db"
    assert db.dump_path == paths.dump_path()


def test_dumps_on_in_a_plain_checkout_off_on_the_server(tmp_path, monkeypatch):
    assert Database(path=tmp_path / "a.db").dump_enabled
    monkeypatch.setenv("CAPEX_HOME", str(tmp_path))
    assert not Database(path=tmp_path / "a.db").dump_enabled
    monkeypatch.setenv("CAPEX_DUMP_SQL", "1")
    assert Database(path=tmp_path / "a.db").dump_enabled


def test_no_dump_written_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPEX_DUMP_SQL", "0")
    db = Database(path=tmp_path / "a.db")
    migrate(db)
    assert not db.dump_path.exists()


def test_explicit_dump_path_always_dumps(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPEX_DUMP_SQL", "0")
    db = Database(path=tmp_path / "a.db", dump_path=tmp_path / "out.sql")
    migrate(db)
    assert (tmp_path / "out.sql").exists()
