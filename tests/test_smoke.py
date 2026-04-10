"""Smoke tests — the bare minimum: package importable, DB migrator runs."""
from __future__ import annotations

from pathlib import Path


def test_package_importable():
    import capex  # noqa: F401
    from capex import PROTOCOL_VERSION

    assert isinstance(PROTOCOL_VERSION, str)
    assert PROTOCOL_VERSION


def test_migrator_produces_schema(tmp_path: Path):
    from capex.db import Database, migrate

    db_path = tmp_path / "test.db"
    dump_path = tmp_path / "dump.sql"
    db = Database(path=db_path, dump_path=dump_path)

    version = migrate(db)
    assert version == 1

    # Schema version recorded
    with db.connect() as conn:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row[0] == 1

        # All expected tables exist
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "companies",
            "source_documents",
            "metric_definitions",
            "extractions",
            "validation_results",
            "audit_log",
            "golden_facts",
            "schema_version",
        }.issubset(tables)

    # Dump was generated
    assert dump_path.exists()
    assert "CREATE TABLE" in dump_path.read_text()
