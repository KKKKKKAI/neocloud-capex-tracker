"""Subscribers for filing emails.

They live in the `subscribers` table of the server DB (migration 0011):
private data that never reaches git, edited in the admin panel or with
`capex notify add|remove|enable|disable`, each change recorded in
settings_audit. Each subscriber has optional ticker and metric filters,
so different recipients can get different cuts.

A YAML file is used instead only when a path is passed or
NOTIFY_SUBSCRIBERS_PATH is set (tests, and the old laptop setup);
`capex notify import-yaml` moves such a file into the DB:

    subscribers:
      - email: alice@example.com
        tickers: ["*"]              # "*" = all tracked
        metrics: ["*"]              # "*" = all 6 headline metrics
        enabled: true
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..db import Database


def subscribers_path() -> Path:
    """The legacy YAML location (NOTIFY_SUBSCRIBERS_PATH overrides)."""
    override = os.environ.get("NOTIFY_SUBSCRIBERS_PATH")
    if override:
        return Path(override)
    from .. import paths

    return paths.local_dir() / "subscribers.yaml"


def _yaml_path(path: Path | None) -> Path | None:
    """The YAML file to use, or None for the DB."""
    if path is not None:
        return path
    override = os.environ.get("NOTIFY_SUBSCRIBERS_PATH")
    return Path(override) if override else None


@dataclass
class Subscriber:
    email: str
    tickers: list[str] = field(default_factory=lambda: ["*"])
    metrics: list[str] = field(default_factory=lambda: ["*"])
    enabled: bool = True

    def matches_ticker(self, ticker: str) -> bool:
        return "*" in self.tickers or ticker in self.tickers

    def matches_metric(self, metric_key: str) -> bool:
        return "*" in self.metrics or metric_key in self.metrics

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "tickers": list(self.tickers),
            "metrics": list(self.metrics),
            "enabled": self.enabled,
        }


# ---- storage -------------------------------------------------------------------

def load_subscribers(path: Path | None = None, *, db: Database | None = None
                     ) -> list[Subscriber]:
    """Every subscriber, enabled or not (a missing YAML file = none)."""
    yaml_path = _yaml_path(path)
    if yaml_path is not None:
        return _load_yaml(yaml_path)
    db = db or Database()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT email, tickers_json, metrics_json, enabled FROM subscribers ORDER BY email"
        ).fetchall()
    return [Subscriber(email=r["email"], tickers=json.loads(r["tickers_json"]),
                       metrics=json.loads(r["metrics_json"]), enabled=bool(r["enabled"]))
            for r in rows]


def add_subscriber(
    email: str,
    *,
    tickers: list[str] | None = None,
    metrics: list[str] | None = None,
    enabled: bool = True,
    path: Path | None = None,
    db: Database | None = None,
    actor: str = "cli",
) -> Subscriber:
    """Add (or update) a subscriber. Idempotent on email."""
    sub = Subscriber(email=_clean_email(email), tickers=tickers or ["*"],
                     metrics=metrics or ["*"], enabled=enabled)
    yaml_path = _yaml_path(path)
    if yaml_path is not None:
        subs = [s for s in _load_yaml(yaml_path) if s.email != sub.email] + [sub]
        _save_yaml(subs, yaml_path)
        return sub
    _db_upsert(sub, db or Database(), actor)
    return sub


def remove_subscriber(email: str, path: Path | None = None, *,
                      db: Database | None = None, actor: str = "cli") -> bool:
    """Remove a subscriber by email. Returns True if removed."""
    email = _clean_email(email)
    yaml_path = _yaml_path(path)
    if yaml_path is not None:
        subs = _load_yaml(yaml_path)
        new = [s for s in subs if s.email != email]
        if len(new) == len(subs):
            return False
        _save_yaml(new, yaml_path)
        return True
    db = db or Database()
    old = _db_get(db, email)
    if old is None:
        return False
    now = _now()
    with db.ops_write() as conn:
        conn.execute("DELETE FROM subscribers WHERE email = ?", (email,))
        _audit(conn, actor, email, old, None, now)
    return True


def set_enabled(email: str, enabled: bool, path: Path | None = None, *,
                db: Database | None = None, actor: str = "cli") -> bool:
    """Toggle a subscriber's enabled flag. Returns True if found."""
    email = _clean_email(email)
    yaml_path = _yaml_path(path)
    if yaml_path is not None:
        subs = _load_yaml(yaml_path)
        found = False
        for s in subs:
            if s.email == email:
                s.enabled = enabled
                found = True
        if found:
            _save_yaml(subs, yaml_path)
        return found
    db = db or Database()
    old = _db_get(db, email)
    if old is None:
        return False
    now = _now()
    with db.ops_write() as conn:
        conn.execute("UPDATE subscribers SET enabled = ?, updated_at = ? WHERE email = ?",
                     (int(enabled), now, email))
        _audit(conn, actor, email, old, {**old, "enabled": enabled}, now)
    return True


