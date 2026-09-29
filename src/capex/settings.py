"""Runtime settings: a typed registry backed by the `settings` table.

The admin panel and `capex settings set` change these without a deploy;
the pipeline reads them at use time. Only keys declared in REGISTRY
exist, each with a type, default, validator and help text, so arbitrary
values (and secrets) can't be stored. Every change is recorded in
`settings_audit`.

    from capex import settings
    settings.get("llm.model")
    settings.set("llm.max_calls_per_day", 100, actor="kai")
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from .db import Database


class SettingError(ValueError):
    """Unknown key, or a value that doesn't fit the setting."""


@dataclass(frozen=True)
class Setting:
    key: str
    type: type
    default: Any
    help: str
    env: str | None = None          # fallback when the DB has no value
    check: Callable[[Any], str | None] | None = None  # returns an error or None


def _between(lo: float, hi: float) -> Callable[[Any], str | None]:
    return lambda v: None if lo <= v <= hi else f"must be between {lo} and {hi}"


def _iso_date_or_blank(v: str) -> str | None:
    if not v:
        return None
    try:
        date.fromisoformat(v)
    except ValueError:
        return "must be YYYY-MM-DD"
    return None


def _iso_time_or_blank(v: str) -> str | None:
    if not v:
        return None
    try:
        datetime.fromisoformat(v)
    except ValueError:
        return "must be an ISO date-time, e.g. 2026-10-01T17:00:00+00:00"
    return None


def _emails(v: list) -> str | None:
    bad = [e for e in v if not isinstance(e, str) or "@" not in e]
    return f"not email addresses: {bad}" if bad else None


def _form_days(v: dict) -> str | None:
    bad = {k: d for k, d in v.items() if not isinstance(d, int) or d < 1}
    return f"days must be positive integers: {bad}" if bad else None


REGISTRY: dict[str, Setting] = {s.key: s for s in [
    # ---- LLM -----------------------------------------------------------
    Setting("llm.model", str, "claude-opus-4-8", "Model for extraction calls."),
    Setting("llm.fallback_model", str, "",
            "Model used when the main one is overloaded (blank = none)."),
    Setting("llm.timeout_s", int, 300, "Seconds before one LLM call is abandoned.",
            check=_between(30, 3600)),
    Setting("llm.max_calls_per_day", int, 150,
            "Hard cap on LLM calls per UTC day (protects the subscription).",
            check=_between(0, 5000)),
    Setting("llm.max_filings_per_run", int, 3,
            "Filings the watcher extracts per run (spreads the backlog).",
            check=_between(1, 50)),
    Setting("llm.paused_until", str, "",
            "LLM calls are refused until this ISO time (set automatically "
            "when a usage limit is hit; blank = not paused).",
            check=_iso_time_or_blank),
    Setting("llm.token_created_at", str, "",
            "Date the CLAUDE_CODE_OAUTH_TOKEN was created (renewal reminders).",
            check=_iso_date_or_blank),
    # ---- Watcher ---------------------------------------------------------
    Setting("watcher.lookback_days", int, 180,
            "Ignore calendar rows whose report date is older than this.",
            check=_between(1, 3650)),
    Setting("watcher.max_attempts", int, 6,
            "Attempts per filing before it is marked failed.", check=_between(1, 50)),
    Setting("watcher.sweep_days", int, 30,
            "The filings sweep queues periodic filings from the last N days "
            "that no calendar row pointed at.", check=_between(1, 365)),
    Setting("watcher.stale_after_days", dict,
            {"10-Q": 21, "10-K": 45, "6-K": 14, "20-F": 150},
            "Days after the report date before an unfound filing is marked stale.",
            check=_form_days),
    # ---- Calendar ---------------------------------------------------------
    Setting("calendar.allow_demo_key", bool, False,
            "Allow Alpha Vantage's 'demo' key (tests only)."),
    # ---- Notifications and alerts -----------------------------------------
    Setting("notify.enabled", bool, True, "Email subscribers about new filings."),
    Setting("notify.max_age_days", int, 14,
            "Don't email about filings older than this (backlog catch-up).",
            check=_between(1, 365)),
    Setting("alerts.enabled", bool, True, "Email the operator when something breaks."),
    Setting("alerts.operator_emails", list, [],
            "Who gets operator alerts.", check=_emails),
    # ---- Publishing (defaults come from the server's stack config) -------
    Setting("publish.public_base_url", str, "", "Public site URL.",
            env="CAPEX_PUBLIC_BASE_URL"),
    Setting("publish.site_bucket", str, "", "S3 bucket behind the public site.",
            env="CAPEX_SITE_BUCKET"),
    Setting("publish.distribution_id", str, "", "CloudFront distribution to invalidate.",
            env="CAPEX_DISTRIBUTION_ID"),
    # ---- Scheduler, backups, housekeeping -----------------------------------
    Setting("scheduler.paused", bool, False,
            "Pause every scheduled job (Run now still works)."),
    Setting("backup.bucket", str, "", "S3 bucket for DB and raw-filing backups.",
            env="CAPEX_BACKUP_BUCKET"),
    Setting("backup.keep_daily", int, 7, "Local daily DB backups to keep.",
            check=_between(1, 90)),
    Setting("prune.keep_workbooks", int, 90,
            "Workbooks kept on the server (and so on the public site).",
            check=_between(1, 5000)),
    Setting("prune.run_log_days", int, 30, "Days of per-run log files to keep.",
            check=_between(1, 3650)),
]}


