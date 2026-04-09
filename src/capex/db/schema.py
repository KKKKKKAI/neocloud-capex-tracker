"""SQLite schema management: migrator + Database wrapper.

Usage:
    from capex.db import Database, migrate

    # Apply pending migrations (idempotent):
    version = migrate()
    print(f"schema at version {version}")

    # Read-only queries:
    db = Database()
    with db.connect() as conn:
        rows = conn.execute("SELECT ticker FROM companies").fetchall()

    # Writes — always use mutating() so dump.sql regenerates on commit:
    with db.mutating() as conn:
        conn.execute("INSERT INTO audit_log (...) VALUES (...)")

The mutating() context manager is the single chokepoint for writes. It:
    1. Opens a connection with foreign_keys = ON
    2. Yields the connection for the caller to run statements on
    3. On successful exit, commits and regenerates data/db/dump.sql
    4. On exception, rolls back and leaves dump.sql untouched

Any code that writes with a raw sqlite3.connect() bypasses the dump hook
and breaks the audit trail. Don't do that.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Resolve the repo root from this file's location: src/capex/db/schema.py → ../../../
REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = REPO_ROOT / "data" / "db" / "capex.db"
DUMP_PATH = REPO_ROOT / "data" / "db" / "dump.sql"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class Database:
    """Thin wrapper around a SQLite file with a mutating-write discipline."""

    def __init__(self, path: Path | None = None, dump_path: Path | None = None) -> None:
        self.path = path or DB_PATH
        self.dump_path = dump_path or DUMP_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a read-capable connection. Foreign keys enforced."""
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def mutating(self) -> Iterator[sqlite3.Connection]:
        """Open a write connection; regenerate dump.sql on successful commit.

        Use this at the *operation* boundary, not around every SQL
        statement. One `with db.mutating()` block = one atomic unit of
        work = one dump.sql regeneration.
        """
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        # Only reached on successful commit. Keep the dump import local
        # to avoid a circular import and to make failures in dump
        # generation surface at the right point in the stack trace.
        from .dump import dump_to_sql

        dump_to_sql(self.path, self.dump_path)


def migrate(db: Database | None = None) -> int:
    """Apply pending migrations in numeric order. Returns the new version."""
    db = db or Database()

    with db.mutating() as conn:
        # Bootstrap schema_version if it doesn't exist yet. This runs
        # before 0001_init.sql so that a clean install has a place to
        # record the fact that 0001 just ran. Idempotent.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version    INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )

        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] if row and row[0] is not None else 0

        new_version = current
        for migration_file in sorted(MIGRATIONS_DIR.glob("[0-9]*.sql")):
            version = _parse_version(migration_file.name)
            if version <= current:
                continue
            sql = migration_file.read_text()
            conn.executescript(sql)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, _now_iso()),
            )
            new_version = version

        return new_version


def _parse_version(filename: str) -> int:
    """Extract the numeric version prefix from a migration filename.

    '0001_init.sql' -> 1
    '0012_add_foo.sql' -> 12
    """
    prefix = filename.split("_", 1)[0]
    return int(prefix)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
