"""Subscriber list management for notify emails.

Subscribers live in `data/_local/subscribers.yaml` which is gitignored —
emails NEVER reach the public repo. Each subscriber has optional ticker
and metric filters so different recipients can get different cuts.

YAML shape:
    subscribers:
      - email: alice@example.com
        tickers: ["*"]              # "*" = all tracked
        metrics: ["*"]              # "*" = all 6 headline metrics
        enabled: true
      - email: bob@example.com
        tickers: ["MSFT", "GOOGL"]
        metrics: ["revenue", "capital_expenditures"]
        enabled: true
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def subscribers_path() -> Path:
    """Default location of subscribers.yaml. Override with NOTIFY_SUBSCRIBERS_PATH."""
    override = os.environ.get("NOTIFY_SUBSCRIBERS_PATH")
    if override:
        return Path(override)
    # repo_root / data / _local / subscribers.yaml
    return Path(__file__).resolve().parents[3] / "data" / "_local" / "subscribers.yaml"


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


def load_subscribers(path: Path | None = None) -> list[Subscriber]:
    """Read subscribers.yaml. Missing file → empty list (not an error)."""
    p = path or subscribers_path()
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    entries = raw.get("subscribers") or []
    out: list[Subscriber] = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("email"):
            continue
        out.append(Subscriber(
            email=str(e["email"]).strip(),
            tickers=list(e.get("tickers") or ["*"]),
            metrics=list(e.get("metrics") or ["*"]),
            enabled=bool(e.get("enabled", True)),
        ))
    return out


def save_subscribers(subs: list[Subscriber], path: Path | None = None) -> None:
    """Write subscribers.yaml (creates parent dir if missing)."""
    p = path or subscribers_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"subscribers": [s.to_dict() for s in subs]}
    p.write_text(
        yaml.safe_dump(payload, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def add_subscriber(
    email: str,
    *,
    tickers: list[str] | None = None,
    metrics: list[str] | None = None,
    enabled: bool = True,
    path: Path | None = None,
) -> Subscriber:
    """Add (or update) a subscriber. Idempotent on email."""
    subs = load_subscribers(path)
    sub = Subscriber(
        email=email.strip(),
        tickers=tickers or ["*"],
        metrics=metrics or ["*"],
        enabled=enabled,
    )
    subs = [s for s in subs if s.email != sub.email] + [sub]
    save_subscribers(subs, path)
    return sub


def remove_subscriber(email: str, path: Path | None = None) -> bool:
    """Remove a subscriber by email. Returns True if removed."""
    subs = load_subscribers(path)
    new = [s for s in subs if s.email != email.strip()]
    if len(new) == len(subs):
        return False
    save_subscribers(new, path)
    return True


def set_enabled(email: str, enabled: bool, path: Path | None = None) -> bool:
    """Toggle a subscriber's enabled flag. Returns True if found."""
    subs = load_subscribers(path)
    found = False
    for s in subs:
        if s.email == email.strip():
            s.enabled = enabled
            found = True
    if found:
        save_subscribers(subs, path)
    return found


def filter_for_ticker(subs: list[Subscriber], ticker: str) -> list[Subscriber]:
    """Return enabled subscribers whose ticker filter matches."""
    return [s for s in subs if s.enabled and s.matches_ticker(ticker)]
