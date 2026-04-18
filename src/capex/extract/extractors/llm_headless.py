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

from pathlib import Path
from typing import Any

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
    verify,
)
from ..base import ExtractionCandidate
from ..coverage import DatasetTreatment, get_company_treatment, get_dataset_treatment


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
        text = extract_text(str(filepath))
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

        metric_desc = get_metric_description(metric_key, ticker, treatment)
        deriv_rules = get_derivation_rules(treatment)
        unit = f"{currency}_millions"

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
            )

            response_a = backend.extract(system="", user=prompt_a)
            result_a = parse_agent_a_response(response_a)

            if not result_a.get("found"):
                return None  # metric not found in filing

            # Agent B: blind verification (only sees excerpts)
            prompt_b = build_agent_b_prompt(
                company_name=company_name,
                form_type=row["form_type"],
                period=row["period_of_report"],
                metric_description=metric_desc,
                excerpts=result_a.get("excerpts", []),
                unit=unit,
                derivation_rules=deriv_rules,
            )

            response_b = backend.extract(system="", user=prompt_b)
            result_b = parse_agent_b_response(response_b)

            # Compare
            verification = verify(result_a, result_b)
            verification.attempts = attempt

            if verification.verified:
                # Build candidate
                value = verification.value_a
                value_usd, fx_rate, fx_date = normalize_to_usd(
                    value, currency, row["period_of_report"], db=db,
                )

                return [ExtractionCandidate(
                    source_document_id=row["id"],
                    metric_key=metric_key,
                    value=value,
                    value_text=f"{currency} {value:,.0f} million" if value else "",
                    unit="USD_millions",
                    quote=verification.best_quote[:250],
                    locator_section=result_a.get("excerpts", [{}])[0].get("location", ""),
                    extraction_type=result_a.get("extraction_type", "direct"),
                    extracting_model="llm-dual-agent",
                    reporting_currency=currency,
                    excerpts=result_a.get("excerpts", []),
                    reasoning=result_a.get("reasoning", ""),
                )]

            elif verification.match_type == "mismatch":
                # A and B disagree — no retry, queue immediately
                break

            # B said not determinable — retry with broader context instruction
            if attempt < MAX_RETRIES:
                # Broaden context for next attempt
                deriv_rules += (
                    f"\n\nRETRY {attempt}: Previous context was insufficient. "
                    f"Include MORE surrounding text — the full table, "
                    f"all column headers, and any footnotes."
                )

        # All attempts failed or mismatch — queue for human review
        # Return None so the router knows extraction didn't succeed
        return None
