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
    3. On successful exit, commits and (when dumps are on) regenerates dump.sql
    4. On exception, rolls back and leaves dump.sql untouched

Any code that writes with a raw sqlite3.connect() bypasses the dump hook
and breaks the audit trail. Don't do that.

Environment:
    CAPEX_HOME / CAPEX_DB_PATH  where the DB lives (see capex.paths)
    CAPEX_DB_JOURNAL_MODE       e.g. WAL on the server, where the scheduler
                                and the admin panel share the DB. Unset =
                                leave the file's mode alone (WAL is unsafe
                                on the /mnt/c checkout).
    CAPEX_DUMP_SQL              1/0. Default: on in a plain checkout (where
                                dump.sql is tracked), off when CAPEX_HOME
                                is set (the server skips the multi-MB
                                rewrite on every write).
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .. import paths

# Kept for scripts that import it: the code checkout.
REPO_ROOT = paths.CODE_ROOT
# Import-time snapshots; Database() resolves the defaults at call time.
DB_PATH = paths.db_path()
DUMP_PATH = paths.dump_path()
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

BUSY_TIMEOUT_S = 30


def dumps_enabled_by_default() -> bool:
    setting = os.environ.get("CAPEX_DUMP_SQL")
    if setting is not None:
        return setting.strip().lower() in ("1", "true", "yes", "on")
    return "CAPEX_HOME" not in os.environ


class Database:
    """Thin wrapper around a SQLite file with a mutating-write discipline."""

    def __init__(self, path: Path | None = None, dump_path: Path | None = None) -> None:
        canonical = paths.db_path()
        self.path = Path(path) if path else canonical
        # An explicit dump_path means the caller wants a dump.
        self.dump_enabled = dump_path is not None or dumps_enabled_by_default()
        if dump_path is None:
            # A DB at a custom path (tests, scratch copies) dumps next to
            # itself — never over the canonical dump.sql.
            dump_path = (
                paths.dump_path()
                if self.path.resolve() == canonical.resolve()
                else self.path.with_name(f"{self.path.stem}.dump.sql")
            )
        self.dump_path = Path(dump_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _open(self) -> sqlite3.Connection:
        mode = os.environ.get("CAPEX_DB_JOURNAL_MODE", "").strip().upper()
        if mode and mode not in ("WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY"):
            raise ValueError(f"unsupported CAPEX_DB_JOURNAL_MODE={mode!r}")
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S)
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_S * 1000}")
        if mode:
            conn.execute(f"PRAGMA journal_mode = {mode}")
            if mode == "WAL":
                conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a read-capable connection. Foreign keys enforced."""
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def connect_ro(self) -> Iterator[sqlite3.Connection]:
        """Open a read-only connection (exporters, viewers)."""
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_S)
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_S * 1000}")
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
        conn = self._open()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if not self.dump_enabled:
            return
        self._dump()

    @contextmanager
    def ops_write(self) -> Iterator[sqlite3.Connection]:
        """One write transaction for operational bookkeeping (scheduler
        runs and requests, schedules, alerts, telemetry). Unlike
        mutating() it never regenerates dump.sql: the dump mirrors the
        system-of-record data for review, not the server's own logs."""
        conn = self._open()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _dump(self) -> None:
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


def latest_version() -> int:
    """The newest migration this code ships."""
    return max(_parse_version(p.name) for p in MIGRATIONS_DIR.glob("[0-9]*.sql"))


def current_version(db: Database) -> int:
    """The DB's applied schema version (0 for a new or unmigrated DB)."""
    try:
        with db.connect() as conn:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.Error:
        return 0
    return (row[0] or 0) if row else 0


def _parse_version(filename: str) -> int:
    """Extract the numeric version prefix from a migration filename.

    '0001_init.sql' -> 1
    '0012_add_foo.sql' -> 12
    """
    prefix = filename.split("_", 1)[0]
    return int(prefix)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
