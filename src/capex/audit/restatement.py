"""Restatement report helpers (post-LLM-rework).

Restatement *capture* now happens inside the LLM dual-agent extractor:
every filing read yields one extraction per period visible in the
target table, with comparative rows written against a virtual
`source_documents` row. The `filing_date DESC` selector then
automatically promotes the most recently-filed observation per cell.

The audit check here is a **reporter**: for each (ticker, metric,
fy, period) cell, compare the currently-authoritative value against
the most recent primary (non-restated) observation for the same cell
— if they differ beyond tolerance, it's evidence that a later filing
restated the period. The finding surfaces in the audit markdown/JSON
for reviewer attention; the winning chart value is already the
restated one.

Writing of restated rows is no longer part of this module. Apply
happens implicitly by running the LLM extractor on newer filings via
`capex extract --ticker T --metric M` or the sweep script
`scripts/sweep_llm_restatements.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..db import Database


@dataclass
class RestatementFinding:
    """One detected restatement (primary original vs latest-filed)."""
    cell_key: str
    ticker: str
    metric_key: str
    fiscal_year: int
    period_type: str
    original_value_usd: float | None
    latest_value_usd: float | None
    delta_pct: float
    original_filing_date: str
    latest_filing_date: str
    latest_source_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_key": self.cell_key,
            "ticker": self.ticker,
            "metric_key": self.metric_key,
            "fiscal_year": self.fiscal_year,
            "period_type": self.period_type,
            "original_value_usd": self.original_value_usd,
            "latest_value_usd": self.latest_value_usd,
            "delta_pct": self.delta_pct,
            "original_filing_date": self.original_filing_date,
            "latest_filing_date": self.latest_filing_date,
            "latest_source_url": self.latest_source_url,
        }


@dataclass
class RestatementSummary:
    findings: list[RestatementFinding] = field(default_factory=list)
    tickers_scanned: int = 0

    @property
    def total(self) -> int:
        return len(self.findings)


DEFAULT_TOLERANCE = 0.005


def detect(
    *,
    db: Database | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
) -> RestatementSummary:
    """Find cells where a later-filed observation disagrees with the
    original (quarter-of-origin) filing's value by more than `tolerance`.

    Purely observational — no writes. Callers can inspect the report to
    see which cells have been restated and re-run `capex extract
    --ticker T --metric M` (or the LLM sweep) to refresh as needed.
    """
    db = db or Database()
    out = RestatementSummary()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT e.metric_key, e.period_type, e.value_usd,
                   sd.ticker, sd.fiscal_year, sd.filing_date, sd.source_url,
                   e.extracting_model
            FROM extractions e
            JOIN source_documents sd ON e.source_document_id = sd.id
            WHERE e.period_type IN ('FY','Q1','Q2','Q3','Q4','H1','H2','9M')
              AND e.value_usd IS NOT NULL
            ORDER BY sd.ticker, sd.fiscal_year, e.metric_key,
                     e.period_type, sd.filing_date
            """
        ).fetchall()

        # Group by cell; compare earliest-filed vs latest-filed observation.
        groups: dict[tuple[str, int, str, str], list[dict]] = {}
        for r in rows:
            key = (r["ticker"], r["fiscal_year"], r["metric_key"],
                   r["period_type"])
            groups.setdefault(key, []).append(dict(r))
        out.tickers_scanned = len({k[0] for k in groups})
        for (ticker, fy, metric, pt), obs in groups.items():
            if len(obs) < 2:
                continue
            obs.sort(key=lambda x: x["filing_date"] or "")
            first, latest = obs[0], obs[-1]
            fv = first.get("value_usd") or 0.0
            lv = latest.get("value_usd") or 0.0
            denom = max(abs(fv), 1.0)
            delta_pct = abs(lv - fv) / denom
            if delta_pct <= tolerance:
                continue
            out.findings.append(RestatementFinding(
                cell_key=f"{ticker}:{metric}:{fy}{pt}",
                ticker=ticker, metric_key=metric,
                fiscal_year=fy, period_type=pt,
                original_value_usd=fv, latest_value_usd=lv,
                delta_pct=delta_pct,
                original_filing_date=first.get("filing_date") or "",
                latest_filing_date=latest.get("filing_date") or "",
                latest_source_url=latest.get("source_url") or "",
            ))
    return out


def render_markdown(summary: RestatementSummary) -> str:
    if not summary.findings:
        return (
            "## Restatements\n\n"
            "_No restatements detected — every cell has a single observation "
            "or all observations agree within tolerance._\n"
        )
    lines = [
        f"## Restatements ({summary.total})",
        "",
        ("Cells where a later-filed observation disagrees with the "
         "original by > 0.5%. The newer value is authoritative via the "
         "`filing_date DESC` selector."),
        "",
        "| Cell | Original | Latest | Δ | Original filed | Latest filed |",
        "|---|---|---|---|---|---|",
    ]
    for f in sorted(summary.findings,
                    key=lambda x: (x.ticker, x.fiscal_year)):
        ov = (f"${f.original_value_usd:,.0f}M"
              if f.original_value_usd is not None else "—")
        lv = (f"${f.latest_value_usd:,.0f}M"
              if f.latest_value_usd is not None else "—")
        lines.append(
            f"| {f.cell_key} | {ov} | {lv} | {f.delta_pct * 100:.1f}% "
            f"| {f.original_filing_date} | "
            f"[{f.latest_filing_date}]({f.latest_source_url or '#'}) |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def render_json(summary: RestatementSummary) -> list[dict[str, Any]]:
    return [f.to_dict() for f in summary.findings]
