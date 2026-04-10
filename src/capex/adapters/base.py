"""Model-agnostic backend protocol.

Defines the interface that all concrete model adapters must implement.
In v1 this is a stub — Claude Code IS the adapter. In Phase 3.5, concrete
implementations (anthropic.py, google.py, openai.py) implement this
protocol and the headless extractor calls them.

See adapters/README.md for the full migration guide.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ModelBackend(Protocol):
    """Minimal contract every model adapter must implement.

    Attributes:
        name: model identifier, e.g. "claude-sonnet-4-6"
        version: adapter version or model version string

    Methods:
        extract: send a system+user prompt pair and get back the model's
            raw text response. The caller is responsible for parsing the
            response (usually JSON) and passing it to writer.py.
    """

    name: str
    version: str

    def extract(
        self,
        system: str,
        user: str,
        response_schema: dict[str, Any] | None = None,
    ) -> str:
        """Send system + user prompt to the model and return the text response.

        Args:
            system: system prompt (role instructions).
            user: user prompt (the filing sections + extraction instructions).
            response_schema: optional JSON schema hint for structured output
                (used by adapters that support tool_use or JSON mode).

        Returns:
            Raw text response from the model. Usually JSON that the caller
            will parse.
        """
        ...
