"""Protocol Elicitation Loop (PEL).

A domain-agnostic engine for closing the feedback loop between an
automated quality check and the human domain expert who knows *why*
something failed. The engine runs the seven stages:

    Detect → Contextualize → Elicit → Formalize → Preview → Propagate → Measure

and the caller supplies four small interfaces (Anomaly, Artifact,
Effect, Checker) to adapt it to a specific domain.

See docs/PROTOCOL_ELICITATION_LOOP.md for the full spec.
"""
from .formalizer import Formalizer, FormalizerResult
from .protocol import Anomaly, Artifact, Checker, Effect, ReviewOutcome
from .session import ReviewSession

__all__ = [
    "Anomaly",
    "Artifact",
    "Checker",
    "Effect",
    "Formalizer",
    "FormalizerResult",
    "ReviewOutcome",
    "ReviewSession",
]
