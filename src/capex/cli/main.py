"""Top-level CLI entrypoint.

Subcommands land as each layer's Python implementation does. After
Phase 2a, the CLI exposes:

    capex db migrate                  apply pending DB migrations
    capex db sync-companies           refresh companies table from YAML
    capex db sync-metrics             refresh metric_definitions from YAML
    capex db sync-all                 migrate + both syncs
    capex fetch <TICKER> <FORM>       fetch latest filing from regulator
    capex organize [--ticker T]       sweep _raw/, copy to canonical layout

Examples:
    python -m capex.cli.main db sync-all
    python -m capex.cli.main fetch MSFT 10-K
    python -m capex.cli.main organize
    python -m capex.cli.main organize --ticker MSFT
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help"):
        _print_help()
        return 0

    cmd = argv[0]
    rest = argv[1:]

    if cmd == "db":
        return _db_command(rest)
    if cmd == "fetch":
        return _fetch_command(rest)
    if cmd == "organize":
        print("DEPRECATED: organize is no longer needed.", file=sys.stderr)
        print("Files are now saved with canonical names at download time.", file=sys.stderr)
        return 0
    if cmd == "extract":
        return _extract_command(rest)
    if cmd == "review":
        return _review_command(rest)
    if cmd == "calendar":
        return _calendar_command(rest)
    if cmd == "monitor":
        return _monitor_command(rest)
    if cmd == "export":
        return _export_command(rest)
    if cmd == "chart":
        return _chart_command(rest)
    if cmd == "reconcile":
        return _reconcile_command(rest)
    if cmd == "audit":
        return _audit_command(rest)
    if cmd == "treatments":
        return _treatments_command(rest)

    print(f"unknown command: {cmd}", file=sys.stderr)
    _print_help()
    return 2


def _db_command(argv: list[str]) -> int:
    from capex.db import migrate
    from capex.db.sync import (
        sync_companies,
        sync_coverage_treatments,
        sync_metric_definitions,
    )

    if not argv:
        print(
            "usage: capex db {migrate|sync-companies|sync-metrics|"
            "sync-coverage|sync-all}",
            file=sys.stderr,
        )
        return 2

    sub = argv[0]
    if sub == "migrate":
        version = migrate()
        print(f"schema at version {version}")
        return 0
    if sub == "sync-companies":
        n = sync_companies()
        print(f"synced {n} companies")
        return 0
    if sub == "sync-metrics":
        n = sync_metric_definitions()
        print(f"synced {n} metric definitions")
        return 0
    if sub == "sync-coverage":
        n = sync_coverage_treatments()
        print(f"synced {n} quarterly_convention rows")
        return 0
    if sub == "sync-all":
        migrate()
        nc = sync_companies()
        nm = sync_metric_definitions()
        nq = sync_coverage_treatments()
        print(
            f"migrate OK; synced {nc} companies, {nm} metric definitions, "
            f"{nq} quarterly conventions"
        )
        return 0

    print(f"unknown db subcommand: {sub}", file=sys.stderr)
    return 2


def _fetch_command(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: capex fetch <TICKER> <FORM>", file=sys.stderr)
        print("  e.g. capex fetch MSFT 10-K", file=sys.stderr)
        return 2

    ticker, form_type = argv
    from capex.fetch.dispatcher import fetch_and_record
    from capex.fetch.errors import FetchError

    try:
        result = fetch_and_record(ticker, form_type)
    except FetchError as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1
    except NotImplementedError as e:
        print(f"not yet implemented: {e}", file=sys.stderr)
        return 1

    status = "already in DB" if result.get("already_existed") else "fetched"
    print(f"{status}: source_documents id={result['id']}")
    print(f"  ticker:           {result['ticker']}")
    print(f"  form_type:        {result['form_type']}")
    print(f"  filing_date:      {result['filing_date']}")
    print(f"  period_of_report: {result['period_of_report']}")
    print(f"  sha256:           {result['sha256']}")
    print(f"  raw_path:         {result['raw_path']}")
    return 0


def _chart_command(argv: list[str]) -> int:
    output = None
    interactive = False
    i = 0
    while i < len(argv):
        if argv[i] in ("-o", "--output") and i + 1 < len(argv):
            output = argv[i + 1]
            i += 2
        elif argv[i] == "--interactive":
            interactive = True
            i += 1
        else:
            print(f"unknown option: {argv[i]}", file=sys.stderr)
            return 2

    from capex.exporters.charts import (
        generate_all_metric_charts,
        generate_cloud_revenue_chart,
    )

    # `-o PATH` still targets the cloud PNG (legacy behaviour); without
    # it, emit all four metric PNGs so the dashboard thumbnails stay in
    # sync with every run.
    if output is not None:
        path = generate_cloud_revenue_chart(output=output)
        print(f"static chart saved to {path}")
    else:
        for p in generate_all_metric_charts():
            print(f"static chart saved to {p}")

    if interactive:
        from capex.exporters.dashboard_html import generate_dashboard_html
        from capex.exporters.earnings_calendar_html import (
            generate_earnings_calendar_html,
        )
        from capex.exporters.interactive_chart import generate_all_interactive
        from capex.exporters.treatments_html import generate_treatments_html

        ipaths = generate_all_interactive()
        for p in ipaths:
            print(f"interactive chart saved to {p}")
        cal_path = generate_earnings_calendar_html()
        print(f"earnings calendar saved to {cal_path}")
        treat_path = generate_treatments_html()
        print(f"treatments viewer saved to {treat_path}")
        dash_path = generate_dashboard_html()
        print(f"dashboard saved to {dash_path}")

    return 0


def _reconcile_command(argv: list[str]) -> int:
    """Derive missing period values via identity reconciliation.

    Usage:
      capex reconcile [--ticker TICKER] [--metric METRIC]
                      [--fy YEAR] [--dry-run]
    """
    ticker = None
    metric_key = None
    fiscal_year: int | None = None
    dry_run = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--ticker" and i + 1 < len(argv):
            ticker = argv[i + 1]
            i += 2
        elif a == "--metric" and i + 1 < len(argv):
            metric_key = argv[i + 1]
            i += 2
        elif a == "--fy" and i + 1 < len(argv):
            fiscal_year = int(argv[i + 1])
            i += 2
        elif a in ("--dry-run", "-n"):
            dry_run = True
            i += 1
        elif a in ("--all",):
            i += 1
        else:
            print(f"unknown option: {a}", file=sys.stderr)
            return 2

    from capex.extract.reconcile import reconcile

    summary = reconcile(
        ticker=ticker,
        metric_key=metric_key,
        fiscal_year=fiscal_year,
        write=not dry_run,
    )
    mode = "dry-run" if dry_run else "committed"
    print(
        f"reconcile {mode}: derived={summary.derived} "
        f"conflicts={summary.conflicts} unresolved_q4={summary.unresolved}"
    )
    return 0


def _audit_command(argv: list[str]) -> int:
    """Dispatch `capex audit ...` subcommands.

    Modes:
      capex audit                → run the full data-quality audit
      capex audit review [...]   → open the human-in-the-loop review session
                                    over the last audit's JSON sidecar
    """
    if argv and argv[0] == "review":
        return _audit_review_command(argv[1:])

    import subprocess
    from pathlib import Path
    script = Path(__file__).resolve().parents[3] / "scripts" / "audit_data_quality.py"
    return subprocess.call([sys.executable, str(script), *argv])


def _audit_review_command(argv: list[str]) -> int:
    """Route `capex audit review` to the PEL-backed review session."""
    cluster = None
    limit = None
    report_path = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--cluster" and i + 1 < len(argv):
            cluster = argv[i + 1]
            i += 2
        elif a == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])
            i += 2
        elif a == "--report" and i + 1 < len(argv):
            from pathlib import Path
            report_path = Path(argv[i + 1])
            i += 2
        elif a in ("-h", "--help"):
            print("usage: capex audit review [--cluster TICKER[:METRIC]] "
                  "[--limit N] [--report PATH]")
            print()
            print("Walks flagged audit clusters, prompts for natural-language")
            print("guidance, formalizes it into data/seeds/human_notes.yaml,")
            print("and logs each interaction to the audit_review_feedback table.")
            return 0
        else:
            print(f"unknown option: {a}", file=sys.stderr)
            return 2

    from capex.audit.review import run_review
    return run_review(
        cluster_filter=cluster,
        limit=limit,
        report_path=report_path,
    )


def _export_command(argv: list[str]) -> int:
    output = None
    i = 0
    while i < len(argv):
        if argv[i] in ("-o", "--output") and i + 1 < len(argv):
            output = argv[i + 1]
            i += 2
        else:
            print(f"unknown option: {argv[i]}", file=sys.stderr)
            return 2

    from capex.exporters.excel import export_workbook

    path = export_workbook(output_path=output)
    print(f"exported to {path}")
    return 0


def _extract_command(argv: list[str]) -> int:
    # Parse flags
    ticker = None
    form_type = None
    metric_key = None
    period = None
    force = False
    batch = False
    i = 0
    while i < len(argv):
        if argv[i] == "--batch":
            batch = True
            i += 1
        elif argv[i] == "--metric" and i + 1 < len(argv):
            metric_key = argv[i + 1]
            i += 2
        elif argv[i] == "--form" and i + 1 < len(argv):
            form_type = argv[i + 1]
            i += 2
        elif argv[i] == "--period" and i + 1 < len(argv):
            period = argv[i + 1]
            i += 2
        elif argv[i] == "--force":
            force = True
            i += 1
        elif not argv[i].startswith("-") and ticker is None:
            ticker = argv[i]
            i += 1
        else:
            print(f"unknown option: {argv[i]}", file=sys.stderr)
            return 2

    # Batch mode: use the router
    if batch:
        return _extract_batch(
            metric_keys=[metric_key] if metric_key else None, force=force,
        )

    # Single ticker with --metric: use the router
    if ticker and metric_key:
        return _extract_single(
            ticker, metric_key, form_type=form_type,
            period=period, force=force,
        )

    # Legacy dry-run mode (no --metric)
    if not ticker:
        print("usage: capex extract <TICKER> [--form FORM] [--metric KEY]", file=sys.stderr)
        print("       capex extract --batch [--metric KEY]", file=sys.stderr)
        return 2

    from capex.db import Database
    from capex.read.sections import estimate_tokens, get_extraction_sections, parse_sections
    from capex.read.text import extract_text

    db = Database()

    # Find the latest source_documents row for this ticker
    with db.connect() as conn:
        if form_type:
            row = conn.execute(
                "SELECT id, ticker, form_type, period_of_report, raw_path FROM source_documents "
                "WHERE ticker = ? AND form_type = ? ORDER BY period_of_report DESC LIMIT 1",
                (ticker, form_type),
            ).fetchone()
        else:
            # Default: latest annual (10-K, 20-F, or HK-AR)
            row = conn.execute(
                "SELECT id, ticker, form_type, period_of_report, raw_path FROM source_documents "
                "WHERE ticker = ? AND form_type IN ('10-K', '20-F', 'HK-AR') "
                "ORDER BY period_of_report DESC LIMIT 1",
                (ticker,),
            ).fetchone()

    if row is None:
        msg = f"no source document found for {ticker}"
        if form_type:
            msg += f" {form_type}"
        print(msg, file=sys.stderr)
        print("run `capex fetch` first", file=sys.stderr)
        return 1

    doc_id, doc_ticker, doc_form, doc_period, raw_path = tuple(row)
    from capex.db.schema import REPO_ROOT
    abs_path = REPO_ROOT / raw_path

    print(f"source_documents id={doc_id}: {doc_ticker} {doc_form} period={doc_period}")
    print(f"  raw_path: {raw_path}")
    print()

    if not abs_path.exists():
        print(f"file not found: {abs_path}", file=sys.stderr)
        print("re-run `capex fetch` to download", file=sys.stderr)
        return 1

    print("extracting text...")
    text = extract_text(abs_path)
    print(f"  full text: {len(text):,} chars")

    print("parsing sections...")
    sections = parse_sections(text, doc_form)
    for name in sorted(sections.keys()):
        if name == "_full":
            continue
        print(f"  {name:16s}  {len(sections[name]):>8,} chars")

    ext_sections = get_extraction_sections(sections, doc_form)
    tokens = estimate_tokens(ext_sections)
    print(f"\nextraction-relevant sections ({len(ext_sections)}):")
    for name, content in ext_sections.items():
        print(f"  {name:16s}  {len(content):>8,} chars")
    print(f"\nestimated extraction context: ~{tokens:,} tokens")

    # Load metric definitions
    with db.connect() as conn:
        metrics = conn.execute("SELECT key, label FROM metric_definitions ORDER BY key").fetchall()
    print(f"\nmetrics to extract ({len(metrics)}):")
    for m in metrics:
        print(f"  {m[0]:35s}  {m[1]}")

    print("\n--- DRY RUN COMPLETE ---")
    print("To perform actual extraction, invoke the read-and-extract skill")
    print(f"in Claude Code: \"extract the headline metrics from {doc_ticker} {doc_form}\"")

    return 0


def _extract_single(
    ticker: str,
    metric_key: str,
    form_type: str | None = None,
    period: str | None = None,
    force: bool = False,
) -> int:
    """Extract a single metric for a ticker via the unified router."""
    from capex.extract.router import extract_metric

    msg = f"extracting {metric_key} for {ticker}"
    if force:
        msg += " (force=overwrite existing)"
    print(f"{msg}...")
    result = extract_metric(
        ticker, metric_key, period=period, form_type=form_type,
        write=True, force=force,
    )

    if result.status == "success":
        n = result.write_summary.get("inserted", 0) if result.write_summary else 0
        s = result.write_summary.get("skipped_existing", 0) if result.write_summary else 0
        o = result.write_summary.get("overwritten", 0) if result.write_summary else 0
        print(f"  ✓ {result.extractor}: inserted={n}, overwritten={o}, skipped={s}")
        return 0
    elif result.status == "needs_interactive":
        print(f"  → needs interactive LLM extraction (chain tried: {result.chain_tried})")
        print(f"    run in Claude Code: \"extract {metric_key} from {ticker}\"")
        return 0
    elif result.status == "needs_verification":
        print("  → extracted but needs dual-agent verification")
        return 0
    else:
        print(f"  ✗ no extractor succeeded (chain tried: {result.chain_tried})")
        return 1


def _extract_batch(
    metric_keys: list[str] | None = None, force: bool = False,
) -> int:
    """Batch extract for all companies."""
    from capex.extract.router import extract_batch

    msg = "running batch extraction"
    if force:
        msg += " (force=overwrite existing)"
    print(f"{msg}...")
    result = extract_batch(metric_keys=metric_keys, force=force)

    print("\n=== Batch Results ===")
    print(f"  succeeded:        {result.summary['succeeded']}")
    print(f"  needs_interactive: {result.summary['needs_interactive']}")
    print(f"  needs_review:     {result.summary['needs_review']}")
    print(f"  failed:           {result.summary['failed']}")

    if result.needs_interactive:
        print("\nNeeds interactive LLM extraction:")
        for ticker, metric in result.needs_interactive:
            print(f"  {ticker:8s} {metric}")

    if result.failed:
        print("\nFailed:")
        for f in result.failed:
            err = f.get("error", f.get("status", "?"))
            print(f"  {f.get('ticker', '?'):8s} {f.get('metric', '?')}: {err}")

    return 0


def _review_command(argv: list[str]) -> int:
    """Show extractions needing human review."""
    from capex.verification.evidence import get_unverified_extractions

    ticker = argv[0] if argv else None
    items = get_unverified_extractions(ticker=ticker)

    if not items:
        print("No items pending review.")
        return 0

    print(f"=== {len(items)} extractions pending verification ===\n")
    for item in items:
        print(
            f"  {item['ticker']:8s} {item['metric_key']:30s} "
            f"{item['period_of_report']}  {item['form_type']:6s}  "
            f"model={item['extracting_model']}"
        )
        if item.get("value_usd"):
            print(f"           value_usd=${item['value_usd']:,.0f}M")

    print("\nTo verify interactively, use the dual-agent workflow in Claude Code.")
    return 0


def _calendar_command(argv: list[str]) -> int:
    """Manage the fiscal earnings calendar."""
    if not argv:
        argv = ["show"]

    subcmd = argv[0]

    if subcmd == "sync":
        import os

        from capex.monitor.calendar import sync_earnings_calendar
        api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "demo")
        result = sync_earnings_calendar(api_key=api_key)
        print(f"Synced {result['synced']} earnings dates, skipped {result['skipped']}")
        if result["errors"]:
            for e in result["errors"]:
                print(f"  error: {e}")
        return 0

    if subcmd == "show":
        return _calendar_show(argv[1:])

    print(f"unknown calendar subcommand: {subcmd}", file=sys.stderr)
    print("  capex calendar sync         sync from Alpha Vantage")
    print("  capex calendar show         show upcoming + recent dates")
    print("  capex calendar show --week         next 7 days only")
    print("  capex calendar show --days N       custom upcoming window")
    print("  capex calendar show --ticker T     filter to one ticker")
    print("  capex calendar show --no-past      skip recent-filings section")
    print("  capex calendar show --format json  emit JSON instead of table")
    return 2


def _calendar_show(flags: list[str]) -> int:
    """Render the boxed-table view of the earnings calendar."""
    import json
    import sqlite3
    from pathlib import Path

    from capex.monitor.calendar import query_for_viewer

    days = 90
    ticker: str | None = None
    include_past = True
    fmt = "table"
    i = 0
    while i < len(flags):
        a = flags[i]
        if a == "--week":
            days = 7
        elif a == "--days" and i + 1 < len(flags):
            days = int(flags[i + 1])
            i += 1
        elif a == "--ticker" and i + 1 < len(flags):
            ticker = flags[i + 1].upper()
            i += 1
        elif a == "--no-past":
            include_past = False
        elif a == "--include-past":
            include_past = True
        elif a == "--format" and i + 1 < len(flags):
            fmt = flags[i + 1]
            i += 1
        else:
            print(f"unknown flag: {a}", file=sys.stderr)
            return 2
        i += 1

    repo_root = Path(__file__).resolve().parents[3]
    db_path = repo_root / "data" / "db" / "capex.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    past = 30 if include_past else 0
    events = query_for_viewer(
        conn, upcoming_days=days, past_days=past, ticker_filter=ticker,
    )
    conn.close()

    if fmt == "json":
        payload = [
            {
                "ticker": e.ticker,
                "company_name": e.company_name,
                "report_date": e.report_date,
                "fiscal_date_ending": e.fiscal_date_ending,
                "fiscal_year": e.fiscal_year,
                "period_label": e.period_label,
                "form_type": e.form_type,
                "status": e.status,
                "source_url": e.source_url,
                "days_from_today": e.days_from_today,
                "updated_at": e.updated_at,
            }
            for e in events
        ]
        print(json.dumps(payload, indent=2))
        return 0

    if not events:
        print("No earnings events in window. Run `capex calendar sync` to populate.")
        return 0

    _print_calendar_table(events)
    return 0


def _print_calendar_table(events: list) -> None:
    """Print a box-drawing table of CalendarEvent rows."""
    headers = ["Ticker", "Report date", "Period end", "Form", "Period",
               "Status", "Offset"]
    rows: list[list[str]] = []
    for e in events:
        period = f"FY{e.fiscal_year % 100:02d} {e.period_label}"
        if e.days_from_today > 0:
            offset = f"in {e.days_from_today}d"
        elif e.days_from_today < 0:
            offset = f"{abs(e.days_from_today)}d ago"
        else:
            offset = "today"
        rows.append([
            e.ticker,
            e.report_date,
            e.fiscal_date_ending,
            e.form_type or "?",
            period,
            e.status,
            offset,
        ])

    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            if len(cell) > widths[i]:
                widths[i] = len(cell)

    def fmt_row(cells: list[str], sep_l: str, sep_m: str, sep_r: str) -> str:
        parts = [f" {cells[i]:<{widths[i]}} " for i in range(len(cells))]
        return sep_l + sep_m.join(parts) + sep_r

    top = "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐"
    mid = "├" + "┼".join("─" * (w + 2) for w in widths) + "┤"
    bot = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"

    print(top)
    print(fmt_row(headers, "│", "│", "│"))
    print(mid)
    for r in rows:
        print(fmt_row(r, "│", "│", "│"))
    print(bot)

    # Summary line
    from collections import Counter
    upcoming = [e for e in events if e.days_from_today >= 0]
    past = [e for e in events if e.days_from_today < 0]
    up_counts = Counter(e.status for e in upcoming)
    past_counts = Counter(e.status for e in past)
    parts = []
    for s in ("upcoming", "detected", "fetched", "extracted", "failed"):
        if up_counts.get(s):
            parts.append(f"{up_counts[s]} {s}")
    past_parts = []
    for s in ("extracted", "fetched", "detected", "failed"):
        if past_counts.get(s):
            past_parts.append(f"{past_counts[s]} {s}")
    pieces = []
    if parts:
        pieces.append(", ".join(parts))
    if past_parts:
        pieces.append(f"last 30d: {', '.join(past_parts)}")
    print("  |  ".join(pieces))


def _treatments_command(argv: list[str]) -> int:
    """Browse human-authored special treatments per company."""
    if not argv:
        argv = ["show"]
    sub = argv[0]
    if sub == "show":
        return _treatments_show(argv[1:])
    print(f"unknown treatments subcommand: {sub}", file=sys.stderr)
    print("  capex treatments show                       dump all companies")
    print("  capex treatments show --ticker T            filter to ticker T")
    print("  capex treatments show --metric KEY          filter to metric KEY")
    print("  capex treatments show --format json         emit JSON")
    return 2


def _treatments_show(flags: list[str]) -> int:
    """Render per-company treatments as a table (default) or JSON."""
    import json
    from dataclasses import asdict

    from capex.audit.treatments_query import query_treatments

    ticker = None
    metric = None
    fmt = "table"
    i = 0
    while i < len(flags):
        a = flags[i]
        if a == "--ticker" and i + 1 < len(flags):
            ticker = flags[i + 1].upper()
            i += 1
        elif a == "--metric" and i + 1 < len(flags):
            metric = flags[i + 1]
            i += 1
        elif a == "--format" and i + 1 < len(flags):
            fmt = flags[i + 1]
            i += 1
        else:
            print(f"unknown flag: {a}", file=sys.stderr)
            return 2
        i += 1

    views = query_treatments(ticker_filter=ticker, metric_filter=metric)
    if fmt == "json":
        print(json.dumps([asdict(v) for v in views], indent=2,
                         ensure_ascii=False))
        return 0

    if not views:
        print("No companies match the filter.")
        return 0
    _print_treatments_table(views)
    return 0


def _print_treatments_table(views: list) -> None:
    """Compact per-company summary table."""
    headers = ["Ticker", "FYE", "Cur", "Approach", "Rules", "Human"]
    rows: list[list[str]] = []
    fye_short = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
                 7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
    for v in views:
        rows.append([
            v.ticker,
            fye_short.get(v.fiscal_year_end_month, str(v.fiscal_year_end_month)),
            v.reporting_currency,
            v.extraction_approach,
            str(len(v.dataset_rules)),
            str(len(v.human_notes)),
        ])
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            if len(cell) > widths[i]:
                widths[i] = len(cell)

    def fmt_row(cells: list[str], sep: str) -> str:
        parts = [f" {cells[i]:<{widths[i]}} " for i in range(len(cells))]
        return sep + sep.join(parts) + sep

    top = "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐"
    mid = "├" + "┼".join("─" * (w + 2) for w in widths) + "┤"
    bot = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"
    print(top)
    print(fmt_row(headers, "│"))
    print(mid)
    for r in rows:
        print(fmt_row(r, "│"))
    print(bot)

    # Detail block for each company
    for v in views:
        print()
        print(f"=== {v.ticker} — {v.full_name} ===")
        if v.company_notes:
            first = v.company_notes.strip().splitlines()[0]
            print(f"  notes: {first[:120]}"
                  f"{'…' if len(first) > 120 else ''}")
        for r in v.dataset_rules:
            metrics = ", ".join(r.metric_keys) or r.dataset
            tag = " [EXCLUDED]" if r.excluded else ""
            print(f"  • {metrics}: {r.treatment}{tag}")
            if r.segment_names:
                segs = ", ".join(f'"{s}"' for s in r.segment_names)
                print(f"      segments: {segs}")
            if r.adjustment and r.adjustment.get("formula"):
                print(f"      formula:  {r.adjustment['formula']}")
        for h in v.human_notes:
            scope_parts = []
            scope = h.scope or {}
            if scope.get("metric_keys"):
                scope_parts.append("/".join(scope["metric_keys"]))
            if scope.get("period_range"):
                scope_parts.append(scope["period_range"])
            scope_line = " · ".join(scope_parts) or "—"
            print(f"  ✎ {h.id} [{h.state}] {scope_line}")
            print(f"      {h.guidance[:120]}"
                  f"{'…' if len(h.guidance) > 120 else ''}")


def _monitor_command(argv: list[str]) -> int:
    """Run the filing monitor for a company or all today's earnings."""
    from capex.monitor.run import main as monitor_main
    return monitor_main(argv)


