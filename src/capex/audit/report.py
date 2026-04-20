"""Markdown report generator for the data-quality audit.

Reads aggregated verdicts from `audit.orchestrator` and produces a
single markdown file — coverage matrix, flagged items, fixed items,
known-unfixable gaps, run metadata.

Also emits a stable-schema JSON sidecar (`*.json`) alongside the
markdown so downstream tooling (e.g. `capex audit review`, the
Protocol Elicitation Loop) can load the flagged cells without
re-parsing prose.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class CellRecord:
    ticker: str
    metric_key: str
    fiscal_year: int
    period_type: str
    value_usd: float | None
    extraction_id: int | None
    extracting_model: str | None
    classification: str   # verified | derived | flagged | gap_fixable | gap_unfixable
    check_results: list   # list[CheckResult]
    llm_verdict: str | None = None
    fix_applied: str | None = None


METRIC_NAMES = [
    "revenue", "capital_expenditures", "operating_cash_flow",
    "depreciation_amortization", "property_plant_equipment_net",
    "cloud_segment_revenue",
]
METRIC_DISPLAY = {
    "revenue": "Revenue",
    "capital_expenditures": "CapEx",
    "operating_cash_flow": "OCF",
    "depreciation_amortization": "D&A",
    "property_plant_equipment_net": "PP&E",
    "cloud_segment_revenue": "Cloud Seg",
}
CLASS_ICONS = {
    "verified": "✓", "derived": "*", "flagged": "⚠",
    "gap_fixable": "⊘", "gap_unfixable": "✗",
}


def write_report(
    cells: list[CellRecord],
    fixes_applied: list[dict],
    run_id: str,
    output: Path,
    *,
    restatement_summary=None,
) -> Path:
    """Write the full markdown report to `output` and return the path.

    Also writes a companion JSON file with the same stem (e.g.
    `data_quality_report.md` → `data_quality_report.json`) containing
    the full set of cells in a stable machine-readable schema.

    `restatement_summary` is an optional
    `capex.audit.restatement.RestatementSummary` — when supplied, a
    dedicated "Restatements" section is rendered and the findings are
    included in the JSON sidecar.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    _hdr(lines, cells, run_id)
    _coverage_matrix(lines, cells)
    _flagged_section(lines, cells)
    _fixed_section(lines, fixes_applied)
    _restatement_section(lines, restatement_summary)
    _unfixable_section(lines, cells)
    _metadata(lines, cells)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_json_sidecar(cells, fixes_applied, run_id, output,
                       restatement_summary=restatement_summary)
    return output


def _restatement_section(lines, summary) -> None:
    if summary is None:
        return
    # Lazy import to avoid a circular dep between report and restatement.
    from . import restatement as _r
    block = _r.render_markdown(summary)
    lines.extend(block.splitlines())
    lines.append("")


def _write_json_sidecar(
    cells: list[CellRecord],
    fixes_applied: list[dict],
    run_id: str,
    md_output: Path,
    *,
    restatement_summary=None,
) -> Path:
    """Emit a machine-readable snapshot alongside the markdown report."""
    json_output = md_output.with_suffix(".json")
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": dict(_counts(cells)),
        "cells": [_cell_to_json(c) for c in cells],
        "fixes_applied": list(fixes_applied),
    }
    if restatement_summary is not None:
        from . import restatement as _r
        payload["restatements"] = _r.render_json(restatement_summary)
    json_output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return json_output


def _cell_to_json(c: CellRecord) -> dict:
    """Serialize a CellRecord (stable schema for downstream tools)."""
    return {
        "cell_key": f"{c.ticker}:{c.metric_key}:{c.fiscal_year}{c.period_type}",
        "ticker": c.ticker,
        "metric_key": c.metric_key,
        "fiscal_year": c.fiscal_year,
        "period_type": c.period_type,
        "value_usd": c.value_usd,
        "extraction_id": c.extraction_id,
        "extracting_model": c.extracting_model,
        "classification": c.classification,
        "check_results": [
            {
                "check": cr.check_name,
                "passed": cr.passed,
                "severity": cr.severity,
                "details": cr.details,
            }
            for cr in c.check_results
        ],
        "llm_verdict": c.llm_verdict,
        "fix_applied": c.fix_applied,
    }


def _hdr(lines, cells, run_id):
    counts = _counts(cells)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"# Data Quality Audit — {ts}")
    lines.append("")
    lines.append(f"**Run ID**: `{run_id}`")
    lines.append("")
    lines.append("**Scope**: 13 tickers × 6 metrics × 2015–2025.  ")
    lines.append(f"**Total cells in universe**: {len(cells)}")
    lines.append("")
    status_line = " · ".join(
        f"{CLASS_ICONS[c]} {counts.get(c, 0)} {c.replace('_', ' ')}"
        for c in ("verified", "derived", "flagged", "gap_fixable", "gap_unfixable")
    )
    lines.append(f"**Status**: {status_line}")
    lines.append("")
    lines.append("---")
    lines.append("")


def _counts(cells: list[CellRecord]) -> dict[str, int]:
    c: dict[str, int] = defaultdict(int)
    for r in cells:
        c[r.classification] += 1
    return c


