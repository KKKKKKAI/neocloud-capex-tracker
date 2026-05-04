"""Email notifications for newly-extracted quarterly filings.

Sends an HTML+text email per (ticker, subscriber) pair when the auto-update
watcher reports a successful extraction. Subscribers live in a gitignored
local YAML file; emails are sent via Gmail SMTP using credentials from
environment variables.

Public surface:
    from capex.notify import notify_subscribers
    notify_subscribers(results, db=None)
"""
from __future__ import annotations

from .orchestrator import notify_subscribers

__all__ = ["notify_subscribers"]
