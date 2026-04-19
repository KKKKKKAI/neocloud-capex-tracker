"""NL → structured Artifact translator.

Takes a prompt template, a JSON response schema, a piece of context,
and a piece of natural language from a human reviewer. Calls a
ModelBackend (e.g. CLIBackend) and returns a validated JSON payload.

Keeps the agent call itself tiny — the expensive thinking lives in the
prompt template, which is a markdown file the domain adapter owns.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


class ModelBackend(Protocol):
    """Minimal LLM backend contract — matches CLIBackend."""
    def extract(self, system: str, user: str,
                response_schema: dict[str, Any] | None = None) -> str: ...


@dataclass
class FormalizerResult:
    """What the formalizer returns."""
    raw_response: str
    parsed: dict[str, Any] | None = None
    clarifying_questions: list[str] = field(default_factory=list)
    confidence: str = "low"        # "low" | "medium" | "high"
    error: str | None = None

    @property
    def is_committable(self) -> bool:
        """Only commit if we got JSON, have high confidence, and the
        formalizer has no questions outstanding."""
        return (
            self.parsed is not None
            and self.confidence == "high"
            and not self.clarifying_questions
            and not self.error
        )


class Formalizer:
    """Wraps a ModelBackend + a prompt template."""

    def __init__(
        self,
        backend: ModelBackend,
        prompt_template: str,
    ) -> None:
        self.backend = backend
        self.prompt_template = prompt_template

    def formalize(
        self,
        *,
        context: dict[str, Any],
        reviewer_input: str,
        extras: dict[str, Any] | None = None,
    ) -> FormalizerResult:
        """Render the prompt and ask the backend for a JSON response."""
        template_vars = {
            "reviewer_input": reviewer_input,
            "context_json": json.dumps(context, indent=2, ensure_ascii=False),
        }
        if extras:
            for k, v in extras.items():
                template_vars[k] = (
                    json.dumps(v, indent=2, ensure_ascii=False)
                    if not isinstance(v, str) else v
                )
        try:
            user_prompt = self.prompt_template.format(**template_vars)
        except KeyError as e:
            return FormalizerResult(
                raw_response="",
                error=f"prompt template missing variable: {e}",
            )

        try:
            raw = self.backend.extract(system="", user=user_prompt)
        except Exception as e:  # backend errors surface as result.error
            return FormalizerResult(raw_response="", error=str(e))

        parsed = _extract_json_block(raw)
        if parsed is None:
            return FormalizerResult(
                raw_response=raw,
                error="no JSON block found in response",
            )
        return FormalizerResult(
            raw_response=raw,
            parsed=parsed,
            clarifying_questions=list(parsed.get("clarifying_questions") or []),
            confidence=str(parsed.get("confidence", "low")).lower(),
        )


_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL
)


def _extract_json_block(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from a raw LLM response.

    Handles both ```json fenced blocks and bare {…} objects. Returns
    None if nothing parseable is present.
    """
    if not text:
        return None
    # First: fenced block
    m = _JSON_FENCE_RE.search(text)
    candidates: list[str] = []
    if m:
        candidates.append(m.group(1))
    # Then: bare object by balanced-brace scan
    first_brace = text.find("{")
    if first_brace >= 0:
        depth = 0
        for i in range(first_brace, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[first_brace:i + 1])
                    break
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None
