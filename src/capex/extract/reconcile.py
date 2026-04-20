"""Period reconciliation engine: derive Q4 and cross-check identities.

For each (ticker, fiscal_year, metric_key) group, apply arithmetic
identities against the stored period values to (a) derive missing
values, and (b) flag discrepancies when redundant values conflict.

Identities (FY = full fiscal year, 9M = nine months YTD, H1 = six
months YTD, Qn = standalone quarter):

    9M + Q4      = FY           -> Q4 = FY - 9M
    Q1 + Q2 + Q3 = 9M
    H1 + Q3      = 9M
    Q1 + Q2      = H1
    Q1+Q2+Q3+Q4  = FY

This module derives missing values by running identities to a fixpoint
and writes the derived rows back to the DB with extraction_type='derived'.

The caller is expected to have applied migration 0007 (period_type
column on extractions).
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..db import Database

# Metrics that can be summed across periods (flow metrics).
FLOW_METRICS = frozenset({
    "capital_expenditures",
    "revenue",
    "operating_cash_flow",
    "depreciation_amortization",
    "cloud_segment_revenue",
})

ACTOR_RECONCILE = "reconcile@0.1.0"

# Default tolerance for identity cross-checks (0.5%).
DEFAULT_TOLERANCE = 0.005


@dataclass
class ReconcileRow:
    ticker: str
    fiscal_year: int
    metric_key: str
    period_type: str
    value: float
    source: str  # 'stored' | 'derived'
    formula: str | None = None
    components: list[int] | None = None  # extraction_ids used for derivation


@dataclass
class ReconcileSummary:
    derived: int
    conflicts: int
    unresolved: int


def _load_existing(
    conn: sqlite3.Connection,
    *,
    ticker: str | None = None,
    metric_key: str | None = None,
    fiscal_year: int | None = None,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT e.id, e.metric_key, e.value_usd, e.value, e.period_type, "
        "e.extraction_type, e.extracting_model, e.extracted_at, "
        "sd.id AS source_document_id, "
        "sd.ticker, sd.fiscal_year, sd.period_of_report, "
        "sd.period_token, sd.form_type, sd.filing_date, "
        "c.fiscal_year_end_month "
        "FROM extractions e "
        "JOIN source_documents sd ON e.source_document_id = sd.id "
        "JOIN companies c ON sd.ticker = c.ticker "
        "WHERE 1=1 "
    )
    args: list[Any] = []
    if ticker:
        sql += " AND sd.ticker = ?"
        args.append(ticker)
    if metric_key:
        sql += " AND e.metric_key = ?"
        args.append(metric_key)
    if fiscal_year is not None:
        sql += " AND sd.fiscal_year = ?"
        args.append(fiscal_year)
    # Order so the *newest* filing_date sorts LAST within each group.
    # _group_rows then uses "last write wins" semantics where a later
    # filing's restated comparative supersedes the original row.
    sql += " ORDER BY sd.ticker, sd.fiscal_year, sd.period_of_report, sd.filing_date"
    return [dict(r) for r in conn.execute(sql, args)]


def _infer_period_type(row: dict[str, Any]) -> str:
    """Best-effort mapping from source_document metadata to period_type.

    Applies only when the extraction row has an empty period_type. Rows
    that already carry period_type are trusted.
    """
    stored = (row.get("period_type") or "").strip()
    if stored:
        return stored

    token = (row.get("period_token") or "").strip()
    form = (row.get("form_type") or "").strip()

    if token == "AR":
        # Only honor AR when the period_of_report lines up with the
        # company's fiscal year-end month. Synthetic XBRL rows sometimes
        # tag mid-year 10-Ks as AR, which would pollute FY values.
        fye = row.get("fiscal_year_end_month")
        period = row.get("period_of_report") or ""
        if fye and len(period) >= 7 and int(period[5:7]) == int(fye):
            return "FY"
        return ""
    if form == "10-Q":
        # 10-Q values in this DB follow the existing pipeline's
        # convention: Q1 == 3M (same whether labelled YTD or 3M column),
        # Q2 == H1 YTD, Q3 == 9M YTD. This matches the de-cumulation
        # logic in exporters/interactive_chart.py.
        return {"Q1": "Q1", "Q2": "H1", "Q3": "9M"}.get(token, "")
    if form == "6-K":
        # 6-K quarterly press releases are standalone by convention.
        if token in ("Q1", "Q2", "Q3", "Q4"):
            return token
    if form in ("20-F", "HK-AR"):
        return "FY" if token == "AR" else ""
    if form == "HK-IR":
        if token == "H1":
            return "H1"
        if token == "H2":
            return "H2"
    return ""


def _group_rows(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, int, str], dict[str, dict]]:
    """Group rows by (ticker, fiscal_year, metric_key) then by period_type.

    When the same period_type has multiple stored rows (e.g. an
    original extraction plus a restated comparative from a later
    filing), the row with the newest `source_documents.filing_date`
    wins. Rows arrive pre-sorted by filing_date ASC (see
    `_load_existing`); we overwrite on each iteration so the last-
    seen row (newest filing) is retained.
    """
    grouped: dict[tuple[str, int, str], dict[str, dict]] = defaultdict(dict)
    for r in rows:
        metric = r["metric_key"]
        if metric not in FLOW_METRICS:
            continue
        ptype = _infer_period_type(r)
        if not ptype:
            continue
        key = (r["ticker"], r["fiscal_year"], metric)
        value = r.get("value_usd") if r.get("value_usd") is not None else r.get("value")
        if value is None:
            continue
        grouped[key][ptype] = {
            "value": abs(float(value)),
            "extraction_id": r["id"],
            "source_document_id": r.get("source_document_id"),
            "period_of_report": r["period_of_report"],
            "form_type": r["form_type"],
            "filing_date": r.get("filing_date"),
            "source": "stored",
        }
    return grouped


def _derive_once(
    periods: dict[str, dict],
    tolerance: float,
) -> tuple[int, int]:
    """Apply identities once. Returns (derived_count, conflict_count)."""
    derived_count = 0
    conflict_count = 0

    def get(pt: str) -> float | None:
        return periods[pt]["value"] if pt in periods else None

    def setd(pt: str, value: float, formula: str, components: list[int]) -> None:
        nonlocal derived_count
        # Guard against obviously wrong derivations (negative or near-zero
        # for a metric that should be positive). This usually means the
        # FY value and the quarterly values are measuring different
        # segments / scopes, so the identity doesn't hold.
        if value <= 0:
            return
        periods[pt] = {
            "value": value,
            "extraction_id": None,
            "period_of_report": None,
            "form_type": None,
            "source": "derived",
            "formula": formula,
            "components": components,
        }
        derived_count += 1

    def check(stored: float, computed: float, label: str) -> bool:
        denom = max(abs(stored), abs(computed), 1.0)
        return abs(stored - computed) / denom <= tolerance

    # Q2 = H1 - Q1 (standalone Q2 from YTD H1)
    if "Q2" not in periods and get("H1") is not None and get("Q1") is not None:
        setd(
            "Q2",
            get("H1") - get("Q1"),
            "Q2 = H1 - Q1",
            [periods["H1"]["extraction_id"], periods["Q1"]["extraction_id"]],
        )

    # Q3 = 9M - H1 (standalone Q3 from YTD 9M)
    if "Q3" not in periods and get("9M") is not None and get("H1") is not None:
        setd(
            "Q3",
            get("9M") - get("H1"),
            "Q3 = 9M - H1",
            [periods["9M"]["extraction_id"], periods["H1"]["extraction_id"]],
        )

    # Q4 = FY - 9M
    if "Q4" not in periods and get("FY") is not None and get("9M") is not None:
        setd(
            "Q4",
            get("FY") - get("9M"),
            "Q4 = FY - 9M",
            [periods["FY"]["extraction_id"], periods["9M"]["extraction_id"]],
        )

    # 9M = Q1+Q2+Q3
    if "9M" not in periods and all(get(p) is not None for p in ("Q1", "Q2", "Q3")):
        setd(
            "9M",
            sum(get(p) for p in ("Q1", "Q2", "Q3")),
            "9M = Q1+Q2+Q3",
            [periods[p]["extraction_id"] for p in ("Q1", "Q2", "Q3")],
        )

    # H1 = Q1+Q2
    if "H1" not in periods and get("Q1") is not None and get("Q2") is not None:
        setd(
            "H1",
            get("Q1") + get("Q2"),
            "H1 = Q1+Q2",
            [periods["Q1"]["extraction_id"], periods["Q2"]["extraction_id"]],
        )

    # 9M = H1 + Q3
    if "9M" not in periods and get("H1") is not None and get("Q3") is not None:
        setd(
            "9M",
            get("H1") + get("Q3"),
            "9M = H1+Q3",
            [periods["H1"]["extraction_id"], periods["Q3"]["extraction_id"]],
        )

    # Q4 = FY - (Q1+Q2+Q3)
    if "Q4" not in periods and all(get(p) is not None for p in ("FY", "Q1", "Q2", "Q3")):
        setd(
            "Q4",
            get("FY") - get("Q1") - get("Q2") - get("Q3"),
            "Q4 = FY - (Q1+Q2+Q3)",
            [periods[p]["extraction_id"] for p in ("FY", "Q1", "Q2", "Q3")],
        )

    # Symmetric: derive any single missing quarter from FY + other three.
    # Particularly useful for non-Dec-FYE filers whose 6-K press releases
    # cover only three fiscal quarters (e.g. BABA files 6-K for fiscal
    # Q1/Q2/Q3 but the fiscal Q4 comes from the 20-F).
    for target in ("Q1", "Q2", "Q3"):
        others = tuple(q for q in ("Q1", "Q2", "Q3", "Q4") if q != target)
        if (
            target not in periods
            and get("FY") is not None
            and all(get(p) is not None for p in others)
        ):
            setd(
                target,
                get("FY") - sum(get(p) for p in others),
                f"{target} = FY - ({'+'.join(others)})",
                [periods[p]["extraction_id"] for p in ("FY",) + others],
            )

    # Consistency: when both FY and Q1..Q4 exist, compare.
    if all(get(p) is not None for p in ("FY", "Q1", "Q2", "Q3", "Q4")):
        stored_fy = get("FY")
        computed = sum(get(p) for p in ("Q1", "Q2", "Q3", "Q4"))
        if not check(stored_fy, computed, "FY vs sum(Q)"):
            conflict_count += 1

    return derived_count, conflict_count


def _reconcile_fixpoint(
    periods: dict[str, dict],
    tolerance: float,
) -> tuple[int, int]:
    total_derived = 0
    total_conflicts = 0
    for _ in range(8):  # identity chain depth is short; 8 iters is plenty
        d, c = _derive_once(periods, tolerance)
        total_derived += d
        total_conflicts += c
        if d == 0:
            break
    return total_derived, total_conflicts


def reconcile(
    *,
    db: Database | None = None,
    ticker: str | None = None,
    metric_key: str | None = None,
    fiscal_year: int | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
    write: bool = True,
) -> ReconcileSummary:
    """Run identity-based reconciliation across the extractions table.

    For every (ticker, fiscal_year, metric_key) group in scope, derive
    missing period_type values and optionally write them back as new
    extraction rows with extraction_type='derived'. Flag any
    conflicts where redundant values disagree beyond the tolerance.
    """
    db = db or Database()
    total_derived = 0
    total_conflicts = 0
    unresolved = 0

    with db.connect() as conn:
        rows = _load_existing(
            conn,
            ticker=ticker,
            metric_key=metric_key,
            fiscal_year=fiscal_year,
        )
    grouped = _group_rows(rows)

    # Run derivation per group, collect what needs writing.
    to_write: list[tuple[tuple[str, int, str], str, dict]] = []
    for key, periods in grouped.items():
        d, c = _reconcile_fixpoint(periods, tolerance)
        total_derived += d
        total_conflicts += c
        for ptype, p in periods.items():
            if p["source"] == "derived" and p.get("extraction_id") is None:
                to_write.append((key, ptype, p))
        # Unresolved Q4s
        if "Q4" not in periods:
            unresolved += 1

    if not write:
        return ReconcileSummary(
            derived=total_derived, conflicts=total_conflicts, unresolved=unresolved,
        )

    _write_derived_rows(db, to_write)
    return ReconcileSummary(
        derived=total_derived, conflicts=total_conflicts, unresolved=unresolved,
    )


def _write_derived_rows(
    db: Database,
    to_write: list[tuple[tuple[str, int, str], str, dict]],
) -> None:
    """Insert derived period values as extraction rows."""
    if not to_write:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with db.mutating() as conn:
        for (ticker, fy, metric), ptype, payload in to_write:
            components = payload.get("components") or []
            source_doc_id = _anchor_source_doc(conn, ticker, fy, components)
            if source_doc_id is None:
                continue
            existing = conn.execute(
                "SELECT id FROM extractions "
                "WHERE source_document_id = ? AND metric_key = ? "
                "AND extracting_model = ? AND period_type = ?",
                (source_doc_id, metric, "reconcile-derived", ptype),
            ).fetchone()
            if existing:
                continue
            value = payload["value"]
            formula = payload.get("formula") or ""
            cur = conn.execute(
                """
                INSERT INTO extractions (
                    source_document_id, metric_key, value, value_text, unit,
                    quote, locator_page, locator_section, extraction_type,
                    confidence, extracting_model, protocol_version,
                    extracted_at, value_usd, fx_rate, fx_rate_date,
                    reporting_currency, period_type, basis_period_months,
                    reporting_convention
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    source_doc_id,
                    metric,
                    value,
                    f"${value:,.0f}M (derived)",
                    "USD_millions",
                    f"Derived: {formula}",
                    None,
                    f"Derived from {formula}",
                    "derived",
                    None,
                    "reconcile-derived",
                    "0.1.0-draft",
                    now,
                    value,
                    None,
                    None,
                    "USD",
                    ptype,
                    _basis_months_for(ptype),
                    None,
                ),
            )
            row_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO audit_log (
                    ts, actor, action, target_table, target_id, payload
                ) VALUES (?, ?, 'extraction_derived', 'extractions', ?, ?)
                """,
                (
                    now,
                    ACTOR_RECONCILE,
                    row_id,
                    json.dumps({
                        "ticker": ticker,
                        "fiscal_year": fy,
                        "metric_key": metric,
                        "period_type": ptype,
                        "formula": formula,
                        "components": components,
                        "value": value,
                    }, sort_keys=True),
                ),
            )


def _basis_months_for(period_type: str) -> int | None:
    return {
        "Q1": 3, "Q2": 3, "Q3": 3, "Q4": 3, "3M_reported": 3,
        "H1": 6, "H2": 6,
        "9M": 9,
        "FY": 12,
    }.get(period_type)


def _anchor_source_doc(
    conn: sqlite3.Connection,
    ticker: str,
    fiscal_year: int,
    components: list[int],
) -> int | None:
    """Pick a source_document to anchor a derived row to.

    Prefer the same source_document as the annual 10-K / 20-F for the
    fiscal year, since derivations typically depend on it. Fall back to
    the first component extraction's source_document.
    """
    row = conn.execute(
        "SELECT id FROM source_documents "
        "WHERE ticker = ? AND fiscal_year = ? AND period_token = 'AR' "
        "ORDER BY period_of_report DESC LIMIT 1",
        (ticker, fiscal_year),
    ).fetchone()
    if row:
        return row[0]
    for eid in components:
        r = conn.execute(
            "SELECT source_document_id FROM extractions WHERE id = ?",
            (eid,),
        ).fetchone()
        if r:
            return r[0]
    return None