def _lookup(key: str) -> Setting:
    try:
        return REGISTRY[key]
    except KeyError:
        raise SettingError(f"unknown setting {key!r}") from None


def coerce(key: str, value: Any) -> Any:
    """Validate `value` for `key`; strings (CLI input) are parsed."""
    s = _lookup(key)
    if isinstance(value, str) and s.type is not str:
        text = value.strip()
        try:
            if s.type is bool:
                if text.lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
                    raise ValueError
                value = text.lower() in ("true", "1", "yes", "on")
            elif s.type is int:
                value = int(text)
            elif s.type is list:
                value = json.loads(text) if text.startswith("[") else [
                    part.strip() for part in text.split(",") if part.strip()
                ]
            elif s.type is dict:
                value = json.loads(text)
        except ValueError:
            raise SettingError(f"{key}: expected {s.type.__name__}, got {value!r}") from None
    if s.type is int and isinstance(value, bool):
        raise SettingError(f"{key}: expected int, got {value!r}")
    if not isinstance(value, s.type):
        raise SettingError(f"{key}: expected {s.type.__name__}, got {type(value).__name__}")
    if s.check and (problem := s.check(value)):
        raise SettingError(f"{key}: {problem}")
    return value


def default(key: str) -> Any:
    s = _lookup(key)
    if s.env and os.environ.get(s.env):
        return os.environ[s.env]
    return s.default


def _stored(key: str, db: Database) -> tuple[bool, Any]:
    if not db.path.exists():  # don't create an empty DB just to read defaults
        return False, None
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM settings WHERE key = ?", (key,)
            ).fetchone()
    except sqlite3.Error:  # DB missing or not migrated yet: use defaults
        return False, None
    return (True, json.loads(row["value_json"])) if row else (False, None)


def get(key: str, db: Database | None = None) -> Any:
    """The stored value, else the default (env fallback, then static)."""
    _lookup(key)
    found, value = _stored(key, db or Database())
    return value if found else default(key)


def set(key: str, value: Any, *, db: Database | None = None, actor: str = "cli") -> Any:
    """Validate, store and audit a new value; returns the stored value."""
    value = coerce(key, value)
    db = db or Database()
    found, old = _stored(key, db)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.mutating() as conn:
        conn.execute(
            "INSERT INTO settings (key, value_json, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "value_json = excluded.value_json, updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by",
            (key, json.dumps(value), now, actor),
        )
        conn.execute(
            "INSERT INTO settings_audit (ts, actor, entity, entity_key, old_json, new_json) "
            "VALUES (?, ?, 'setting', ?, ?, ?)",
            (now, actor, key, json.dumps(old) if found else None, json.dumps(value)),
        )
    return value


def reset(key: str, *, db: Database | None = None, actor: str = "cli") -> None:
    """Drop the stored value so the default applies again (audited)."""
    _lookup(key)
    db = db or Database()
    found, old = _stored(key, db)
    if not found:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.mutating() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.execute(
            "INSERT INTO settings_audit (ts, actor, entity, entity_key, old_json, new_json) "
            "VALUES (?, ?, 'setting', ?, ?, NULL)",
            (now, actor, key, json.dumps(old)),
        )


def all_settings(db: Database | None = None) -> list[tuple[Setting, Any, bool]]:
    """`(setting, effective value, is_default)` for every registered key."""
    db = db or Database()
    rows = []
    for key, s in REGISTRY.items():
        found, value = _stored(key, db)
        rows.append((s, value if found else default(key), not found))
    return rows
