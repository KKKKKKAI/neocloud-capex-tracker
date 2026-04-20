"""Restatement detector + applier.

Connected to the `capex audit` CLI. On every audit run it:

1. Iterates the tracked universe (ticker × metric × FY).
2. For each ticker + metric, invokes the restated-comparative
   extractor to see what the latest filing says about earlier
   periods.
3. Emits findings when a later filing's value disagrees with our
   currently-stored value beyond `tolerance`.

When `capex audit --apply` is passed, the detector also writes the
restated extractions via `writer.write_extractions` so the next
chart regeneration picks them up automatically (per the filing_date
DESC selector ordering shipped in this change set).

See `docs/RESTATEMENT_POLICY.md` for the full policy.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..db import Database
from ..extract.coverage import get_all_tickers
from ..extract.extractors.restated import (
    RestatedCandidate,
    detect_annual_restatements,
)

DEFAULT_TOLERANCE = 0.005  # 0.5%
METRICS_TO_SCAN = (
    "cloud_segment_revenue",
    # Headline metrics are XBRL-native; restatements flow through the
    # XBRL restated-capture path in xbrl/timeseries.py. Segment-table
    # parsing is only meaningful for segment metrics.
)


@dataclass
class RestatementFinding:
    """One detected restatement. Wire-format for the audit report."""
    cell_key: str                     # "TICKER:METRIC_KEY:FY{year}FY"
    ticker: str
    metric_key: str
    fiscal_year: int
    period_type: str
    existing_value_usd: float | None
    restated_value_usd: float
    delta_pct: float
    existing_extraction_id: int | None
    source_document_id: int           # the *restating* source doc
    source_filing_date: str
    source_url: str
    segment_name: str = ""
    table_context: str = ""
    applied: bool = False             # True when write-back succeeded
    new_extraction_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_key": self.cell_key,
            "ticker": self.ticker,
            "metric_key": self.metric_key,
            "fiscal_year": self.fiscal_year,
            "period_type": self.period_type,
            "existing_value_usd": self.existing_value_usd,
            "restated_value_usd": self.restated_value_usd,
            "delta_pct": self.delta_pct,
            "existing_extraction_id": self.existing_extraction_id,
            "source_document_id": self.source_document_id,
            "source_filing_date": self.source_filing_date,
            "source_url": self.source_url,
            "segment_name": self.segment_name,
            "applied": self.applied,
            "new_extraction_id": self.new_extraction_id,
        }


@dataclass
class RestatementSummary:
    """Aggregate report for a run."""
    findings: list[RestatementFinding] = field(default_factory=list)
    tickers_scanned: int = 0
    metrics_scanned: int = 0
    apply: bool = False
    applied_count: int = 0

    @property
    def total(self) -> int:
        return len(self.findings)


def _cell_key(ticker: str, metric: str, fy: int, pt: str) -> str:
    return f"{ticker}:{metric}:{fy}{pt}"


def _candidate_to_finding(c: RestatedCandidate) -> RestatementFinding:
    return RestatementFinding(
        cell_key=_cell_key(c.ticker, c.metric_key, c.fiscal_year, c.period_type),
        ticker=c.ticker,
        metric_key=c.metric_key,
        fiscal_year=c.fiscal_year,
        period_type=c.period_type,
        existing_value_usd=c.existing_value_usd,
        restated_value_usd=c.restated_value_usd,
        delta_pct=c.delta_pct,
        existing_extraction_id=c.existing_extraction_id,
        source_document_id=c.source_document_id,
        source_filing_date=c.source_filing_date,
        source_url=c.source_url,
        segment_name=c.segment_name,
        table_context=c.table_context,
    )


def detect(
    *,
    db: Database | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
    tickers: list[str] | None = None,
    metrics: tuple[str, ...] = METRICS_TO_SCAN,
) -> RestatementSummary:
    """Scan all tracked tickers × metrics for restatements."""
    db = db or Database()
    summary = RestatementSummary()
    if tickers is None:
        tickers = get_all_tickers()
    summary.tickers_scanned = len(tickers)
    summary.metrics_scanned = len(metrics)
    with db.connect() as conn:
        for ticker in tickers:
            for metric in metrics:
                candidates = detect_annual_restatements(
                    conn, ticker, metric, tolerance=tolerance,
                )
                for c in candidates:
                    summary.findings.append(_candidate_to_finding(c))
    return summary


def _ensure_restated_source_doc(
    conn, ticker: str, fiscal_year: int, restating_sd_id: int, now: str,
) -> int:
    """Get/create a *virtual* source_documents row for a restated period.

    The restated extraction needs to appear in the chart/Excel as
    belonging to the restated period (its `fiscal_year`), but must
    still cite the *restating* filing for provenance. We create a new
    source_documents row whose:
      - `fiscal_year` = the restated period's year (so selectors key
        correctly),
      - `period_of_report` = the ISO end-of-FY date for that year,
      - `source_url` / `filing_date` / `accession_number` = copied
        from the restating source_doc (so citations rewire).

    Returns the id of the virtual row (reuses if already present).
    """
    restating = conn.execute(
        "SELECT ticker, form_type, filing_date, period_of_report, "
        "       fiscal_year, source_url, accession_number "
        "FROM source_documents WHERE id = ?",
        (restating_sd_id,),
    ).fetchone()
    if not restating:
        raise ValueError(f"restating source_doc {restating_sd_id} not found")
    # Use the FYE-month from companies to produce a clean period_of_report
    co = conn.execute(
        "SELECT fiscal_year_end_month FROM companies WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    fye_month = (co["fiscal_year_end_month"] or 12) if co else 12
    last_day = {1:31, 2:28, 3:31, 4:30, 5:31, 6:30,
                7:31, 8:31, 9:30, 10:31, 11:30, 12:31}[fye_month]
    period_end = f"{fiscal_year:04d}-{fye_month:02d}-{last_day:02d}"
    # Idempotent: look for an existing virtual row from this same
    # restating accession already materialised for this period. We
    # distinguish virtual rows by the raw_path prefix
    # `restated-virtual://`; the `source` column uses the underlying
    # filing's source ('sec_edgar' / 'hkex') to satisfy the CHECK
    # constraint on that column.
    virt_raw = f"restated-virtual://{ticker}/{fiscal_year}"
    existing = conn.execute(
        "SELECT id FROM source_documents WHERE ticker=? AND period_of_report=? "
        "AND accession_number=? AND form_type='6-K' AND raw_path=?",
        (ticker, period_end, restating["accession_number"] or "", virt_raw),
    ).fetchone()
    if existing:
        return existing["id"]
    # Source column must satisfy CHECK IN ('sec_edgar','hkex','xbrl_api')
    # — use the restating filing's source rather than inventing a new one.
    src_col = conn.execute(
        "SELECT source FROM source_documents WHERE id = ?",
        (restating_sd_id,),
    ).fetchone()
    src_value = (src_col["source"] if src_col else "sec_edgar") or "sec_edgar"
    # Use form_type='6-K' to avoid colliding with the original 10-K's
    # (ticker, form_type, period_of_report) UNIQUE key. 6-K is valid
    # per the CHECK constraint and semantically acceptable: a restated
    # comparative IS a supplementary disclosure of a prior period. The
    # virtual row's `raw_path` starts with `restated-virtual://` to
    # keep these separable from real 6-K press releases.
    cur = conn.execute(
        """
        INSERT INTO source_documents
            (ticker, form_type, filing_date, period_of_report, fiscal_year,
             period_token, sha256, raw_path, source, source_url,
             accession_number, fetched_at, fetcher_version, protocol_version)
        VALUES (?, '6-K', ?, ?, ?, 'AR', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker, restating["filing_date"],
            period_end, fiscal_year,
            f"restated-{ticker}-{fiscal_year}-{restating['accession_number'] or ''}",
            virt_raw,
            src_value,
            restating["source_url"] or "",
            restating["accession_number"] or "",
            now, "restatement-applier@0.1.0", "0.1.0-draft",
        ),
    )
    return cur.lastrowid


