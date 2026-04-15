"""Daily monitor entry point — called by cron or manually.

Checks if any company has earnings today (from fiscal_calendar),
polls SEC for the filing, runs dual-agent extraction, regenerates
outputs, and pushes to GitHub.

Usage:
    # Via cron (daily at 6 AM):
    PYTHONPATH=src python3 -m capex.monitor.run

    # Manual trigger for one company:
    PYTHONPATH=src python3 -m capex.monitor.run MSFT

    # Manual trigger for all today's earnings:
    PYTHONPATH=src python3 -m capex.monitor.run --all-today
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..adapters.cli_backend import CLIBackend
from ..db import Database
from .calendar import get_todays_earnings
from .watcher import watch_and_extract, poll_sec_latest, already_in_db

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
    if argv and argv[0] == "--all-today":
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

    return 0


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
        return company.filing_cadence.get("quarterly") or company.filing_cadence.get("annual") or "10-Q"
    return "10-Q"


def _regenerate_outputs() -> None:
    """Regenerate Excel workbook and charts."""
    try:
        from ..exporters.excel import export_workbook
        export_workbook()
        print("  Excel regenerated")
    except Exception as e:
        print(f"  Excel error: {e}")

    try:
        from ..exporters.interactive_chart import generate_interactive
        from ..exporters.charts import generate_cloud_revenue_chart
        generate_cloud_revenue_chart()
        generate_interactive()
        print("  Charts regenerated")
    except Exception as e:
        print(f"  Chart error: {e}")


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
