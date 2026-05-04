"""Daily monitor entry point — called by cron or manually.

Checks if any company has earnings today (from fiscal_calendar),
polls SEC for the filing, runs dual-agent extraction, regenerates
outputs, and pushes to GitHub.

Usage:
    # Via cron (daily, after US market close):
    PYTHONPATH=src python3 -m capex.monitor.run --catch-up

    # All companies whose earnings have been announced but never
    # extracted, regardless of how far back:
    PYTHONPATH=src python3 -m capex.monitor.run --catch-up

    # Same, but bounded floor (only look back to 2026-04-25):
    PYTHONPATH=src python3 -m capex.monitor.run --catch-up --since 2026-04-25

    # Strict same-day mode (today's earnings only):
    PYTHONPATH=src python3 -m capex.monitor.run --all-today

    # Manual trigger for one company:
    PYTHONPATH=src python3 -m capex.monitor.run MSFT
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

from ..adapters.cli_backend import CLIBackend
from ..db import Database
from .calendar import get_pending_earnings, get_todays_earnings
from .watcher import watch_and_extract

REPO_ROOT = Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    db = Database()

    # Detect CLI backend
    tool = CLIBackend.detect_available()
    if not tool:
        print("ERROR: No LLM CLI tool found (claude/gemini/codex)")
        return 1
    backend = CLIBackend(tool)
    print(f"Using CLI backend: {backend}")

    # Determine what to process
    if argv and argv[0] == "--catch-up":
        since = _parse_since_flag(argv[1:])
        earnings = get_pending_earnings(since=since, db=db)
        if not earnings:
            window = f" since {since}" if since else ""
            print(f"No pending earnings to catch up on{window}.")
            return 0
        print(
            f"Catch-up: {len(earnings)} pending — "
            f"{[(e['ticker'], e['report_date']) for e in earnings]}"
        )
        tickers_forms = [
            (e["ticker"], _infer_form_type(e, db))
            for e in earnings
        ]
    elif argv and argv[0] == "--all-today":
        earnings = get_todays_earnings(db=db)
        if not earnings:
            print("No earnings scheduled today.")
            return 0
        print(f"Earnings today: {[e['ticker'] for e in earnings]}")
        tickers_forms = [
            (e["ticker"], _infer_form_type(e, db))
            for e in earnings
        ]
    elif argv and not argv[0].startswith("-"):
        # Manual trigger for specific ticker
        ticker = argv[0]
        form_type = argv[1] if len(argv) > 1 else _default_form_type(ticker, db)
        tickers_forms = [(ticker, form_type)]
    else:
        # Default: check today's earnings
        earnings = get_todays_earnings(db=db)
        if not earnings:
            print("No earnings scheduled today.")
            return 0
        print(f"Earnings today: {[e['ticker'] for e in earnings]}")
        tickers_forms = [
            (e["ticker"], _infer_form_type(e, db))
            for e in earnings
        ]

    # Process each company
    results = []
    for ticker, form_type in tickers_forms:
        print(f"\n=== Processing {ticker} ({form_type}) ===")
        result = watch_and_extract(
            ticker, form_type,
            backend=backend, db=db,
            max_polls=1,       # single poll for manual/cron (not a long-running daemon)
            interval=0,
        )
        results.append(result)

        if result["status"] == "success":
            print(f"  Extracted: {result.get('metrics_extracted', [])}")
            if result.get("issues"):
                print(f"  Issues: {result['issues']}")
        else:
            print(f"  Status: {result['status']}")

    # Regenerate outputs if any succeeded
    successes = [r for r in results if r["status"] == "success"]
    if successes:
        print("\nRegenerating outputs...")
        _regenerate_outputs()
        _git_commit_and_push(results)
        _create_github_issue(results)
        _notify_subscribers(results, db=db)

    return 0


def _notify_subscribers(results: list[dict], db: Database) -> None:
    """Send email notifications to enabled subscribers (failsafe)."""
    try:
        from ..notify import notify_subscribers
        summary = notify_subscribers(results, db=db)
        print(
            f"  Notify: sent={summary['sent']} "
            f"skipped={summary['skipped']} errors={len(summary['errors'])}"
        )
        for err in summary["errors"][:3]:
            print(f"    - {err.get('phase')}: {err.get('error')}")
    except Exception as e:
        print(f"  Notify error: {e}")


def _parse_since_flag(rest: list[str]) -> str | None:
    """Pull `--since YYYY-MM-DD` out of remaining argv. Returns None if absent."""
    if "--since" in rest:
        i = rest.index("--since")
        if i + 1 < len(rest):
            return rest[i + 1]
        raise SystemExit("--since requires a YYYY-MM-DD argument")
    return None


def _infer_form_type(earning: dict, db: Database) -> str:
    """Infer the expected form type from the calendar entry or company config."""
    if earning.get("form_type"):
        return earning["form_type"]

    ticker = earning["ticker"]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT preferred_source FROM companies WHERE ticker = ?",
            (ticker,),
        ).fetchone()

    if row and row["preferred_source"] == "hkex":
        return "HK-AR"

    # Check if this is a quarter-end or year-end
    from ..extract.coverage import get_company_treatment
    company = get_company_treatment(ticker)
    if not company:
        return "10-Q"

    cadence = company.filing_cadence
    return cadence.get("quarterly") or cadence.get("annual") or "10-Q"


def _default_form_type(ticker: str, db: Database) -> str:
    """Get the default quarterly form type for a company."""
    from ..extract.coverage import get_company_treatment
    company = get_company_treatment(ticker)
    if company:
        cadence = company.filing_cadence
        return cadence.get("quarterly") or cadence.get("annual") or "10-Q"
    return "10-Q"


def _regenerate_outputs() -> None:
    """Reconcile period_types, then regenerate workbook + all chart pages.

    Reconcile MUST run before the exporters: XBRL-extracted rows land
    with `period_type=''` and the chart selectors filter by
    `period_type IN ('FY','Q1','Q2','Q3','Q4')`. Without reconcile the
    new period would be invisible to every chart and HTML viewer.
    """
    try:
        from ..extract.reconcile import reconcile
        summary = reconcile(write=True)
        print(
            f"  Reconcile: derived={summary.derived} "
            f"conflicts={summary.conflicts} unresolved={summary.unresolved}"
        )
    except Exception as e:
        print(f"  Reconcile error: {e}")

    try:
        from ..exporters.excel import export_workbook
        export_workbook()
        print("  Excel regenerated")
    except Exception as e:
        print(f"  Excel error: {e}")

    try:
        from ..exporters.charts import generate_all_metric_charts
        from ..exporters.dashboard_html import generate_dashboard_html
        from ..exporters.earnings_calendar_html import (
            generate_earnings_calendar_html,
        )
        from ..exporters.interactive_chart import generate_all_interactive
        from ..exporters.treatments_html import generate_treatments_html
        generate_all_metric_charts()
        generate_all_interactive()
        generate_dashboard_html()
        generate_earnings_calendar_html()
        generate_treatments_html()
        print("  Charts + dashboards regenerated")
    except Exception as e:
        print(f"  Chart error: {e}")

    try:
        from pathlib import Path

        from ..db.dump import dump_to_sql
        dump_to_sql(
            Path("data/db/capex.db"),
            Path("data/db/dump.sql"),
        )
        print("  dump.sql regenerated")
    except Exception as e:
        print(f"  dump.sql error: {e}")


def _git_commit_and_push(results: list[dict]) -> None:
    """Auto-commit and push results."""
    today = date.today().isoformat()
    tickers = [r["ticker"] for r in results if r["status"] == "success"]
    msg = f"data(auto): {today} earnings update ({', '.join(tickers)})"

    try:
        subprocess.run(
            ["git", "add", "data/db/", "workbook/", "charts/", "docs/"],
            cwd=str(REPO_ROOT), check=True,
        )
        # Check if there are changes to commit
        result = subprocess.run(
            ["git", "diff", "--staged", "--quiet"],
            cwd=str(REPO_ROOT),
        )
        if result.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", msg],
                cwd=str(REPO_ROOT), check=True,
            )
            subprocess.run(
                ["git", "push"],
                cwd=str(REPO_ROOT), check=True,
            )
            print(f"  Committed and pushed: {msg}")
        else:
            print("  No changes to commit")
    except Exception as e:
        print(f"  Git error: {e}")


def _create_github_issue(results: list[dict]) -> None:
    """Create a GitHub issue summarizing the extraction run."""
    today = date.today().isoformat()
    successes = [r for r in results if r["status"] == "success"]
    failures = [r for r in results if r["status"] != "success"]

    if not successes:
        return

    title = f"Filing update: {today}"
    body_lines = [f"## Automated extraction — {today}\n"]

    for r in successes:
        metrics = ", ".join(r.get("metrics_extracted", []))
        body_lines.append(
            f"- **{r['ticker']}** {r.get('period', '?')}: "
            f"extracted {metrics}"
        )
        if r.get("issues"):
            for issue in r["issues"]:
                body_lines.append(f"  - Issue: {issue}")

    if failures:
        body_lines.append("\n### Failed")
        for r in failures:
            body_lines.append(f"- **{r['ticker']}**: {r['status']}")

    body = "\n".join(body_lines)

    try:
        subprocess.run(
            ["gh", "issue", "create",
             "--title", title,
             "--body", body,
             "--label", "auto-extraction"],
            cwd=str(REPO_ROOT), check=True,
        )
        print(f"  GitHub issue created: {title}")
    except Exception as e:
        print(f"  GitHub issue error: {e}")


if __name__ == "__main__":
    sys.exit(main())