def _coverage_matrix(lines, cells: list[CellRecord]):
    lines.append("## Coverage matrix")
    lines.append("")
    lines.append("Percentages are `(verified + derived) / total` for each ticker × metric bucket.")
    lines.append("")
    tickers = sorted({r.ticker for r in cells})
    header = "| Ticker | " + " | ".join(METRIC_DISPLAY[m] for m in METRIC_NAMES) + " |"
    sep = "|---|" + "|".join(["---"] * len(METRIC_NAMES)) + "|"
    lines.append(header)
    lines.append(sep)
    for t in tickers:
        row = [t]
        for m in METRIC_NAMES:
            sub = [r for r in cells if r.ticker == t and r.metric_key == m]
            if not sub:
                row.append("—")
                continue
            ok = sum(1 for r in sub if r.classification in ("verified", "derived"))
            pct = 100.0 * ok / len(sub)
            flagged = sum(1 for r in sub if r.classification == "flagged")
            marker = "⚠" if flagged else "✓"
            row.append(f"{pct:.0f}% {marker}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")


def _flagged_section(lines, cells: list[CellRecord]):
    flagged = [r for r in cells if r.classification == "flagged"]
    lines.append(f"## Flagged items ({len(flagged)})")
    lines.append("")
    if not flagged:
        lines.append("_No mechanical flags in this run._")
        lines.append("")
        return
    by_ticker: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in flagged:
        by_ticker[r.ticker][r.metric_key].append(r)
    for ticker in sorted(by_ticker):
        for metric in sorted(by_ticker[ticker]):
            lines.append(f"### {ticker} — {METRIC_DISPLAY.get(metric, metric)}")
            for r in sorted(by_ticker[ticker][metric],
                            key=lambda x: (x.fiscal_year, x.period_type)):
                lines.append(_flagged_bullet(r))
            lines.append("")


def _flagged_bullet(r: CellRecord) -> str:
    tag = f"{r.fiscal_year}{r.period_type}" if r.period_type != "FY" else f"FY{r.fiscal_year}"
    failed = [cr for cr in r.check_results if not cr.passed]
    fail_desc = ", ".join(f"`{cr.check_name}`" for cr in failed) or "(no mechanical fail)"
    val = f"${r.value_usd:,.0f}M" if r.value_usd is not None else "—"
    parts = [f"- **{tag}** {val}: failed {fail_desc}"]
    for cr in failed[:3]:
        d = cr.details
        if "violations" in d:
            for v in d["violations"]:
                parts.append(f"  - identity `{v['identity']}` off by {v['delta_pct']}% "
                             f"(sum={v.get('lhs_sum', '?')}, rhs={v.get('rhs', '?')})")
        elif "outside" in d:
            parts.append(f"  - out-of-range ({d['outside']} {d['lo']}–{d['hi']})")
        elif "factor" in d:
            parts.append(f"  - continuity jump factor {d['factor']}x "
                         f"({d['prev_label']} → {d['this_label']})")
        elif "quote_numbers" in d:
            parts.append(f"  - quote has no match for {val}; "
                         f"numbers in quote: {d['quote_numbers'][:5]}")
        elif "duration_days" in d:
            parts.append(f"  - period_type/duration mismatch: "
                         f"expected {d['expected']}, got {d['duration_days']}d")
        elif "expected_any_of" in d:
            parts.append(f"  - segment name missing: expected any of "
                         f"{d['expected_any_of']}")
    if r.llm_verdict:
        parts.append(f"  - LLM verdict: **{r.llm_verdict}**")
    return "\n".join(parts)


def _fixed_section(lines, fixes: list[dict]):
    lines.append(f"## Fixed in this run ({len(fixes)})")
    lines.append("")
    if not fixes:
        lines.append("_No fixes applied (dry-run or nothing flagged fixable)._")
        lines.append("")
        return
    by_ticker: dict[str, list] = defaultdict(list)
    for f in fixes:
        by_ticker[f.get("ticker", "?")].append(f)
    for ticker in sorted(by_ticker):
        lines.append(f"### {ticker}")
        for f in by_ticker[ticker]:
            fy = f.get("fiscal_year", "?")
            pt = f.get("period_type", "?")
            m = f.get("metric_key", "?")
            old = f.get("old_usd")
            new = f.get("new_usd")
            strategy = f.get("fix_class", "?")
            lines.append(
                f"- **{fy}{pt}** {METRIC_DISPLAY.get(m, m)}: "
                f"${old:,.0f}M → ${new:,.0f}M ({strategy})"
                if old is not None and new is not None
                else f"- **{fy}{pt}** {METRIC_DISPLAY.get(m, m)}: {strategy}"
            )
        lines.append("")


def _unfixable_section(lines, cells: list[CellRecord]):
    unfixable = [r for r in cells if r.classification == "gap_unfixable"]
    lines.append(f"## Known unfixable gaps ({len(unfixable)})")
    lines.append("")
    if not unfixable:
        lines.append("_None._")
        lines.append("")
        return
    # Group by reason
    by_ticker: dict[str, list] = defaultdict(list)
    for r in unfixable:
        by_ticker[r.ticker].append(r)
    for ticker in sorted(by_ticker):
        metrics_affected = sorted({r.metric_key for r in by_ticker[ticker]})
        years_affected = sorted({r.fiscal_year for r in by_ticker[ticker]})
        lines.append(f"- **{ticker}**: {len(by_ticker[ticker])} cells "
                     f"(metrics: {', '.join(METRIC_DISPLAY.get(m, m) for m in metrics_affected)}; "
                     f"years: {min(years_affected)}-{max(years_affected)})")
    lines.append("")


def _metadata(lines, cells: list[CellRecord]):
    lines.append("## Run metadata")
    lines.append("")
    lines.append("- Database: `data/db/capex.db`")
    lines.append(f"- Metrics: {', '.join(METRIC_NAMES)}")
    lines.append("- Checks: gap, identity, range, continuity, cross_source, "
                 "sign, currency, segment_def, period_type")
    lines.append(f"- Total cells audited: {len(cells)}")
    lines.append("")
