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

    print(f"unknown command: {cmd}", file=sys.stderr)
    _print_help()
    return 2


def _db_command(argv: list[str]) -> int:
    from capex.db import migrate
    from capex.db.sync import sync_companies, sync_metric_definitions

    if not argv:
        print("usage: capex db {migrate|sync-companies|sync-metrics|sync-all}", file=sys.stderr)
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
    if sub == "sync-all":
        migrate()
        nc = sync_companies()
        nm = sync_metric_definitions()
        print(f"migrate OK; synced {nc} companies and {nm} metric definitions")
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

    from capex.exporters.charts import generate_cloud_revenue_chart

    path = generate_cloud_revenue_chart(output=output)
    print(f"static chart saved to {path}")

    if interactive:
        from capex.exporters.interactive_chart import generate_interactive

        ipath = generate_interactive(output=output)
        print(f"interactive chart saved to {ipath}")

    return 0


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
        elif not argv[i].startswith("-") and ticker is None:
            ticker = argv[i]
            i += 1
        else:
            print(f"unknown option: {argv[i]}", file=sys.stderr)
            return 2

    # Batch mode: use the router
    if batch:
        return _extract_batch(metric_keys=[metric_key] if metric_key else None)

    # Single ticker with --metric: use the router
    if ticker and metric_key:
        return _extract_single(ticker, metric_key, form_type=form_type)

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


def _extract_single(ticker: str, metric_key: str, form_type: str | None = None) -> int:
    """Extract a single metric for a ticker via the unified router."""
    from capex.extract.router import extract_metric

    print(f"extracting {metric_key} for {ticker}...")
    result = extract_metric(ticker, metric_key, form_type=form_type, write=True)

    if result.status == "success":
        n = result.write_summary.get("inserted", 0) if result.write_summary else 0
        s = result.write_summary.get("skipped_existing", 0) if result.write_summary else 0
        print(f"  ✓ {result.extractor}: inserted={n}, skipped={s}")
        return 0
    elif result.status == "needs_interactive":
        print(f"  → needs interactive LLM extraction (chain tried: {result.chain_tried})")
        print(f"    run in Claude Code: \"extract {metric_key} from {ticker}\"")
        return 0
    elif result.status == "needs_verification":
        print(f"  → extracted but needs dual-agent verification")
        return 0
    else:
        print(f"  ✗ no extractor succeeded (chain tried: {result.chain_tried})")
        return 1


def _extract_batch(metric_keys: list[str] | None = None) -> int:
    """Batch extract for all companies."""
    from capex.extract.router import extract_batch

    print("running batch extraction...")
    result = extract_batch(metric_keys=metric_keys)

    print(f"\n=== Batch Results ===")
    print(f"  succeeded:        {result.summary['succeeded']}")
    print(f"  needs_interactive: {result.summary['needs_interactive']}")
    print(f"  needs_review:     {result.summary['needs_review']}")
    print(f"  failed:           {result.summary['failed']}")

    if result.needs_interactive:
        print(f"\nNeeds interactive LLM extraction:")
        for ticker, metric in result.needs_interactive:
            print(f"  {ticker:8s} {metric}")

    if result.failed:
        print(f"\nFailed:")
        for f in result.failed:
            print(f"  {f.get('ticker', '?'):8s} {f.get('metric', '?')}: {f.get('error', f.get('status', '?'))}")

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

    print(f"\nTo verify interactively, use the dual-agent workflow in Claude Code.")
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
        from capex.monitor.calendar import get_upcoming_earnings
        days = 90
        if "--week" in argv:
            days = 7
        upcoming = get_upcoming_earnings(days=days)
        if not upcoming:
            print("No upcoming earnings.")
            return 0
        print(f"Upcoming earnings (next {days} days):")
        for u in upcoming:
            status = u["status"]
            mark = "  " if status == "upcoming" else f" [{status}]"
            print(f"  {u['report_date']}  {u['ticker']:8s}  "
                  f"{u['form_type'] or '?':6s}  "
                  f"Q ending {u['fiscal_date_ending']}{mark}")
        return 0

    print(f"unknown calendar subcommand: {subcmd}", file=sys.stderr)
    print("  capex calendar sync         sync from Alpha Vantage")
    print("  capex calendar show         show upcoming dates")
    print("  capex calendar show --week   this week only")
    return 2


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
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
