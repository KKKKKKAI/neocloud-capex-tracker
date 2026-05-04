"""Filing-level headless extractor — one Agent A call per filing.

Extracts every requested metric in a single Agent A read of the filing
context, then runs one Agent B verification per metric (each batched
across all periods Agent A returned for that metric).

Cost envelope vs. the per-metric `LLMHeadlessExtractor`:
    per-metric path: 6 × Agent A (~100K chars each) + 6 × Agent B
    this path:       1 × Agent A (~106K chars)        + 6 × Agent B

For metrics where Agent A returned nothing usable, this extractor
returns None for that metric_key — the router falls back to the
per-metric path which retains the 3-attempt context-broadening loop.

Per-metric helpers (period_type derivation, FX normalization, restated
source-doc creation) are shared with `llm_headless.py`.
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
    build_agent_a_multi_metric_prompt,
    build_agent_b_prompt,
    get_derivation_rules,
    get_metric_description,
    parse_agent_a_multi_metric_response,
    parse_agent_b_response,
    verify_periods_batch,
)
from ..base import ExtractionCandidate
from ..coverage import get_company_treatment, get_dataset_treatment
from ..virtual_source_docs import ensure_restated_source_doc
from .llm_headless import _fiscal_year_from, _period_type_from


class LLMHeadlessFilingExtractor:
    """Multi-metric, single-filing-read dual-agent extractor."""

    name = "llm-filing"

    def extract_filing(
        self,
        ticker: str,
        form_type: str,
        period: str,
        metric_keys: list[str],
        *,
        backend: Any,
        db: Database | None = None,
    ) -> dict[str, list[ExtractionCandidate] | None]:
        """Run one Agent A + N Agent B passes for `metric_keys` against
        the latest matching source_documents row.

        Returns a dict keyed by metric_key. The value is the verified
        candidate list (possibly empty) on success, or `None` when
        the metric needs the per-metric fallback path. Every key in
        `metric_keys` is present in the returned dict.
        """
        db = db or Database()
        if not metric_keys:
            return {}

        # Resolve the source doc once
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id, raw_path, form_type, period_of_report, ticker "
                "FROM source_documents "
                "WHERE ticker = ? AND form_type = ? AND period_of_report = ? "
                "ORDER BY filing_date DESC LIMIT 1",
                (ticker, form_type, period),
            ).fetchone()

        if not row:
            return {k: None for k in metric_keys}

        filepath = Path(row["raw_path"])
        if not filepath.exists():
            from ...db.schema import REPO_ROOT
            filepath = REPO_ROOT / row["raw_path"]
            if not filepath.exists():
                return {k: None for k in metric_keys}

        # Load filing once
        text = extract_text(filepath)
        sections = parse_sections(text, row["form_type"])
        ext_sections = get_extraction_sections(sections, row["form_type"])
        if not ext_sections:
            return {k: None for k in metric_keys}

        sections_text = "\n\n".join(
            f"## {name}\n{content}" for name, content in ext_sections.items()
        )
        if len(sections_text) > 100_000:
            sections_text = sections_text[:100_000]

        # Company + per-metric context
        company = get_company_treatment(ticker)
        company_name = company.full_name if company else ticker
        currency = company.reporting_currency if company else "USD"
        unit = f"{currency}_millions"

        with db.connect() as conn:
            fye_row = conn.execute(
                "SELECT fiscal_year_end_month FROM companies WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        fye_month = fye_row["fiscal_year_end_month"] if fye_row else 12

        try:
            fy_int: int | None = int(str(row["period_of_report"])[:4])
        except (ValueError, TypeError):
            fy_int = None

        per_metric_specs: list[dict[str, str]] = []
        treatments: dict[str, Any] = {}
        for mk in metric_keys:
            treatment = get_dataset_treatment(ticker, mk)
            treatments[mk] = treatment
            hnotes = hn_mod.resolve(
                ticker=ticker, metric_key=mk, fiscal_year=fy_int,
                form_type=row["form_type"],
            )
            per_metric_specs.append({
                "metric_key": mk,
                "metric_description": get_metric_description(mk, ticker, treatment),
                "derivation_rules": get_derivation_rules(treatment),
                "human_notes_block": hn_mod.format_for_prompt(hnotes),
                # sections_text only read from index 0 by the builder
                "sections_text": sections_text,
            })

        # Single Agent A call
        prompt_a = build_agent_a_multi_metric_prompt(
            company_name=company_name,
            form_type=row["form_type"],
            period=row["period_of_report"],
            metrics=per_metric_specs,
            unit=unit,
        )
        try:
            response_a = backend.extract(system="", user=prompt_a)
        except Exception:
            return {k: None for k in metric_keys}

        per_metric_a = parse_agent_a_multi_metric_response(
            response_a, expected_keys=list(metric_keys),
        )

        # Per-metric Agent B + candidate assembly
        results: dict[str, list[ExtractionCandidate] | None] = {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        for mk in metric_keys:
            entry = per_metric_a.get(mk)
            if entry is None:
                results[mk] = None  # parse failure → fallback
                continue
            if not entry.get("found"):
                # Agent A genuinely couldn't find it. Treat as
                # "extracted nothing" (success with empty list) so the
                # router doesn't redundantly retry per-metric for a
                # metric the filing doesn't carry.
                results[mk] = []
                continue
            periods = entry.get("periods") or []
            if not periods:
                results[mk] = []
                continue

            treatment = treatments[mk]
            metric_desc = get_metric_description(mk, ticker, treatment)
            deriv_rules = get_derivation_rules(treatment)

            prompt_b = build_agent_b_prompt(
                company_name=company_name,
                form_type=row["form_type"],
                periods=periods,
                metric_description=metric_desc,
                unit=unit,
                derivation_rules=deriv_rules,
            )
            try:
                response_b = backend.extract(system="", user=prompt_b)
            except Exception:
                results[mk] = None
                continue
            result_b = parse_agent_b_response(response_b)
            verifications = verify_periods_batch(periods, result_b)

            candidates: list[ExtractionCandidate] = []
            any_primary_ok = False
            for p, ver in zip(periods, verifications, strict=False):
                if not ver.verified:
                    continue
                role = (p.get("role") or "primary").lower()
                value = ver.value_a
                value_usd, fx_rate, fx_date = normalize_to_usd(
                    value, currency,
                    p.get("period_of_report") or row["period_of_report"],
                    db=db,
                )
                if role == "primary":
                    source_doc_id = row["id"]
                    extracting_model = "llm-dual-agent"
                    any_primary_ok = True
                else:
                    if value in (None, 0):
                        continue
                    comp_period = p.get("period_of_report") or ""
                    comp_fy = _fiscal_year_from(comp_period, fye_month)
                    if comp_fy is None:
                        continue
                    with db.mutating() as conn:
                        source_doc_id = ensure_restated_source_doc(
                            conn, ticker, comp_fy, row["id"], now,
                            period_of_report=comp_period,
                        )
                    extracting_model = "llm-dual-agent-restated@0.1.0"

                basis = p.get("basis_period_months") or 0
                period_type = _period_type_from(
                    basis,
                    p.get("period_of_report") or "",
                    row["form_type"],
                    fye_month=fye_month,
                )
                candidates.append(ExtractionCandidate(
                    source_document_id=source_doc_id,
                    metric_key=mk,
                    value=value,
                    value_text=(
                        f"{currency} {value:,.0f} million "
                        f"({'restated' if role == 'comparative' else 'primary'})"
                    ) if value else "",
                    unit="USD_millions",
                    quote=ver.best_quote[:250],
                    locator_section=(
                        (p.get("excerpts") or [{}])[0].get("location", "")
                    ),
                    extraction_type="direct",
                    extracting_model=extracting_model,
                    reporting_currency=currency,
                    excerpts=p.get("excerpts", []),
                    reasoning=p.get("reasoning", ""),
                    period_type=period_type,
                    basis_period_months=basis or None,
                ))

            if not any_primary_ok and not candidates:
                # No verified primary AND no verified comparatives —
                # let the per-metric path retry with broader context.
                results[mk] = None
            else:
                results[mk] = candidates

        return results
