"""Abstract types for the Protocol Elicitation Loop.

The four interfaces below are the only thing a domain adapter needs to
implement. The engine in `session.py` is unaware of what an Anomaly
actually is — it only knows it can be rendered, has an id, and can be
passed to the Formalizer + Effect + Checker.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class Anomaly:
    """A flagged item surfaced by an automated check.

    `id` is a short stable key (e.g. `BABA:cloud_segment_revenue:2023Q1`).
    `context` carries everything the human needs to make a judgement
    (source quote, URL, check failures, neighboring values, current
    treatment). The engine treats it as opaque — only the UI renderer
    reads its fields.
    """
    id: str
    title: str                    # Short human-readable label
    context: dict[str, Any]        # Domain-specific — renderer consumes


@dataclass
class Artifact:
    """The structured output the formalizer produces from NL input.

    For capex this is a HumanNote. Other domains could be: a routing
    rule, a suppression pattern, a labelling example. The engine just
    writes it via the `write` callable the caller supplies.
    """
    id: str
    data: dict[str, Any]           # Serializable blob
    affected_ids: list[str]        # Anomaly ids this artifact applies to


class Effect(Protocol):
    """Re-run the pipeline for a list of affected anomaly ids.

    Called after an artifact is committed. Should apply the new
    guidance and produce fresh values for the anomalies. Returns a
    summary string for display.
    """
    def __call__(self, affected_ids: list[str]) -> str: ...


class Checker(Protocol):
    """Re-validate the affected anomalies after an effect runs.

    Returns `(passed, total)` counts. `passed` is how many of the
    anomalies now pass all checks.
    """
    def __call__(self, affected_ids: list[str]) -> tuple[int, int]: ...


@dataclass
class ReviewOutcome:
    """What happened for one anomaly review."""
    anomaly_id: str
    action: str              # "committed" | "skipped" | "quit"
    artifact_id: str | None = None
    reviewer_input: str = ""
    passed_after: int = 0
    total_after: int = 0
    notes: str = ""