def apply(
    summary: RestatementSummary,
    *,
    db: Database | None = None,
) -> RestatementSummary:
    """Write each finding as a new extraction row.

    Creates a virtual `source_documents` row whose `fiscal_year` matches
    the restated period but whose citation fields (source_url,
    accession_number, filing_date) point at the restating filing. That
    way the `filing_date DESC` selector promotes this row over the
    original FY row, chart attributes it to the right year, and Excel
    cell comments cite the restating filing.
    """
    db = db or Database()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary.apply = True
    for f in summary.findings:
        try:
            with db.mutating() as conn:
                virt_sd_id = _ensure_restated_source_doc(
                    conn, f.ticker, f.fiscal_year,
                    f.source_document_id, now,
                )
                # Avoid double-writes: same virt_sd_id + metric + period_type.
                dup = conn.execute(
                    "SELECT id FROM extractions "
                    "WHERE source_document_id=? AND metric_key=? "
                    "AND period_type=? AND extracting_model LIKE 'restated-%'",
                    (virt_sd_id, f.metric_key, f.period_type),
                ).fetchone()
                if dup:
                    f.applied = True
                    f.new_extraction_id = dup["id"]
                    continue

                cur = conn.execute(
                    """
                    INSERT INTO extractions (
                        source_document_id, metric_key, value, value_text,
                        unit, quote, locator_section, extraction_type,
                        extracting_model, protocol_version, extracted_at,
                        value_usd, fx_rate, reporting_currency,
                        period_type, basis_period_months
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        virt_sd_id,
                        f.metric_key,
                        f.restated_value_usd,
                        f"${f.restated_value_usd:,.0f}M (restated)",
                        "USD_millions",
                        (f.table_context or
                         f"Restated in filing {f.source_document_id} "
                         f"— delta vs original: {f.delta_pct * 100:.2f}%")[:500],
                        f"Segment table: {f.segment_name}"[:250],
                        "direct",
                        "restated-segment@0.1.0",
                        "0.1.0-draft",
                        now,
                        f.restated_value_usd,
                        None,
                        "USD",
                        f.period_type,
                        12 if f.period_type == "FY" else 3,
                    ),
                )
                new_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO audit_log
                        (ts, actor, action, target_table, target_id, payload)
                    VALUES (?, 'restatement-applier@0.1.0', ?, 'extractions', ?, ?)
                    """,
                    (
                        now, "extraction_restated", new_id,
                        json.dumps({
                            "cell_key": f.cell_key,
                            "existing_extraction_id": f.existing_extraction_id,
                            "existing_value_usd": f.existing_value_usd,
                            "restated_value_usd": f.restated_value_usd,
                            "delta_pct": f.delta_pct,
                            "restating_source_document_id": f.source_document_id,
                            "virtual_source_document_id": virt_sd_id,
                            "source_filing_date": f.source_filing_date,
                        }, sort_keys=True),
                    ),
                )
                f.applied = True
                f.new_extraction_id = new_id
                summary.applied_count += 1
        except Exception as e:  # noqa: BLE001
            # Best-effort: continue scanning even if one write fails.
            f.applied = False
            f.new_extraction_id = None
            f.table_context = (f.table_context or "") + f"\n[APPLY ERROR: {e!s}]"
    return summary


