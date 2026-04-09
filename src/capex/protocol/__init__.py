"""Versioned Pydantic schemas for the interchange protocol.

The actual v0.1.0 models land in Phase 3. Until then this module just
re-exports the PROTOCOL_VERSION constant from the package root.
"""
from __future__ import annotations

from .. import PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION"]
