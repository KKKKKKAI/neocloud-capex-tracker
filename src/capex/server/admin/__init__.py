"""The admin panel: control the server from a browser, through an SSH tunnel.

    capex server admin [--port 8081]            (capex-admin.service on the host)
    scripts/admin_tunnel.sh, then http://localhost:8081

FastAPI + Jinja2, server-rendered forms, no JavaScript, bound to
127.0.0.1. There is no password: the SSH key is the gate. Guards:
- only a Host of localhost:8081 or 127.0.0.1:8081 is answered, so a web
  page can't reach the panel through DNS rebinding;
- every POST needs the page's form token and a same-origin
  Origin/Referer (no cross-site form posts);
- responses are no-store, can't be framed, and may run no scripts.

The panel edits settings, the watchlist, schedules, calendar rows,
filing events and subscribers (each change audited in settings_audit),
and queues job requests. It never runs a job itself: the scheduler does.
"""
from __future__ import annotations

from .app import DEFAULT_PORT, create_app

__all__ = ["DEFAULT_PORT", "create_app"]
