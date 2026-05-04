"""End-to-end notify entry point — called from monitor/run.py.

For each successful filing in `results`, build a per-filing email and
send it to every enabled subscriber whose ticker filter matches.
Crash-safe: all exceptions are caught and logged so a notify failure
never breaks the cron run that just succeeded at extraction.
"""
from __future__ import annotations

import traceback
from typing import Any

from ..db import Database
from ..extract.coverage import get_company_treatment
from .email_sender import SMTPNotConfigured, send_email
from .formatter import (
    DEFAULT_METRIC_ORDER,
    FilingContext,
    build_html,
    build_subject,
    build_text,
)
from .performance import get_performance
from .subscribers import filter_for_ticker, load_subscribers


def _resolve_filing_context(
    result: dict[str, Any],
    db: Database,
    metric_keys: list[str],
) -> FilingContext | None:
    """Pull source_documents row + per-metric performance for one filing."""
    ticker = result["ticker"]
    period = result.get("period")
    if not period:
        return None

    with db.connect() as conn:
        sd = conn.execute(
            """
            SELECT form_type, filing_date, source_url
            FROM source_documents
            WHERE ticker = ? AND period_of_report = ?
            ORDER BY filing_date DESC LIMIT 1
            """,
            (ticker, period),
        ).fetchone()
    if sd is None:
        return None

    company = get_company_treatment(ticker)
    company_name = company.full_name if company else ticker

    perfs = []
    for mk in metric_keys:
        p = get_performance(ticker, mk, period, db=db)
        if p is not None:
            perfs.append(p)

    if not perfs:
        return None

    return FilingContext(
        ticker=ticker,
        company_name=company_name,
        form_type=sd["form_type"] or "",
        period_of_report=period,
        filing_date=sd["filing_date"] or "",
        source_url=sd["source_url"],
        period_label=perfs[0].current.period_label,
        performances=perfs,
    )


def notify_subscribers(
    results: list[dict[str, Any]],
    *,
    db: Database | None = None,
    send_fn=send_email,
) -> dict[str, Any]:
    """Send one email per (subscriber, successful filing) pair.

    Returns a summary dict {sent: N, skipped: N, errors: [...]} so the
    caller can log results. Never raises — SMTP / formatting errors
    surface in `errors` but the function always returns cleanly.
    """
    db = db or Database()
    summary: dict[str, Any] = {"sent": 0, "skipped": 0, "errors": []}

    try:
        subs = load_subscribers()
    except Exception as e:
        summary["errors"].append({"phase": "load_subscribers", "error": str(e)})
        return summary

    if not subs:
        summary["skipped"] = len(results)
        return summary

    successes = [r for r in results if r.get("status") == "success"]
    for result in successes:
        ticker = result["ticker"]
        matching = filter_for_ticker(subs, ticker)
        if not matching:
            summary["skipped"] += 1
            continue
        for sub in matching:
            try:
                metric_keys = (
                    list(DEFAULT_METRIC_ORDER) if "*" in sub.metrics
                    else [m for m in DEFAULT_METRIC_ORDER if m in sub.metrics]
                )
                ctx = _resolve_filing_context(result, db, metric_keys)
                if ctx is None:
                    summary["skipped"] += 1
                    continue
                send_fn(
                    to_email=sub.email,
                    subject=build_subject(ctx),
                    html_body=build_html(ctx),
                    text_body=build_text(ctx),
                )
                summary["sent"] += 1
            except SMTPNotConfigured as e:
                summary["errors"].append({
                    "phase": "smtp_config",
                    "subscriber": sub.email,
                    "error": str(e),
                })
                # No point trying other subscribers if SMTP isn't set up
                return summary
            except Exception as e:
                summary["errors"].append({
                    "phase": "send",
                    "subscriber": sub.email,
                    "ticker": ticker,
                    "error": f"{type(e).__name__}: {e}",
                    "traceback": traceback.format_exc(),
                })
    return summary
