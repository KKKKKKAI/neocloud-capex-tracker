"""ReviewSession — domain-agnostic state machine for the PEL.

Steps through a list of Anomalies, prompts a human for natural-language
guidance, calls the Formalizer, shows a preview, commits the artifact,
runs the effect, re-checks, and reports. The I/O layer (what a "prompt"
looks like, what a "preview" looks like) is injected via callables so
the same engine can drive a terminal REPL, a web UI, or a test.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .formalizer import Formalizer, FormalizerResult
from .protocol import Anomaly, Artifact, Checker, Effect, ReviewOutcome


@dataclass
class ReviewCallbacks:
    """I/O plumbing the engine calls out to."""
    render_anomaly: Callable[[Anomaly, int, int], None]
    read_input: Callable[[str], str]         # prompt → user text
    render_preview: Callable[[FormalizerResult], None]
    ask_commit: Callable[[], bool]           # y/N
    render_summary: Callable[[ReviewOutcome], None]


class ReviewSession:
    """Drive a reviewer through a list of anomalies."""

    def __init__(
        self,
        *,
        anomalies: Iterable[Anomaly],
        formalizer: Formalizer,
        write_artifact: Callable[[Artifact, str], None],
        effect: Effect,
        checker: Checker,
        build_artifact: Callable[[FormalizerResult, Anomaly], Artifact],
        callbacks: ReviewCallbacks,
        log_interaction: Callable[
            [Anomaly, str, FormalizerResult, Artifact | None], None
        ] | None = None,
    ) -> None:
        self.anomalies = list(anomalies)
        self.formalizer = formalizer
        self.write_artifact = write_artifact
        self.effect = effect
        self.checker = checker
        self.build_artifact = build_artifact
        self.cb = callbacks
        self.log = log_interaction

    def run(self, *, run_id: str) -> list[ReviewOutcome]:
        """Walk the anomalies, returning an outcome per review."""
        outcomes: list[ReviewOutcome] = []
        total = len(self.anomalies)
        for i, a in enumerate(self.anomalies, start=1):
            self.cb.render_anomaly(a, i, total)
            user_text = self.cb.read_input(
                "> What should future extractors watch out for here?\n"
                "  (empty = skip, 'quit' = exit session): "
            ).strip()
            if user_text.lower() in ("quit", "q", "exit"):
                outcomes.append(ReviewOutcome(anomaly_id=a.id, action="quit"))
                break
            if not user_text:
                outcomes.append(ReviewOutcome(anomaly_id=a.id, action="skipped"))
                continue

            # Iterate until the formalizer is confident + question-free,
            # or the reviewer bails.
            result = self._iterate_with_clarifications(a, user_text)
            self.cb.render_preview(result)

            if not result.is_committable:
                outcomes.append(
                    ReviewOutcome(
                        anomaly_id=a.id,
                        action="skipped",
                        reviewer_input=user_text,
                        notes="not committable",
                    )
                )
                if self.log is not None:
                    self.log(a, user_text, result, None)
                continue

            if not self.cb.ask_commit():
                outcomes.append(
                    ReviewOutcome(
                        anomaly_id=a.id,
                        action="skipped",
                        reviewer_input=user_text,
                        notes="reviewer declined",
                    )
                )
                if self.log is not None:
                    self.log(a, user_text, result, None)
                continue

            artifact = self.build_artifact(result, a)
            self.write_artifact(artifact, run_id)
            if self.log is not None:
                self.log(a, user_text, result, artifact)

            effect_summary = self.effect(artifact.affected_ids)
            passed, affected_total = self.checker(artifact.affected_ids)
            outcome = ReviewOutcome(
                anomaly_id=a.id,
                action="committed",
                artifact_id=artifact.id,
                reviewer_input=user_text,
                passed_after=passed,
                total_after=affected_total,
                notes=effect_summary,
            )
            outcomes.append(outcome)
            self.cb.render_summary(outcome)
        return outcomes

    def _iterate_with_clarifications(
        self, anomaly: Anomaly, initial_input: str,
    ) -> FormalizerResult:
        user_text = initial_input
        extras: dict[str, Any] = {"prior_dialog": []}
        for _ in range(3):
            result = self.formalizer.formalize(
                context=anomaly.context,
                reviewer_input=user_text,
                extras=extras,
            )
            if result.is_committable or result.error:
                return result
            if not result.clarifying_questions:
                # Low confidence but no question — stop, let reviewer retry
                return result
            q = result.clarifying_questions[0]
            answer = self.cb.read_input(f"  ? {q}\n  > ").strip()
            if not answer or answer.lower() == "quit":
                return result
            extras["prior_dialog"] = extras.get("prior_dialog", []) + [
                {"q": q, "a": answer}
            ]
            user_text = f"{initial_input}\n\nClarification: {answer}"
        return result
