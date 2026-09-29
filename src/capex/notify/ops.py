"""Operator alerts: short emails to `alerts.operator_emails` when something
on the server needs a person (a job failed, the Claude token was
rejected, a health check went red).

Each alert has a key ("job:watcher", "llm-auth", "health:disk"); the same
key is sent at most once per `min_interval` (default 6 h), tracked in
`alerts_sent`, so a job failing every 20 minutes sends one email, not 72
a day. Sending never raises: an alert must not break the job reporting it.
"""
from __future__ import annotations

import html
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import settings
from ..db import Database
from ..monitor.clock import utc_iso
from .email_sender import send_email

DEFAULT_INTERVAL = timedelta(hours=6)
SUBJECT_PREFIX = "[capex] "


def alert(
    key: str,
    subject: str,
    body: str,
    *,
    db: Database | None = None,
    min_interval: timedelta = DEFAULT_INTERVAL,
    now: datetime | None = None,
    send_fn: Callable[..., Any] | None = None,
    log: Callable[[str], None] = print,
) -> bool:
    """Email the operator unless `key` was alerted within `min_interval`.
    Returns True when an email went out."""
    send_fn = send_fn or send_email
    db = db or Database()
    now = now or datetime.now(timezone.utc)
    if not settings.get("alerts.enabled", db):
        return False
    recipients = settings.get("alerts.operator_emails", db)
    if not recipients:
        log(f"alert {key}: no alerts.operator_emails configured")
        return False
    with db.connect() as conn:
        row = conn.execute("SELECT last_sent_at FROM alerts_sent WHERE key = ?",
                           (key,)).fetchone()
    if row and now - datetime.fromisoformat(row["last_sent_at"]) < min_interval:
        with db.ops_write() as conn:
            conn.execute("UPDATE alerts_sent SET count = count + 1 WHERE key = ?", (key,))
        return False
    base = settings.get("publish.public_base_url", db)
    text = body + (f"\n\nPublic site: {base}" if base else "") + (
        "\n\nSent by the neocloud-capex-tracker server. Repeats of this alert are "
        f"held back for {min_interval}.")
    try:
        for to in recipients:
            send_fn(to_email=to, subject=SUBJECT_PREFIX + subject, text_body=text,
                    html_body=f"<pre style='font-size:13px'>{html.escape(text)}</pre>")
    except Exception as e:  # SMTP down, not configured, ...: log and carry on
        log(f"alert {key}: could not send ({type(e).__name__}: {e})")
        return False
    with db.ops_write() as conn:
        conn.execute(
            "INSERT INTO alerts_sent (key, last_sent_at, count) VALUES (?, ?, 1) "
            "ON CONFLICT(key) DO UPDATE SET last_sent_at = excluded.last_sent_at, "
            "count = count + 1",
            (key, utc_iso(now)),
        )
    log(f"alert {key}: emailed {len(recipients)} operator(s)")
    return True
