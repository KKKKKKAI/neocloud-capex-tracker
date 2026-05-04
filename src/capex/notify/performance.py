"""QoQ + YoY comparisons for one (ticker, metric, period) cell.

Used by formatter.py to fill the email's performance table. Pulls
from the same `extractions` rows the chart selectors consume —
filtered by `period_type IN ('Q1','Q2','Q3','Q4','FY')` and ordered
by `(fiscal_year, period_token, filing_date DESC)` so restated rows
supersede originals.

A "cell" here is the (ticker, metric_key, period_of_report) triple
that just got extracted. We need:
  - the row that just landed (current value + period label)
  - the row from the immediately-prior quarter (QoQ)
  - the row from the same quarter one fiscal year back (YoY)
"""
from __future__ import annotations

from dataclasses import dataclass

from ..db import Database

# Order quarters along a calendar timeline so QoQ navigation works
# regardless of the company's fiscal year start.
_Q_ORDER = ("Q1", "Q2", "Q3", "Q4")


@dataclass
class CellSnapshot:
    """One value + its human-readable period label."""

    value: float | None
    period_type: str       # FY, Q1..Q4
    fiscal_year: int
    period_label: str      # e.g. "Q1 FY2026"


@dataclass
class PerformanceTriple:
    """current + QoQ + YoY for one (ticker, metric_key, period) cell."""

    metric_key: str
    current: CellSnapshot
    prior_qtr: CellSnapshot | None
    prior_year: CellSnapshot | None

    @property
    def qoq_pct(self) -> float | None:
        return _delta_pct(self.current.value, self.prior_qtr.value if self.prior_qtr else None)

    @property
    def yoy_pct(self) -> float | None:
        return _delta_pct(self.current.value, self.prior_year.value if self.prior_year else None)


def _delta_pct(curr: float | None, prior: float | None) -> float | None:
    if curr is None or prior is None or prior == 0:
        return None
    return (curr - prior) / abs(prior) * 100.0


def _label(period_type: str, fiscal_year: int) -> str:
    if period_type == "FY":
        return f"FY{fiscal_year}"
    return f"{period_type} FY{fiscal_year}"


def _prior_quarter(period_type: str, fiscal_year: int) -> tuple[str, int] | None:
    """Step one calendar quarter backward from (period_type, fiscal_year)."""
    if period_type == "FY":
        # No "prior quarter" definition for an FY row — skip.
        return None
    if period_type not in _Q_ORDER:
        return None
    idx = _Q_ORDER.index(period_type)
    if idx == 0:
        return ("Q4", fiscal_year - 1)
    return (_Q_ORDER[idx - 1], fiscal_year)


def _fetch_value(
    db: Database,
    ticker: str,
    metric_key: str,
    period_type: str,
    fiscal_year: int,
) -> CellSnapshot | None:
    """Latest-filing wins (restatement-aware) row for this cell, if any."""
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT e.value, e.period_type, sd.fiscal_year
            FROM extractions e
            JOIN source_documents sd ON sd.id = e.source_document_id
            WHERE sd.ticker = ?
              AND e.metric_key = ?
              AND e.period_type = ?
              AND sd.fiscal_year = ?
              AND e.value IS NOT NULL
            ORDER BY sd.filing_date DESC, e.id DESC
            LIMIT 1
            """,
            (ticker, metric_key, period_type, fiscal_year),
        ).fetchone()
    if row is None:
        return None
    return CellSnapshot(
        value=row["value"],
        period_type=row["period_type"],
        fiscal_year=row["fiscal_year"],
        period_label=_label(row["period_type"], row["fiscal_year"]),
    )


def get_performance(
    ticker: str,
    metric_key: str,
    period_of_report: str,
    *,
    db: Database | None = None,
) -> PerformanceTriple | None:
    """Return current + QoQ + YoY for the row at (ticker, metric_key, period_of_report).

    `period_of_report` is the ISO period-end date of the filing the
    notification is about. We resolve that to a (period_type, fiscal_year)
    via the `extractions` row, then walk backward one quarter (QoQ) and
    one year (YoY).
    """
    db = db or Database()
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT e.value, e.period_type, sd.fiscal_year
            FROM extractions e
            JOIN source_documents sd ON sd.id = e.source_document_id
            WHERE sd.ticker = ?
              AND e.metric_key = ?
              AND sd.period_of_report = ?
              AND e.period_type IN ('FY','Q1','Q2','Q3','Q4')
              AND e.value IS NOT NULL
            ORDER BY sd.filing_date DESC, e.id DESC
            LIMIT 1
            """,
            (ticker, metric_key, period_of_report),
        ).fetchone()
    if row is None:
        return None

    current = CellSnapshot(
        value=row["value"],
        period_type=row["period_type"],
        fiscal_year=row["fiscal_year"],
        period_label=_label(row["period_type"], row["fiscal_year"]),
    )

    prior_q_key = _prior_quarter(current.period_type, current.fiscal_year)
    prior_qtr = (
        _fetch_value(db, ticker, metric_key, prior_q_key[0], prior_q_key[1])
        if prior_q_key else None
    )
    prior_year = _fetch_value(
        db, ticker, metric_key,
        current.period_type, current.fiscal_year - 1,
    )

    return PerformanceTriple(
        metric_key=metric_key,
        current=current,
        prior_qtr=prior_qtr,
        prior_year=prior_year,
    )
