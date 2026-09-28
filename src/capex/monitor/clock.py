"""Time helpers for the watcher.

Earnings calendars are published in US/Eastern ("reports after the close
on 2026-10-28"), so "is this report date due yet?" is decided in Eastern
time; everything stored in the DB is UTC ISO-8601.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(moment: datetime | None = None) -> str:
    return (moment or utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def today_eastern() -> date:
    return datetime.now(EASTERN).date()
