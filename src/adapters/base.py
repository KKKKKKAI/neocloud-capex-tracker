"""Backend adapter base class (placeholder).

All concrete model backends (Claude, Gemini, MiniMax, ...) implement this
interface. The extraction layer depends only on this protocol, never on a
specific provider SDK.
"""
from __future__ import annotations

from typing import Any, Protocol


class ModelBackend(Protocol):
    """Minimal contract every model adapter must implement."""

    name: str
    version: str

    def complete(
        self,
        system: str,
        user: str,
        response_schema: dict[str, Any] | None = None,
    ) -> str:
        """Return the model's raw textual response."""
        ...
