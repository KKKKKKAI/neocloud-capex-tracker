"""SQLite storage trunk.

Public API:
    Database          — wraps a SQLite file, provides the mutating() context
    migrate()         — apply pending migrations, return new version
    DB_PATH           — default path to data/db/capex.db
    DUMP_PATH         — default path to data/db/dump.sql

Design notes:
    * Every write goes through Database.mutating(), which regenerates
      dump.sql on successful commit. Do not use a raw sqlite3.connect()
      for writes — it bypasses the dump hook and breaks auditability.
    * Reads may use Database.connect() directly.
    * Migrations live in db/migrations/*.sql, applied in numeric order.
    * The schema_version table is the single source of truth for which
      migrations have been applied.
"""
from __future__ import annotations

from .schema import DB_PATH, DUMP_PATH, Database, migrate

__all__ = ["Database", "migrate", "DB_PATH", "DUMP_PATH"]