def import_yaml(path: Path, *, db: Database | None = None, actor: str = "import-yaml") -> int:
    """Copy every subscriber in a YAML file into the DB. Returns the count."""
    db = db or Database()
    subs = _load_yaml(path)
    for s in subs:
        _db_upsert(Subscriber(_clean_email(s.email), s.tickers, s.metrics, s.enabled), db, actor)
    return len(subs)


def filter_for_ticker(subs: list[Subscriber], ticker: str) -> list[Subscriber]:
    """Return enabled subscribers whose ticker filter matches."""
    return [s for s in subs if s.enabled and s.matches_ticker(ticker)]


# ---- helpers ------------------------------------------------------------------------

def _db_upsert(sub: Subscriber, db: Database, actor: str) -> None:
    old = _db_get(db, sub.email)
    now = _now()
    with db.ops_write() as conn:
        conn.execute(
            "INSERT INTO subscribers (email, tickers_json, metrics_json, enabled, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET "
            "tickers_json = excluded.tickers_json, metrics_json = excluded.metrics_json, "
            "enabled = excluded.enabled, updated_at = excluded.updated_at",
            (sub.email, json.dumps(sub.tickers), json.dumps(sub.metrics), int(sub.enabled),
             now, now),
        )
        _audit(conn, actor, sub.email, old, sub.to_dict(), now)


def _clean_email(email: str) -> str:
    email = email.strip()
    if "@" not in email or " " in email:
        raise ValueError(f"not an email address: {email!r}")
    return email


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db_get(db: Database, email: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT email, tickers_json, metrics_json, enabled FROM subscribers WHERE email = ?",
            (email,),
        ).fetchone()
    if row is None:
        return None
    return {"email": row["email"], "tickers": json.loads(row["tickers_json"]),
            "metrics": json.loads(row["metrics_json"]), "enabled": bool(row["enabled"])}


def _audit(conn: Any, actor: str, email: str, old: dict | None, new: dict | None,
           now: str) -> None:
    conn.execute(
        "INSERT INTO settings_audit (ts, actor, entity, entity_key, old_json, new_json) "
        "VALUES (?, ?, 'subscriber', ?, ?, ?)",
        (now, actor, email, json.dumps(old) if old else None, json.dumps(new) if new else None),
    )


def _load_yaml(p: Path) -> list[Subscriber]:
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: list[Subscriber] = []
    for e in raw.get("subscribers") or []:
        if not isinstance(e, dict) or not e.get("email"):
            continue
        out.append(Subscriber(
            email=str(e["email"]).strip(),
            tickers=list(e.get("tickers") or ["*"]),
            metrics=list(e.get("metrics") or ["*"]),
            enabled=bool(e.get("enabled", True)),
        ))
    return out


def _save_yaml(subs: list[Subscriber], p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"subscribers": [s.to_dict() for s in subs]}
    p.write_text(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False),
                 encoding="utf-8")


# Kept for callers of the old YAML-only API.
def save_subscribers(subs: list[Subscriber], path: Path | None = None) -> None:
    """Write a YAML subscriber file (legacy)."""
    _save_yaml(subs, path or subscribers_path())
