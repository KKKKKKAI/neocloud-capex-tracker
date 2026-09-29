"""A Database at a custom path must never regenerate the canonical
data/db/dump.sql (the test suite used to overwrite the tracked dump)."""
from __future__ import annotations

from capex.db.schema import DB_PATH, DUMP_PATH, Database, migrate


def test_custom_db_path_dumps_next_to_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPEX_DUMP_SQL", "1")   # dumps are opt-in
    db = Database(path=tmp_path / "scratch.db")
    assert db.dump_path == tmp_path / "scratch.dump.sql"
    migrate(db)
    assert db.dump_path.exists()
    assert "CREATE TABLE" in db.dump_path.read_text(encoding="utf-8")


def test_canonical_db_keeps_canonical_dump():
    assert Database().dump_path == DUMP_PATH
    assert Database(path=DB_PATH).dump_path == DUMP_PATH


def test_explicit_dump_path_wins(tmp_path):
    db = Database(path=tmp_path / "a.db", dump_path=tmp_path / "custom.sql")
    assert db.dump_path == tmp_path / "custom.sql"