def _print_help() -> None:
    print(
        "neocloud-capex-tracker CLI\n"
        "\n"
        "commands:\n"
        "    db migrate          apply pending database migrations\n"
        "    db sync-companies   refresh companies table from _identity.yaml\n"
        "    db sync-metrics     refresh metric_definitions table from YAML seed\n"
        "    db sync-all         run migrate + both syncs\n"
        "    fetch <T> <FORM>    fetch latest <FORM> for ticker <T> from regulator\n"
        "                        e.g. capex fetch MSFT 10-K\n"
        "    organize            (DEPRECATED — naming happens at fetch time)\n"
        "    extract <TICKER>    dry-run: show sections + metrics for extraction\n"
        "                        --form FORM    specify form type (default: latest annual)\n"
        "                        --metric KEY   extract specific metric via router\n"
        "                        --batch        extract all companies (automated only)\n"
        "    review [TICKER]     show extractions pending human verification\n"
        "    calendar sync       sync earnings dates from Alpha Vantage\n"
        "    calendar show       show upcoming earnings (--week for 7 days)\n"
        "    monitor <TICKER>    poll SEC for latest filing + extract via LLM\n"
        "    monitor --all-today process all companies with earnings today\n"
        "    export              generate Excel workbook from DB\n"
        "                        -o PATH      output path (default: workbook/capex_tracker.xlsx)\n"
        "    chart               regenerate charts (YoY auto-recalculated)\n"
        "                        --interactive  also generate Plotly HTML\n"
        "    audit               run the data-quality audit (markdown + JSON)\n"
        "                        --apply        apply mechanical fixes\n"
        "                        --with-llm     re-verify flagged via LLM\n"
        "    audit review        open human-in-the-loop review of flagged items\n"
        "                        --cluster T[:M]  filter to one cluster\n"
        "                        --limit N        review at most N clusters\n"
        "    treatments show     browse per-company human-authored rules\n"
        "                        --ticker T       filter to ticker T\n"
        "                        --metric KEY     filter to metric KEY\n"
        "                        --format json    emit JSON instead of table\n"
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
