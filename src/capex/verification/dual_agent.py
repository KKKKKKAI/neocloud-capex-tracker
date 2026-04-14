"""Dual-agent verification for hallucination prevention.

Two-pass extraction where Agent A extracts a value + context from the
full filing, then Agent B independently verifies from ONLY the context.

Agent B never sees Agent A's value or reasoning — it must deduce the
answer independently from the evidence excerpts alone.

If the values match: verified, evidence becomes the citation.
If they mismatch: refused, queued for human review.
If B says insufficient: retry up to 3 times with broader context.

This module is metric-agnostic — it works for any document + data point.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .comparator import compare_values, is_verified

PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_RETRIES = 3


@dataclass
class VerificationResult:
    """Result of the dual-agent verification cycle."""

    value_a: float | None = None
    value_b: float | None = None
    verified: bool = False
    match_type: str = "not_found"  # exact, approximate, mismatch, not_found
    excerpts: list[dict[str, str]] = field(default_factory=list)
    reasoning_a: str = ""
    reasoning_b: str = ""
    attempts: int = 0
    needs_review: bool = False

    # Best quote for Excel citation (single sentence from primary_value)
    best_quote: str = ""


def build_agent_a_prompt(
    company_name: str,
    form_type: str,
    period: str,
    metric_description: str,
    sections_text: str,
    unit: str = "USD_millions",
    derivation_rules: str = "",
) -> str:
    """Format the Agent A prompt for extraction + context capture."""
    template = (PROMPTS_DIR / "agent_a.txt").read_text(encoding="utf-8")
    return template.format(
        company_name=company_name,
        form_type=form_type,
        period=period,
        metric_description=metric_description,
        sections_text=sections_text,
        unit=unit,
        derivation_rules=derivation_rules,
    )


def build_agent_b_prompt(
    company_name: str,
    form_type: str,
    period: str,
    metric_description: str,
    excerpts: list[dict[str, str]],
    unit: str = "USD_millions",
    derivation_rules: str = "",
) -> str:
    """Format the Agent B prompt for blind verification.

    CRITICAL: Agent B receives ONLY the excerpts — not Agent A's value
    or reasoning. This prevents confirmation bias.
    """
    template = (PROMPTS_DIR / "agent_b.txt").read_text(encoding="utf-8")

    # Format excerpts for B — text + location + role, no value
    excerpts_text = ""
    for i, exc in enumerate(excerpts, 1):
        role = exc.get("role", "unknown")
        location = exc.get("location", "unknown")
        text = exc.get("text", "")
        excerpts_text += f"\n--- Excerpt {i} (role: {role}, location: {location}) ---\n"
        excerpts_text += text
        excerpts_text += "\n"

    return template.format(
        company_name=company_name,
        form_type=form_type,
        period=period,
        metric_description=metric_description,
        excerpts_text=excerpts_text,
        unit=unit,
        derivation_rules=derivation_rules,
    )


def parse_agent_a_response(response_text: str) -> dict[str, Any]:
    """Parse Agent A's JSON response.

    Robust: strips markdown fences, handles common JSON issues.
    """
    import json
    import re

    # Strip markdown code fences
    text = response_text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {"found": False, "value": None, "excerpts": [], "reasoning": "Failed to parse response"}


def parse_agent_b_response(response_text: str) -> dict[str, Any]:
    """Parse Agent B's JSON response."""
    return parse_agent_a_response(response_text)  # Same parsing logic


def verify(
    agent_a_result: dict[str, Any],
    agent_b_result: dict[str, Any],
) -> VerificationResult:
    """Compare Agent A and B results, return verification verdict.

    This is the pure comparison step — no LLM calls. The caller
    is responsible for running Agent A and B (either interactively
    in Claude Code or via API).
    """
    value_a = agent_a_result.get("value")
    value_b = agent_b_result.get("value")
    excerpts = agent_a_result.get("excerpts", [])
    reasoning_a = agent_a_result.get("reasoning", "")
    reasoning_b = agent_b_result.get("reasoning", "")

    # Handle "not found" cases
    if not agent_a_result.get("found", False):
        return VerificationResult(
            value_a=None, value_b=None,
            verified=False, match_type="not_found",
            excerpts=excerpts, reasoning_a=reasoning_a,
            reasoning_b=reasoning_b,
            needs_review=False,  # not found is a valid outcome
        )

    # Handle B saying insufficient
    if not agent_b_result.get("determinable", False):
        return VerificationResult(
            value_a=value_a, value_b=None,
            verified=False, match_type="not_found",
            excerpts=excerpts, reasoning_a=reasoning_a,
            reasoning_b=reasoning_b,
            needs_review=True,  # might succeed with broader context
        )

    # Compare values
    match_type = compare_values(value_a, value_b)

    # Extract best quote for citation
    best_quote = _extract_best_quote(excerpts)

    return VerificationResult(
        value_a=value_a,
        value_b=value_b,
        verified=is_verified(match_type),
        match_type=match_type,
        excerpts=excerpts,
        reasoning_a=reasoning_a,
        reasoning_b=reasoning_b,
        needs_review=(match_type == "mismatch"),
        best_quote=best_quote,
    )


def _extract_best_quote(excerpts: list[dict[str, str]]) -> str:
    """Extract the most relevant sentence from the primary_value excerpt.

    Returns a single sentence containing a number — this becomes the
    Excel cell comment quote.
    """
    import re

    for exc in excerpts:
        if exc.get("role") != "primary_value":
            continue
        text = exc.get("text", "")
        # Split into sentences, find first with a number
        sentences = re.split(r'(?<=[.!?])\s+', text)
        for sentence in sentences:
            if re.search(r'\d', sentence):
                return sentence[:250]
        if sentences:
            return sentences[0][:250]

    return ""


def get_metric_description(
    metric_key: str,
    ticker: str,
    dataset_treatment: Any | None = None,
) -> str:
    """Build a human-readable metric description for the agent prompts.

    Includes segment names, derivation formulas, and caveats from
    coverage.yaml.
    """
    base_descriptions = {
        "capital_expenditures": "Capital expenditures (purchases of property and equipment) for the period, in millions",
        "revenue": "Total revenue for the period, in millions",
        "operating_cash_flow": "Net cash provided by operating activities for the period, in millions",
        "depreciation_amortization": "Depreciation and amortization expense for the period, in millions",
        "property_plant_equipment_net": "Property, plant and equipment (net of depreciation) at period end, in millions",
        "cloud_segment_revenue": "Cloud/datacenter segment revenue for the period, in millions",
    }

    desc = base_descriptions.get(metric_key, f"{metric_key} for the period, in millions")

    if dataset_treatment and metric_key == "cloud_segment_revenue":
        if dataset_treatment.segment_names:
            names = ", ".join(f'"{n}"' for n in dataset_treatment.segment_names)
            desc += f". Look for segment(s) named: {names}"

        if dataset_treatment.adjustment:
            formula = dataset_treatment.adjustment.get("formula", "")
            if formula:
                desc += f". Derivation: {formula}"

    return desc


def get_derivation_rules(dataset_treatment: Any | None = None) -> str:
    """Build derivation rules string for the agent prompts."""
    if not dataset_treatment or not dataset_treatment.adjustment:
        return ""

    adj = dataset_treatment.adjustment
    lines = []
    if adj.get("formula"):
        lines.append(f"DERIVATION FORMULA: {adj['formula']}")
    if adj.get("rationale"):
        lines.append(f"RATIONALE: {adj['rationale'].strip()}")
    if adj.get("caveats"):
        for c in adj["caveats"]:
            lines.append(f"CAVEAT: {c}")

    return "\n".join(lines)
