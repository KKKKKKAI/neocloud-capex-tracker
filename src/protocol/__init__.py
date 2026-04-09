"""Versioned interchange protocol.

Defines the structured contract that every LLM-producing layer must comply
with. This is the single source of truth for what a valid extraction looks
like across model backends.

See docs/SYSTEM_DESIGN.md §5 for the full protocol specification.
"""

PROTOCOL_VERSION = "0.1.0-draft"
