"""Shared data layer for the per-company treatments viewer.

`query_treatments()` returns one `CompanyTreatmentView` per tracked
company, combining:

  - coverage.yaml structured rules (filing_cadence, quarterly_convention,
    extraction_approach, per-dataset treatments + adjustments + notes,
    exclusions, free-form company notes)
  - human_notes.yaml PEL-elicited notes (scoped, with provenance)
  - audit_review_feedback DB rows (verbatim reviewer input joined on
    formalized_note_id)

The HTML exporter (`exporters/treatments_html.py`) and the CLI
(`capex treatments show`) both consume this function so the surfaces
never drift apart.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..db import Database
from ..extract.coverage import (
    get_all_tickers,
    get_company_treatment,
    iter_dataset_rules,
)
from . import human_notes as hn_mod


@dataclass
class DatasetRule:
    dataset: str                       # "cloud_segment_revenue", …
    metric_keys: list[str]
    treatment: str                     # "named_segment", "xbrl_default", …
    segment_names: list[str] = field(default_factory=list)
    segment_start: str | None = None
    adjustment: dict | None = None     # {method, formula, rationale, caveats}
    extraction_method: str | None = None
    notes: str = ""
    excluded: bool = False
    exclusion_reason: str | None = None


@dataclass
class HumanNoteView:
    id: str
    scope: dict
    guidance: str
    keywords_to_match: list[str]
    cautions: list[str]
    state: str
    added_at: str
    added_by: str
    source_audit_run_id: str
    source_cell_keys: list[str]
    rationale: str
    # Joined from audit_review_feedback (may be None if not elicited via PEL)
    reviewer: str | None = None
    reviewer_input: str | None = None
    reviewed_at: str | None = None


@dataclass
class CompanyTreatmentView:
    ticker: str
    full_name: str
    category: str
    reporting_currency: str
    fiscal_year_end_month: int
    coverage_start: str
    filing_cadence: dict
    extraction_approach: str
    quarterly_convention: dict
    company_notes: str
    restatement_policy: dict = field(default_factory=dict)
    dataset_rules: list[DatasetRule] = field(default_factory=list)
    human_notes: list[HumanNoteView] = field(default_factory=list)


def _company_fiscal_year_end(
    conn: sqlite3.Connection, ticker: str,
) -> int:
    row = conn.execute(
        "SELECT fiscal_year_end_month FROM companies WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    return row[0] if row else 12


def _coverage_raw_company(ticker: str) -> dict:
    """Pull the raw companies.<ticker> subtree from coverage.yaml."""
    from ..extract.coverage import _load_raw
    return (_load_raw().get("companies") or {}).get(ticker) or {}


def _load_feedback_index(conn: sqlite3.Connection) -> dict[str, dict]:
    """Map human_note_id → {reviewer, reviewer_input, reviewed_at}."""
    out: dict[str, dict] = {}
    rows = conn.execute(
        "SELECT formalized_note_id, human_input, reviewer, reviewed_at "
        "FROM audit_review_feedback "
        "WHERE formalized_note_id IS NOT NULL"
    ).fetchall()
    for r in rows:
        nid = r[0]
        if nid and nid not in out:
            out[nid] = {
                "reviewer": r[2],
                "reviewer_input": r[1],
                "reviewed_at": r[3],
            }
    return out


def _human_note_view(n: hn_mod.HumanNote, fb_index: dict) -> HumanNoteView:
    fb = fb_index.get(n.id) or {}
    return HumanNoteView(
        id=n.id,
        scope=n.scope.to_dict(),
        guidance=n.guidance,
        keywords_to_match=list(n.keywords_to_match),
        cautions=list(n.cautions),
        state=n.state,
        added_at=n.added_at,
        added_by=n.added_by,
        source_audit_run_id=n.source_audit_run_id,
        source_cell_keys=list(n.source_cell_keys),
        rationale=n.rationale,
        reviewer=fb.get("reviewer"),
        reviewer_input=fb.get("reviewer_input"),
        reviewed_at=fb.get("reviewed_at"),
    )


def query_treatments(
    *,
    ticker_filter: str | None = None,
    metric_filter: str | None = None,
    db: Database | None = None,
    db_path: str | Path | None = None,
) -> list[CompanyTreatmentView]:
    """Return one CompanyTreatmentView per company, ordered by ticker."""
    db = db or (Database(Path(db_path)) if db_path else Database())
    all_notes = hn_mod.load_all()
    out: list[CompanyTreatmentView] = []
    with db.connect() as conn:
        fb_index = _load_feedback_index(conn)
        tickers = get_all_tickers()
        if ticker_filter:
            tickers = [t for t in tickers if t == ticker_filter]
        for t in sorted(tickers):
            co = get_company_treatment(t)
            if co is None:
                continue
            raw_co = _coverage_raw_company(t)
            fye = _company_fiscal_year_end(conn, t)

            rules = iter_dataset_rules(t)
            if metric_filter:
                rules = [
                    r for r in rules
                    if metric_filter in (r.get("metric_keys") or [])
                ]
            dataset_rules = [
                DatasetRule(
                    dataset=r["dataset"],
                    metric_keys=r.get("metric_keys") or [],
                    treatment=r.get("treatment", ""),
                    segment_names=list(r.get("segment_names") or []),
                    segment_start=r.get("segment_start"),
                    adjustment=r.get("adjustment"),
                    extraction_method=r.get("extraction_method"),
                    notes=r.get("notes", ""),
                    excluded=r.get("excluded", False),
                    exclusion_reason=r.get("exclusion_reason"),
                )
                for r in rules
            ]

            # Human notes scoped to this ticker (or global / null ticker).
            notes_for_t = [
                n for n in all_notes
                if n.scope.ticker == t or n.scope.ticker is None
            ]
            if metric_filter:
                notes_for_t = [
                    n for n in notes_for_t
                    if not n.scope.metric_keys
                    or metric_filter in n.scope.metric_keys
                ]
            human_notes_list = [
                _human_note_view(n, fb_index) for n in notes_for_t
            ]

            out.append(CompanyTreatmentView(
                ticker=t,
                full_name=co.full_name,
                category=co.category,
                reporting_currency=co.reporting_currency,
                fiscal_year_end_month=fye,
                coverage_start=co.coverage_start,
                filing_cadence=dict(co.filing_cadence or {}),
                extraction_approach=co.extraction_approach,
                quarterly_convention=raw_co.get("quarterly_convention") or {},
                company_notes=co.notes or "",
                restatement_policy=raw_co.get("restatement_policy") or {},
                dataset_rules=dataset_rules,
                human_notes=human_notes_list,
            ))
    return out
