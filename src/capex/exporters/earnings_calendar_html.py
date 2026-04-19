"""Self-contained HTML earnings-calendar viewer for GitHub Pages.

Reads `fiscal_calendar` + `source_documents` via `query_for_viewer`,
groups events by date, and renders a single page (`docs/calendar.html`)
with the same nav bar as the four chart pages.
"""
from __future__ import annotations

import html
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

from ..monitor.calendar import CalendarEvent, query_for_viewer
from .interactive_chart import COLORS, _build_nav_html

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = REPO_ROOT / "data" / "db" / "capex.db"
DOCS_DIR = REPO_ROOT / "docs"

STATUS_COLORS = {
    "upcoming":  ("#2E75B6", "#E8F0F8"),   # blue
    "detected":  ("#B7791F", "#FEF3C7"),   # yellow
    "fetched":   ("#6B46C1", "#EDE9FE"),   # purple
    "extracted": ("#2F855A", "#D1FAE5"),   # green
    "failed":    ("#C53030", "#FED7D7"),   # red
}


def generate_earnings_calendar_html(
    output: str | Path | None = None,
    db_path: str | Path | None = None,
    upcoming_days: int = 90,
    past_days: int = 30,
) -> Path:
    """Render `docs/calendar.html` (or the given `output` path)."""
    output = Path(output or DOCS_DIR / "calendar.html")
    output.parent.mkdir(parents=True, exist_ok=True)
    db_path = db_path or DB_PATH

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    events = query_for_viewer(conn, upcoming_days=upcoming_days, past_days=past_days)
    # Most-recent sync timestamp (from fiscal_calendar.updated_at)
    row = conn.execute(
        "SELECT MAX(updated_at) AS last FROM fiscal_calendar"
    ).fetchone()
    last_sync = row["last"] if row else None
    conn.close()

    html_str = _build_html(events, last_sync)
    output.write_text(html_str, encoding="utf-8")
    return output


def _countdown_label(days: int) -> str:
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    if days > 0:
        return f"in {days} days"
    return f"{abs(days)} days ago"


def _status_badge(status: str) -> str:
    fg, bg = STATUS_COLORS.get(status, ("#444", "#eee"))
    return (
        f'<span class="badge" style="color:{fg};background:{bg};">'
        f"{html.escape(status)}</span>"
    )


def _ticker_chip(ticker: str) -> str:
    colour = COLORS.get(ticker, "#888")
    return (
        f'<span class="chip" style="background:{colour};">'
        f"{html.escape(ticker)}</span>"
    )


def _event_row(ev: CalendarEvent) -> str:
    period = f"FY{ev.fiscal_year % 100:02d} {ev.period_label}"
    form = ev.form_type or "?"
    link = ""
    if ev.source_url:
        link = (
            f' <a class="filing-link" href="{html.escape(ev.source_url)}"'
            f' target="_blank" rel="noopener">view filing →</a>'
        )
    title = html.escape(ev.company_name)
    return (
        '<li class="ev-row">'
        f'{_ticker_chip(ev.ticker)}'
        f'<span class="ev-period" title="{title}">{period}</span>'
        f'<span class="ev-form">{html.escape(form)}</span>'
        f'<span class="ev-end">period-end {html.escape(ev.fiscal_date_ending)}</span>'
        f'{_status_badge(ev.status)}'
        f'{link}'
        "</li>"
    )


def _group_by_date(events: list[CalendarEvent]) -> list[tuple[str, list[CalendarEvent]]]:
    by_date: dict[str, list[CalendarEvent]] = defaultdict(list)
    for ev in events:
        by_date[ev.report_date].append(ev)
    return sorted(by_date.items(), key=lambda p: p[0])


def _date_heading(day_iso: str, days_from_today: int) -> str:
    try:
        d = date.fromisoformat(day_iso)
        weekday = d.strftime("%a")
    except ValueError:
        weekday = ""
    countdown = _countdown_label(days_from_today)
    return (
        f'<h3 class="date-heading">{html.escape(weekday)} '
        f'{html.escape(day_iso)} <span class="countdown">— {countdown}</span></h3>'
    )


