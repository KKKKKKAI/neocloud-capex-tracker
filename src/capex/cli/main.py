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
        return _organize_command(rest)
    if cmd == "extract":
        return _extract_command(rest)
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


def _organize_command(argv: list[str]) -> int:
    ticker_filter = None
    dry_run = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--ticker":
            if i + 1 >= len(argv):
                print("--ticker requires a value", file=sys.stderr)
                return 2
            ticker_filter = argv[i + 1]
            i += 2
        elif arg == "--dry-run":
            dry_run = True
            i += 1
        else:
            print(f"unknown option: {arg}", file=sys.stderr)
            return 2

    from capex.organize.walker import sweep

    summary = sweep(ticker_filter=ticker_filter, dry_run=dry_run)
    print("organize sweep complete:")
    print(f"  scanned:                  {summary['scanned']}")
    print(f"  copied:                   {summary['copied']}")
    print(f"  skipped_already_canonical: {summary['skipped_already_canonical']}")
    print(f"  collisions:               {summary['collisions']}")
    if summary["errors"]:
        print(f"  errors ({len(summary['errors'])}):")
        for err in summary["errors"]:
            print(f"    - {err}")
        return 1
    return 0


def _chart_command(argv: list[str]) -> int:
    output = None
    i = 0
    while i < len(argv):
        if argv[i] in ("-o", "--output") and i + 1 < len(argv):
            output = argv[i + 1]
            i += 2
        else:
            print(f"unknown option: {argv[i]}", file=sys.stderr)
            return 2

    from capex.exporters.charts import generate_cloud_revenue_chart

    path = generate_cloud_revenue_chart(output=output)
    print(f"chart saved to {path}")
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
    if not argv:
        print("usage: capex extract <TICKER> [--form FORM]", file=sys.stderr)
        print("  dry-run: shows sections + prompt for extraction", file=sys.stderr)
        print("  e.g. capex extract MSFT", file=sys.stderr)
        return 2

    ticker = argv[0]
    form_type = None
    i = 1
    while i < len(argv):
        if argv[i] == "--form" and i + 1 < len(argv):
            form_type = argv[i + 1]
            i += 2
        else:
            print(f"unknown option: {argv[i]}", file=sys.stderr)
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
        "    organize            sweep data/_sources/ and create canonical copies\n"
        "                        --ticker T   only this ticker\n"
        "                        --dry-run    log actions without writing\n"
        "    extract <TICKER>    dry-run: show sections + metrics for extraction\n"
        "                        --form FORM  specify form type (default: latest annual)\n"
        "    export              generate Excel workbook from DB\n"
        "                        -o PATH      output path (default: workbook/capex_tracker.xlsx)\n"
        "    chart               regenerate charts (YoY auto-recalculated)\n"
        "                        -o PATH  output path\n"
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
