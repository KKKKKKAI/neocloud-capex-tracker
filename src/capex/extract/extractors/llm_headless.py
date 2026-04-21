"""Headless LLM extractor — dual-agent verification via CLI backend.

Python orchestrates everything. The CLI tool (claude -p / gemini -p)
is called ONLY for text generation — no tool use, no permissions.

Flow:
    1. Python loads filing sections from disk
    2. Python formats Agent A prompt → subprocess call → parse JSON
    3. Python formats Agent B prompt (excerpts only) → subprocess → parse
    4. Python compares A vs B
    5. Python writes to DB if verified, queues for review if mismatch
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...audit import human_notes as hn_mod
from ...db import Database
from ...fx.rates import normalize_to_usd
from ...read.sections import get_extraction_sections, parse_sections
from ...read.text import extract_text
from ...verification.dual_agent import (
    MAX_RETRIES,
    build_agent_a_prompt,
    build_agent_b_prompt,
    get_derivation_rules,
    get_metric_description,
    parse_agent_a_response,
    parse_agent_b_response,
    verify_periods_batch,
)
from ..base import ExtractionCandidate
from ..coverage import DatasetTreatment, get_company_treatment, get_dataset_treatment
from ..virtual_source_docs import ensure_restated_source_doc


def _fiscal_year_from(period_of_report: str, fye_month: int) -> int | None:
    """Convert a period-end ISO date + fiscal-year-end month → fiscal year.

    A fiscal year FY{N} spans the 12 months ending on `fye_month` of
    calendar year N. So a period ending on a month > fye_month belongs
    to FY{calendar_year+1}; any month ≤ fye_month belongs to FY{calendar_year}.

    MSFT (FYE=6): 2024-06-30 → FY2024; 2024-12-31 → FY2025.
    BABA (FYE=3): 2024-03-31 → FY2024; 2024-06-30 → FY2025.
    AMZN (FYE=12): every date → calendar year (the common case).
    """
    try:
        cy = int(period_of_report[:4])
        pm = int(period_of_report[5:7])
    except (ValueError, IndexError):
        return None
    if not 1 <= pm <= 12 or not 1 <= fye_month <= 12:
        return None
    return cy if pm <= fye_month else cy + 1


def _period_type_from(
    basis_months: int,
    period_of_report: str,
    form_type: str,
    fye_month: int | None = None,
) -> str:
    """Map Agent A's `(basis_period_months, period_of_report, form_type,
    fye_month)` to the canonical period_type used by selectors.

    Rules:
      12 → FY
       9 → 9M
       6 → H1
       3 → Q1/Q2/Q3/Q4 based on how many 3-month steps the period
           end is past the company's fiscal year-end month. Falls
           back to blank when `fye_month` is not supplied (reconcile
           will fill from `source_documents.period_token` in that
           case).
    """
    if basis_months == 12:
        return "FY"
    if basis_months == 9:
        return "9M"
    if basis_months == 6:
        return "H1"
    if basis_months == 3 and period_of_report and fye_month:
        try:
            pm = int(period_of_report[5:7])
        except (ValueError, IndexError):
            return ""
        # months-past-FYE-start, 0-based: Q1 = 0..2 months past FY start
        # FY starts in (fye_month % 12) + 1. So distance of the period's
        # END from that start - 1:
        # Position within fiscal year where this quarter ends.
        offset = (pm - fye_month - 1) % 12
        q_num = (offset // 3) + 1
        return f"Q{q_num}"
    return ""


class LLMHeadlessExtractor:
    """Extraction backend using CLI tool for dual-agent verification.

    Unlike llm_interactive.py (which returns needs_interactive=True),
    this extractor makes actual LLM calls via the CLI backend and
    runs the full Agent A → Agent B → compare pipeline.
    """

    name = "llm"

    def can_handle(
        self,
        ticker: str,
        metric_key: str,
        form_type: str | None,
        treatment: DatasetTreatment | None,
    ) -> bool:
        return True  # universal fallback

    def extract(
        self,
        ticker: str,
        metric_key: str,
        period: str | None = None,
        form_type: str | None = None,
        **kwargs: Any,
    ) -> list[ExtractionCandidate] | None:
        """Run full dual-agent extraction via CLI backend.

        Returns verified candidates or None (mismatch → queued for review).
        """
        backend = kwargs.get("backend")
        if backend is None:
            return None  # no CLI tool available

        db = kwargs.get("db") or Database()

        # Find the filing
        with db.connect() as conn:
            query = (
                "SELECT id, raw_path, form_type, period_of_report, ticker "
                "FROM source_documents WHERE ticker = ? "
            )
            params: list[Any] = [ticker]
            if period:
                query += "AND period_of_report = ? "
                params.append(period)
            if form_type:
                query += "AND form_type = ? "
                params.append(form_type)
            query += "ORDER BY period_of_report DESC LIMIT 1"
            row = conn.execute(query, params).fetchone()

        if not row:
            return None

        filepath = Path(row["raw_path"])
        if not filepath.exists():
            # Try relative to repo root
            from ...db.schema import REPO_ROOT
            filepath = REPO_ROOT / row["raw_path"]
            if not filepath.exists():
                return None

        # Load filing sections
        text = extract_text(filepath)
        sections = parse_sections(text, row["form_type"])
        ext_sections = get_extraction_sections(sections, row["form_type"])
        if not ext_sections:
            return None

        sections_text = "\n\n".join(
            f"## {name}\n{content}"
            for name, content in ext_sections.items()
        )
        # Truncate to ~100K chars to stay within CLI tool context limits
        if len(sections_text) > 100_000:
            sections_text = sections_text[:100_000]

        # Get company info
        company = get_company_treatment(ticker)
        treatment = get_dataset_treatment(ticker, metric_key)
        company_name = company.full_name if company else ticker
        currency = company.reporting_currency if company else "USD"
        # Fiscal-year-end month for quarterly period_type derivation
        with db.connect() as conn:
            fye_row = conn.execute(
                "SELECT fiscal_year_end_month FROM companies WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        fye_month = fye_row["fiscal_year_end_month"] if fye_row else 12

        metric_desc = get_metric_description(metric_key, ticker, treatment)
        deriv_rules = get_derivation_rules(treatment)
        unit = f"{currency}_millions"

        # Resolve any human-authored guidance that applies to this cell
        # (from data/seeds/human_notes.yaml, elicited via
        # `capex audit review`). Empty block when no notes apply.
        fy_int: int | None = None
        try:
            fy_int = int(str(row["period_of_report"])[:4])
        except (ValueError, TypeError):
            fy_int = None
        hnotes = hn_mod.resolve(
            ticker=ticker,
            metric_key=metric_key,
            fiscal_year=fy_int,
            form_type=row["form_type"],
        )
        human_notes_block = hn_mod.format_for_prompt(hnotes)

        # Retry loop (max 3 attempts if B says insufficient context)
        for attempt in range(1, MAX_RETRIES + 1):
            # Agent A: extract value + context
            prompt_a = build_agent_a_prompt(
                company_name=company_name,
                form_type=row["form_type"],
                period=row["period_of_report"],
                metric_description=metric_desc,
                sections_text=sections_text,
                unit=unit,
                derivation_rules=deriv_rules,
                human_notes_block=human_notes_block,
            )

            response_a = backend.extract(system="", user=prompt_a)
            result_a = parse_agent_a_response(response_a)

            if not result_a.get("found"):
                return None  # metric not found in filing

            periods = result_a.get("periods") or []
            if not periods:
                return None

            # ONE batched Agent B call covers every period Agent A
            # returned. Per-period verdicts come back as an array.
            prompt_b = build_agent_b_prompt(
                company_name=company_name,
                form_type=row["form_type"],
                periods=periods,
                metric_description=metric_desc,
                unit=unit,
                derivation_rules=deriv_rules,
            )
            response_b = backend.extract(system="", user=prompt_b)
            result_b = parse_agent_b_response(response_b)
            verifications = verify_periods_batch(periods, result_b)

            candidates: list[ExtractionCandidate] = []
            any_primary_ok = False
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for period, verification in zip(periods, verifications, strict=False):
                verification.attempts = attempt
                if not verification.verified:
                    continue  # skip unverified periods

                role = (period.get("role") or "primary").lower()
                value = verification.value_a
                value_usd, fx_rate, fx_date = normalize_to_usd(
                    value, currency,
                    period.get("period_of_report") or row["period_of_report"],
                    db=db,
                )
                if role == "primary":
                    source_doc_id = row["id"]
                    extracting_model = "llm-dual-agent"
                    any_primary_ok = True
                else:
                    # User-directed guardrail: a 0/None restated value is
                    # almost always an LLM mis-read of an empty cell. Skip
                    # silently — the original as-reported row stays
                    # authoritative via the filing_date DESC selector.
                    if value in (None, 0):
                        continue
                    comp_period = period.get("period_of_report") or ""
                    comp_fy = _fiscal_year_from(comp_period, fye_month)
                    if comp_fy is None:
                        continue
                    with db.mutating() as conn:
                        source_doc_id = ensure_restated_source_doc(
                            conn, ticker, comp_fy, row["id"], now,
                            period_of_report=comp_period,
                        )
                    extracting_model = "llm-dual-agent-restated@0.1.0"

                # Derive period_type from basis_period_months + the period's
                # end-date month (for 3-month rows) so the chart selector
                # picks the row up correctly.
                basis = period.get("basis_period_months") or 0
                period_type = _period_type_from(
                    basis,
                    period.get("period_of_report") or "",
                    row["form_type"],
                    fye_month=fye_month,
                )

                candidates.append(ExtractionCandidate(
                    source_document_id=source_doc_id,
                    metric_key=metric_key,
                    value=value,
                    value_text=(
                        f"{currency} {value:,.0f} million "
                        f"({'restated' if role == 'comparative' else 'primary'})"
                    ) if value else "",
                    unit="USD_millions",
                    quote=verification.best_quote[:250],
                    locator_section=(
                        (period.get("excerpts") or [{}])[0].get("location", "")
                    ),
                    extraction_type="direct",
                    extracting_model=extracting_model,
                    reporting_currency=currency,
                    excerpts=period.get("excerpts", []),
                    reasoning=period.get("reasoning", ""),
                    period_type=period_type,
                    basis_period_months=basis or None,
                ))

            if not any_primary_ok:
                # Primary verification failed — queue for human review.
                # (Comparatives without a verified primary are still
                # returned if they verified, but we also need to surface
                # that the primary step missed so the router can flag.)
                if not candidates:
                    # Nothing verified at all — retry with broader ctx
                    if attempt < MAX_RETRIES:
                        deriv_rules += (
                            f"\n\nRETRY {attempt}: primary period did not "
                            f"verify. Ensure you've quoted column headers "
                            f"AND row values and that the primary period's "
                            f"column header matches '{row['period_of_report']}'."
                        )
                        continue
                    return None
                # Comparatives verified but primary didn't; skip this
                # filing's primary write but keep verified comparatives.
            return candidates or None

        return None