def _build_html(events: list[CalendarEvent], last_sync: str | None) -> str:
    upcoming = [e for e in events if e.days_from_today >= 0]
    past = [e for e in events if e.days_from_today < 0]

    # Next-up summary: first upcoming date
    next_card = ""
    if upcoming:
        first_day = upcoming[0].report_date
        first_batch = [e for e in upcoming if e.report_date == first_day]
        chips = "".join(_ticker_chip(e.ticker) for e in first_batch)
        cd = _countdown_label(first_batch[0].days_from_today)
        next_card = (
            '<div class="next-card">'
            '<div class="next-label">Next up</div>'
            f'<div class="next-chips">{chips}</div>'
            f'<div class="next-meta">{cd} · {html.escape(first_day)}</div>'
            "</div>"
        )

    def render_groups(items: list[CalendarEvent]) -> str:
        if not items:
            return '<p class="empty">No events in this window.</p>'
        parts: list[str] = []
        for day, evs in _group_by_date(items):
            parts.append('<section class="day-group">')
            parts.append(_date_heading(day, evs[0].days_from_today))
            parts.append('<ul class="ev-list">')
            parts.extend(_event_row(e) for e in evs)
            parts.append("</ul>")
            parts.append("</section>")
        return "\n".join(parts)

    upcoming_html = render_groups(upcoming)
    past_html = render_groups(past)

    if not events:
        body_empty = (
            '<div class="empty-state">'
            "<p>No earnings events in the current window.</p>"
            "<p>Run <code>capex calendar sync</code> to pull upcoming dates "
            "from Alpha Vantage.</p>"
            "</div>"
        )
        upcoming_html = body_empty
        past_html = ""
        next_card = ""

    sync_caption = ""
    if last_sync:
        sync_caption = (
            f'<p class="sync-caption">Last calendar sync: '
            f'<code>{html.escape(last_sync)}</code></p>'
        )

    nav_html = _build_nav_html("calendar")
    return _HTML_TEMPLATE.format(
        nav_html=nav_html,
        next_card=next_card,
        upcoming_html=upcoming_html,
        past_html=past_html,
        sync_caption=sync_caption,
    )


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Earnings calendar — AI Infrastructure Ecosystem</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         margin: 0; padding: 20px; background: #fafafa; color: #222; }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ text-align: center; margin: 0 0 6px; font-size: 22px; }}
  .subtitle {{ text-align: center; color: #666; margin: 0 0 20px; font-size: 13px; }}
  .nav {{ text-align: center; margin: 0 auto 18px; max-width: 1400px; }}
  .nav-pill {{ display: inline-block; padding: 6px 14px; margin: 0 4px;
    border: 1px solid #888; border-radius: 18px; color: #555; font-size: 13px;
    text-decoration: none; background: #fff; }}
  .nav-pill:hover {{ border-color: #2E75B6; color: #2E75B6; }}
  .nav-pill.active {{ background: #2E75B6; color: #fff; border-color: #2E75B6;
    font-weight: bold; }}
  h2 {{ font-size: 16px; margin: 24px 0 10px; color: #333;
    border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
  h3.date-heading {{ font-size: 14px; margin: 12px 0 6px; color: #444;
    font-weight: 600; }}
  .countdown {{ color: #888; font-weight: normal; font-size: 13px; }}
  .next-card {{ background: #fff; border: 1px solid #d1d9e0;
    border-radius: 8px; padding: 14px 18px; margin-bottom: 18px; }}
  .next-label {{ font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.08em; color: #888; margin-bottom: 6px; }}
  .next-chips .chip {{ margin-right: 6px; }}
  .next-meta {{ margin-top: 6px; color: #555; font-size: 13px; }}
  .day-group {{ margin-bottom: 10px; }}
  ul.ev-list {{ list-style: none; padding: 0; margin: 0; }}
  li.ev-row {{ display: flex; align-items: center; gap: 10px;
    padding: 6px 10px; margin: 3px 0; background: #fff;
    border: 1px solid #e5e7eb; border-radius: 6px; font-size: 13px; }}
  .chip {{ display: inline-block; min-width: 48px; padding: 2px 8px;
    border-radius: 10px; color: #fff; font-size: 11px; font-weight: bold;
    text-align: center; }}
  .ev-period {{ min-width: 80px; font-weight: 600; color: #333; }}
  .ev-form {{ min-width: 48px; color: #666; }}
  .ev-end {{ flex: 1; color: #888; font-size: 12px; }}
  .badge {{ padding: 2px 8px; border-radius: 10px; font-size: 11px;
    font-weight: 600; text-transform: lowercase; }}
  .filing-link {{ color: #2E75B6; text-decoration: none; font-size: 12px; }}
  .filing-link:hover {{ text-decoration: underline; }}
  .empty, .empty-state {{ color: #888; font-style: italic; text-align: center;
    margin: 20px 0; }}
  .sync-caption {{ text-align: center; color: #aaa; font-size: 11px;
    margin-top: 30px; }}
  code {{ background: #eee; padding: 1px 4px; border-radius: 3px; font-size: 12px; }}
</style>
</head>
<body>
{nav_html}
<div class="container">
  <h1>Earnings calendar</h1>
  <p class="subtitle">Upcoming 90 days · recent 30 days · 13 tracked companies</p>
  {next_card}
  <h2>Upcoming</h2>
  {upcoming_html}
  <h2>Recently filed</h2>
  {past_html}
  {sync_caption}
</div>
</body>
</html>
"""