def render_markdown(summary: RestatementSummary) -> str:
    """Render a short markdown section for the audit report."""
    if not summary.findings:
        return "## Restatements\n\n_No restatements detected in the latest filings._\n"
    if summary.apply:
        mode_hint = (
            f"Applied {summary.applied_count} of {summary.total} findings."
        )
    else:
        mode_hint = "Dry-run — re-run with `capex audit --apply` to commit."
    intro = (
        f"Scanned {summary.tickers_scanned} tickers × "
        f"{summary.metrics_scanned} metrics from the latest 10-K/20-F for "
        f"each. A finding indicates the latest filing's segment table "
        f"reports a materially-different value for an earlier period than "
        f"what's in the DB; the newer filing's value wins by the "
        f"`filing_date DESC` selector rule once written back. {mode_hint}"
    )
    lines: list[str] = [
        f"## Restatements ({summary.total})",
        "",
        intro,
        "",
        "| Cell | Existing | Restated | Δ | Filing |",
        "|---|---|---|---|---|",
    ]
    for f in sorted(summary.findings,
                    key=lambda x: (x.ticker, x.fiscal_year)):
        ex = (f"${f.existing_value_usd:,.0f}M"
              if f.existing_value_usd is not None else "—")
        lines.append(
            f"| {f.cell_key} | {ex} | ${f.restated_value_usd:,.0f}M "
            f"| {f.delta_pct * 100:.1f}% "
            f"| [filed {f.source_filing_date}]({f.source_url or '#'}) "
            f"{'✓' if f.applied else ''} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def render_json(summary: RestatementSummary) -> list[dict[str, Any]]:
    return [f.to_dict() for f in summary.findings]
