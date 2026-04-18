#!/usr/bin/env python3
"""Audit period-variant coverage per (ticker × fiscal_year).

Prints a matrix showing which of {Q1, Q2, Q3, Q4, H1, 9M, FY} are
filled for each company-year for a given metric. Derived rows (from
reconcile) are marked with `*`. Missing cells are `·`.

Usage:
    python scripts/audit_quarterly_coverage.py [--metric revenue]
                                                [--start-year 2019]
                                                [--end-year 2026]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

PERIOD_ORDER = ["Q1", "Q2", "Q3", "Q4", "H1", "9M", "FY"]


def _load(
    conn: sqlite3.Connection,
    metric_key: str,
    start_year: int,
    end_year: int,
) -> dict[tuple[str, int], dict[str, str]]:
    """Return {(ticker, fy): {period_type: "stored"|"derived"|"·"}}."""
    sql = (
        "SELECT sd.ticker, sd.fiscal_year, e.period_type, e.extracting_model "
        "FROM extractions e "
        "JOIN source_documents sd ON e.source_document_id = sd.id "
        "WHERE e.metric_key = ? "
        "  AND sd.fiscal_year BETWEEN ? AND ? "
        "  AND e.period_type != '' "
        "  AND e.value_usd IS NOT NULL"
    )
    matrix: dict[tuple[str, int], dict[str, str]] = {}
    for r in conn.execute(sql, (metric_key, start_year, end_year)):
        key = (r[0], r[1])
        ptype = r[2]
        model = r[3] or ""
        is_derived = "derived" in model or "reconcile" in model
        current = matrix.setdefault(key, {})
        if ptype not in current:
            current[ptype] = "derived" if is_derived else "stored"
        elif current[ptype] == "derived" and not is_derived:
            current[ptype] = "stored"
    return matrix


def _render(
    matrix: dict[tuple[str, int], dict[str, str]],
    start_year: int,
    end_year: int,
) -> str:
    tickers = sorted({k[0] for k in matrix.keys()})
    lines = []
    header = "Ticker  FY      " + " ".join(f"{p:>4}" for p in PERIOD_ORDER)
    lines.append(header)
    lines.append("-" * len(header))

    for ticker in tickers:
        for fy in range(start_year, end_year + 1):
            row = matrix.get((ticker, fy), {})
            if not row:
                continue
            cells = []
            for p in PERIOD_ORDER:
                status = row.get(p)
                if status == "stored":
                    cells.append(f"{'✓':>4}")
                elif status == "derived":
                    cells.append(f"{'*':>4}")
                else:
                    cells.append(f"{'·':>4}")
            lines.append(f"{ticker:<7} {fy}    " + " ".join(cells))
    lines.append("")
    lines.append("Legend:  ✓ stored  * derived (via reconcile)  · missing")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metric", default="revenue")
    parser.add_argument("--start-year", type=int, default=2019)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument(
        "--db", default=str(REPO_ROOT / "data" / "db" / "capex.db"),
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        matrix = _load(conn, args.metric, args.start_year, args.end_year)
    finally:
        conn.close()

    print(f"Period coverage for metric={args.metric!r} "
          f"({args.start_year}-{args.end_year}):")
    print()
    print(_render(matrix, args.start_year, args.end_year))
    return 0


if __name__ == "__main__":
    sys.exit(main())
